"""Фоновый бот: long polling, меню команд и мягкая остановка.

Запускается из обычной консоли МаксИИмки (debug_console.py) в том же asyncio-цикле,
поэтому отдельное окно и второй запуск шлюза не нужны. Ответы бота идут через
те же handlers, что и консольные сообщения.

Тот же цикл использует main.py (и для MAX, и для Telegram): marker сохраняется
между перезапусками (без него MAX после рестарта отдаёт только ПОСЛЕДНЕЕ событие),
401 останавливает polling вместо бесконечных повторов, задачи-обработчики
держатся по ссылке и гасятся при остановке.
"""
import asyncio
import logging
import time
from pathlib import Path

import agent
import config
import llm
from . import handlers
from .max_client import MaxApiError, MaxClient

log = logging.getLogger("neurotutor.bot")

MARKER_PATH = Path(config.DATA_DIR) / "max_update_marker.txt"
TG_OFFSET_PATH = Path(config.DATA_DIR) / "tg_update_offset.txt"


def _status(exc: Exception) -> int | None:
    """HTTP-код из MaxApiError / TgApiError (у обоих есть .status)."""
    return getattr(exc, "status", None)


class BotRuntime:
    def __init__(self, client=None):
        self.client = client or MaxClient()
        self.username: str | None = None
        self._task: asyncio.Task | None = None
        self._handlers: set[asyncio.Task] = set()
        self._stopped = asyncio.Event()
        self._is_max = isinstance(self.client, MaxClient)
        self.marker_path = MARKER_PATH if self._is_max else TG_OFFSET_PATH

    async def start(self):
        self._task = asyncio.create_task(self._run(), name="max-bot-polling")
        return self

    async def wait_ready(self, timeout: float = 5.0) -> str | None:
        """Короткое ожидание первого /me — только ради строчки в консоли."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while self.username is None and self._task and not self._task.done():
            if loop.time() >= deadline:
                break
            await asyncio.sleep(0.1)
        return self.username

    async def stop(self):
        self._stopped.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        for task in list(self._handlers):
            task.cancel()
        if self._handlers:
            await asyncio.gather(*self._handlers, return_exceptions=True)
        await self.client.close()

    # ---------- marker ----------

    def _load_marker(self) -> int | None:
        try:
            raw = self.marker_path.read_text("utf-8").strip()
            return int(raw) if raw else None
        except (OSError, ValueError):
            return None

    def _save_marker(self, marker) -> None:
        try:
            self.marker_path.parent.mkdir(parents=True, exist_ok=True)
            self.marker_path.write_text(str(marker), "utf-8")
        except OSError:
            log.warning("не смог сохранить marker — после перезапуска старые события потеряются")

    def _reset_marker(self) -> None:
        try:
            self.marker_path.unlink()
        except OSError:
            pass

    # ---------- цикл ----------

    async def _run(self):
        # Контекст-копия досталась от консоли: гасим её спиннер/размышления
        # в этом task'е, а все дочерние задачи унаследуют уже тихий контекст.
        agent.set_status_sink(None)
        llm.set_reasoning_sink(None)
        llm.set_stats_sink(None)

        marker = self._load_marker()
        backoff = 5
        commands_done = False
        commands_retry_at = 0.0
        while not self._stopped.is_set():
            try:
                info = await self.client.get_me()
                self.username = info.get("username") or info.get("first_name")
                log.info("%s-бот на связи: @%s", config.MESSENGER.upper(), self.username)
                break
            except asyncio.CancelledError:
                raise
            except RuntimeError as exc:
                if _status(exc) == 401:
                    log.error("бот остановлен: %s", exc)
                    return
                log.warning("бот: %s (повтор через %d c)", exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)
            except Exception:
                log.exception("бот: ошибка подключения")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)
        if self._stopped.is_set():
            return

        backoff = 5
        while not self._stopped.is_set():
            try:
                now = time.monotonic()
                if not commands_done and now >= commands_retry_at:
                    try:
                        await self.client.set_commands()
                        commands_done = True
                    except Exception as exc:
                        commands_retry_at = now + 300
                        log.warning("меню команд не зарегистрировано: %s", exc)

                updates, marker = await self.client.get_updates(marker)
                for update in updates:
                    task = asyncio.create_task(handlers.handle_update(self.client, update))
                    self._handlers.add(task)
                    task.add_done_callback(self._handlers.discard)
                if marker is not None:
                    self._save_marker(marker)
                backoff = 5
            except asyncio.CancelledError:
                raise
            except RuntimeError as exc:
                status = _status(exc)
                if status == 401:
                    log.error("бот остановлен: %s", exc)
                    return
                if (isinstance(exc, MaxApiError) and status in (400, 404)
                        and marker is not None):
                    log.warning("MAX-бот: marker устарел (%s) — начинаю с последнего события", exc)
                    marker = None
                    self._reset_marker()
                    continue
                log.warning("бот: %s (повтор через %d c)", exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)
            except Exception:
                log.exception("бот: непредвиденная ошибка polling")
                await asyncio.sleep(5)


async def start_bot() -> BotRuntime | None:
    """Поднимает бота, если он включён и токен найден. None — бот не нужен."""
    if not config.MAX_BOT_ENABLED or config.MESSENGER != "max":
        return None
    try:
        runtime = BotRuntime()
    except RuntimeError as exc:
        log.warning("MAX-бот не запущен: %s", exc)
        return None
    await runtime.start()
    return runtime
