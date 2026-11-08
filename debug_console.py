"""Диалог с ботом прямо в консоли PyCharm, без мессенджера.

• сообщения вводятся с клавиатуры
• inline-кнопки «нажимаются» цифрой (1, 2, ...)
• /файл <путь> — отправить боту локальный PDF/TXT как вложение

Стенд передаёт handlers нейтральные события — ровно тот формат, который
MaxClient.to_event и TgClient.to_event выдают из живых апдейтов, поэтому
здесь проверяется та же логика, что и в мессенджере."""
import asyncio
import atexit
import contextlib
import math
import os
import shutil
import sys
import time
from pathlib import Path

import config
import console_settings
import gateway_manager
import agent
import db
import llm
import scheduler
from bot.console_client import ConsoleClient
from bot import handlers

UID = CID = config.CONSOLE_UID  # «пользователь» консоли

EFFORT_LEVELS = ("auto", "none", "low", "medium", "high", "xhigh", "max", "ultracode")
_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


def _enable_vt() -> bool:
    """Включает обработку ANSI в консоли Windows; без этого коды очистки видны как текст."""
    if os.name != "nt":
        return True
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        return bool(kernel32.SetConsoleMode(handle, mode.value | 0x0004))
    except Exception:
        return False


_VT = _enable_vt()


def reasoning_visible() -> bool:
    return (os.getenv("MAXIMKA_REASONING") or "show").strip().lower() != "hide"


def current_effort() -> str:
    return (os.getenv("LLM_REASONING_EFFORT") or "").strip().lower() or "auto"


def _spinner_on() -> bool:
    mode = os.getenv("MAXIMKA_SPINNER", "auto")
    if mode == "0":
        return False
    return mode == "1" or sys.stdout.isatty()


@contextlib.asynccontextmanager
async def thinking():
    """Крутилка на время запроса к модели; в pipe по умолчанию выключена (MAXIMKA_SPINNER=1 включает)."""
    global _status_text, _no_erase, _spinner_muted, _quiet, _reason_buf, _request_start
    _request_start = time.monotonic()
    _status_text = "думаю"
    _no_erase = False
    # приветствие /start отправляется вне thinking() и оставляет флаг True —
    # без сброса спиннер молчит весь первый запрос
    _spinner_muted = False
    _quiet = False
    _reason_buf = ""
    if not _spinner_on():
        yield
        return
    stop = asyncio.Event()

    async def spin():
        started = time.monotonic()
        i = 0
        while not stop.is_set():
            if not _quiet and not _spinner_muted:
                cols = max(shutil.get_terminal_size((100, 24)).columns, 40)
                suffix = (f" · {config.LLM_MODEL} · effort {current_effort()} · "
                          f"{time.monotonic() - started:4.1f}s")
                room = max(12, cols - len(suffix) - 3)
                label = _status_text
                if len(label) > room:
                    label = label[:room - 1].rstrip() + "…"
                sys.stdout.write(f"\r{_FRAMES[i % len(_FRAMES)]} {label}{suffix}")
                sys.stdout.flush()
            i += 1
            try:
                await asyncio.wait_for(stop.wait(), 0.12)
            except asyncio.TimeoutError:
                pass

    task = asyncio.create_task(spin())
    try:
        yield
    finally:
        stop.set()
        await task
        globals()["_spinner_muted"] = False
        if _quiet:
            print()
            globals()["_quiet"] = False
        if _pending_stats:
            print_stats(_pending_stats)
            globals()["_pending_stats"] = None
        sys.stdout.write("\r" + " " * 80 + "\r")
        sys.stdout.flush()


def handle_effort(text: str) -> None:
    parts = text.split(maxsplit=1)
    arg = parts[1].strip().lower() if len(parts) > 1 else ""
    if not arg:
        print(f"⚙️ effort: {current_effort()} | доступно: {', '.join(EFFORT_LEVELS)}")
        print("   сменить: /эффорт xhigh · вернуть авто: /эффорт auto")
        return
    if arg not in EFFORT_LEVELS:
        print(f"⚠️ Нет уровня «{arg}». Доступно: {', '.join(EFFORT_LEVELS)}")
        return
    os.environ["LLM_REASONING_EFFORT"] = arg
    print(f"⚙️ effort → {arg}")


_quiet = False          # спиннер молчит, пока печатаются размышления
_spinner_muted = False  # спиннер молчит, пока бот выводит сообщение (иначе затирает строку)
_reason_buf = ""        # уже выведенный текст размышлений (нужен для подсчёта строк)
_pending_stats = None   # статистика ждёт конца ответа, чтобы не влезать в блок
_status_text = "думаю"  # текущий статус агента в спиннере
_no_erase = False       # статусы уже напечатаны в поток размышлений — не стираем блок
_request_start = 0.0    # начало текущего запроса: статистика показывает и общее время


def _clear_line() -> None:
    sys.stdout.write("\r" + " " * 80 + "\r")
    sys.stdout.flush()


def _reason_line_count(text: str) -> int:
    cols = max(shutil.get_terminal_size((80, 24)).columns, 20)
    parts = text.split("\n")
    lines = sum(max(1, math.ceil(len(p) / cols)) for p in parts[:-1])
    if parts[-1]:
        lines += max(1, math.ceil(len(parts[-1]) / cols))
    return lines


def _erase_reasoning() -> None:
    """Стирает блок размышлений, когда пришёл ответ. В pipe не стираем — там ANSI некуда девать."""
    global _quiet, _reason_buf, _no_erase
    if _no_erase:
        _no_erase = False
        _reason_buf = ""
        _quiet = False
        return
    if _reason_buf and _VT and (sys.stdout.isatty() or os.getenv("MAXIMKA_TTY") == "1"):
        lines = _reason_line_count(_reason_buf)
        sys.stdout.write(f"\x1b[{lines}A")
        for _ in range(lines + 1):
            sys.stdout.write("\x1b[2K\n")
        sys.stdout.write(f"\x1b[{lines + 1}A")
        sys.stdout.flush()
    _reason_buf = ""
    _quiet = False


def on_reasoning(fragment: str) -> None:
    global _quiet, _reason_buf
    if not reasoning_visible():
        return
    if not _quiet:
        _quiet = True
        _clear_line()
        print("🧠 размышление:")
    _reason_buf += fragment
    sys.stdout.write(fragment)
    sys.stdout.flush()


def on_status(text: str) -> None:
    """Статус агента: в спиннере или зелёной строкой в потоке размышлений."""
    global _status_text, _no_erase
    text = (text or "").strip()
    if not text or text == _status_text:
        return
    _status_text = text
    if os.getenv("MAXIMKA_AGENT_STATUS", "1") == "0":
        return
    if reasoning_visible() and _VT and (sys.stdout.isatty() or os.getenv("MAXIMKA_TTY") == "1"):
        # стираем кадр спиннера, чтобы зелёная строка не вставала под застывшим «⠴ думаю»
        if not _quiet:
            _clear_line()
        sys.stdout.write(f"\x1b[32m▶ {text}\x1b[0m\n")
        sys.stdout.flush()
        _no_erase = True


def on_stats(info: dict) -> None:
    global _pending_stats
    _pending_stats = info


def print_stats(info: dict) -> None:
    approx = "~" if info.get("estimated") else ""
    credits = info.get("credits")
    tail = f" · credits {credits:.4f}" if isinstance(credits, (int, float)) else ""
    total = time.monotonic() - _request_start if _request_start else 0.0
    total_tail = f" · всего {total:.1f}s" if total > 0 else ""
    print(f"   ↳ {info.get('model')} · effort {info.get('effort')} · "
          f"вход {approx}{info.get('prompt_tokens', 0)} · выход {approx}{info.get('completion_tokens', 0)} · "
          f"вызов {info.get('seconds', 0):.1f}s{total_tail}{tail}")


def event(text: str = "", attachments: list | None = None) -> dict:
    return {"kind": "message", "user_id": UID, "chat_id": CID,
            "text": text.strip(), "attachments": attachments or []}


async def press_button(client, num: int):
    if not 1 <= num <= len(client.buttons):
        print(f"⚠️ Нет кнопки {num} (есть 1–{len(client.buttons)})"); return
    _text, payload = client.buttons[num - 1]
    await handlers.on_callback(client, UID, CID, payload)


async def send_local_file(client, path_str: str):
    path = Path(path_str.strip().strip('"'))
    if not path.is_file():
        print(f"⚠️ Файл не найден: {path.resolve()}"); return
    db.set_state(UID, "upload")
    await handlers.on_message(client, UID, CID, event(attachments=[
        {"name": path.name, "handle": str(path), "mime": ""}]))


async def main(*, http_ready=None):
    import runtime_settings
    from miniapp.server import start_console_server
    settings = runtime_settings.initialize()
    gateway_status = gateway_manager.ensure(settings)
    atexit.register(gateway_manager.stop)
    llm.set_reasoning_sink(on_reasoning)
    llm.set_stats_sink(on_stats)
    agent.set_status_sink(on_status)
    client = ConsoleClient()
    _send_text = client.send_text

    async def send_text(chat_id, text, keyboard=None, **kwargs):
        global _pending_stats, _spinner_muted
        _spinner_muted = True
        _erase_reasoning()   # размышления пропадают, остаётся ответ
        result = await _send_text(chat_id, text, keyboard, **kwargs)
        if _pending_stats:
            print_stats(_pending_stats)
            _pending_stats = None
        return result

    client.send_text = send_text
    db.touch_user(UID, CID)
    print("=== «МаксИИмка» — консольная отладка (без мессенджера) ===")
    print("Команды: /start /upload /файлы /карточки [тема] /повторение /тест /конспект /план /стата /настройки /очистить")
    print("Особое: /файл <путь> — загрузить файл; /настройки — модель, effort, OCR, показ; /выход — закрыть всё; цифра — нажать кнопку")
    print(f"Мозги: {config.LLM_MODEL}, effort={current_effort()}, "
          f"OCR={'вкл' if config.OCR_ENABLED else 'выкл'}, "
          f"показ={'размышления + статусы' if reasoning_visible() else 'только статусы'}, "
          f"сменить — /эффорт {'|'.join(EFFORT_LEVELS)}\n")
    print(f"Шлюз: {gateway_status}\n")

    bot_runtime = None
    scheduler_started = False
    if config.MESSENGER == "max" and config.MAX_BOT_ENABLED:
        if not config.MAX_TOKEN:
            print("MAX-бот: токен не найден (max_token.txt) — бот выключен.\n")
        else:
            try:
                from bot.runtime import start_bot
                bot_runtime = await start_bot()
                if bot_runtime:
                    scheduler.start()
                    scheduler_started = True
                username = await bot_runtime.wait_ready(6) if bot_runtime else None
                if username:
                    print(f"MAX-бот: @{username} — работает в фоне (long polling).\n")
                elif bot_runtime:
                    print("MAX-бот: подключаюсь в фоне; сообщения придут, как только "
                          "появится сеть.\n")
            except Exception as exc:
                print(f"MAX-бот: не запущен — {exc}\n")
    http_server = http_task = http_socket = None
    try:
        http_server, http_task, http_socket = await start_console_server(
            bot_runtime.client if bot_runtime else None)
        if http_ready:
            http_ready(http_server)
        if not config.LOCAL_MINIAPP:
            print(f"Мини-приложение: http://127.0.0.1:{config.HTTP_PORT}/app/\n")
    except OSError:
        print(f"Мини-приложение: порт {config.HTTP_PORT} занят. Проверьте второй запуск бота.\n")
    await handlers.on_message(client, UID, CID, event("/start"))

    loop = asyncio.get_running_loop()
    try:
        while True:
            try:
                text = (await loop.run_in_executor(None, input, "\n👤 ТЫ: ")).strip()
            except (EOFError, KeyboardInterrupt):
                break
            if not text or text.lower() in {"выход", "выйти", "exit", "quit",
                                            "/выход", "/выйти", "/exit", "/quit"}:
                if text:
                    break
                continue
            if text.lower() in {"/настройки", "/settings"}:
                settings = console_settings.load()
                before_provider = settings["provider"]
                before_gw = (settings["gw_parallel"], settings["gw_timeout"], settings["gw_window"])
                settings = await asyncio.to_thread(runtime_settings.edit_in_console)
                if settings["provider"] == "qoder" and (settings["gw_parallel"], settings["gw_timeout"], settings["gw_window"]) != before_gw:
                    print("↻ Перезапускаю шлюз с новыми настройками…")
                    print(f"Шлюз: {gateway_manager.restart(settings)}")
                elif settings["provider"] == "qoder" and before_provider != "qoder":
                    print(f"Шлюз: {gateway_manager.ensure(settings)}")
                print(f"⚙️ {config.LLM_MODEL} · effort {current_effort()} · контекст "
                      f"{settings['context'] or 'без ограничения'} · "
                      f"OCR {'вкл' if config.OCR_ENABLED else 'выкл'} · "
                      f"показ {'размышления + статусы' if reasoning_visible() else 'только статусы'}")
                continue
            if text.lower() == "/эффорт" or text.lower().startswith("/эффорт ") \
                    or text.lower() == "/effort" or text.lower().startswith("/effort "):
                handle_effort(text); continue
            if text.lower() == "/файл" or text.lower().startswith("/файл "):
                # граница обязательна: иначе /файлы съедались как /файл с путём «ы»
                await send_local_file(client, text[len("/файл"):]); continue
            state, _ = db.get_state(UID)
            if text.isdigit() and state != "quiz":   # в тесте цифры — это ответы!
                await press_button(client, int(text)); continue
            async with thinking():
                await handlers.on_message(client, UID, CID, event(text))
    finally:
        if http_server:
            http_server.should_exit = True
            await http_task
            http_socket.close()
        if scheduler_started:
            scheduler.sched.shutdown(wait=False)
        if bot_runtime:
            await bot_runtime.stop()


if __name__ == "__main__":
    asyncio.run(main())
