import os
import logging
from datetime import datetime

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardRemove,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

import gspread
from oauth2client.service_account import ServiceAccountCredentials

# ================= НАСТРОЙКИ =================

TELEGRAM_BOT_TOKEN = os.getenv(
    "TELEGRAM_BOT_TOKEN",
    "8462456972:AAHBUSVkSYEsJWmexYBoK-gLcTbsdj1LLXo",
)

GOOGLE_SHEET_ID = os.getenv(
    "GOOGLE_SHEET_ID",
    "1O1LQ0y9IC4k4sp6_q5Uq5E8hABVLkh_29txBaygULdA",
)

GOOGLE_CREDS_PATH = os.getenv(
    "GOOGLE_APPLICATION_CREDENTIALS",
    r"C:\Users\Алексей\Desktop\osg-helper-bot\gsa.json",
)

LOG_SHEET_NAME = "ChecksLog"

SHELF_LIFE_DAYS = 360

NETWORKS = {
    "Самокат": 80,
    "ВкусВилл": 70,
    "Монетка": 80,
}

# ============== GOOGLE SHEETS ================

scope = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/drive",
]

creds = ServiceAccountCredentials.from_json_keyfile_name(
    GOOGLE_CREDS_PATH, scope
)
gc = gspread.authorize(creds)
sh = gc.open_by_key(GOOGLE_SHEET_ID)
log_ws = sh.worksheet(LOG_SHEET_NAME)

# ================= ЛОГИКА ====================

def parse_short_date(text: str):
    text = text.strip()
    if not text.isdigit() or len(text) != 6:
        return None
    try:
        return datetime.strptime(
            f"{text[:2]}.{text[2:4]}.20{text[4:]}",
            "%d.%m.%Y"
        ).date()
    except ValueError:
        return None

def calculate_osg(batch_date, arrival_date):
    days_passed = (arrival_date - batch_date).days
    osg = ((SHELF_LIFE_DAYS - days_passed) / SHELF_LIFE_DAYS) * 100
    return round(osg, 1), days_passed

def log_check(user, batch_date, arrival_date, network, osg, blocked):
    log_ws.append_row([
        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        user.id,
        user.username or "",
        batch_date.strftime("%d.%m.%Y"),
        arrival_date.strftime("%d.%m.%Y"),
        network,
        osg,
        "OK" if not blocked else "BLOCK",
        ", ".join(blocked),
    ])

# ================= UI ========================

def main_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📅 Дата розлива", callback_data="set_batch")],
        [InlineKeyboardButton("📦 Дата прихода", callback_data="set_arrival")],
        [InlineKeyboardButton("🏬 Проверить по сети", callback_data="choose_network")],
    ])

def networks_keyboard():
    rows = [[InlineKeyboardButton(n, callback_data=f"net_{n}")] for n in NETWORKS]
    rows.append([InlineKeyboardButton("⬅️ В меню", callback_data="back")])
    return InlineKeyboardMarkup(rows)

# ================= ХЕНДЛЕРЫ ==================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()

    # 🔥 ГАРАНТИРОВАННО УБИРАЕМ ЗАЛИПШУЮ REPLY-КЛАВИАТУРУ
    await update.message.reply_text(
        "⌛ Обновляю интерфейс…",
        reply_markup=ReplyKeyboardRemove()
    )

    await update.message.reply_text(
        "💧 Проверка OSG на дату прихода\n\nВыбери действие:",
        reply_markup=main_menu()
    )

async def callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "set_batch":
        context.user_data["awaiting"] = "batch"
        await query.message.edit_text(
            "📅 Введи дату розлива\n"
            "Только цифры (ДДММГГ)\n"
            "Пример: 151225"
        )
        return

    if data == "set_arrival":
        context.user_data["awaiting"] = "arrival"
        await query.message.edit_text(
            "📦 Введи дату прихода\n"
            "Только цифры (ДДММГГ)\n"
            "Пример: 090226"
        )
        return

    if data == "choose_network":
        if "batch_date" not in context.user_data or "arrival_date" not in context.user_data:
            await query.message.edit_text(
                "❗ Сначала введи дату розлива и дату прихода",
                reply_markup=main_menu()
            )
            return

        await query.message.edit_text(
            "🏬 Выбери сеть:",
            reply_markup=networks_keyboard()
        )
        return

    if data.startswith("net_"):
        network = data.replace("net_", "")
        batch = context.user_data["batch_date"]
        arrival = context.user_data["arrival_date"]

        osg, days_passed = calculate_osg(batch, arrival)
        required = NETWORKS[network]
        diff = round(required - osg, 1)
        blocked = [n for n, min_osg in NETWORKS.items() if osg < min_osg]

        text = (
            f"🏬 {network}\n\n"
            f"🏭 Розлив: {batch.strftime('%d.%m.%Y')}\n"
            f"📦 Приход: {arrival.strftime('%d.%m.%Y')}\n"
            f"⏳ Прошло дней: {days_passed}\n\n"
            f"💧 OSG на дату прихода: {osg}%\n"
            f"Требование сети: ≥ {required}%\n"
        )

        if osg >= required:
            text += "✅ Статус: МОЖНО"
        else:
            text += f"❌ Статус: НЕЛЬЗЯ\nНедостает: {diff}%"

        log_check(query.from_user, batch, arrival, network, osg, blocked)

        await query.message.edit_text(
            text,
            reply_markup=networks_keyboard()
        )
        return

    if data == "back":
        await query.message.edit_text(
            "💧 Проверка OSG на дату прихода\n\nВыбери действие:",
            reply_markup=main_menu()
        )

async def text_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    awaiting = context.user_data.get("awaiting")

    if awaiting not in ("batch", "arrival"):
        return

    date_value = parse_short_date(update.message.text)

    if not date_value:
        await update.message.reply_text(
            "❌ Неверный ввод.\nПример: 090226",
            reply_markup=ReplyKeyboardRemove()
        )
        return

    if awaiting == "batch":
        context.user_data["batch_date"] = date_value
        label = "розлива"
    else:
        context.user_data["arrival_date"] = date_value
        label = "прихода"

    context.user_data["awaiting"] = None

    await update.message.reply_text(
        f"✅ Дата {label}: {date_value.strftime('%d.%m.%Y')}",
        reply_markup=ReplyKeyboardRemove()
    )

    await update.message.reply_text(
        "Выбери действие:",
        reply_markup=main_menu()
    )

# ================= ЗАПУСК ====================

def main():
    logging.basicConfig(level=logging.INFO)

    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(callbacks))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_input))

    app.run_polling()

if __name__ == "__main__":
    main()
