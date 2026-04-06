#!/usr/bin/env python3
"""Telegram-бот для замены суммы в PDF-чеках (Альфа-Банк, ВТБ) и создания выписок.
Отправьте чек → выберите банк → введите "с какой на какую" → получите готовый PDF.
Или /create_statement для создания выписки.
"""
import logging
import os
import re
import time
import traceback

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass
import tempfile
from pathlib import Path

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import Conflict, InvalidToken, NetworkError, TimedOut, TelegramError
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
    handlers=[
        logging.FileHandler("bot.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)

from pdf_patcher import patch_pdf_file

# Состояние пользователя: {file_path, file_name, bank} или {mode, step, ...} для выписки
USER_STATE: dict[int, dict] = {}

# Белый список — только эти пользователи могут использовать бота
ALLOWED_USERS: set[int] = {1445265832, 7076663447, 8178442784, 6646800148}


def is_allowed(update: Update) -> bool:
    uid = update.effective_user.id if update.effective_user else None
    return uid in ALLOWED_USERS


async def answer_callback_query_safe(update: Update) -> None:
    """Снимает «часики» у inline-кнопки; без ответа Telegram может копить запросы и вести себя нестабильно."""
    q = update.callback_query
    if not q:
        return
    try:
        await q.answer()
    except TelegramError:
        pass


# Поля для пошагового ввода Альфа СБП
ALFA_SBP_FIELDS = [
    ("amount", "💰 Сумма перевода (число, например: 5000)"),
    ("date_time", "📅 Дата и время (например: 20.03.2026 14:30:00 мск)"),
    ("recipient", "👤 Получатель (например: Александр Евгеньевич Ж)"),
    ("phone", "📱 Телефон получателя (например: +7 (900) 351-70-80)"),
    ("bank", "🏦 Банк получателя (например: ВТБ, Сбербанк, Т-Банк)"),
    ("account", "💳 Последние 4 цифры счёта (например: 1234)"),
]


def parse_amounts(text: str) -> tuple[int, int] | None:
    """Разбор '10 5000' или '10 5 000' → (10, 5000)."""
    nums = re.findall(r"\d+", text.strip())
    if len(nums) >= 2:
        return int(nums[0]), int("".join(nums[1:]))
    return None


def parse_amount_pairs(text: str) -> list[tuple[int, int]]:
    """Разбор '10 5000 50 1000' → [(10, 5000), (50, 1000)]."""
    nums = re.findall(r"\d+", text.strip())
    pairs = []
    i = 0
    while i + 1 < len(nums):
        pairs.append((int(nums[i]), int("".join(nums[i + 1 : i + 2]))))
        i += 2
    return pairs


def parse_custom_replacement(text: str) -> tuple[str, str] | None:
    """Разбор 'поле=значение' → (поле, значение)."""
    text = text.strip()
    if "=" in text:
        k, _, v = text.partition("=")
        return (k.strip().lower(), v.strip()) if k.strip() and v.strip() else None
    return None


# --- Чеки (текущий flow) ---


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return
    msg = update.effective_message
    if not msg:
        return
    uid = update.effective_user.id
    if uid in USER_STATE and "file_path" in USER_STATE[uid]:
        try:
            os.unlink(USER_STATE[uid]["file_path"])
        except OSError:
            pass
        del USER_STATE[uid]
    await msg.reply_text(
        "👋 Привет! Я помогу изменить чек.\n\n"
        "📋 **Как пользоваться:**\n"
        "1. Отправьте PDF-чек\n"
        "2. Выберите режим:\n"
        "   • *Альфа-Банк / ВТБ / Авто* — замена суммы\n"
        "   • *Альфа СБП (все поля)* — сумма, дата, получатель, телефон, банк, счёт\n"
        "3. Введите новые значения\n\n"
        "📄 Создать выписку: /create_statement\n\n"
        "✅ Структура PDF, метаданные и контрольные ключи сохраняются.",
        parse_mode="Markdown",
    )


async def cmd_create_statement(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Команда создания выписки — выбор варианта."""
    if not is_allowed(update):
        return
    msg = update.effective_message
    if not msg:
        return
    uid = update.effective_user.id
    if uid in USER_STATE:
        if "file_path" in USER_STATE[uid]:
            try:
                os.unlink(USER_STATE[uid]["file_path"])
            except OSError:
                pass
        del USER_STATE[uid]

    keyboard = [
        [
            InlineKeyboardButton("✏️ Редактирование своей выписки", callback_data="stmt_edit"),
        ],
        [
            InlineKeyboardButton("📄 Выписка по чеку", callback_data="stmt_receipt"),
        ],
    ]
    await msg.reply_text(
        "📄 **Создание выписки**\n\n"
        "Выберите вариант:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def handle_stmt_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработка выбора варианта выписки."""
    await answer_callback_query_safe(update)
    if not is_allowed(update):
        return
    query = update.callback_query
    if not query:
        return

    uid = update.effective_user.id
    data = query.data

    if data == "stmt_edit":
        USER_STATE[uid] = {
            "mode": "statement_edit",
            "step": "upload",
        }
        await query.edit_message_text(
            "✏️ **Редактирование своей выписки**\n\n"
            "Отправьте PDF-файл выписки.",
            parse_mode="Markdown",
        )
    elif data == "stmt_receipt":
        USER_STATE[uid] = {
            "mode": "statement_from_receipt",
            "step": "upload",
        }
        await query.edit_message_text(
            "📄 **Выписка по чеку**\n\n"
            "Отправьте PDF-файл чека.",
            parse_mode="Markdown",
        )


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return
    msg = update.effective_message
    if not msg:
        return
    doc = msg.document
    if not doc or not doc.file_name or not doc.file_name.lower().endswith(".pdf"):
        await msg.reply_text("❌ Отправьте PDF-файл.")
        return

    uid = update.effective_user.id
    state = USER_STATE.get(uid, {})

    # Режим выписки
    if state.get("mode", "").startswith("statement_"):
        await _handle_statement_document(update, context, doc, uid, state)
        return

    # Режим чека (текущий flow)
    file = await context.bot.get_file(doc.file_id)
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as fp:
        await file.download_to_drive(fp.name)
        old = USER_STATE.get(uid, {})
        if "file_path" in old and old["file_path"] != fp.name:
            try:
                os.unlink(old["file_path"])
            except OSError:
                pass
        USER_STATE[uid] = {
            "file_path": fp.name,
            "file_name": doc.file_name,
        }

    keyboard = [
        [
            InlineKeyboardButton("🏦 Альфа-Банк", callback_data="bank_alfa"),
            InlineKeyboardButton("🏛 ВТБ", callback_data="bank_vtb"),
        ],
        [
            InlineKeyboardButton("💛 Т-Банк", callback_data="bank_tbank"),
        ],
        [
            InlineKeyboardButton("🏦 Альфа СБП (все поля)", callback_data="bank_alfa_sbp_full"),
        ],
        [InlineKeyboardButton("🔍 Авто", callback_data="bank_auto")],
    ]
    await msg.reply_text(
        "📎 Чек получен. Выберите банк:\n\n"
        "• *Альфа-Банк / ВТБ / Т-Банк / Авто* — замена только суммы\n"
        "• *Альфа СБП (все поля)* — замена суммы, даты, получателя, телефона, банка, счёта",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def _handle_statement_document(
    update: Update, context: ContextTypes.DEFAULT_TYPE,
    doc, uid: int, state: dict,
) -> None:
    """Обработка документа в режиме выписки."""
    msg = update.effective_message
    if not msg:
        return
    file = await context.bot.get_file(doc.file_id)
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as fp:
        await file.download_to_drive(fp.name)

    mode = state.get("mode", "")
    step = state.get("step", "upload")

    if mode == "statement_edit":
        from vyписка_service import (
            scan_statement_amounts,
            scan_statement_transactions,
            calculate_balance_and_expenses,
        )
        amounts = scan_statement_amounts(Path(fp.name))
        transactions = scan_statement_transactions(Path(fp.name))
        USER_STATE[uid] = {
            "mode": "statement_edit",
            "step": "amounts",
            "file_path": fp.name,
            "file_name": doc.file_name,
            "replacements": {},
            "transactions": transactions,
            "amounts_found": amounts,
        }
        await msg.reply_text(
            "✅ Выписка получена.\n\n"
            "💰 Введите замены сумм: *с какой на какую*\n"
            "Например: `10 10000` или `10 5000 50 1000`",
            parse_mode="Markdown",
        )

    elif mode == "statement_from_receipt":
        from receipt_extractor import extract_from_receipt, generate_fio_from_first_letter
        from vyписка_service import BASE_STATEMENT, patch_statement, calculate_balance_and_expenses

        extracted = extract_from_receipt(Path(fp.name))
        amount = extracted.get("amount")
        if not amount:
            await msg.reply_text("❌ Не удалось извлечь сумму из чека.")
            try:
                os.unlink(fp.name)
            except OSError:
                pass
            del USER_STATE[uid]
            return

        fio_recipient = extracted.get("fio_recipient", "") or extracted.get("fio_payer", "")
        first_letter = fio_recipient[0] if fio_recipient else "И"
        generated_fio = generate_fio_from_first_letter(first_letter)

        USER_STATE[uid] = {
            "mode": "statement_from_receipt",
            "step": "balance",
            "file_path": fp.name,
            "file_name": doc.file_name,
            "extracted": extracted,
            "amount": amount,
            "generated_fio": generated_fio,
        }
        await msg.reply_text(
            f"✅ Чек получен.\n\n"
            f"📊 Сумма: {amount} ₽\n"
            f"👤 ФИО (сгенерировано): {generated_fio}\n\n"
            "💰 Введите баланс на начало периода (число):",
        )


async def handle_bank_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await answer_callback_query_safe(update)
    if not is_allowed(update):
        return
    query = update.callback_query
    if not query:
        return

    user_id = update.effective_user.id
    if user_id not in USER_STATE:
        await query.edit_message_text("❌ Чек не найден. Отправьте PDF заново.")
        return

    # Альфа СБП — полная замена
    if query.data == "bank_alfa_sbp_full":
        USER_STATE[user_id]["mode"] = "alfa_sbp_full"
        USER_STATE[user_id]["step"] = 0
        USER_STATE[user_id]["alfa_fields"] = {}
        field_key, prompt = ALFA_SBP_FIELDS[0]
        await query.edit_message_text(
            "🏦 **Альфа-Банк СБП — замена всех полей**\n\n"
            "Введите новые значения пошагово.\n"
            "Отправьте `-` чтобы оставить поле без изменений.\n\n"
            f"Шаг 1/{len(ALFA_SBP_FIELDS)}: {prompt}",
            parse_mode="Markdown",
        )
        return

    bank_map = {"bank_alfa": "alfa", "bank_vtb": "vtb", "bank_tbank": "tbank", "bank_auto": "auto"}
    bank = bank_map.get(query.data, "auto")
    bank_name = {"alfa": "Альфа-Банк", "vtb": "ВТБ", "tbank": "Т-Банк", "auto": "Авто"}[bank]
    USER_STATE[user_id]["bank"] = bank

    await query.edit_message_text(
        f"✅ Банк: {bank_name}\n\n"
        "💰 Введите сумму: *с какой на какую* менять\n"
        "Например: `10 5000` или `50 10000`",
        parse_mode="Markdown",
    )


async def handle_amounts(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return
    msg = update.effective_message
    if not msg or not msg.text:
        return
    user_id = update.effective_user.id
    state = USER_STATE.get(user_id, {})

    # Режим выписки — текст
    if state.get("mode", "").startswith("statement_"):
        await _handle_statement_text(update, context, user_id, state)
        return

    # Режим Альфа СБП — пошаговый ввод
    if state.get("mode") == "alfa_sbp_full":
        await _handle_alfa_sbp_step(update, context, user_id, state)
        return

    # Режим чека
    if user_id not in USER_STATE or "bank" not in USER_STATE[user_id]:
        await msg.reply_text("❌ Сначала отправьте чек и выберите банк.")
        return

    parsed = parse_amounts(msg.text)
    if not parsed:
        await msg.reply_text(
            "❌ Неверный формат. Введите две суммы, например: 10 5000",
        )
        return

    amount_from, amount_to = parsed
    if amount_from <= 0 or amount_to <= 0:
        await msg.reply_text("❌ Суммы должны быть больше 0.")
        return

    state = USER_STATE[user_id]
    inp = state["file_path"]
    bank = state["bank"]

    out_name = Path(state["file_name"]).stem + f"_{amount_to}.pdf"
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as out_fp:
        out_path = out_fp.name

    ok, err = patch_pdf_file(inp, out_path, amount_from, amount_to, bank=bank)

    try:
        os.unlink(inp)
    except OSError:
        pass
    del USER_STATE[user_id]

    if not ok:
        await msg.reply_text(f"❌ Ошибка: {err}")
        try:
            os.unlink(out_path)
        except OSError:
            pass
        return

    with open(out_path, "rb") as f:
        await msg.reply_document(
            document=f,
            filename=out_name,
            caption=f"✅ Готово: {amount_from} ₽ → {amount_to} ₽",
        )

    try:
        os.unlink(out_path)
    except OSError:
        pass


async def _handle_alfa_sbp_step(
    update: Update, context: ContextTypes.DEFAULT_TYPE,
    uid: int, state: dict,
) -> None:
    """Пошаговый ввод полей Альфа СБП."""
    msg = update.effective_message
    if not msg or not msg.text:
        return
    text = msg.text.strip()
    step = state.get("step", 0)
    fields = state.setdefault("alfa_fields", {})

    if step >= len(ALFA_SBP_FIELDS):
        return

    field_key, _ = ALFA_SBP_FIELDS[step]

    if text != "-":
        if field_key == "amount":
            nums = re.findall(r"\d+", text)
            if not nums:
                await msg.reply_text("❌ Введите число (например: 5000)")
                return
            fields["amount"] = int("".join(nums))
        elif field_key == "account":
            digits = re.findall(r"\d+", text)
            last4 = "".join(digits)
            if len(last4) < 4:
                await msg.reply_text("❌ Нужно ровно 4 цифры (например: 1234)")
                return
            last4 = last4[-4:]
            fields["account_last4"] = last4
        else:
            fields[field_key] = text

    step += 1
    USER_STATE[uid]["step"] = step

    if step < len(ALFA_SBP_FIELDS):
        field_key_next, prompt_next = ALFA_SBP_FIELDS[step]
        await msg.reply_text(
            f"✅ Принято.\n\nШаг {step + 1}/{len(ALFA_SBP_FIELDS)}: {prompt_next}\n"
            "Отправьте `-` чтобы пропустить.",
        )
    else:
        summary_lines = []
        for fk, label in ALFA_SBP_FIELDS:
            val = fields.get(fk) or fields.get("account_last4" if fk == "account" else fk)
            summary_lines.append(f"  {label.split('(')[0].strip()}: {val or '(без изменений)'}")
        summary = "\n".join(summary_lines)

        await msg.reply_text(f"📋 Параметры:\n{summary}\n\n⏳ Генерирую PDF...")

        await _apply_alfa_sbp_patch(update, context, uid, state)


async def _apply_alfa_sbp_patch(
    update: Update, context: ContextTypes.DEFAULT_TYPE,
    uid: int, state: dict,
) -> None:
    """Применяет все замены к Альфа СБП чеку через cid_patch_amount (zero-delta для счёта)."""
    import zlib
    from pathlib import Path

    msg = update.effective_message
    if not msg:
        return

    fields = state.get("alfa_fields", {})
    inp_path = Path(state["file_path"])
    data = inp_path.read_bytes()

    # Извлекаем текущие значения из PDF
    uni_to_cid = {}
    cid_to_uni = {}
    for m in re.finditer(rb'<<(.*?)/Length\s+(\d+)(.*?)>>\s*stream\r?\n', data, re.DOTALL):
        raw = data[m.end(): m.end() + int(m.group(2))]
        try:
            dec = zlib.decompress(raw)
        except zlib.error:
            continue
        if b'beginbfchar' in dec:
            for mm in re.finditer(rb'<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>', dec):
                cid = int(mm.group(1), 16)
                uni = int(mm.group(2), 16)
                cid_to_uni[cid] = chr(uni)
                uni_to_cid[chr(uni)] = mm.group(1).decode().upper().zfill(4)
            break
        if b'beginbfrange' in dec:
            for mm in re.finditer(rb'<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>', dec):
                s = int(mm.group(1), 16)
                e = int(mm.group(2), 16)
                u = int(mm.group(3), 16)
                for i in range(e - s + 1):
                    cid_to_uni[s + i] = chr(u + i)
                    uni_to_cid[chr(u + i)] = f'{s + i:04X}'
            break

    # Декодируем все текстовые блоки из PDF
    current_texts = []
    for m in re.finditer(rb'<<(.*?)/Length\s+(\d+)(.*?)>>\s*stream\r?\n', data, re.DOTALL):
        sl = int(m.group(2))
        ss = m.end()
        try:
            dec = zlib.decompress(data[ss:ss + sl])
        except zlib.error:
            continue
        if b'BT' not in dec:
            continue
        for tj in re.finditer(rb'<([0-9A-Fa-f]+)>\s*Tj', dec):
            hexstr = tj.group(1).decode()
            text = ''
            for i in range(0, len(hexstr), 4):
                cid = int(hexstr[i:i + 4], 16)
                text += cid_to_uni.get(cid, '?')
            current_texts.append(text)
        break

    # Находим текущие значения полей
    def find_text(pattern):
        for t in current_texts:
            if re.search(pattern, t.replace('\xa0', ' ')):
                return t
        return None

    current_amount_text = find_text(r'\d+\s*RUR')
    current_datetime_text = find_text(r'\d{2}\.\d{2}\.\d{4}\s+\d{2}:\d{2}:\d{2}')
    current_date_formed = find_text(r'\d{2}\.\d{2}\.\d{4}\s+\d{2}:\d{2}\s+мск')
    current_recipient = None
    current_phone = None
    current_bank = None
    current_account = None
    current_commission = find_text(r'^0\s*RUR')

    # Ищем по порядку полей (after labels)
    label_order = []
    for t in current_texts:
        tc = t.replace('\xa0', ' ').strip()
        label_order.append(tc)

    for i, tc in enumerate(label_order):
        if tc == 'Получатель' and i + 1 < len(label_order):
            current_recipient = current_texts[i + 1]
        elif 'телефона получателя' in tc and i + 1 < len(label_order):
            current_phone = current_texts[i + 1]
        elif tc == 'Банк получателя' and i + 1 < len(label_order):
            current_bank = current_texts[i + 1]
        elif 'Счёт списания' in tc and i + 1 < len(label_order):
            current_account = current_texts[i + 1]

    # Строим список замен для cid_patch_amount
    replacements = []

    if "amount" in fields and current_amount_text:
        old_amt = current_amount_text.replace('\xa0', ' ').strip()
        new_amt_num = fields["amount"]
        new_amt_str = f"{new_amt_num:,}".replace(",", "\xa0") + "\xa0RUR\xa0"
        if old_amt.endswith(' '):
            pass
        replacements.append((old_amt, new_amt_str))

    if "date_time" in fields:
        new_dt = fields["date_time"]
        if current_datetime_text:
            old_dt = current_datetime_text.replace('\xa0', ' ').strip()
            # Format: match the style — replace only the value part
            new_dt_clean = new_dt.replace(' ', '\xa0')
            if not new_dt_clean.endswith('\xa0'):
                new_dt_clean += '\xa0'
            replacements.append((old_dt, new_dt_clean))
        if current_date_formed:
            old_df = current_date_formed.replace('\xa0', ' ').strip()
            # "Сформирована" date = same date but HH:MM мск
            dt_parts = new_dt.split()
            if len(dt_parts) >= 2:
                formed = dt_parts[0] + '\xa0' + dt_parts[1][:5] + '\xa0мск'
                replacements.append((old_df, formed))

    if "recipient" in fields and current_recipient:
        old_r = current_recipient.replace('\xa0', ' ').strip()
        new_r = fields["recipient"].replace(' ', '\xa0')
        if old_r.endswith(' '):
            new_r += '\xa0'
        replacements.append((old_r, new_r))

    if "phone" in fields and current_phone:
        old_p = current_phone.replace('\xa0', ' ').strip()
        new_p = fields["phone"].replace(' ', '\xa0')
        replacements.append((old_p, new_p))

    if "bank" in fields and current_bank:
        old_b = current_bank.replace('\xa0', ' ').strip()
        new_b = fields["bank"].replace(' ', '\xa0')
        replacements.append((old_b, new_b))

    # Применяем текстовые замены через cid_patch_amount
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as out_fp:
        out_path = out_fp.name

    applied_text = False
    if replacements:
        try:
            from cid_patch_amount import patch_replacements
            text_reps = [(old.replace('\xa0', ' '), new.replace('\xa0', ' '))
                         for old, new in replacements if old.replace('\xa0', ' ') != new.replace('\xa0', ' ')]
            if text_reps:
                applied_text = patch_replacements(inp_path, Path(out_path), text_reps)
        except Exception as e:
            await msg.reply_text(f"⚠️ Ошибка текстовых замен: {e}")

    if not applied_text:
        import shutil
        shutil.copy2(str(inp_path), out_path)

    # Применяем замену счёта (zero-delta с контрольным ключом)
    if "account_last4" in fields:
        try:
            from patch_account_last4 import patch_account_last4 as do_patch_account

            # Находим текущий 20-значный счёт
            acct_20 = None
            if current_account:
                digits = re.findall(r'\d+', current_account.replace('\xa0', ''))
                full = ''.join(digits)
                if len(full) == 20:
                    acct_20 = full

            if not acct_20:
                acct_20 = "40817810980480002476"

            with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
                tmp_path = tmp.name

            ok = do_patch_account(
                input_pdf=out_path,
                output_pdf=tmp_path,
                old_account=acct_20,
                new_last4=fields["account_last4"],
            )
            if ok:
                import shutil
                shutil.move(tmp_path, out_path)
            else:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                await msg.reply_text("⚠️ Не удалось заменить счёт (возможно, номер не найден в PDF)")
        except Exception as e:
            await msg.reply_text(f"⚠️ Ошибка замены счёта: {e}")

    # Отправляем результат
    try:
        os.unlink(state["file_path"])
    except OSError:
        pass
    del USER_STATE[uid]

    out_name = Path(state.get("file_name", "чек.pdf")).stem + "_patched.pdf"
    try:
        with open(out_path, "rb") as f:
            caption_parts = []
            if "amount" in fields:
                caption_parts.append(f"Сумма: {fields['amount']} RUR")
            if "account_last4" in fields:
                caption_parts.append(f"Счёт: ****{fields['account_last4']}")
            if "recipient" in fields:
                caption_parts.append(f"Получатель: {fields['recipient']}")
            caption = "✅ Готово! " + ", ".join(caption_parts) if caption_parts else "✅ Готово!"

            await msg.reply_document(
                document=f,
                filename=out_name,
                caption=caption,
            )
    except Exception as e:
        await msg.reply_text(f"❌ Ошибка отправки: {e}")

    try:
        os.unlink(out_path)
    except OSError:
        pass


async def _handle_statement_text(
    update: Update, context: ContextTypes.DEFAULT_TYPE,
    uid: int, state: dict,
) -> None:
    """Обработка текста в режиме выписки."""
    msg = update.effective_message
    if not msg or not msg.text:
        return
    mode = state.get("mode", "")
    step = state.get("step", "")
    text = msg.text.strip()

    if mode == "statement_edit":
        if step == "amounts":
            pairs = parse_amount_pairs(text)
            if not pairs:
                await msg.reply_text(
                    "❌ Неверный формат. Введите пары: `10 10000` или `10 5000 50 1000`",
                )
                return

            amounts = [(p[0], p[1]) for p in pairs]
            USER_STATE[uid]["replacements"] = {"amounts": amounts}
            USER_STATE[uid]["step"] = "confirm"

            from vyписка_service import calculate_balance_and_expenses

            trans = state.get("transactions", [])
            balance_start = 55242.65  # default, можно извлечь из выписки
            balance_end, expenses = calculate_balance_and_expenses(trans, balance_start)

            keyboard = [
                [
                    InlineKeyboardButton("⏭ Пропустить", callback_data="stmt_skip"),
                    InlineKeyboardButton("➡️ Далее", callback_data="stmt_next"),
                ],
                [InlineKeyboardButton("➕ Свои замены", callback_data="stmt_custom")],
            ]
            await msg.reply_text(
                f"✅ Замены: {amounts}\n\n"
                f"📊 Расходы: {expenses:.2f} ₽\n"
                f"📊 Баланс на конец: {balance_end:.2f} ₽\n\n"
                "Пропустить / Далее или + свои замены (ФИО, телефон, баланс):",
                reply_markup=InlineKeyboardMarkup(keyboard),
            )

        elif step == "custom":
            parsed = parse_custom_replacement(text)
            if not parsed:
                await msg.reply_text(
                    "❌ Формат: `поле=значение`\n"
                    "Например: ФИО=Иванов Иван И. или баланс_начало=55242.65",
                )
                return

            key, value = parsed
            repl = USER_STATE[uid].setdefault("replacements", {})

            if key in ("fio", "фио"):
                from vyписка_service import get_missing_chars
                fp = Path(USER_STATE[uid]["file_path"])
                missing = get_missing_chars(fp, value)
                if missing:
                    keyboard = [
                        [
                            InlineKeyboardButton("🔄 Повторить", callback_data="stmt_retry_fio"),
                            InlineKeyboardButton("⏭ Без замены ФИО", callback_data="stmt_skip_fio"),
                        ],
                    ]
                    await msg.reply_text(
                        f"⚠️ Недоступные символы в выписке: {''.join(missing)}\n"
                        "Повторите с другими символами или пропустите.",
                        reply_markup=InlineKeyboardMarkup(keyboard),
                    )
                    USER_STATE[uid]["pending_fio"] = value
                    return
                repl["fio"] = value
            elif key in ("баланс_начало", "balance_start", "balance"):
                try:
                    repl["balance_start"] = float(value.replace(",", "."))
                except ValueError:
                    await msg.reply_text("❌ Введите число для баланса.")
                    return
            elif key in ("телефон", "phone"):
                repl["phone"] = value
            elif key in ("номер_заявки", "application_id"):
                repl["application_id"] = value

            await msg.reply_text(
                f"✅ Добавлено: {key}={value}\n\n"
                "Введите ещё замены или нажмите Далее.",
            )

    elif mode == "statement_from_receipt" and step == "balance":
        try:
            balance_start = float(text.replace(",", ".").replace(" ", ""))
        except ValueError:
            await msg.reply_text("❌ Введите число (баланс на начало).")
            return

        from vyписка_service import BASE_STATEMENT, patch_statement, calculate_balance_and_expenses

        amount = USER_STATE[uid]["amount"]
        balance_end, expenses = calculate_balance_and_expenses([float(amount)], balance_start)

        from vyписка_service import BASE_AMOUNT, BASE_OLD_FIO
        # Базовая выписка имеет одну операцию → заменяем на сумму из чека
        repl = {
            "amounts": [(BASE_AMOUNT, amount)],
            "balance_end": balance_end,
            "expenses": expenses,
            "fio": USER_STATE[uid].get("generated_fio", "Иванов Иван Иванович"),
            "old_fio": BASE_OLD_FIO,
        }

        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as out_fp:
            out_path = out_fp.name

        ok, err = patch_statement(BASE_STATEMENT, Path(out_path), repl)

        try:
            os.unlink(USER_STATE[uid]["file_path"])
        except OSError:
            pass
        del USER_STATE[uid]

        if not ok:
            await msg.reply_text(f"❌ Ошибка: {err}")
            try:
                os.unlink(out_path)
            except OSError:
                pass
            return

        out_name = f"выписка_{amount}.pdf"
        with open(out_path, "rb") as f:
            await msg.reply_document(
                document=f,
                filename=out_name,
                caption=f"✅ Выписка готова: {amount} ₽",
            )

        try:
            os.unlink(out_path)
        except OSError:
            pass


async def handle_stmt_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработка callback для выписки: stmt_skip, stmt_next, stmt_custom, stmt_retry_fio, stmt_skip_fio."""
    await answer_callback_query_safe(update)
    if not is_allowed(update):
        return
    query = update.callback_query
    if not query:
        return

    uid = update.effective_user.id
    state = USER_STATE.get(uid, {})
    data = query.data

    if data == "stmt_skip":
        await _do_stmt_apply(update, context, uid, state, skip_custom=True)
    elif data == "stmt_next":
        await _do_stmt_apply(update, context, uid, state, skip_custom=False)
    elif data == "stmt_custom":
        USER_STATE[uid]["step"] = "custom"
        await query.edit_message_text(
            "➕ Введите замены в формате `поле=значение`:\n"
            "• ФИО=Иванов Иван И.\n"
            "• баланс_начало=55242.65\n"
            "• телефон=+7 999 123-45-67\n"
            "• номер_заявки=B606...\n\n"
            "После ввода нажмите Далее.",
        )
    elif data == "stmt_retry_fio":
        USER_STATE[uid]["step"] = "custom"
        await query.edit_message_text("Введите ФИО заново (без недоступных символов):")
    elif data == "stmt_skip_fio":
        USER_STATE[uid].pop("pending_fio", None)
        await query.edit_message_text("ФИО не заменяется. Введите другие замены или Далее.")


async def _do_stmt_apply(
    update: Update, context: ContextTypes.DEFAULT_TYPE,
    uid: int, state: dict, skip_custom: bool,
) -> None:
    """Применить патч выписки и отправить PDF."""
    cq = update.callback_query
    if not cq or cq.message is None:
        logger.error("_do_stmt_apply: нет callback_query или message")
        return

    from vyписка_service import (
        patch_statement,
        calculate_balance_and_expenses,
    )

    fp = Path(state["file_path"])
    repl = state.get("replacements", {}).copy()
    trans = list(state.get("transactions", []))

    # Применить замены сумм к транзакциям для расчёта (только ненулевые)
    amount_map = {}
    for pair in repl.get("amounts", []):
        if len(pair) >= 2:
            amount_map[int(pair[0])] = pair[1]
            amount_map[float(pair[0])] = pair[1]
    trans_replaced = [
        float(amount_map.get(int(t), amount_map.get(t, t)))
        for t in trans
        if t > 0
    ]

    balance_start = repl.get("balance_start", 55242.65)
    balance_end, expenses = calculate_balance_and_expenses(trans_replaced, balance_start)
    repl["balance_end"] = balance_end
    repl["expenses"] = expenses
    from vyписка_service import BASE_OLD_FIO
    repl.setdefault("old_fio", BASE_OLD_FIO)

    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as out_fp:
        out_path = out_fp.name

    ok, err = patch_statement(fp, Path(out_path), repl)

    try:
        os.unlink(state["file_path"])
    except OSError:
        pass
    del USER_STATE[uid]

    if not ok:
        try:
            await cq.edit_message_text(f"❌ Ошибка: {err}")
        except TelegramError:
            pass
        try:
            os.unlink(out_path)
        except OSError:
            pass
        return

    out_name = Path(state.get("file_name", "выписка.pdf")).stem + "_patched.pdf"
    with open(out_path, "rb") as f:
        await cq.message.reply_document(
            document=f,
            filename=out_name,
            caption="✅ Выписка готова",
        )

    try:
        os.unlink(out_path)
    except OSError:
        pass


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Глобальный обработчик ошибок — логирует и не роняет бота."""
    err = context.error
    if isinstance(err, (TimedOut, NetworkError)):
        logger.warning("Сеть Telegram (retry у polling): %s", err)
        return
    if isinstance(err, Conflict):
        logger.error(
            "Telegram Conflict: с одним токеном запущено несколько ботов. "
            "Остановите лишние процессы или второй экземпляр."
        )
        return

    tb = "".join(traceback.format_exception(type(err), err, err.__traceback__))
    logger.error("Исключение: %s\n%s", err, tb)

    if not isinstance(update, Update):
        return
    try:
        if update.effective_message:
            await update.effective_message.reply_text(
                "⚠️ Внутренняя ошибка. Попробуйте ещё раз или начните с /start",
            )
        elif update.callback_query:
            try:
                await update.callback_query.answer(
                    "Ошибка. Попробуйте /start",
                    show_alert=True,
                )
            except TelegramError:
                pass
    except TelegramError:
        pass


async def _post_init(app: Application) -> None:
    """Сброс webhook перед polling — иначе getUpdates не получает обновления."""
    try:
        await app.bot.delete_webhook(drop_pending_updates=True)
        logger.info("Webhook сброшен, polling готов")
    except Exception as e:
        logger.warning("Webhook: %s", e)


def _build_app(token: str) -> Application:
    return (
        Application.builder()
        .token(token)
        .post_init(_post_init)
        .read_timeout(30)
        .write_timeout(30)
        .connect_timeout(30)
        .pool_timeout(30)
        .get_updates_read_timeout(30)
        .get_updates_write_timeout(30)
        .get_updates_connect_timeout(30)
        .get_updates_pool_timeout(30)
        .build()
    )


def _register_handlers(app: Application) -> None:
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("create_statement", cmd_create_statement))
    app.add_handler(MessageHandler(filters.Document.PDF, handle_document))
    app.add_handler(CallbackQueryHandler(handle_bank_callback, pattern="^bank_"))
    app.add_handler(CallbackQueryHandler(handle_stmt_choice, pattern="^stmt_edit$|^stmt_receipt$"))
    app.add_handler(CallbackQueryHandler(handle_stmt_callback, pattern="^stmt_"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_amounts))
    app.add_error_handler(error_handler)


def main() -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        logger.error("Задайте TELEGRAM_BOT_TOKEN")
        return

    restart_delay = int(os.environ.get("BOT_RESTART_DELAY_SEC", "8"))

    while True:
        try:
            app = _build_app(token)
            _register_handlers(app)
            logger.info("Бот запущен (polling)...")
            app.run_polling(
                allowed_updates=Update.ALL_TYPES,
                drop_pending_updates=True,
                bootstrap_retries=-1,
            )
            logger.info("Polling завершён штатно.")
            break
        except InvalidToken:
            logger.critical("Неверный TELEGRAM_BOT_TOKEN")
            raise SystemExit(1) from None
        except Exception:
            logger.exception(
                "Сбой polling/приложения, перезапуск через %s с (или задайте BOT_RESTART_DELAY_SEC)",
                restart_delay,
            )
            time.sleep(restart_delay)


if __name__ == "__main__":
    main()
