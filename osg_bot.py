
import os
import re
import math
import logging
from datetime import datetime, timedelta

# === Логи ===
logging.basicConfig(level=logging.INFO)

# === Telegram ===
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

# === Настройки расчёта ===
SHELF_LIFE_DAYS = 360          # срок годности, дней
TARGET_OSG_PERCENT = 82        # целевой ОСГ, %
SAFETY_BUFFER_DAYS = 2         # дополнительный запас, дней

# === Часовые пояса ===
# Сборка — Аша/Челябинск (UTC+5), Доставка — Москва (UTC+3)
try:
    from zoneinfo import ZoneInfo
    TZ_PICK = ZoneInfo("Asia/Yekaterinburg")  # Аша/Челябинск
    TZ_DELIV = ZoneInfo("Europe/Moscow")      # Москва
except Exception:
    # fallback если ZoneInfo недоступен (на Windows поставь пакет tzdata)
    from datetime import timezone, timedelta
    TZ_PICK  = timezone(timedelta(hours=5))
    TZ_DELIV = timezone(timedelta(hours=3))

HELP_TEXT = (
    "👋 Введи дату сборки (Аша, UTC+5), чтобы проверить допустимую дату производства.\n"
    "Примеры: 2025-11-10, 10.11.2025, завтра, в пн, через 3 дня.\n\n"
    "Правило недели: Чт–Вс → доставка в ближайший понедельник (Москва, UTC+3). "
    "Пн–Ср → доставка в эту же дату (Москва).\n"
    f"Параметры: СГ={SHELF_LIFE_DAYS} дн, ОСГ≥{TARGET_OSG_PERCENT}%, запас {SAFETY_BUFFER_DAYS} дн."
)

# ---------- Вспомогательные функции ----------

def parse_human_date(s: str, now_dt: datetime) -> datetime | None:
    """
    Парсит человеческие форматы: сегодня/завтра/послезавтра,
    'в пн/вт/…', 'через N дней', стандартные даты.
    Возвращает datetime с tz now_dt.tzinfo.
    """
    s = (s or "").strip().lower()
    if not s:
        return None

    if s in ("сегодня", "today"):
        return now_dt
    if s in ("завтра", "tomorrow"):
        return now_dt + timedelta(days=1)
    if s in ("послезавтра",):
        return now_dt + timedelta(days=2)

    m = re.match(r"через\s+(\d+)\s*(дн|дня|дней)?", s)
    if m:
        return now_dt + timedelta(days=int(m.group(1)))

    weekdays = {"пн":0, "вт":1, "ср":2, "чт":3, "пт":4, "сб":5, "вс":6}
    m = re.match(r"в\s*(пн|вт|ср|чт|пт|сб|вс)$", s)
    if m:
        target = weekdays[m.group(1)]
        delta = (target - now_dt.weekday()) % 7
        if delta == 0:
            delta = 7
        return now_dt + timedelta(days=delta)

    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y"):
        try:
            # подставим TZ от now_dt
            dt = datetime.strptime(s, fmt)
            return dt.replace(tzinfo=now_dt.tzinfo)
        except ValueError:
            pass

    return None

def parse_human_date_local(s: str, now_pick: datetime) -> datetime | None:
    return parse_human_date(s, now_pick)

def resolve_delivery_date_from_pick(input_date_pick: datetime) -> datetime:
    """
    input_date_pick — дата сборки (Аша, TZ_PICK).
    Если это Чт–Вс → доставка ближайший Пн (Москва).
    Если Пн–Ср → доставка в эту дату (Москва).
    Возвращаем datetime (Москва) с временем 12:00.
    """
    wd = input_date_pick.weekday()  # Mon=0..Sun=6
    if wd >= 3:  # Thu..Sun
        delta = 7 - wd if wd != 6 else 1
        delivery_local = input_date_pick + timedelta(days=delta)
    else:
        delivery_local = input_date_pick

    d = delivery_local.date()
    delivery_msk = datetime(d.year, d.month, d.day, 12, 0, 0, tzinfo=TZ_DELIV)
    return delivery_msk

def min_prod_date(delivery: datetime,
                  shelf=SHELF_LIFE_DAYS,
                  target=TARGET_OSG_PERCENT,
                  safety=SAFETY_BUFFER_DAYS):
    """
    ОСГ >= target%  => прошедшие дни <= shelf * (1 - target%)
    Берем потолок и уменьшаем на safety для запаса (требуем свежее).
    """
    max_elapsed = shelf * (1 - target/100.0)     # 360*(1-0.82)=64.8
    allowed_age = max(0, math.ceil(max_elapsed) - safety)  # 65 -> 63
    return (delivery - timedelta(days=allowed_age)).date()

# ---------- Telegram handlers ----------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Бот расчёта дат производства для ОСГ.\n" + HELP_TEXT)

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP_TEXT)

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        text = (update.message.text or "").strip()
        print(f"[DEBUG] Получен текст: {text!r}")

        # просим отправлять одну дату за раз
        if any(sep in text for sep in [",", ";", "\n"]):
            await update.message.reply_text(
                "Пожалуйста, отправляй одну дату за раз. Примеры: 10.11.2025, «в пн», «через 3 дня»."
            )
            return

        now_pick = datetime.now(TZ_PICK)
        dt_pick = parse_human_date_local(text, now_pick)
        if not dt_pick:
            await update.message.reply_text(
                "Не распознала дату 🤔\nПримеры: 2025-11-10, 10.11.2025, «в пн», «через 3 дня»."
            )
            return

        delivery = resolve_delivery_date_from_pick(dt_pick)
        min_prod = min_prod_date(delivery)

        reply = (
            f"📦 Сборка (Аша, UTC+5): *{dt_pick.strftime('%d.%m.%Y (%a)')}*\n"
            f"🚚 Доставка (Москва, UTC+3): *{delivery.strftime('%d.%m.%Y (%a)')}*\n"
            f"🧾 Производство — *не раньше {min_prod.strftime('%d.%m.%Y')}* "
            f"(ОСГ ≥ {TARGET_OSG_PERCENT}% + {SAFETY_BUFFER_DAYS} дн)"
        )
        await update.message.reply_text(reply, parse_mode="Markdown")

    except Exception as e:
        logging.exception("Критическая ошибка в handle_text", exc_info=e)
        await update.message.reply_text("Ой, что-то пошло не так. Я записал ошибку в лог.")

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logging.exception("Ошибка в обработчике", exc_info=context.error)

# ---------- main ----------

def main():
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        print("❌ Токен не найден в переменных окружения")
        return

    app = Application.builder().token(token).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_error_handler(error_handler)

    print("✅ Бот запущен. Ожидание сообщений...")
    app.run_polling()

if __name__ == "__main__":
    main()