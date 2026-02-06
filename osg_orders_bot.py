# -*- coding: utf-8 -*-
"""
OSG Orders Bot — работа по контрагентам + логирование пользователей
Google Sheets (gspread + сервисный аккаунт)

Лист Orders:
- Contractor
- DeliveryDate

Лист UserLog (создаётся автоматически):
- timestamp | user_id | username | name | action | extra
"""

import os
import logging
from typing import Dict, List, Optional
from datetime import datetime, timedelta

import gspread
from gspread.exceptions import WorksheetNotFound

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# -------------------- ЛОГИРОВАНИЕ --------------------
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("osg-bot")

# -------------------- НАСТРОЙКИ ----------------------
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

ORDERS_SHEET_NAME = "Orders"
LOG_SHEET_NAME = "UserLog"

# Параметры расчёта (БУФЕР УБРАН)
SHELF_LIFE_DAYS = 360
TARGET_OSG_PERCENT = 80

# -------------------- КЭШ ----------------------------
CONTRACTORS_CACHE: Dict[str, Dict[str, str]] = {}

# -------------------- КНОПКИ -------------------------
REPLY_KB = ReplyKeyboardMarkup(
    [["Обновить", "Контрагенты", "Диагностика"]],
    resize_keyboard=True,
    one_time_keyboard=False,
)

# -------------------- УТИЛИТЫ ------------------------
def parse_date(date_str: str) -> Optional[datetime]:
    if not date_str:
        return None
    for fmt in ("%d.%m.%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(date_str.strip(), fmt)
        except ValueError:
            pass
    return None


def min_production_date_for_osg(delivery_dt: datetime) -> datetime:
    """
    Расчёт БЕЗ буфера
    """
    max_age_float = (100 - TARGET_OSG_PERCENT) / 100 * SHELF_LIFE_DAYS
    max_age_days = int(max_age_float)
    return delivery_dt - timedelta(days=max_age_days)


def gs_client():
    return gspread.service_account(filename=GOOGLE_CREDS_PATH)


def open_orders_ws():
    sh = gs_client().open_by_key(GOOGLE_SHEET_ID)
    return sh, sh.worksheet(ORDERS_SHEET_NAME)


def get_log_ws(sh):
    try:
        return sh.worksheet(LOG_SHEET_NAME)
    except WorksheetNotFound:
        ws = sh.add_worksheet(title=LOG_SHEET_NAME, rows=1000, cols=6)
        ws.append_row(["timestamp", "user_id", "username", "name", "action", "extra"])
        return ws


def log_user_action(user, action: str, extra: str = ""):
    try:
        if not user:
            return
        sh, _ = open_orders_ws()
        ws = get_log_ws(sh)
        ws.append_row([
            datetime.now().strftime("%d.%m.%Y %H:%M:%S"),
            user.id,
            user.username or "",
            f"{user.first_name or ''} {user.last_name or ''}".strip(),
            action,
            extra
        ])
    except Exception:
        logger.exception("Ошибка логирования")

# -------------------- ДАННЫЕ -------------------------
def load_contractors():
    _, ws = open_orders_ws()
    rows = ws.get_all_values()
    headers = [h.lower().strip() for h in rows[0]]

    idx_c = headers.index("contractor")
    idx_d = headers.index("deliverydate")

    data = {}
    for r in rows[1:]:
        if len(r) <= max(idx_c, idx_d):
            continue
        name = r[idx_c].strip()
        if name:
            data[name] = {"delivery": r[idx_d].strip()}
    return data


def contractors_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(name, callback_data=name)]
        for name in sorted(CONTRACTORS_CACHE)
    ])

# -------------------- HANDLERS -----------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    log_user_action(update.effective_user, "start")
    await update.message.reply_text(
        "Бот расчёта дат производства под OSG (80%).\n"
        "Работай через кнопки 👇",
        reply_markup=REPLY_KB
    )


async def reload_data(update: Update, context: ContextTypes.DEFAULT_TYPE):
    log_user_action(update.effective_user, "reload")
    global CONTRACTORS_CACHE
    CONTRACTORS_CACHE = load_contractors()
    await update.message.reply_text(
        f"Загружено контрагентов: {len(CONTRACTORS_CACHE)}",
        reply_markup=REPLY_KB
    )


async def show_contractors(update: Update, context: ContextTypes.DEFAULT_TYPE):
    log_user_action(update.effective_user, "show_contractors")
    if not CONTRACTORS_CACHE:
        await update.message.reply_text("Сначала нажми «Обновить»", reply_markup=REPLY_KB)
        return
    await update.message.reply_text(
        "Выбери контрагента:",
        reply_markup=contractors_keyboard()
    )


async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    contractor = query.data
    log_user_action(query.from_user, "select_contractor", contractor)

    delivery_dt = parse_date(CONTRACTORS_CACHE[contractor]["delivery"])
    prod_date = min_production_date_for_osg(delivery_dt)

    await query.message.reply_text(
        f"🏢 {contractor}\n"
        f"📦 Доставка: {delivery_dt.strftime('%d.%m.%Y')}\n"
        f"🏭 Производство не раньше: {prod_date.strftime('%d.%m.%Y')}",
        reply_markup=REPLY_KB
    )


async def text_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = (update.message.text or "").strip().lower()

    if txt == "обновить":
        await reload_data(update, context)
    elif txt == "контрагенты":
        await show_contractors(update, context)
    elif txt == "диагностика":
        await update.message.reply_text("Подключение активно", reply_markup=REPLY_KB)
    else:
        await update.message.reply_text("Используй кнопки 👇", reply_markup=REPLY_KB)

# -------------------- MAIN ---------------------------
def main():
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("reload", reload_data))
    app.add_handler(CommandHandler("contractors", show_contractors))

    # ❗ СНАЧАЛА callback
    app.add_handler(CallbackQueryHandler(callback_handler))

    # ❗ ПОТОМ текст
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_router))

    logger.info("Бот запущен")
    app.run_polling()


if __name__ == "__main__":
    main()
