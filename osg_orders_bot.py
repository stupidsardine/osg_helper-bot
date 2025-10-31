# -*- coding: utf-8 -*-
# -*- coding: utf-8 -*-
"""
OSG Orders Bot — PTB v21+
Google Sheets (gspread + сервисный аккаунт)

Минимальная структура листа (ORDERS_SHEET_NAME):
- OrderNo        — номер заказа
- DeliveryDate   — дата доставки (dd.mm.yyyy, yyyy-mm-dd, dd/mm/yyyy, dd.mm.yy)

Логика:
— считаем минимальную дату розлива так, чтобы к дате доставки OSG сохранился ≥ TARGET_OSG_PERCENT,
  используя срок годности SHELF_LIFE_DAYS и технологический буфер SAFETY_BUFFER_DAYS.
— никаких колонок OSG больше не требуется.
— кнопки под строкой ввода: Обновить / Заказы / Диагностика (и резервная команда /menu).
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
logger.setLevel(logging.DEBUG)

# -------------------- НАСТРОЙКИ ----------------------
# можно переопределить через ENV; по умолчанию подставлены твои значения
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8462456972:AAHBUSVkSYEsJWmexYBoK-gLcTbsdj1LLXo")
GOOGLE_SHEET_ID    = os.getenv("GOOGLE_SHEET_ID",    "1O1LQ0y9IC4k4sp6_q5Uq5E8hABVLkh_29txBaygULdA")
GOOGLE_CREDS_PATH  = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", r"C:\Users\Алексей\Desktop\osg-helper-bot\gsa.json")
ORDERS_SHEET_NAME  = os.getenv("ORDERS_SHEET_NAME",  "Orders").strip()

# Параметры расчёта (можно переопределить ENV)
SHELF_LIFE_DAYS     = int(os.getenv("SHELF_LIFE_DAYS",     "360"))  # срок годности (дней)
TARGET_OSG_PERCENT  = int(os.getenv("TARGET_OSG_PERCENT",  "80"))   # целевой OSG (%)
SAFETY_BUFFER_DAYS  = int(os.getenv("SAFETY_BUFFER_DAYS",  "3"))    # технологический буфер (дней)

# Кэш заказов: {order_no: "delivery_str"}
ORDERS_CACHE: Dict[str, str] = {}

# Кнопки под строкой ввода
REPLY_KB = ReplyKeyboardMarkup(
    [["Обновить", "Заказы", "Диагностика"]],
    resize_keyboard=True,
    one_time_keyboard=False,
)

# -------------------- УТИЛИТЫ ------------------------
def parse_date(date_str: str) -> Optional[datetime]:
    """Пытается распознать текстовую дату в нескольких форматах."""
    if not date_str:
        return None
    s = str(date_str).strip()
    if not s:
        return None

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

    if isinstance(date_str, datetime):
        return date_str
    return None


def min_production_date_for_osg(delivery_dt: datetime) -> datetime:
    """
    Производить не раньше такой даты, чтобы к DeliveryDate продукт сохранил OSG ≥ TARGET_OSG_PERCENT.
    Модель: линейное падение OSG 100% -> 0% за SHELF_LIFE_DAYS.
    max_age_days = floor((100 - target)/100 * shelf_life) - buffer
    """
    max_age_float = (100 - TARGET_OSG_PERCENT) / 100 * SHELF_LIFE_DAYS
    max_age_days = max(0, int(max_age_float) - SAFETY_BUFFER_DAYS)
    return delivery_dt - timedelta(days=max_age_days)


def _gs_open_worksheet():
    """Возвращает (sh, ws) — книгу и лист по имени."""
    gc = gspread.service_account(filename=GOOGLE_CREDS_PATH)
    sh = gc.open_by_key(GOOGLE_SHEET_ID)
    ws = sh.worksheet(ORDERS_SHEET_NAME)
    return sh, ws


def load_orders_from_sheet() -> Dict[str, str]:
    """Читает все строки и возвращает {order_no: delivery_str}."""
    _, ws = _gs_open_worksheet()
    values = ws.get_all_values()
    if not values:
        return {}

    headers = [h.strip().lower() for h in values[0]]
    try:
        idx_order = headers.index("orderno")
        idx_date  = headers.index("deliverydate")
    except ValueError:
        raise KeyError("В первой строке должны быть колонки 'OrderNo' и 'DeliveryDate'.")

    orders: Dict[str, str] = {}
    for row in values[1:]:
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
        "Бот расчёта дат производства под целевой OSG.\n\n"
        "Команды:\n"
        "/reload — перечитать книгу и обновить кэш\n"
        "/orders — показать список заказов\n"
        "/debug  — диагностика Google Sheets\n"
        "/menu   — показать панель кнопок\n\n"
        "Параметры:\n"
        f"• Целевой OSG: ≥ {TARGET_OSG_PERCENT}%\n"
        f"• Срок годности: {SHELF_LIFE_DAYS} дней\n"
        f"• Буфер: {SAFETY_BUFFER_DAYS} дн."
    )
    await update.message.reply_text(text, reply_markup=REPLY_KB)

# Ручной вызов клавиатуры (если вдруг пропала)
async def menu_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Меню:", reply_markup=REPLY_KB)

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
        await update.message.reply_text(msg, reply_markup=REPLY_KB)
    except Exception as e:
        logger.exception("DEBUG error")
        await update.message.reply_text(f"⚠️ Ошибка при доступе к Google Sheets: {e}", reply_markup=REPLY_KB)

async def reload_orders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Перечитать таблицу, собрать кэш."""
    try:
        global ORDERS_CACHE
        ORDERS_CACHE = load_orders_from_sheet()
        await update.message.reply_text(
            f"✅ Загружено {len(ORDERS_CACHE)} заказов из Google Sheets.",
            reply_markup=REPLY_KB
        )
    except Exception as e:
        logger.exception("Ошибка при загрузке данных")
        await update.message.reply_text(f"⚠️ Ошибка при загрузке данных: {e}", reply_markup=REPLY_KB)

def _orders_keyboard() -> InlineKeyboardMarkup:
    if not ORDERS_CACHE:
        return InlineKeyboardMarkup([[InlineKeyboardButton("Пусто", callback_data="noop")]])
    buttons = [[InlineKeyboardButton(order_no, callback_data=order_no)]
               for order_no in sorted(ORDERS_CACHE)]
    return InlineKeyboardMarkup(buttons)

async def show_orders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показать кнопки с заказами."""
    if not ORDERS_CACHE:
        await update.message.reply_text("Кэш пуст. Сначала выполните /reload", reply_markup=REPLY_KB)
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

    # расчёт минимальной даты производства (чтобы к дате доставки OSG ≥ целевого)
    min_prod = min_production_date_for_osg(delivery_dt)

    reply = (
        f"📦 Заказ: {order_no}\n"
        f"📅 Дата доставки: {delivery_dt.strftime('%d.%m.%Y')}\n"
        f"💧 Требуемый OSG: ≥ {TARGET_OSG_PERCENT}%\n"
        f"🏭 Производство — *не раньше*: {min_prod.strftime('%d.%m.%Y')}\n"
        f"📊 Параметры: СГ={SHELF_LIFE_DAYS} дней, буфер={SAFETY_BUFFER_DAYS} дн."
    )
    await query.edit_message_text(reply, parse_mode="Markdown")

# Текстовые кнопки (ReplyKeyboard)
async def on_text_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = (update.message.text or "").strip()
    if txt == "Обновить":
        await reload_orders(update, context)
    elif txt == "Заказы":
        await show_orders(update, context)
    elif txt == "Диагностика":
        await debug(update, context)

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

    # Команды
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", start))
    app.add_handler(CommandHandler("menu", menu_cmd))
    app.add_handler(CommandHandler("debug", debug))
    app.add_handler(CommandHandler("reload", reload_orders))
    app.add_handler(CommandHandler("orders", show_orders))

    # Текстовые кнопки (ReplyKeyboard)
    app.add_handler(MessageHandler(filters.Regex(r"^(Обновить|Заказы|Диагностика)$"), on_text_buttons))

    # Кнопки-заказы (Inline)
    app.add_handler(CallbackQueryHandler(button_callback))

    logger.info("Бот запущен. Ожидаю сообщения…")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback
        traceback.print_exc()
        raise
