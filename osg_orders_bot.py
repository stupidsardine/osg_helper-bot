# -*- coding: utf-8 -*-
"""
OSG Orders Bot — v21+ (python-telegram-bot)
Google Sheets (gspread + сервисный аккаунт)

ENV/настройки (варианты):
- В .env/переменных окружения (рекомендуется)
    TELEGRAM_BOT_TOKEN=xxx:yyyy
    GOOGLE_SHEET_ID=1A2B3C... (ID таблицы в URL)
    GOOGLE_APPLICATION_CREDENTIALS=./gsa.json  (путь к ключу сервисного аккаунта)
- Либо задай константы ниже (fallback).
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
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)

# -------------------- ЛОГИРОВАНИЕ --------------------
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,  # хочешь больше подробностей — DEBUG
)
logger = logging.getLogger("osg-bot")
logger.setLevel(logging.DEBUG)

# -------------------- НАСТРОЙКИ ----------------------
TELEGRAM_BOT_TOKEN = "8462456972:AAHBUSVkSYEsJWmexYBoK-gLcTbsdj1LLXo"
GOOGLE_SHEET_ID = "1O1LQ0y9IC4k4sp6_q5Uq5E8hABVLkh_29txBaygULdA"
GOOGLE_CREDS_PATH = r"C:\Users\Алексей\Desktop\osg-helper-bot\gsa.json"
ORDERS_SHEET_NAME = "Orders"



# Параметры расчёта (можно вынести в ENV при желании)
SHELF_LIFE_DAYS = int(os.getenv("SHELF_LIFE_DAYS", "360"))      # срок годности (дней)
TARGET_OSG_PERCENT = int(os.getenv("TARGET_OSG_PERCENT", "82")) # целевой ОСС (в %)
SAFETY_BUFFER_DAYS = int(os.getenv("SAFETY_BUFFER_DAYS", "2"))  # технологический буфер

# Какая вкладка в книге
ORDERS_SHEET_NAME = os.getenv("ORDERS_SHEET_NAME", "Orders").strip()

# Кэш заказов: {order_no: "dd.mm.yyyy"}
ORDERS_CACHE: Dict[str, str] = {}

# -------------------- УТИЛИТЫ ------------------------
def parse_date(date_str: str) -> Optional[datetime]:
    """Пытается распознать текстовую дату в нескольких форматах."""
    if not date_str:
        return None

    # Терпимо относимся к «пустым» строкам и лишним пробелам
    s = str(date_str).strip()
    if not s:
        return None

    # Популярные форматы
    formats: List[str] = [
        "%d.%m.%Y",
        "%Y-%m-%d",
        "%d-%m-%Y",
        "%d/%m/%Y",
        "%d.%m.%y",
    ]
    for fmt in formats:
        try:
            return datetime.strptime(s, fmt)
        except Exception:
            continue

    # Иногда в Google Sheets дата может прийти уже как datetime.date/datetime
    if isinstance(date_str, datetime):
        return date_str

    return None


def min_production_date_for_osg(delivery_dt: datetime) -> datetime:
    """
    Расчёт «не раньше какого дня можно производить», чтобы к дате доставки
    ОСС был >= TARGET_OSG_PERCENT.

    Предположим линейное падение ОСС: 100% -> 0% за SHELF_LIFE_DAYS.
      age_max_days = floor((100 - target)/100 * shelf_life) - buffer
    Производить не раньше: delivery_dt - age_max_days.
    """
    # Максимальный допустимый возраст к дате доставки
    max_age_float = (100 - TARGET_OSG_PERCENT) / 100 * SHELF_LIFE_DAYS
    max_age_days = max(0, int(max_age_float) - SAFETY_BUFFER_DAYS)
    return delivery_dt - timedelta(days=max_age_days)


def _orders_keyboard() -> InlineKeyboardMarkup:
    if not ORDERS_CACHE:
        return InlineKeyboardMarkup([[InlineKeyboardButton("Пусто", callback_data="noop")]])
    buttons = [[InlineKeyboardButton(order_no, callback_data=order_no)] for order_no in sorted(ORDERS_CACHE)]
    return InlineKeyboardMarkup(buttons)


def _gs_open_worksheet():
    """Возвращает (sh, ws) — книгу и лист по имени."""
    # service_account уже возвращает готовый gspread.Client
    gc = gspread.service_account(filename=GOOGLE_CREDS_PATH)

    sh = gc.open_by_key(GOOGLE_SHEET_ID)
    ws = sh.worksheet(ORDERS_SHEET_NAME)  # лист по имени
    return sh, ws




def load_orders_from_sheet() -> Dict[str, str]:
    """Читает все строки и возвращает {order_no: delivery_str}."""
    _, ws = _gs_open_worksheet()

    values = ws.get_all_values()  # вся таблица как список списков
    if not values:
        return {}

    # ---- заголовки
    headers = [h.strip().lower() for h in values[0]]  # убрали пробелы и привели к lower
    try:
        idx_order = headers.index("orderno")
        idx_date  = headers.index("deliverydate")
    except ValueError:
        raise KeyError(
            f"В первой строке должны быть колонки 'OrderNo' и 'DeliveryDate'. Сейчас: {headers}"
        )

    # ---- данные
    orders: Dict[str, str] = {}
    for row in values[1:]:
        # защита от коротких строк
        if len(row) <= max(idx_order, idx_date):
            continue
        order_no = (row[idx_order] or "").strip()
        delivery = (row[idx_date]  or "").strip()
        if not order_no:
            continue
        orders[order_no] = delivery or "—"

    return orders

# -------------------- ОБРАБОТЧИКИ -------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "Бот расчёта дат производства под ОСС.\n\n"
        "Команды:\n"
        "/reload — перечитать книгу и обновить кэш заказов\n"
        "/orders — показать кнопки с номерами заказов\n"
        "/debug — диагностика связи с Google Sheets\n\n"
        "Правило: считаем минимальную дату розлива так, чтобы к дате доставки\n"
        f"ОСС был ≥ {TARGET_OSG_PERCENT}%, при сроке годности {SHELF_LIFE_DAYS} дней\n"
        f"и буфере {SAFETY_BUFFER_DAYS} дн."
    )
    await update.message.reply_text(text)


async def debug(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Проверка подключения к Google Sheets."""
    try:
        sh, ws = _gs_open_worksheet()
        first_row = ws.row_values(1)
        worksheets = [w.title for w in sh.worksheets()]
        msg = (
            "✅ Подключение к Google Sheets — OK\n"
            f"Книга: {sh.title}\n"
            f"Листы: {', '.join(worksheets)}\n"
            f"Использую лист: {ws.title}\n"
            f"Заголовки первой строки: {first_row}"
        )
        await update.message.reply_text(msg)
    except Exception as e:
        logger.exception("DEBUG error")
        await update.message.reply_text(f"⚠️ Ошибка при доступе к Google Sheets: {e}")


async def reload_orders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Перечитать таблицу, собрать кэш."""
    try:
        global ORDERS_CACHE
        ORDERS_CACHE = load_orders_from_sheet()
        await update.message.reply_text(f"✅ Загружено {len(ORDERS_CACHE)} заказов из Google Sheets.")
    except Exception as e:
        logger.exception("Ошибка при загрузке данных")
        await update.message.reply_text(f"⚠️ Ошибка при загрузке данных: {e}")


async def show_orders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показать кнопки с заказами."""
    if not ORDERS_CACHE:
        await update.message.reply_text("Кэш пуст. Сначала выполните /reload")
        return
    await update.message.reply_text("Выбери заказ:", reply_markup=_orders_keyboard())


async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка нажатия на номер заказа."""
    query = update.callback_query
    await query.answer()

    order_no = query.data
    if order_no == "noop":
        return

    delivery_str = ORDERS_CACHE.get(order_no, "")
    delivery_dt = parse_date(delivery_str)
    if delivery_dt is None:
        await query.edit_message_text(
            f"📦 Заказ: {order_no}\n⚠️ Не удалось распознать дату доставки: {delivery_str}"
        )
        return

    # расчёт минимальной даты производства
    min_prod = min_production_date_for_osg(delivery_dt)

    reply = (
        f"📦 Заказ: {order_no}\n"
        f"📅 Дата доставки: {delivery_dt.strftime('%d.%m.%Y')}\n"
        f"💧 Требуемый ОСС: ≥ {TARGET_OSG_PERCENT}%\n"
        f"🏭 Производство — не раньше: {min_prod.strftime('%d.%m.%Y')}\n"
        f"📊 Параметры: СГ={SHELF_LIFE_DAYS} дней, буфер={SAFETY_BUFFER_DAYS} дн."
    )
    await query.edit_message_text(reply)

# --- очистка webhook перед стартом, чтобы не было конфликта getUpdates ---
async def _clear_webhook(app: Application):
    try:
        await app.bot.delete_webhook(drop_pending_updates=True)
        logger.info("Webhook очищен (drop_pending_updates=True).")
    except Exception:
        logger.exception("Не удалось очистить webhook")

# -------------------- main --------------------------
def main():
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN не задан. Проверь ENV/настройки.")

    app = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .post_init(_clear_webhook)
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", start))
    app.add_handler(CommandHandler("debug", debug))
    app.add_handler(CommandHandler("reload", reload_orders))
    app.add_handler(CommandHandler("orders", show_orders))
    app.add_handler(CallbackQueryHandler(button_callback))

    logger.info("Бот запущен. Ожидаю сообщения…")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    try:
        # Можно включить детальный лог PTB при необходимости:
        # os.environ["PTB_LOG_LEVEL"] = "DEBUG"
        main()
    except Exception:
        import traceback
        traceback.print_exc()
        raise
