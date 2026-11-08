"""Настройки работающего бота: один профиль для терминала и HTTP-интерфейса."""
import copy
import os
import re
from urllib.parse import urlsplit

import config
import console_settings as settings_ui
import gateway_manager
from admin_setup import ensure_admin_id

FIELDS = ("provider", "base_url", "model", "effort", "context", "gw_parallel",
          "gw_timeout", "gw_window", "ocr_backend", "ocr_workers", "ocr",
          "display_mode", "spinner")
CHOICES = {
    "provider": ("openai", "qoder"),
    "effort": settings_ui.EFFORTS,
    "context": settings_ui.CONTEXTS,
    "gw_parallel": settings_ui.GW_PARALLEL_VALUES,
    "gw_timeout": settings_ui.GW_TIMEOUT_VALUES,
    "gw_window": settings_ui.GW_WINDOW_VALUES,
    "ocr_backend": settings_ui.OCR_BACKENDS,
    "ocr_workers": settings_ui.OCR_WORKERS_VALUES,
    "display_mode": settings_ui.DISPLAY_MODES,
    "spinner": settings_ui.SPINNERS,
}


class SettingsConflict(ValueError):
    pass


def initialize():
    ensure_admin_id(interactive=False if os.getenv("MAXIMKA_DESKTOP_RUN") else None)
    with settings_ui.SETTINGS_LOCK:
        settings = settings_ui.load()
        settings_ui.apply(settings)
    return settings


def revision() -> str:
    try:
        return str(settings_ui.SETTINGS_PATH.stat().st_mtime_ns)
    except FileNotFoundError:
        return "0"


def public_snapshot(settings=None):
    with settings_ui.SETTINGS_LOCK:
        settings = settings or settings_ui.load()
        return {
            "values": {key: settings[key] for key in FIELDS},
            "revision": revision(),
            "api_key_set": bool(settings.get("api_key")),
            "profiles": {
                profile: {"base_url": settings[f"{profile}_base_url"],
                          "model": settings[f"{profile}_model"],
                          "api_key_set": bool(settings.get(f"{profile}_api_key"))}
                for profile in ("openai", "qoder")
            },
            "choices": CHOICES,
            "capabilities": {
                "vision": settings_ui._vision(settings),
                "efforts": settings_ui._efforts(settings),
            },
        }


def prepare(current, patch):
    if not isinstance(patch, dict) or set(patch) - set(FIELDS) - {"api_key"}:
        raise ValueError("В запросе есть неизвестные настройки.")
    result = copy.deepcopy(current)
    provider = patch.get("provider", result["provider"])
    if provider not in CHOICES["provider"]:
        raise ValueError("Неизвестное подключение.")
    if provider != result["provider"]:
        result["provider"] = provider
        for field in ("base_url", "api_key", "model"):
            result[field] = result[f"{provider}_{field}"]
    for key, value in patch.items():
        if key in CHOICES:
            choices = CHOICES[key]
            expected = type(choices[0])
            if type(value) is not expected or value not in choices:
                raise ValueError(f"Недопустимое значение настройки {key}.")
        elif key == "ocr":
            if not isinstance(value, bool):
                raise ValueError("OCR должен быть включён или выключен.")
        elif not isinstance(value, str) or len(value) > (4096 if key == "api_key" else 500):
            raise ValueError(f"Недопустимое значение настройки {key}.")
        if key == "base_url":
            value = value.strip().rstrip("/")
            try:
                parsed = urlsplit(value)
                valid = (parsed.scheme in ("http", "https") and parsed.hostname
                         and parsed.port != 0 and not parsed.username
                         and not parsed.password and not parsed.query and not parsed.fragment)
            except ValueError:
                valid = False
            if not valid or re.search(r"[\s\x00-\x1f]", value):
                raise ValueError("Укажите полный HTTP(S)-адрес API без пароля и параметров.")
        if key == "model":
            value = value.strip()
            if not value or re.search(r"[\x00-\x1f]", value):
                raise ValueError("Укажите название модели.")
        if key == "api_key" and re.search(r"[\r\n\x00]", value):
            raise ValueError("API-ключ должен занимать одну строку.")
        result[key] = value
    for field in ("base_url", "api_key", "model"):
        result[f"{provider}_{field}"] = result[field]
    return result


def update(patch, expected_revision):
    with settings_ui.SETTINGS_LOCK:
        if expected_revision != revision():
            raise SettingsConflict("Настройки уже изменились. Обновите их и повторите сохранение.")
        previous = settings_ui.load()
        settings = prepare(previous, patch)
        try:
            settings_ui.apply(settings)
            settings_ui.save(settings)
        except Exception:
            settings_ui.apply(previous)
            raise
        message = "Настройки сохранены и применены."
        # Шлюз только при явном выборе Qoder; управление ограничено своим процессом.
        if settings["provider"] == "qoder":
            changed = any(settings[key] != previous[key] for key in
                          ("gw_parallel", "gw_timeout", "gw_window", "base_url", "api_key"))
            try:
                status = (gateway_manager.restart(settings) if changed
                          and previous["provider"] == "qoder" else gateway_manager.ensure(settings))
                message += " " + redact(status, settings)
            except Exception:
                message += " Шлюз не запущен. Проверьте подключение."
        return {**public_snapshot(settings), "message": message}


def edit_in_console():
    with settings_ui.SETTINGS_LOCK:
        return settings_ui.run(settings_ui.load())


def redact(message, settings):
    text = str(message)
    for key in ("api_key", "openai_api_key", "qoder_api_key"):
        secret = settings.get(key)
        if secret:
            text = text.replace(secret, "••••")
    if config.MAX_TOKEN:
        text = text.replace(config.MAX_TOKEN, "••••")
    return text[:1600]


def check(action):
    allowed = {"load_models", "test_connection", "test_vision", "test_capabilities"}
    if action not in allowed:
        raise ValueError("Неизвестная проверка.")
    with settings_ui.SETTINGS_LOCK:
        settings = settings_ui.load()
        checked_revision = revision()
    try:
        names, message = settings_ui._settings_action(action, settings, [settings["model"]], False)
    except Exception:
        names, message = [settings["model"]], "Ошибка проверки подключения. Проверьте адрес, ключ и доступность API."
    return {"models": names, "message": redact(message, settings),
            "ok": not message.startswith(("Ошибка", "Проверка:")),
            "revision": checked_revision, "settings": public_snapshot()}
