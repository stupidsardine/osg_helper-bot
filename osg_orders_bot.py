# -*- coding: utf-8 -*-
"""
OSG Orders Bot
python-telegram-bot == 21.x
gspread + Google Service Account

Команды:
  /start  /help   – приветствие и инструкция
  /reload        – перечитать книгу и обновить кэш заказов
  /orders        – показать кнопки с номерами заказов
  /debug         – диагностика подключения к Google Sheets
"""

import os
import logging
from typing import Dict, List

import gspread
from gspread.exceptions import WorksheetNotFound
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)
from datetime import datetime, timedelta

from datetime import datetime, timedelta
# --- Функция для распознавания разных форматов даты ---
def parse_date(date_str):
    """Пытается понять дату в разных форматах"""
    if not date_str:
        return None
    if isinstance(date_str, datetime):
        return date_str
    for fmt in ("%d.%m.%Y", "%Y-%m-%d", "%d/%m/%Y", "%Y.%m.%d"):
        try:
            return datetime.strptime(date_str.strip(), fmt)
        except Exception:
            continue
    return None


# --- Параметры расчёта ОСС ---
SHELF_LIFE_DAYS = 360          # общий срок годности, дней
TARGET_OSG_PERCENT = 80        # требуемый ОСС на дату отгрузки, %
SAFETY_BUFFER_DAYS = 2         # небольшой запас

def min_production_date_for_osg(delivery_dt: datetime) -> datetime.date:
    """
    Минимальная дата производства, чтобы на дату отгрузки
    ОСС был >= TARGET_OSG_PERCENT (с учётом буфера).
    """
    max_elapsed = int(SHELF_LIFE_DAYS * (1 - TARGET_OSG_PERCENT / 100))
    allowed_age = max(0, max_elapsed - SAFETY_BUFFER_DAYS)
    return (delivery_dt - timedelta(days=allowed_age)).date()



# -------------------- НАСТРОЙКИ --------------------
# Можно задать здесь или через переменные окружения:
#   TELEGRAM_BOT_TOKEN, GOOGLE_SHEET_ID, GOOGLE_APPLICATION_CREDENTIALS
#   (GOOGLE_APPLICATION_CREDENTIALS — путь к JSON-файлу сервисного аккаунта)
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "<В8462456972:AAHBUSVkSYEsJWmexYBoK-gLcTbsdj1LLXoСТАВЬ_СЮДА_ТОКЕН>")
GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID", "<1pduByH_gIF9PiLdbFU1IK3yFWJrwGc-maXCumi8r4q8>")
GOOGLE_CREDS_PATH = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "gsa.json")
# Вкладка (лист) по умолчанию. Если такого листа нет, бот возьмет первый.
SHEET_TITLE = os.getenv("GOOGLE_SHEET_TITLE", "Orders")

# -------------------- ЛОГИ -------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("osg-bot")

# Кэш заказов: {OrderNo: DeliveryDate}
ORDERS_CACHE: Dict[str, str] = {}


from gspread.exceptions import WorksheetNotFound, APIError

SHEET_TITLE = "Orders"        # ожидаемое имя листа (вкладки)
HEADER_ORDER = "OrderNo"
HEADER_DATE  = "DeliveryDate"

def _connect_sheet():
    """Подключение к книге по ID. Бросает понятную ошибку при проблеме доступа/ID."""
    import gspread
    from google.oauth2.service_account import Credentials

    scopes = [
        "https://www.googleapis.com/auth/spreadsheets.readonly",
        "https://www.googleapis.com/auth/drive.readonly",
    ]
    creds = Credentials.from_service_account_file(GOOGLE_CREDS_PATH, scopes=scopes)
    client = gspread.authorize(creds)

    try:
        sh = client.open_by_key(GOOGLE_SHEET_ID)
    except APIError as e:
        code = getattr(e.response, "status_code", None)
        raise RuntimeError(
            f"Google API: не удалось открыть книгу по ID ({GOOGLE_SHEET_ID}). "
            f"Статус: {code or 'unknown'}."
        ) from e

    return client, sh


def load_orders_from_sheet() -> dict[str, str]:
    """
    Загружает словарь {order_no: delivery_date_str}.
    Пытается взять лист 'Orders', если нет — берёт первый лист и предупреждает.
    Бросает RuntimeError с понятным текстом вместо голого [404].
    """
    global ORDERS_CACHE
    client, sh = _connect_sheet()

    # пробуем целевой лист
    try:
        ws = sh.worksheet(SHEET_TITLE)
        used_sheet = SHEET_TITLE
    except WorksheetNotFound:
        # fallback — первый лист
        ws = sh.get_worksheet(0)
        used_sheet = ws.title

    try:
        rows = ws.get_all_records()
    except APIError as e:
        code = getattr(e.response, "status_code", None)
        raise RuntimeError(
            f"Google API: не удалось прочитать лист '{used_sheet}'. Статус: {code or 'unknown'}."
        ) from e

    if not rows:
        raise RuntimeError(
            f"Лист '{used_sheet}' пустой или нет заголовков '{HEADER_ORDER}'/'{HEADER_DATE}'."
        )

    data: dict[str, str] = {}
    for r in rows:
        order_no = str(r.get(HEADER_ORDER, "")).strip()
        delivery  = str(r.get(HEADER_DATE, "")).strip()
        if not order_no:
            continue

        # приводим дату к человекочитаемой, если возможно
        dt = parse_date(delivery)
        data[order_no] = dt.strftime("%d.%m.%Y") if dt else delivery

    ORDERS_CACHE = data
    return ORDERS_CACHE, used_sheet

# -------------------- Хэндлеры ---------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "Бот расчёта дат производства и проверки заказов из Google Sheets.\n\n"
        "Команды:\n"
        "• /reload — перечитать таблицу и обновить кэш\n"
        "• /orders — показать кнопки с заказами\n"
        "• /debug — диагностика подключения к таблице\n\n"
        "Требуется лист с названием «Orders» (или будет выбран первый лист),\n"
        "в первой строке заголовки: OrderNo, DeliveryDate."
    )
    await update.message.reply_text(text)


async def debug(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает, к какой книге/листу подключились и какие заголовки видим."""
    try:
        if not os.path.exists(GOOGLE_CREDS_PATH):
            await update.message.reply_text(
                f"❌ Креды не найдены: {GOOGLE_CREDS_PATH}"
            )
            return

        client = gspread.service_account(filename=GOOGLE_CREDS_PATH)
        sh = client.open_by_key(GOOGLE_SHEET_ID)
        titles = [ws.title for ws in sh.worksheets()]
        try:
            ws = sh.worksheet(SHEET_TITLE.strip())
        except WorksheetNotFound:
            ws = sh.sheet1

        header = [c.strip() for c in ws.row_values(1)]

        msg = (
            "✅ Подключение к Google Sheets — OK\n"
            f"Книга: {sh.title}\n"
            f"Листы: {', '.join(titles)}\n"
            f"Использую лист: {ws.title}\n"
            f"Заголовки первой строки: {header}"
        )
        await update.message.reply_text(msg)
    except Exception as e:
        logger.exception("Ошибка /debug")
        await update.message.reply_text(f"❌ Ошибка Google Sheets: {e}")


async def reload_orders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        (cache, used_sheet) = load_orders_from_sheet()
        n = len(cache)
        extra = "" if used_sheet == SHEET_TITLE else f"\n⚠️ Лист '{SHEET_TITLE}' не найден. Использован лист: '{used_sheet}'."
        await update.message.reply_text(f"✅ Загружено {n} заказов из Google Sheets.{extra}")
    except RuntimeError as e:
        await update.message.reply_text(f"⚠️ Ошибка при загрузке: {e}")
    except Exception as e:
        # на всякий случай ловим всё остальное
        await update.message.reply_text(f"⚠️ Непредвиденная ошибка: {e}")



def _orders_keyboard() -> InlineKeyboardMarkup:
    """Строит клавиатуру с кнопками заказов из кэша."""
    if not ORDERS_CACHE:
        return InlineKeyboardMarkup(
            [[InlineKeyboardButton("Пусто", callback_data="noop")]]
        )

    buttons: List[List[InlineKeyboardButton]] = []
    for order_no in sorted(ORDERS_CACHE.keys()):
        buttons.append([InlineKeyboardButton(order_no, callback_data=order_no)])

    return InlineKeyboardMarkup(buttons)


async def show_orders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показать кнопки с номерами заказов."""
    if not ORDERS_CACHE:
        await update.message.reply_text("Кэш пуст. Сначала выполните /reload.")
        return
    await update.message.reply_text("Выбери заказ:", reply_markup=_orders_keyboard())


async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    order_no = query.data
    if order_no == "noop":
        await query.edit_message_text("Кэш пуст. Сначала выполните /reload")
        return

    # 1) Берём строковую дату из кэша
    delivery_str = ORDERS_CACHE.get(order_no)
    if not delivery_str:
        await query.edit_message_text(f"📦 Заказ: {order_no}\n⚠️ Дата доставки не найдена")
        return

    # 2) Пробуем распарсить в datetime (поддержим 2 популярных формата)
    delivery_dt = None
    for fmt in ("%d.%m.%Y", "%Y-%m-%d"):
        try:
            delivery_dt = datetime.strptime(delivery_str, fmt)
            break
        except ValueError:
            pass

    if delivery_dt is None:
        await query.edit_message_text(
            f"📦 Заказ: {order_no}\n⚠️ Не удалось распознать дату доставки: {delivery_str}"
        )
        return

    # 3) Считаем минимальную дату розлива под требуемый ОСС
delivery_dt = parse_date(delivery_str)

if delivery_dt is None:
    await query.edit_message_text(
        f"📦 Заказ: {order_no}\n⚠️ Не удалось распознать дату доставки: {delivery_str}"
    )
    return

min_prod = min_production_date_for_osg(delivery_dt)

# 4) Отвечаем
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
    await app.bot.delete_webhook(drop_pending_updates=True)


# -------------------- main -------------------------
def main():
    if not TELEGRAM_BOT_TOKEN or TELEGRAM_BOT_TOKEN.startswith("<"):
        logger.error("TELEGRAM_BOT_TOKEN не задан. Установите значение в коде или через переменную окружения.")
        return

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
    main()
