# -*- coding: utf-8 -*-
"""
OSG Orders Bot — работа по контрагентам
Google Sheets (gspread + сервисный аккаунт)

Структура листа (ORDERS_SHEET_NAME):
- Contractor    — контрагент
- DeliveryDate  — дата доставки (dd.mm.yyyy, yyyy-mm-dd, dd/mm/yyyy, dd.mm.yy)

Логика:
— Ты выбираешь контрагента из списка.
— Бот берёт его дату доставки и считает минимальную дату производства
  так, чтобы к доставке OSG был ≥ TARGET_OSG_PERCENT,
  с учётом SHELF_LIFE_DAYS и SAFETY_BUFFER_DAYS.
"""

import os
import logging
from typing import Dict, List, Optional
from datetime import datetime, timedelta

import gspread

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
ORDERS_SHEET_NAME = os.getenv("ORDERS_SHEET_NAME", "Orders").strip()

# Параметры расчёта
SHELF_LIFE_DAYS = int(os.getenv("SHELF_LIFE_DAYS", "360"))   # срок годности (дней)
TARGET_OSG_PERCENT = int(os.getenv("TARGET_OSG_PERCENT", "80"))  # целевой OSG (%)
SAFETY_BUFFER_DAYS = int(os.getenv("SAFETY_BUFFER_DAYS", "3"))   # буфер (дней)

# Кэш данных: { contractor_name: {"delivery": "дата"} }
CONTRACTORS_CACHE: Dict[str, Dict[str, str]] = {}

# Кнопки под строкой ввода
REPLY_KB = ReplyKeyboardMarkup(
    [["Обновить", "Контрагенты", "Диагностика"]],
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
    Производить не раньше такой даты, чтобы к DeliveryDate
    продукт сохранил OSG ≥ TARGET_OSG_PERCENT.

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


def load_contractors_from_sheet() -> Dict[str, Dict[str, str]]:
    """
    Читает таблицу и возвращает словарь:
    {
        "ООО Ромашка": {"delivery": "21.11.2025"},
        "ИП Иванов":   {"delivery": "22.11.2025"},
    }
    Если один контрагент встречается несколько раз — берётся последняя строка.
    """
    _, ws = _gs_open_worksheet()
    values = ws.get_all_values()
    if not values:
        return {}

    headers = [h.strip().lower() for h in values[0]]

    try:
        idx_contractor = headers.index("contractor")
        idx_date = headers.index("deliverydate")
    except ValueError:
        raise KeyError("В первой строке должны быть колонки 'Contractor' и 'DeliveryDate'.")

    data: Dict[str, Dict[str, str]] = {}

    for row in values[1:]:
        if len(row) <= max(idx_contractor, idx_date):
            continue

        contractor = (row[idx_contractor] or "").strip()
        delivery = (row[idx_date] or "").strip()

        if not contractor:
            continue

        data[contractor] = {
            "delivery": delivery or "—"
        }

    return data


def _contractors_keyboard() -> InlineKeyboardMarkup:
    """Инлайн-клавиатура с контрагентами."""
    if not CONTRACTORS_CACHE:
        return InlineKeyboardMarkup([[InlineKeyboardButton("Пусто", callback_data="noop")]])

    buttons = [
        [InlineKeyboardButton(name, callback_data=name)]
        for name in sorted(CONTRACTORS_CACHE)
    ]
    return InlineKeyboardMarkup(buttons)


# -------------------- ОБРАБОТЧИКИ -------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /start — приветствие и меню.
    """
    text = (
        "Бот расчёта дат производства под OSG по контрагентам.\n\n"
        "Я работаю по кнопкам внизу 👇\n\n"
        "Команды:\n"
        "/reload       — перечитать таблицу и обновить кэш\n"
        "/contractors  — показать список контрагентов\n"
        "/debug        — диагностика Google Sheets\n"
        "/menu         — показать панель кнопок\n\n"
        "Параметры:\n"
        f"• Целевой OSG: ≥ {TARGET_OSG_PERCENT}%\n"
        f"• Срок годности: {SHELF_LIFE_DAYS} дней\n"
        f"• Буфер: {SAFETY_BUFFER_DAYS} дн."
    )
    await update.message.reply_text(text, reply_markup=REPLY_KB)


async def menu_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ручной вызов панели кнопок."""
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


async def reload_contractors(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Перечитать таблицу, собрать кэш контрагентов."""
    try:
        global CONTRACTORS_CACHE
        CONTRACTORS_CACHE = load_contractors_from_sheet()
        await update.message.reply_text(
            f"✅ Загружено {len(CONTRACTORS_CACHE)} контрагентов из Google Sheets.",
            reply_markup=REPLY_KB
        )
    except Exception as e:
        logger.exception("Ошибка при загрузке данных")
        await update.message.reply_text(f"⚠️ Ошибка при загрузке данных: {e}", reply_markup=REPLY_KB)


async def show_contractors(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показать список контрагентов."""
    if not CONTRACTORS_CACHE:
        await update.message.reply_text("Кэш пуст. Сначала нажми «Обновить».", reply_markup=REPLY_KB)
        return

    await update.message.reply_text("Выбери контрагента:", reply_markup=_contractors_keyboard())


async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка нажатия на контрагента (инлайн-кнопка)."""
    query = update.callback_query
    await query.answer()

    contractor = query.data
    if contractor == "noop":
        return

    info = CONTRACTORS_CACHE.get(contractor) or {}
    delivery_str = info.get("delivery", "")
    delivery_dt = parse_date(delivery_str)

    if delivery_dt is None:
        await query.message.reply_text(
            f"🏢 Контрагент: {contractor}\n⚠️ Не удалось распознать дату доставки: {delivery_str}",
            reply_markup=REPLY_KB
        )
        return

    min_prod = min_production_date_for_osg(delivery_dt)

    reply = (
        f"🏢 Контрагент: {contractor}\n"
        f"📅 Дата доставки: {delivery_dt.strftime('%d.%m.%Y')}\n"
        f"💧 Требуемый OSG: ≥ {TARGET_OSG_PERCENT}%\n"
        f"🏭 Производство — *не раньше*: {min_prod.strftime('%d.%m.%Y')}\n"
        f"📊 Параметры: СГ={SHELF_LIFE_DAYS} дней, буфер={SAFETY_BUFFER_DAYS} дн."
    )

    await query.message.reply_text(reply, reply_markup=REPLY_KB)


async def on_any_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Любой текст: либо обрабатываем как одну из кнопок,
    либо говорим, что бот работает по кнопкам.
    """
    txt = (update.message.text or "").strip()

    if txt == "Обновить":
        await reload_contractors(update, context)
    elif txt == "Контрагенты":
        await show_contractors(update, context)
    elif txt == "Диагностика":
        await debug(update, context)
    else:
        await update.message.reply_text(
            "Я работаю по кнопкам внизу 👇\n"
            "Пожалуйста, используй «Обновить», «Контрагенты» или «Диагностика».",
            reply_markup=REPLY_KB
        )


# --- очистка webhook перед стартом, чтобы не мешал polling ---
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
    app.add_handler(CommandHandler("reload", reload_contractors))
    app.add_handler(CommandHandler("contractors", show_contractors))
    # на всякий случай старая команда /orders ведёт туда же
    app.add_handler(CommandHandler("orders", show_contractors))

    # Любой текст
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_any_text))

    # Инлайн-кнопки
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
