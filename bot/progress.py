"""Прогресс в чате: одно сообщение, которое бот редактирует по ходу работы.

MAX не даёт «печатает…» для ботов, а слать десятки сообщений нельзя (лимит
2/сек). Поэтому отправляем одну строку и обновляем её через PUT /messages с
notify=false: пользователь видит стадию агента и хвост размышлений модели,
но не получает push на каждое обновление.
"""
import asyncio
import logging
import time
from user_errors import dialog_unavailable

log = logging.getLogger("neurotutor.progress")

REASON_LIMIT = 2400   # столько последних символов размышлений держим в сообщении
TEXT_LIMIT = 3900


class ChatProgress:
    def __init__(self, client, chat_id, initial: str = "⏳ Думаю…", throttle: float = 1.0,
                 show_reasoning: bool = True):
        self.client = client
        self.chat_id = chat_id
        self.show_reasoning = show_reasoning
        self._status = (initial or "⏳ Думаю…").strip()
        self._reasoning = ""
        self._reason_trimmed = False
        self._shown_render: str | None = None
        self._message_id = None
        self._closed = False
        self._task: asyncio.Task | None = None
        self._heartbeat: asyncio.Task | None = None
        self._throttle = throttle
        self._last_edit = 0.0
        self._version = 0
        self._started = time.monotonic()
        self._loop = asyncio.get_running_loop()

    @property
    def message_id(self):
        return self._message_id

    async def start(self):
        try:
            self._message_id = await self.client.send_text(
                self.chat_id, self._status, return_id=True)
        except Exception as exc:
            if dialog_unavailable(exc):
                raise
            log.exception("не удалось отправить прогресс-сообщение")
            return self
        if self._message_id:
            self._shown_render = self._status
            self._last_edit = time.monotonic()
            # Раз в секунду обновляем время, даже если статус не менялся.
            self._heartbeat = asyncio.create_task(self._tick(), name="chat-progress-tick")
            if self._version:
                self._ensure_flush()
        return self

    # ---------- статус агента ----------

    def status(self, text: str) -> None:
        """Синхронный sink: вызывается и из event loop, и из потоков OCR."""
        text = (text or "").strip()
        if not text:
            return
        try:
            self._loop.call_soon_threadsafe(self._set_status, text)
        except RuntimeError:
            pass  # loop уже закрывается

    def _set_status(self, text: str) -> None:
        if self._closed or text == self._status:
            return
        self._status = text
        self._version += 1
        if self._message_id:
            self._ensure_flush()

    # ---------- размышления модели ----------

    def feed_reason(self, fragment: str) -> None:
        """Тот же стиль, что в консоли: «🧠 размышление: …», только последним
        хвостом — в чат нельзя вывалить 40k символов chain-of-thought.
        В режиме «только статусы» хвост не показываем и не копим."""
        if not fragment or not self.show_reasoning:
            return
        try:
            self._loop.call_soon_threadsafe(self._append_reason, str(fragment))
        except RuntimeError:
            pass

    def _append_reason(self, fragment: str) -> None:
        if self._closed:
            return
        self._reasoning += fragment
        if len(self._reasoning) > REASON_LIMIT:
            self._reasoning = self._reasoning[-REASON_LIMIT:]
            self._reason_trimmed = True
        self._version += 1
        if self._message_id:
            self._ensure_flush()

    # ---------- отрисовка/редактирование ----------

    def _render(self) -> str:
        body = self._status
        if self._reasoning:
            prefix = "…\n" if self._reason_trimmed else ""
            body += "\n\n🧠 размышление:\n" + prefix + self._reasoning
        elapsed = int(time.monotonic() - self._started)
        if elapsed >= 1:
            body += f" · {elapsed} с"
        return body[:TEXT_LIMIT]

    def _ensure_flush(self) -> None:
        if self._closed:
            return
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._flush(), name="chat-progress")

    async def _tick(self) -> None:
        while not self._closed:
            await asyncio.sleep(self._throttle)
            if self._closed or not self._message_id:
                return
            self._ensure_flush()

    async def _flush(self) -> None:
        while not self._closed and self._message_id:
            version = self._version
            render = self._render()
            if render == self._shown_render:
                if version == self._version:
                    return
                continue
            wait = self._last_edit + self._throttle - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            if self._closed:
                return
            version = self._version
            render = self._render()
            if render == self._shown_render:
                if version == self._version:
                    return
                continue
            try:
                await self.client.edit_text(self.chat_id, self._message_id, render)
            except Exception as exc:
                log.debug("прогресс не обновился: %s", exc)
                self._last_edit = time.monotonic()
                return
            self._shown_render = render
            self._last_edit = time.monotonic()
            if self._version == version:
                return

    async def finish(self, text: str, keyboard=None) -> None:
        """Заменить прогресс итоговым сообщением (или отправить его, если
        прогресс-сообщение не удалось создать). Размышления при этом исчезают,
        как в терминале после ответа."""
        if self._closed:
            return
        self._closed = True
        for task in (self._heartbeat, self._task):
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        if not self._message_id:
            try:
                await self.client.send_text(self.chat_id, text, keyboard=keyboard,
                                            keyboard_last=True)
            except Exception:
                log.exception("не удалось отправить итог операции")
            return
        try:
            await self.client.edit_text(self.chat_id, self._message_id, text,
                                        keyboard=keyboard, notify=True)
        except Exception:
            log.exception("не удалось заменить прогресс итогом — отправляю отдельно")
            try:
                await self.client.send_text(self.chat_id, text, keyboard=keyboard,
                                            keyboard_last=True)
            except Exception:
                log.exception("не удалось отправить итог операции")
