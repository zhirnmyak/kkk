"""Первичная настройка администратора, общая для сервера и консоли."""
import json
import os
import sys
from pathlib import Path

import config

ADMIN_PATH = config.DATA_DIR / "admin.json"


def parse_admin_id(value) -> int:
    if isinstance(value, bool):
        raise ValueError("Нужен числовой ID пользователя MAX.")
    text = str(value).strip()
    if not text.isascii() or not text.isdigit() or not 0 < int(text) < 2**63:
        raise ValueError("ID администратора должен быть положительным целым числом.")
    return int(text)


def get_admin_id() -> int | None:
    value = os.getenv("MAX_ADMIN_ID", "").strip()
    if value:
        return parse_admin_id(value)
    try:
        saved = json.loads(ADMIN_PATH.read_text("utf-8"))
        return parse_admin_id(saved["admin_id"])
    except FileNotFoundError:
        return None
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"Не удалось прочитать ID администратора из {ADMIN_PATH}.") from exc


def ensure_admin_id(*, interactive=None, read=None, write=print) -> int | None:
    """Не читаем stdin сценария/службы. Пропуск не назначает администратора MAX."""
    admin_id = get_admin_id()
    if admin_id:
        return admin_id
    if interactive is None:
        interactive = sys.stdin.isatty()
    if not interactive:
        write("Администратор не задан. Укажите MAX_ADMIN_ID или запустите бота в терминале.")
        return None
    read = read or input
    write("\nНастройка администратора мини-приложения.")
    write("Свой ID можно узнать командой /id в MAX. Enter — настроить при следующем запуске.")
    while True:
        try:
            value = read("ID администратора MAX: ").strip()
        except EOFError:
            return None
        if not value:
            write("Администратор MAX пока не задан. Бот продолжит работу.")
            return None
        try:
            admin_id = parse_admin_id(value)
        except ValueError as exc:
            write(str(exc))
            continue
        ADMIN_PATH.parent.mkdir(parents=True, exist_ok=True)
        temporary = ADMIN_PATH.with_suffix(".tmp")
        temporary.write_text(json.dumps({"admin_id": admin_id}) + "\n", encoding="utf-8")
        temporary.replace(ADMIN_PATH)
        write(f"Администратор {admin_id} сохранён.")
        return admin_id


if __name__ == "__main__":
    ensure_admin_id(interactive=True)
