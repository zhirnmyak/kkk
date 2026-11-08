"""Настройки проекта — всё через переменные окружения."""
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# Системный socks4-прокси роняет httpx (openai, huggingface_hub).
# Прокси задаём явно (TG_PROXY, VISION_PROXY), системные переменные вычищаем.
# Чистим ПОСЛЕ load_dotenv: иначе прокси из .env вернулся бы обратно.
for _v in ("ALL_PROXY", "all_proxy", "HTTP_PROXY", "http_proxy",
           "HTTPS_PROXY", "https_proxy"):
    os.environ.pop(_v, None)

os.environ["NO_PROXY"] = "*"
os.environ["no_proxy"] = "*"

BASE_DIR = Path(__file__).resolve().parent


def _int(name: str, default: int) -> int:
    """Число из окружения. Пустая строка или опечатка в .env не роняют импорт
    (раньше TG_POLL_TIMEOUT= без значения давал ValueError на старте)."""
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


OCR_ENABLED = os.getenv("OCR_ENABLED", "1") == "1"
# если выбранный LLM-OCR упёрся в дневной лимит — дочитать локальным EasyOCR
OCR_FALLBACK_EASYOCR = os.getenv("OCR_FALLBACK_EASYOCR", "1") == "1"
# сколько страниц PDF распознавать одновременно; 0 = все страницы сразу
OCR_WORKERS = _int("OCR_WORKERS", 3)
# --- Max Bot API ---
def _read_max_token() -> str:
    """Токен берём из отдельного файла (по умолчанию max_token.txt), а не из .env.
    Переменная MAX_BOT_TOKEN остаётся запасным вариантом для CI/тестов."""
    token = os.getenv("MAX_BOT_TOKEN", "").strip()
    if token:
        return token
    path = Path(os.getenv("MAX_TOKEN_FILE", BASE_DIR / "max_token.txt"))
    if not path.is_absolute():
        path = BASE_DIR / path
    try:
        raw = path.read_text(encoding="utf-8-sig").strip()
    except OSError:
        return ""
    if raw.lower().startswith("max_bot_token="):
        raw = raw.split("=", 1)[1]
    return raw.strip().strip('"').strip("'").strip()


MAX_TOKEN = _read_max_token()
MAX_API = os.getenv("MAX_API", "https://platform-api2.max.ru").rstrip("/")
# platform-api2.max.ru подписан НУЦ Минцифры: certifi его не знает, поэтому
# официальные корневой и промежуточный сертификаты лежат в certs/ и подмешиваются
# к стандартному набору (см. bot/max_client.py).
MAX_CA_DIR = Path(os.getenv("MAX_CA_DIR") or BASE_DIR / "certs")
if not MAX_CA_DIR.is_absolute():
    MAX_CA_DIR = BASE_DIR / MAX_CA_DIR
MAX_BOT_ENABLED = os.getenv("MAX_BOT_ENABLED", "1") == "1"
# Запасной вариант показа размышлений, если бот запускается без консоли.
# При обычном запуске бот читает «Показ» из /настройки (MAXIMKA_DISPLAY).
MAX_BOT_REASONING = os.getenv("MAX_BOT_REASONING", "show").strip().lower() != "hide"
MAX_POLL_TIMEOUT = _int("MAX_POLL_TIMEOUT", 30)
MAX_DOWNLOAD_MB = _int("MAX_DOWNLOAD_MB", 50)
# «Пользователь консоли» не должен получать напоминания в мессенджер
CONSOLE_UID = _int("MAXIMKA_CONSOLE_UID", 1)

# --- Telegram Bot API: второй транспорт, для отладки пока нет Max-токена ---
TG_TOKEN = os.getenv("TG_BOT_TOKEN", "")
TG_API = os.getenv("TG_API", "https://api.telegram.org")
# api.telegram.org может не открываться без прокси (см. начало файла — системные
# переменные вычищены), поэтому прокси задаём явно, как для VISION:
#   TG_PROXY=http://127.0.0.1:10809   (или socks5://127.0.0.1:1080)
TG_PROXY = os.getenv("TG_PROXY", "")
# Секунд ожидания в одном getUpdates. Каждый холд = 1 запрос к воркеру-ретранслятору,
# а бесплатный тариф Cloudflare Workers — 100 тыс. запросов/месяц (при 30 с ≈ 86 тыс.).
TG_POLL_TIMEOUT = _int("TG_POLL_TIMEOUT", 30)
# Скачивание вложений идёт через тот же путь, что и API, но тело файла в разы
# больше JSON-ответа, а сервер файлов Telegram отдаёт его неспешно.
TG_FILE_TIMEOUT = _int("TG_FILE_TIMEOUT", 240)

# где работать сейчас: max | tg
MESSENGER = os.getenv("MESSENGER", "max").strip().lower()

# --- Приём апдейтов в main.py ---
# polling — long polling (по умолчанию, публичный URL не нужен);
# webhook — только POST /webhook. MAX при активной подписке polling не отдаёт,
# поэтому режимы взаимоисключающие.
UPDATES_MODE = os.getenv("UPDATES_MODE", "polling").strip().lower()
# Секрет вебхука: MAX присылает его в X-Max-Bot-Api-Secret (поле secret в
# POST /subscriptions), Telegram — в X-Telegram-Bot-Api-Secret-Token (secret_token
# в setWebhook). Без секрета /webhook отклоняет все запросы.
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip()
# FastAPI слушает только localhost; наружу — через обратный прокси с HTTPS
HTTP_HOST = os.getenv("HTTP_HOST", "127.0.0.1").strip() or "127.0.0.1"
HTTP_PORT = _int("HTTP_PORT", 8000)
# Имя бота с подключённым мини-приложением в MAX (без @).
# Пустое значение скрывает кнопку, пока приложение не подключено в кабинете MAX.
MAX_WEBAPP_BOT = os.getenv("MAX_WEBAPP_BOT", "").strip().lstrip("@")
MAX_INIT_DATA_TTL = max(60, _int("MAX_INIT_DATA_TTL", 3600))
# Только явный локальный запуск; в этом режиме HTTP принудительно слушает loopback.
LOCAL_MINIAPP = os.getenv("MAX_LOCAL_MINIAPP", "0") == "1"


# --- LLM (OpenAI-совместимый) ---
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "openai")
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://api.openai.com/v1")
LLM_API_KEY = os.getenv("LLM_API_KEY", "")
LLM_MODEL = os.getenv("LLM_MODEL", "gpt-4o-mini")

TRANSCRIBE_BACKEND = os.getenv("TRANSCRIBE_BACKEND", "easyocr")   # easyocr | vision | llm

# --- Генерация картинок ---
# По умолчанию выключена: flux-1-schnell теряет субъект сцены («мальчик гладит
# собаку» → два пса) и не рисует читаемый текст. Включить: IMAGE_ENABLED=1
IMAGE_ENABLED = os.getenv("IMAGE_ENABLED", "0") == "1"
# пусто = авто: cloudflare, если LLM_BASE_URL указывает на Workers AI, иначе gateway
IMAGE_BACKEND = os.getenv("IMAGE_BACKEND", "")
IMAGE_MODEL = os.getenv("IMAGE_MODEL", "@cf/black-forest-labs/flux-1-schnell")
IMAGE_STEPS = _int("IMAGE_STEPS", 6)   # 4 заметно хуже: модель не дорисовывает сцену
IMAGE_TIMEOUT = _int("IMAGE_TIMEOUT", 90)

# Vision-провайдер (наследует LLM_*, если строки не заданы или пустые:
# «VISION_BASE_URL=» в .env раньше давал пустой адрес вместо LLM_BASE_URL)
VISION_BASE_URL = os.getenv("VISION_BASE_URL") or LLM_BASE_URL
VISION_API_KEY  = os.getenv("VISION_API_KEY") or LLM_API_KEY
VISION_MODEL    = os.getenv("VISION_MODEL") or LLM_MODEL
VISION_PROXY    = os.getenv("VISION_PROXY", "")   # напр. http://127.0.0.1:10809

# --- Эмбеддинги (для e5-моделей нужны префиксы query:/passage:) ---
EMBED_MODEL = os.getenv("EMBED_MODEL", "intfloat/multilingual-e5-small")

# --- RAG ---
CHUNK_SIZE = 900
CHUNK_OVERLAP = 150
TOP_K = 4

# --- Интервальное повторение (Лейтнер), дни по уровням ---
LEITNER_INTERVALS = [1, 2, 4, 8, 16]
REMINDER_HOUR = 19
# --- Режимы отладки (1 = без внешних сервисов) ---
LLM_MOCK = os.getenv("LLM_MOCK", "0") == "1"      # фейковый LLM, ключ не нужен
EMBED_MOCK = os.getenv("EMBED_MOCK", "0") == "1"  # фейковые эмбеддинги, без скачивания модели

# --- Пути ---
# Путь к данным можно переопределять (пригодится для тестов). Относительный путь
# считаем от папки проекта, а не от текущего каталога: запуск из другой папки
# (PyCharm, bot/) раньше молча заводил вторую пустую базу (отсюда bot/data/).
DATA_DIR = Path(os.getenv("DATA_DIR") or "data")
if not DATA_DIR.is_absolute():
    DATA_DIR = BASE_DIR / DATA_DIR
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "tutor.db"
CHROMA_DIR = DATA_DIR / "chroma"
