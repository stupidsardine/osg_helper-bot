# diag.py — быстрая проверка доступа сервисного аккаунта к Google Sheets
import os, json, gspread

print("== Диагностика Google Sheets ==")

# 1) Путь к ключам сервисного аккаунта (gsa.json)
creds_path = os.getenv(
    "GOOGLE_APPLICATION_CREDENTIALS",
    r"C:\Users\Алексей\Desktop\osg-helper-bot\gsa.json"  # ← поправь путь, если у тебя другой
)
print("GOOGLE_APPLICATION_CREDENTIALS:", creds_path)

with open(creds_path, "r", encoding="utf-8") as f:
    sa = json.load(f)
sa_email = sa.get("client_email")
print("Service account email:", sa_email)

# 2) ID таблицы
sheet_id = os.getenv("GOOGLE_SHEET_ID") or "ПОДСТАВЬ_СВОЙ_ID_ТАБЛИЦЫ"
print("GOOGLE_SHEET_ID:", sheet_id)

# 3) Пробуем авторизоваться и открыть таблицу
gc = gspread.service_account(filename=creds_path)
try:
    sh = gc.open_by_key(sheet_id)
    print("✅ Таблица доступна:", sh.title)
    print("Листы:", [ws.title for ws in sh.worksheets()])
except gspread.SpreadsheetNotFound as e:
    print("❌ SpreadsheetNotFound: таблица не найдена или нет прав.")
    print("Проверь:")
    print("  1) sheet_id — ровно тот кусок из URL между /d/ и /edit")
    print("  2) Таблица расшарена на:", sa_email, "(роль: Редактор)")
    print("  3) В проекте включены Google Sheets API и Google Drive API")
    raise
