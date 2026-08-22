import os
import sys
from pathlib import Path
from dotenv import load_dotenv

# Определяем путь к директории проекта и загружаем .env
BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"

if ENV_PATH.exists():
    load_dotenv(dotenv_path=ENV_PATH)
else:
    load_dotenv()

# Чтение и валидация токена бота
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

if not BOT_TOKEN or BOT_TOKEN == "1234567890:ABCdefGHIjklMNOpqrsTUVwxyz_EXAMPLE":
    print("❌ ОШИБКА КИБЕРБЕЗОПАСНОСТИ: Не задан корректный BOT_TOKEN в файле .env!")
    print("Заполните BOT_TOKEN в файле .env реальным токеном от @BotFather.")
    sys.exit(1)

# Настройка Rate Limiter (по умолчанию 3 секунды между запросами от одного пользователя)
try:
    RATE_LIMIT_SECONDS = float(os.getenv("RATE_LIMIT_SECONDS", "3.0"))
except ValueError:
    RATE_LIMIT_SECONDS = 3.0