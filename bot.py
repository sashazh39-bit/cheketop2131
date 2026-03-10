#!/usr/bin/env python3
"""Telegram-бот для замены суммы в PDF-чеках (Альфа-Банк, ВТБ).
Отправьте чек → выберите банк → введите "с какой на какую" → получите готовый PDF.
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

# Состояние пользователя: {file_path, file_name, bank}
USER_STATE: dict[int, dict] = {}


def parse_amounts(text: str) -> tuple[int, int] | None:
    """Разбор '10 5000' или '10 5 000' → (10, 5000)."""
    nums = re.findall(r"\d+", text.strip())
    if len(nums) >= 2:
        return int(nums[0]), int("".join(nums[1:]))
    return None


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
        "✅ Структура и метаданные PDF сохраняются.",
        parse_mode="Markdown",
    )


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    doc = update.message.document
    if not doc or not doc.file_name or not doc.file_name.lower().endswith(".pdf"):
        await update.message.reply_text("❌ Отправьте PDF-файл чека.")
        return

    file = await context.bot.get_file(doc.file_id)
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as fp:
        await file.download_to_drive(fp.name)
        # Очистка предыдущего чека, если был
        old = USER_STATE.get(update.effective_user.id, {})
        if "file_path" in old and old["file_path"] != fp.name:
            try:
                os.unlink(old["file_path"])
            except OSError:
                pass
        USER_STATE[update.effective_user.id] = {
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
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        "📎 Чек получен. Выберите банк:",
        reply_markup=reply_markup,
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


def main() -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        print("Задайте TELEGRAM_BOT_TOKEN")
        return

    app = Application.builder().token(token).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(MessageHandler(filters.Document.PDF, handle_document))
    app.add_handler(CallbackQueryHandler(handle_bank_callback, pattern="^bank_"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_amounts))

    print("Бот запущен...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
