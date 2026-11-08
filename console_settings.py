"""Экран настроек консоли МаксИИмки: модель, effort, контекст, OCR, размышления, спиннер.

↑/↓ — строка, ←/→ — значение, Enter/пробел — переключить, Esc — сохранить и выйти.
Клик мышью (Windows Terminal): левая половина строки — назад/выключить,
правая — вперёд/включить. Настройки лежат в data/settings.json.
"""
import json
import os
import re
import sys
import time
import threading
import tempfile
from pathlib import Path

import config
import reasoning_support
import vision_support
import model_checks

APP_ROOT = Path(__file__).resolve().parent
SETTINGS_PATH = APP_ROOT / "data" / "settings.json"
MODEL_HINT_PATH = APP_ROOT / "data" / "model.txt"
SETTINGS_LOCK = threading.RLock()

EFFORTS = ("auto", "none", "minimal", "low", "medium", "high", "xhigh", "max", "ultracode")
CONTEXTS = (0, 100_000, 200_000, 400_000, 1_000_000)
SPINNERS = ("auto", "on", "off")
OCR_BACKENDS = ("easyocr", "llm")
OCR_WORKERS_VALUES = tuple(range(1, 11)) + (0,)  # 0 = все страницы сразу
GW_PARALLEL_VALUES = tuple(range(1, 11))
GW_TIMEOUT_VALUES = (60, 120, 180, 300, 600)
GW_WINDOW_VALUES = ("hidden", "window")
DISPLAY_MODES = ("statuses", "reasoning")  # только статусы | размышления + зелёные статусы
FALLBACK_MODELS = ("DeepSeek-Flash", "Qwen3.8-Flash")

ROWS = (
    ("provider", "Подключение", "cycle"),
    ("base_url", "Адрес API (/v1)", "text"),
    ("api_key", "API-ключ", "text"),
    ("load_models", "Загрузить модели", "action"),
    ("model", "Модель", "cycle"),
    ("test_connection", "Тест подключения", "action"),
    ("effort", "Размышления (effort)", "cycle"),
    ("context", "Контекст", "cycle"),
    ("gw_parallel", "Параллельно шлюза", "cycle"),
    ("gw_timeout", "Таймаут шлюза", "cycle"),
    ("gw_window", "Окно шлюза", "cycle"),
    ("ocr_backend", "Движок OCR", "cycle"),
    ("ocr_workers", "Потоки OCR", "cycle"),
    ("ocr", "OCR сканов и рукописи", "toggle"),
    ("display_mode", "Показ", "cycle"),
    ("spinner", "Спиннер «думаю»", "cycle"),
    ("test_vision", "Проверить Vision для OCR", "action"),
    ("test_capabilities", "Перепроверить модель", "action"),
)

RESET = "\x1b[0m"
DIM = "\x1b[2m"
BOLD = "\x1b[1m"
CYAN = "\x1b[36m"
GREEN = "\x1b[42m\x1b[30m"
GREY = "\x1b[100m\x1b[97m"


def _env_int(name: str, default: int = 0) -> int:
    raw = (os.getenv(name) or "").strip()
    return int(raw) if raw.isdigit() else default


def load() -> dict:
    remote = config.LLM_PROVIDER != "qoder"
    settings = {
        "provider": config.LLM_PROVIDER,
        "openai_base_url": config.LLM_BASE_URL if remote else "https://api.openai.com/v1",
        "openai_api_key": config.LLM_API_KEY if remote else "",
        "openai_model": config.LLM_MODEL if remote else "gpt-4o-mini",
        "qoder_base_url": config.LLM_BASE_URL if not remote else "http://127.0.0.1:8787/v1",
        "qoder_api_key": config.LLM_API_KEY if not remote else "change-me",
        "qoder_model": config.LLM_MODEL if not remote else "Qwen3.8-Flash",
        "model": os.getenv("LLM_MODEL") or config.LLM_MODEL,
        "effort": (os.getenv("LLM_REASONING_EFFORT") or "auto").strip().lower(),
        "context": _env_int("LLM_CONTEXT_WINDOW", 0),
        "gw_parallel": _env_int("GATEWAY_MAX_PARALLEL", 3),
        "gw_timeout": max(1, _env_int("GATEWAY_TIMEOUT_MS", 180000) // 1000),
        "gw_window": (os.getenv("GATEWAY_WINDOW") or "hidden").strip().lower(),
        "ocr_backend": (os.getenv("TRANSCRIBE_BACKEND") or config.TRANSCRIBE_BACKEND or "easyocr").strip().lower(),
        "ocr_workers": _env_int("OCR_WORKERS", 3),
        "ocr": (os.getenv("OCR_ENABLED") or "1").strip() == "1",
        "display_mode": (os.getenv("MAXIMKA_DISPLAY") or "statuses").strip().lower(),
        "spinner": (os.getenv("MAXIMKA_SPINNER") or "auto").strip().lower(),
        "ui_version": 3,
    }
    if SETTINGS_PATH.exists():
        try:
            saved = json.loads(SETTINGS_PATH.read_text("utf-8"))
        except (OSError, ValueError):
            saved = {}
        if isinstance(saved, dict):
            for key in settings:
                if key in saved and saved[key] is not None:
                    settings[key] = saved[key]
            if "provider" not in saved and not remote:
                settings["qoder_model"] = saved.get("model") or settings["qoder_model"]
                settings["effort"] = "auto"
                settings["context"] = 0
    if settings["effort"] not in EFFORTS:
        settings["effort"] = "auto"
    if settings["context"] not in CONTEXTS:
        settings["context"] = 0
    if settings["spinner"] not in SPINNERS:
        settings["spinner"] = "auto"
    if settings["ocr_backend"] not in OCR_BACKENDS:
        settings["ocr_backend"] = "easyocr"
    if settings["ocr_workers"] not in OCR_WORKERS_VALUES:
        settings["ocr_workers"] = 3
    if settings["gw_parallel"] not in GW_PARALLEL_VALUES:
        settings["gw_parallel"] = 3
    if settings["gw_timeout"] not in GW_TIMEOUT_VALUES:
        settings["gw_timeout"] = 180
    if settings["gw_window"] not in GW_WINDOW_VALUES:
        settings["gw_window"] = "hidden"
    if settings["display_mode"] not in DISPLAY_MODES:
        settings["display_mode"] = "statuses"
    if os.getenv("MAXIMKA_QODER_EXPLICIT") == "1":
        settings["provider"] = "qoder"
    if settings["provider"] not in ("openai", "qoder"):
        settings["provider"] = "openai"
    profile = settings["provider"]
    settings["base_url"] = settings[profile + "_base_url"]
    settings["api_key"] = settings[profile + "_api_key"]
    settings["model"] = settings[profile + "_model"]
    if profile == "qoder" and os.getenv("MAXIMKA_MODEL_EXPLICIT") == "1":
        settings["model"] = os.getenv("LLM_MODEL") or settings["model"]
    return settings


def apply(settings: dict) -> None:
    _normalize_effort(settings)
    _normalize_ocr(settings)
    profile = settings["provider"]
    for key in ("base_url", "api_key", "model"):
        settings[profile + "_" + key] = settings[key]
    config.LLM_PROVIDER = profile
    os.environ["LLM_PROVIDER"] = profile
    config.LLM_BASE_URL = settings["base_url"]
    config.LLM_API_KEY = settings["api_key"]
    os.environ["LLM_BASE_URL"] = config.LLM_BASE_URL
    os.environ["LLM_API_KEY"] = config.LLM_API_KEY
    # Пустые VISION_* наследуют текущее подключение.
    config.VISION_BASE_URL = os.getenv("VISION_BASE_URL") or config.LLM_BASE_URL
    config.VISION_API_KEY = os.getenv("VISION_API_KEY") or config.LLM_API_KEY
    config.VISION_MODEL = os.getenv("VISION_MODEL") or settings["model"]
    config.LLM_MODEL = settings["model"]
    os.environ["LLM_MODEL"] = settings["model"]
    os.environ["LLM_REASONING_EFFORT"] = settings["effort"]
    if settings["context"]:
        os.environ["LLM_CONTEXT_WINDOW"] = str(settings["context"])
    else:
        os.environ.pop("LLM_CONTEXT_WINDOW", None)
    config.OCR_ENABLED = bool(settings["ocr"])
    os.environ["OCR_ENABLED"] = "1" if settings["ocr"] else "0"
    config.TRANSCRIBE_BACKEND = settings["ocr_backend"]
    os.environ["TRANSCRIBE_BACKEND"] = settings["ocr_backend"]
    config.OCR_WORKERS = settings["ocr_workers"]
    os.environ["OCR_WORKERS"] = str(settings["ocr_workers"])
    os.environ["GATEWAY_MAX_PARALLEL"] = str(settings["gw_parallel"])
    os.environ["GATEWAY_TIMEOUT_MS"] = str(settings["gw_timeout"] * 1000)
    os.environ["GATEWAY_WINDOW"] = settings["gw_window"]
    reasoning_on = settings["display_mode"] == "reasoning"
    os.environ["MAXIMKA_REASONING"] = "show" if reasoning_on else "hide"
    os.environ["MAXIMKA_AGENT_STATUS"] = "1"
    os.environ["MAXIMKA_DISPLAY"] = settings["display_mode"]
    os.environ["MAXIMKA_SPINNER"] = {"auto": "auto", "on": "1", "off": "0"}[settings["spinner"]]


def save(settings: dict) -> None:
    with SETTINGS_LOCK:
        SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps(settings, ensure_ascii=False, indent=2) + "\n"
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=SETTINGS_PATH.parent,
                                         prefix=".settings-", suffix=".tmp", delete=False) as file:
            temporary = Path(file.name)
            file.write(text)
        try:
            temporary.replace(SETTINGS_PATH)
        finally:
            temporary.unlink(missing_ok=True)
        # start-terminal.cmd читает эту подсказку модели.
        try:
            MODEL_HINT_PATH.write_text(settings["model"] + "\r\n", "utf-8")
        except OSError:
            # Подсказка необязательна; основной профиль уже сохранён.
            pass


def _api_request(settings: dict, path: str, payload=None) -> dict:
    model_checks.check_cancelled()
    import httpx
    headers = {"Authorization": f"Bearer {settings['api_key']}"} if settings["api_key"] else {}
    try:
        with httpx.Client(timeout=30 if payload is not None else 15, trust_env=False) as client:
            url = settings["base_url"].rstrip("/") + path
            response = (client.get(url, headers=headers) if payload is None else
                        client.post(url, headers=headers, json=payload))
            response.raise_for_status()
            model_checks.check_cancelled()
            data = response.json()
            if not isinstance(data, dict):
                raise ValueError()
            return data
    except httpx.HTTPStatusError as exc:
        code = exc.response.status_code
        hint = {401: "проверь API-ключ", 403: "нет доступа", 404: "проверь адрес API и модель; эндпоинт может не поддерживаться", 429: "лимит запросов или баланса"}.get(code, "ошибка провайдера")
        raise RuntimeError(f"HTTP {code}: {hint}") from exc
    except httpx.TimeoutException:
        raise RuntimeError("Таймаут: сервер не ответил вовремя") from None
    except httpx.RequestError:
        raise RuntimeError("Не удалось подключиться: проверь адрес API и сеть") from None
    except ValueError:
        raise RuntimeError("Сервер вернул некорректный JSON") from None


def fetch_models(settings: dict | None = None) -> list[str]:
    settings = settings or {"base_url": config.LLM_BASE_URL, "api_key": config.LLM_API_KEY}
    data = _api_request(settings, "/models")
    items = data.get("data")
    if not isinstance(items, list):
        raise RuntimeError("В ответе /models отсутствует список data")
    names = list(dict.fromkeys(item["id"] for item in items
                              if isinstance(item, dict) and isinstance(item.get("id"), str) and item["id"].strip()))
    if not names:
        raise RuntimeError("Эндпоинт вернул пустой список моделей")
    for item in items:
        if not isinstance(item, dict) or item.get("id") not in names:
            continue
        vision_support.read_metadata(settings["base_url"], settings["api_key"], item)
        params = item.get("supported_parameters")
        levels = item.get("reasoning_effort")
        if isinstance(levels, dict):
            levels = levels.get("enum")
        if isinstance(params, dict):
            field = params.get("reasoning_effort")
            if isinstance(field, dict):
                levels = field.get("enum", levels)
        levels = tuple(x for x in levels if x in EFFORTS and x != "auto") if isinstance(levels, list) else ()
        if levels:
            reasoning_support.remember(settings["base_url"], settings["api_key"], item["id"],
                                       True, levels, "metadata")
        elif isinstance(params, list) and "reasoning_effort" not in params and "reasoning" not in params:
            reasoning_support.remember(settings["base_url"], settings["api_key"], item["id"],
                                       False, source="metadata")
    return names


def _support(settings):
    return reasoning_support.get(settings["base_url"], settings["api_key"], settings["model"])


def _efforts(settings):
    info = _support(settings)
    if not info or info["supported"] is None:
        return ("auto",)
    if info["supported"] is False:
        return ("auto",)
    return tuple(dict.fromkeys(("auto", *info["efforts"])))


def _normalize_effort(settings):
    if _support(settings) and settings.get("effort") not in _efforts(settings):
        settings["effort"] = "auto"


def detect_reasoning(settings, force=False):
    """Без метаданных проверяем только low, не выдумываем остальные уровни."""
    if _support(settings) is not None and not force:
        _normalize_effort(settings)
        return
    import httpx
    try:
        data = _api_request(settings, "/chat/completions", {
            "model": settings["model"],
            "messages": [{"role": "user", "content": "Reply with OK."}],
            "max_tokens": 16, "stream": False, "reasoning_effort": "low",
        })
        if not isinstance(data.get("choices"), list) or not data["choices"]:
            if force:
                reasoning_support.remember(settings["base_url"], settings["api_key"], settings["model"], None)
            return
        reasoning_support.remember(settings["base_url"], settings["api_key"],
                                   settings["model"], True, ("low",))
    except RuntimeError as exc:
        cause = exc.__cause__
        if (isinstance(cause, httpx.HTTPStatusError) and cause.response.status_code == 400
                and reasoning_support.rejected(cause.response.text)):
            reasoning_support.remember(settings["base_url"], settings["api_key"],
                                       settings["model"], False)
        else:
            if force:
                reasoning_support.remember(settings["base_url"], settings["api_key"], settings["model"], None)
            return str(exc)
    _normalize_effort(settings)


def test_connection(settings: dict) -> str:
    if not settings["model"].strip():
        raise RuntimeError("Сначала выбери или введи модель")
    started = time.monotonic()
    data = _api_request(settings, "/chat/completions", {
        "model": settings["model"], "messages": [{"role": "user", "content": "Reply with OK."}],
        "max_tokens": 16, "stream": False,
    })
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict) or not isinstance(choices[0].get("message"), dict):
        raise RuntimeError("Ответ не соответствует chat/completions: нет choices/message")
    return f"Тест пройден: модель ответила за {time.monotonic() - started:.1f} с"


def _vision(settings):
    return vision_support.get(settings["base_url"], settings["api_key"], settings["model"])


def _normalize_ocr(settings):
    info = _vision(settings)
    if info and info["supported"] is False and settings.get("ocr_backend") == "llm":
        settings["ocr_backend"] = "easyocr"


def detect_vision(settings, force=False):
    if _vision(settings) is not None and not force:
        _normalize_ocr(settings)
        return
    import httpx
    code, image_url = vision_support.probe_image()
    try:
        data = _api_request(settings, "/chat/completions", {
            "model": settings["model"], "max_tokens": 32, "stream": False,
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": "Read the code in this image. Reply with the code only."},
                {"type": "image_url", "image_url": {"url": image_url}},
            ]}],
        })
        choices = data.get("choices")
        text = ((choices[0].get("message") or {}).get("content")
                if isinstance(choices, list) and choices and isinstance(choices[0], dict) else None)
        if isinstance(text, str) and code in text.upper():
            vision_support.remember(settings["base_url"], settings["api_key"], settings["model"], True)
        else:
            vision_support.remember(settings["base_url"], settings["api_key"], settings["model"], None)
    except RuntimeError as exc:
        cause = exc.__cause__
        if (isinstance(cause, httpx.HTTPStatusError) and cause.response.status_code in (400, 422)
                and vision_support.rejected(cause.response.text)):
            vision_support.remember(settings["base_url"], settings["api_key"], settings["model"], False)
        else:
            vision_support.remember(settings["base_url"], settings["api_key"], settings["model"], None)
            return str(exc)
    _normalize_ocr(settings)


def _vision_label(settings):
    info = _vision(settings)
    if not info or info["supported"] is None:
        return "не проверено"
    if info["supported"] is False:
        return "не поддерживается"
    return "по данным API" if info["source"] == "metadata" else "чтение проверено"


def _models_with(current: str) -> list[str]:
    try:
        names = fetch_models()
    except RuntimeError:
        names = []
    if current and current not in names:
        names.insert(0, current)
    return names


def _context_label(value: int) -> str:
    return f"{value:,}".replace(",", " ") if value else "без ограничения"


def _value(settings: dict, key: str, _models: list[str]) -> str:
    if key == "provider":
        return "OpenAI-совместимый API" if settings[key] == "openai" else "Qoder (локальный шлюз)"
    if key == "base_url":
        return settings[key]
    if key == "api_key":
        return "********" if settings[key] else "не задан"
    if key == "model":
        return settings["model"]
    if key == "effort":
        info = _support(settings)
        if not info or info["supported"] is None:
            return "auto · поддержка не определена"
        if info["supported"] is False:
            return "не поддерживается"
        suffix = "уровни из API" if info["source"] == "metadata" else "API принял low"
        return f"{settings['effort']} · {suffix}"
    if key == "context":
        return _context_label(settings["context"])
    if key == "ocr_backend":
        if settings["ocr_backend"] == "easyocr":
            return "EasyOCR (локально)"
        return f"Vision · {settings['model']} · {_vision_label(settings)}"
    if key == "ocr_workers":
        return "все" if settings["ocr_workers"] == 0 else str(settings["ocr_workers"])
    if key == "gw_parallel":
        return str(settings["gw_parallel"])
    if key == "gw_timeout":
        return f"{settings['gw_timeout']} с"
    if key == "gw_window":
        return {"hidden": "скрыто", "window": "отдельное"}[settings["gw_window"]]
    if key == "display_mode":
        return {"statuses": "только статусы", "reasoning": "размышления + статусы"}[settings["display_mode"]]
    return {"auto": "авто", "on": "вкл", "off": "выкл"}[settings["spinner"]]


def render(settings: dict, selected: int, models: list[str], status: str = "") -> str:
    lines = [
        BOLD + "НАСТРОЙКИ МаксИИмки" + RESET,
        DIM + "↑/↓ строка · ←/→ выбор · Enter ввод · C отменить проверку · Esc сохранить" + RESET,
        "",
    ]
    for i, (key, label, kind) in enumerate(ROWS):
        cursor = CYAN + "▸" + RESET if i == selected else " "
        if kind == "action":
            body = f"[ {label} ]"
        elif kind == "toggle":
            badge = GREEN + " ВКЛ " + RESET if settings[key] else GREY + " ВЫКЛ " + RESET
            body = f"{label:<30} {badge}"
        else:
            body = f"{label:<30} {DIM}◀{RESET} {_value(settings, key, models)} {DIM}▶{RESET}"
        if i == selected:
            body = BOLD + body + RESET
        lines.append(f" {cursor} {body}")
    lines.append("")
    lines.append("Модель: ←/→ выбор из списка · Enter — ввести название")
    lines.append(f"Vision для OCR: {_vision_label(settings)} · {settings['model']}")
    if status:
        lines.append(status)
    lines.append(DIM + "Сохраняется в data/settings.json и применяется сразу" + RESET)
    return "\n".join(lines) + "\n"


def _cycle(settings: dict, key: str, models: list[str], step: int) -> None:
    if key == "provider":
        import gateway_manager
        gateway_manager.stop()
        apply(settings)
        settings["provider"] = "qoder" if settings["provider"] == "openai" else "openai"
        for field in ("base_url", "api_key", "model"):
            settings[field] = settings[settings["provider"] + "_" + field]
    elif key == "model" and models:
        index = models.index(settings["model"]) if settings["model"] in models else 0
        settings["model"] = models[(index + step) % len(models)]
    elif key == "effort":
        choices = _efforts(settings)
        index = choices.index(settings["effort"]) if settings["effort"] in choices else 0
        settings["effort"] = choices[(index + step) % len(choices)]
    elif key == "context":
        index = CONTEXTS.index(settings["context"])
        settings["context"] = CONTEXTS[(index + step) % len(CONTEXTS)]
    elif key == "spinner":
        index = SPINNERS.index(settings["spinner"])
        settings["spinner"] = SPINNERS[(index + step) % len(SPINNERS)]
    elif key == "ocr_backend":
        info = _vision(settings)
        choices = OCR_BACKENDS if info and info["supported"] is True else ("easyocr",)
        index = choices.index(settings["ocr_backend"]) if settings["ocr_backend"] in choices else 0
        settings["ocr_backend"] = choices[(index + step) % len(choices)]
    elif key == "ocr_workers":
        index = OCR_WORKERS_VALUES.index(settings["ocr_workers"])
        settings["ocr_workers"] = OCR_WORKERS_VALUES[(index + step) % len(OCR_WORKERS_VALUES)]
    elif key == "gw_parallel":
        index = GW_PARALLEL_VALUES.index(settings["gw_parallel"])
        settings["gw_parallel"] = GW_PARALLEL_VALUES[(index + step) % len(GW_PARALLEL_VALUES)]
    elif key == "gw_timeout":
        index = GW_TIMEOUT_VALUES.index(settings["gw_timeout"])
        settings["gw_timeout"] = GW_TIMEOUT_VALUES[(index + step) % len(GW_TIMEOUT_VALUES)]
    elif key == "gw_window":
        index = GW_WINDOW_VALUES.index(settings["gw_window"])
        settings["gw_window"] = GW_WINDOW_VALUES[(index + step) % len(GW_WINDOW_VALUES)]
    elif key == "display_mode":
        index = DISPLAY_MODES.index(settings["display_mode"])
        settings["display_mode"] = DISPLAY_MODES[(index + step) % len(DISPLAY_MODES)]


def _set_toggle(settings: dict, key: str, value: bool | None = None) -> None:
    settings[key] = (not settings[key]) if value is None else value


def _read_key():
    import msvcrt
    ch = msvcrt.getwch()
    if ch in ("\x00", "\xe0"):
        code = msvcrt.getwch()
        return {"H": "up", "P": "down", "K": "left", "M": "right"}.get(code)
    if ch == "\x1b":
        seq = ""
        deadline = time.monotonic() + 0.05
        while time.monotonic() < deadline:
            if msvcrt.kbhit():
                seq += msvcrt.getwch()
                deadline = time.monotonic() + 0.02
            else:
                time.sleep(0.002)
        if not seq:
            return "esc"
        if seq.startswith("[<"):
            match = re.match(r"\[<(\d+);(\d+);(\d+)([Mm])", seq)
            if match:
                return ("mouse", int(match.group(1)), int(match.group(2)),
                        int(match.group(3)), match.group(4))
            return None
        return {"[A": "up", "[B": "down", "[C": "right", "[D": "left"}.get(seq)
    if ch in ("\r", "\n"):
        return "enter"
    if ch == " ":
        return "space"
    if ch == "\x03":
        return "esc"
    return ch.lower()


def _poll_key():
    import msvcrt
    return _read_key() if msvcrt.kbhit() else None


def _identity(settings):
    return tuple(settings[key] for key in ("provider", "base_url", "api_key", "model"))


def _background_action(key, snapshot, models, select_first=True):
    if snapshot["provider"] == "qoder":
        import gateway_manager
        gateway_manager.ensure(snapshot)
    model_checks.check_cancelled()
    names, status = _settings_action(key, snapshot, models, select_first=select_first)
    if key in ("load_models", "detect"):
        messages = [detect_reasoning(snapshot), detect_vision(snapshot)]
        warnings = [message for message in messages if message]
        if warnings:
            status += " · Проверка: " + "; ".join(dict.fromkeys(warnings))
    return names, status, snapshot["model"]


def run(settings: dict) -> dict:
    if os.name != "nt" or not sys.stdin.isatty():
        print("Экран настроек работает только в интерактивной консоли Windows.")
        return settings
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass
    models, status, selected = [settings["model"]], "", 0
    job, job_identity, job_label = None, None, ""

    def start(key, select_first=True):
        nonlocal job, job_identity, job_label
        if job:
            job.cancel()
        snapshot, names = settings.copy(), list(models)
        job_identity = _identity(settings)
        job_label = {"load_models": "Загружаю модели и проверяю возможности",
                     "detect": "Проверяю возможности модели",
                     "test_connection": "Проверяю подключение",
                     "test_vision": "Проверяю Vision",
                     "test_capabilities": "Перепроверяю возможности модели"}[key]
        job = model_checks.Job(lambda: _background_action(key, snapshot, names, select_first))

    out = sys.stdout
    start("load_models", select_first=False)
    last_frame = ""
    try:
        out.write("\x1b[?1049h\x1b[?1000h\x1b[?1006h\x1b[?25l")
        while True:
            if job and job.done.is_set():
                if not job.cancelled.is_set() and job_identity == _identity(settings):
                    if job.error:
                        status = "Проверка не завершена. Проверь подключение и повтори."
                    elif job.result:
                        models, status, settings["model"] = job.result
                        apply(settings)
                        save(settings)
                job = None
            display_status = status
            if job:
                spinner = "|/-\\"[int(time.monotonic() * 5) % 4]
                display_status = f"{spinner} {job_label}… C — отменить"
            frame = render(settings, selected, models, display_status)
            if frame != last_frame:
                out.write("\x1b[2J\x1b[H" + frame)
                out.flush()
                last_frame = frame
            key = _poll_key()
            if key is None:
                time.sleep(0.03)
                continue
            if key == "esc":
                break
            if key in ("c", "с"):
                if job:
                    job.cancel()
                    job = None
                    status = "Проверка отменена"
                continue
            before = _identity(settings)
            if key in ("up", "down"):
                selected = (selected + (-1 if key == "up" else 1)) % len(ROWS)
            else:
                row_key, _label, kind = ROWS[selected]
                step = -1 if key == "left" else 1
                activate = key in ("enter", "space")
                if isinstance(key, tuple) and key[0] == "mouse":
                    button, x, y, press = key[1:]
                    index = y - 4
                    if press != "M" or button != 0 or not 0 <= index < len(ROWS):
                        continue
                    selected = index
                    row_key, _label, kind = ROWS[selected]
                    step, activate = (-1 if x < 45 else 1), True
                if key not in ("left", "right", "enter", "space") and not isinstance(key, tuple):
                    continue
                if row_key == "provider" and job:
                    job.cancel()
                    job = None
                if kind == "action":
                    if activate:
                        start(row_key)
                elif activate and (kind == "text" or (row_key == "model" and settings["provider"] == "openai")):
                    _edit(settings, row_key)
                elif kind == "toggle":
                    _set_toggle(settings, row_key, None if activate else key == "right")
                else:
                    _cycle(settings, row_key, models, step)
                apply(settings)
            after = _identity(settings)
            if after != before:
                if after[:3] != before[:3]:
                    models = [settings["model"]]
                    start("load_models")
                else:
                    start("detect")
            save(settings)
    except KeyboardInterrupt:
        pass
    finally:
        if job:
            job.cancel()
        out.write("\x1b[?1006l\x1b[?1000l\x1b[?1049l\x1b[?25h")
        out.flush()
    save(settings)
    apply(settings)
    return settings


def _settings_action(key: str, settings: dict, models: list[str], select_first: bool = True):
    try:
        if key == "detect":
            return models, "Возможности модели проверены"
        if key == "test_capabilities":
            messages = [detect_reasoning(settings, force=True), detect_vision(settings, force=True)]
            warnings = [m for m in messages if m]
            return models, ("Проверка: " + "; ".join(warnings) if warnings else "Возможности модели перепроверены")
        if key == "load_models":
            names = list(fetch_models(settings))
            count = len(names)
            if settings["model"] not in names:
                if select_first:
                    settings["model"] = names[0]
                elif settings["model"]:
                    names.insert(0, settings["model"])
            return names, f"Загружено моделей: {count} · /models"
        if key == "test_vision":
            warning = detect_vision(settings, force=True)
            return models, (f"Проверка: {warning}" if warning else f"Vision для OCR: {_vision_label(settings)}")
        return models, test_connection(settings)
    except RuntimeError as exc:
        # После смены подключения не показываем список от прежнего провайдера.
        return models or [settings["model"]], f"Ошибка: {exc}"


def _edit(settings: dict, key: str) -> None:
    """Обычный строковый ввод позволяет вставлять адрес, модель и ключ."""
    import getpass
    from urllib.parse import urlsplit
    sys.stdout.write("\x1b[?1006l\x1b[?1000l\x1b[?25h\n")
    sys.stdout.flush()
    try:
        label = {"base_url": "Адрес API, включая /v1", "api_key": "API-ключ (ввод скрыт)", "model": "Название модели"}[key]
        prompt = label + " [Enter — оставить, '-' — очистить ключ]: "
        value = (getpass.getpass(prompt) if key == "api_key" else input(prompt)).strip()
        if not value:
            return
        if key == "base_url":
            parsed = urlsplit(value)
            if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
                input("Нужен адрес http(s)://хост/v1 без ключа в URL. Enter — назад.")
                return
            value = value.rstrip("/")
        if settings["provider"] == "qoder" and key in ("base_url", "api_key"):
            import gateway_manager
            gateway_manager.stop()
        settings[key] = "" if key == "api_key" and value == "-" else value
    except (EOFError, KeyboardInterrupt):
        pass
    finally:
        sys.stdout.write("\x1b[?1000h\x1b[?1006h\x1b[?25l")
        sys.stdout.flush()
