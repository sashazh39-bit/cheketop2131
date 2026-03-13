#!/usr/bin/env python3
"""Telegram-бот для замены суммы в PDF-чеках (Альфа-Банк, ВТБ) и создания выписок.
Отправьте чек → выберите банк → введите "с какой на какую" → получите готовый PDF.
Или /create_statement для создания выписки.
"""
import os
import re

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass
import tempfile
from pathlib import Path

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

from pdf_patcher import patch_pdf_file

# Состояние пользователя: {file_path, file_name, bank} или {mode, step, ...} для выписки
USER_STATE: dict[int, dict] = {}


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
    uid = update.effective_user.id
    if uid in USER_STATE and "file_path" in USER_STATE[uid]:
        try:
            os.unlink(USER_STATE[uid]["file_path"])
        except OSError:
            pass
        del USER_STATE[uid]
    await update.message.reply_text(
        "👋 Привет! Я помогу изменить сумму в чеке.\n\n"
        "📋 **Как пользоваться:**\n"
        "1. Отправьте PDF-чек\n"
        "2. Выберите банк (Альфа-Банк или ВТБ)\n"
        "3. Введите сумму: *с какой на какую* (например: 10 5000 или 50 10000)\n\n"
        "📄 Создать выписку: /create_statement\n\n"
        "✅ Структура и метаданные PDF сохраняются.",
        parse_mode="Markdown",
    )


async def cmd_create_statement(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Команда создания выписки — выбор варианта."""
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
    await update.message.reply_text(
        "📄 **Создание выписки**\n\n"
        "Выберите вариант:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def handle_stmt_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработка выбора варианта выписки."""
    query = update.callback_query
    await query.answer()

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
    doc = update.message.document
    if not doc or not doc.file_name or not doc.file_name.lower().endswith(".pdf"):
        await update.message.reply_text("❌ Отправьте PDF-файл.")
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
        [InlineKeyboardButton("🔍 Авто", callback_data="bank_auto")],
    ]
    await update.message.reply_text(
        "📎 Чек получен. Выберите банк:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def _handle_statement_document(
    update: Update, context: ContextTypes.DEFAULT_TYPE,
    doc, uid: int, state: dict,
) -> None:
    """Обработка документа в режиме выписки."""
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
        await update.message.reply_text(
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
            await update.message.reply_text("❌ Не удалось извлечь сумму из чека.")
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
        await update.message.reply_text(
            f"✅ Чек получен.\n\n"
            f"📊 Сумма: {amount} ₽\n"
            f"👤 ФИО (сгенерировано): {generated_fio}\n\n"
            "💰 Введите баланс на начало периода (число):",
        )


async def handle_bank_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    user_id = update.effective_user.id
    if user_id not in USER_STATE:
        await query.edit_message_text("❌ Чек не найден. Отправьте PDF заново.")
        return

    bank_map = {"bank_alfa": "alfa", "bank_vtb": "vtb", "bank_auto": "auto"}
    bank = bank_map.get(query.data, "auto")
    bank_name = {"alfa": "Альфа-Банк", "vtb": "ВТБ", "auto": "Авто"}[bank]
    USER_STATE[user_id]["bank"] = bank

    await query.edit_message_text(
        f"✅ Банк: {bank_name}\n\n"
        "💰 Введите сумму: *с какой на какую* менять\n"
        "Например: `10 5000` или `50 10000`",
        parse_mode="Markdown",
    )


async def handle_amounts(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    state = USER_STATE.get(user_id, {})

    # Режим выписки — текст
    if state.get("mode", "").startswith("statement_"):
        await _handle_statement_text(update, context, user_id, state)
        return

    # Режим чека
    if user_id not in USER_STATE or "bank" not in USER_STATE[user_id]:
        await update.message.reply_text("❌ Сначала отправьте чек и выберите банк.")
        return

    parsed = parse_amounts(update.message.text)
    if not parsed:
        await update.message.reply_text(
            "❌ Неверный формат. Введите две суммы, например: 10 5000",
        )
        return

    amount_from, amount_to = parsed
    if amount_from <= 0 or amount_to <= 0:
        await update.message.reply_text("❌ Суммы должны быть больше 0.")
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
        await update.message.reply_text(f"❌ Ошибка: {err}")
        try:
            os.unlink(out_path)
        except OSError:
            pass
        return

    with open(out_path, "rb") as f:
        await update.message.reply_document(
            document=f,
            filename=out_name,
            caption=f"✅ Готово: {amount_from} ₽ → {amount_to} ₽",
        )

    try:
        os.unlink(out_path)
    except OSError:
        pass


async def _handle_statement_text(
    update: Update, context: ContextTypes.DEFAULT_TYPE,
    uid: int, state: dict,
) -> None:
    """Обработка текста в режиме выписки."""
    mode = state.get("mode", "")
    step = state.get("step", "")
    text = update.message.text.strip()

    if mode == "statement_edit":
        if step == "amounts":
            pairs = parse_amount_pairs(text)
            if not pairs:
                await update.message.reply_text(
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
            await update.message.reply_text(
                f"✅ Замены: {amounts}\n\n"
                f"📊 Расходы: {expenses:.2f} ₽\n"
                f"📊 Баланс на конец: {balance_end:.2f} ₽\n\n"
                "Пропустить / Далее или + свои замены (ФИО, телефон, баланс):",
                reply_markup=InlineKeyboardMarkup(keyboard),
            )

        elif step == "custom":
            parsed = parse_custom_replacement(text)
            if not parsed:
                await update.message.reply_text(
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
                    await update.message.reply_text(
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
                    await update.message.reply_text("❌ Введите число для баланса.")
                    return
            elif key in ("телефон", "phone"):
                repl["phone"] = value
            elif key in ("номер_заявки", "application_id"):
                repl["application_id"] = value

            await update.message.reply_text(
                f"✅ Добавлено: {key}={value}\n\n"
                "Введите ещё замены или нажмите Далее.",
            )

    elif mode == "statement_from_receipt" and step == "balance":
        try:
            balance_start = float(text.replace(",", ".").replace(" ", ""))
        except ValueError:
            await update.message.reply_text("❌ Введите число (баланс на начало).")
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
            await update.message.reply_text(f"❌ Ошибка: {err}")
            try:
                os.unlink(out_path)
            except OSError:
                pass
            return

        out_name = f"выписка_{amount}.pdf"
        with open(out_path, "rb") as f:
            await update.message.reply_document(
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
    query = update.callback_query
    await query.answer()

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
        await update.callback_query.edit_message_text(f"❌ Ошибка: {err}")
        try:
            os.unlink(out_path)
        except OSError:
            pass
        return

    out_name = Path(state.get("file_name", "выписка.pdf")).stem + "_patched.pdf"
    with open(out_path, "rb") as f:
        await update.callback_query.message.reply_document(
            document=f,
            filename=out_name,
            caption="✅ Выписка готова",
        )

    try:
        os.unlink(out_path)
    except OSError:
        pass


def main() -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        print("Задайте TELEGRAM_BOT_TOKEN")
        return

    app = Application.builder().token(token).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("create_statement", cmd_create_statement))
    app.add_handler(MessageHandler(filters.Document.PDF, handle_document))
    app.add_handler(CallbackQueryHandler(handle_bank_callback, pattern="^bank_"))
    app.add_handler(CallbackQueryHandler(handle_stmt_choice, pattern="^stmt_edit$|^stmt_receipt$"))
    app.add_handler(CallbackQueryHandler(handle_stmt_callback, pattern="^stmt_"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_amounts))

    print("Бот запущен...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
