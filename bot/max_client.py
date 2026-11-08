"""Клиент Bot API мессенджера MAX.

Интерфейс (send_text / send_image / answer_callback / download / keyboard /
get_updates / set_commands / to_event) общий с bot/tg_client.py — handlers.py
не знает, в каком мессенджере работает.

MAX Bot API:
• база https://platform-api2.max.ru, токен только в заголовке Authorization;
• домен подписан НУЦ Минцифры, поэтому в SSL-контекст подмешиваются
  официальные сертификаты из certs/ к стандартному набору certifi;
• медиа сначала грузятся через POST /uploads?type=..., потом отправляются
  вложением с полученным token;
• файл можно отправить только один в сообщении, изображений — до 12.
"""
import asyncio
import json
import logging
import mimetypes
import ssl
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import certifi
import httpx

import config

log = logging.getLogger("neurotutor.max")

TEXT_LIMIT = 3800          # жёсткий лимит MAX — 4000 символов
# Хосты MAX (API, CDN вложений и загрузок). Токен бота повторно отправляем только
# им: ссылка на вложение приходит из апдейта, и при 401/403 токен раньше уходил
# на любой адрес из этой ссылки.
TRUSTED_TOKEN_HOSTS = ("max.ru", "oneme.ru", "okcdn.ru")
MAX_IMAGE_BYTES = 50 * 1024 * 1024
MAX_ATTACHMENT_BYTES = config.MAX_DOWNLOAD_MB * 1024 * 1024

BOT_COMMANDS = [
    ("start", "Приветствие и список возможностей"),
    ("загрузить", "Прислать PDF, фото или текст конспекта"),
    ("файлы", "Мои конспекты"),
    ("удалить", "Удалить один конспект"),
    ("очистить", "Удалить все материалы"),
    ("конспект", "Краткая выжимка последнего документа"),
    ("карточки", "Флеш-карточки по теме"),
    ("повторение", "Интервальное повторение"),
    ("тест", "Квиз с разбором ошибок"),
    ("план", "План подготовки к экзамену"),
    ("стата", "Мой прогресс"),
    ("картинка", "Сгенерировать изображение"),
    ("меню", "Показать кнопки главного меню"),
    ("помощь", "Список команд"),
    ("id", "Мой ID для настройки администратора"),
]

BOT_COMMANDS_LATIN = [
    ("start", "Приветствие и список возможностей"),
    ("upload", "Прислать PDF, фото или текст конспекта"),
    ("files", "Мои конспекты"),
    ("delete", "Удалить один конспект"),
    ("clear", "Удалить все материалы"),
    ("summary", "Краткая выжимка последнего документа"),
    ("cards", "Флеш-карточки по теме"),
    ("review", "Интервальное повторение"),
    ("quiz", "Квиз с разбором ошибок"),
    ("plan", "План подготовки к экзамену"),
    ("stats", "Мой прогресс"),
    ("image", "Сгенерировать изображение"),
    ("menu", "Показать кнопки главного меню"),
    ("help", "Список команд"),
    ("id", "Мой ID для настройки администратора"),
]


class MaxApiError(RuntimeError):
    def __init__(self, message: str, status: int | None = None, code: str | None = None):
        super().__init__(message)
        self.status = status
        self.code = code


def _ssl_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context(cafile=certifi.where())
    for name in ("russian_trusted_root_ca.pem", "russian_trusted_sub_ca.pem"):
        path = Path(config.MAX_CA_DIR) / name
        if path.is_file():
            ctx.load_verify_locations(cafile=str(path))
    return ctx


def _split_long(text: str, limit: int = TEXT_LIMIT) -> list[str]:
    """Режем по строкам: иначе markdown-разметка рвётся посреди слова."""
    text = text or ""
    parts, current = [], ""
    for line in text.splitlines(keepends=True):
        if len(current) + len(line) > limit and current:
            parts.append(current)
            current = ""
        current += line
        while len(current) > limit:
            parts.append(current[:limit])
            current = current[limit:]
    if current.strip() or not parts:
        parts.append(current)
    return parts


def _find_token(value, depth: int = 0) -> str | None:
    """В ответах загрузки token лежит то в token, то в photos.photoIds.token."""
    if depth > 5:
        return None
    if isinstance(value, dict):
        token = value.get("token")
        if isinstance(token, str) and token:
            return token
        for item in value.values():
            found = _find_token(item, depth + 1)
            if found:
                return found
    elif isinstance(value, list):
        for item in value:
            found = _find_token(item, depth + 1)
            if found:
                return found
    return None


def _token_allowed(url: str) -> bool:
    """Можно ли отправить токен бота на этот адрес: только HTTPS и только хосты MAX."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    host = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme != "https" or not host:
        return False
    return any(host == suffix or host.endswith("." + suffix) for suffix in TRUSTED_TOKEN_HOSTS)


def _mime_for(name: str, fallback: str = "") -> str:
    return mimetypes.guess_type(name or "")[0] or fallback


def _decode_address(value) -> dict:
    """Нейтральный chat_id MAX → query-параметры /messages.

    Новые записи хранят адрес строкой: c:<chat_id> для чата/диалога,
    u:<user_id> для диалога без chat_id. Число считаем user_id (старый формат).
    """
    if isinstance(value, bool):
        raise ValueError("некорректный адрес MAX")
    if isinstance(value, int):
        return {"user_id": value}
    text = str(value or "").strip()
    if text.startswith("u:"):
        return {"user_id": int(text[2:])}
    if text.startswith("c:"):
        return {"chat_id": int(text[2:])}
    if text.lstrip("-").isdigit():
        return {"user_id": int(text)}
    raise ValueError(f"некорректный адрес MAX: {value!r}")


class MaxClient:
    def __init__(self, token: str | None = None):
        # токен читаем при вызове: значение по умолчанию в сигнатуре привязалось
        # бы в момент импорта модуля, до загрузки .env в тестах
        self.token = (token or config.MAX_TOKEN or "").strip()
        if not self.token:
            raise RuntimeError(
                "Токен MAX не найден. Положите его в max_token.txt рядом с приложением "
                "(файл в .gitignore) или задайте MAX_BOT_TOKEN.")
        self.base = config.MAX_API
        self.http = httpx.AsyncClient(
            timeout=httpx.Timeout(65.0, connect=20.0),
            verify=_ssl_context(),
            follow_redirects=True,
        )

    # ---------- низкий уровень ----------

    async def _request(self, method: str, path: str, *, params=None, json_body=None,
                       timeout: float | None = None) -> dict:
        url = f"{self.base}{path}"
        headers = {"Authorization": self.token, "Accept": "application/json"}
        last_exc: Exception | None = None
        for attempt in range(3):
            try:
                r = await self.http.request(method, url, params=params, json=json_body,
                                            headers=headers, timeout=timeout)
            except httpx.TransportError as exc:
                last_exc = exc
                raise RuntimeError(self._network_error(exc)) from exc
            if r.status_code == 429:
                try:
                    wait = float(r.headers.get("Retry-After", "2"))
                except ValueError:
                    wait = 2.0
                await asyncio.sleep(min(max(wait, 0.5), 10.0))
                continue
            if r.status_code == 401:
                raise MaxApiError("MAX: токен неверен или отозван (401)", 401)
            if r.status_code >= 400:
                code, message = self._error_body(r)
                raise MaxApiError(f"MAX {method} {path}: {message} (HTTP {r.status_code})",
                                  r.status_code, code)
            if not r.content:
                return {}
            try:
                return r.json()
            except ValueError:
                return {"raw": r.text}
        raise RuntimeError(f"MAX: не вышли из ограничения запросов — {last_exc or '429'}")

    @staticmethod
    def _error_body(response: httpx.Response) -> tuple[str | None, str]:
        try:
            data = response.json()
        except ValueError:
            return None, (response.text or "ошибка")[:300]
        if isinstance(data, dict):
            return data.get("code"), (data.get("message") or data.get("error")
                                      or data.get("description") or "ошибка")
        return None, str(data)[:300]

    @staticmethod
    def _network_error(exc: Exception) -> str:
        text = str(exc)
        if "CERTIFICATE_VERIFY_FAILED" in text:
            return ("не удалось проверить сертификат platform-api2.max.ru — "
                    "проверьте certs/russian_trusted_*.pem")
        if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout)):
            return f"не достучаться до {config.MAX_API} ({type(exc).__name__})"
        return f"соединение с {config.MAX_API} оборвалось ({type(exc).__name__})"

    async def close(self):
        await self.http.aclose()

    # ---------- информация о боте и команды ----------

    async def get_me(self) -> dict:
        return await self._request("GET", "/me", timeout=20)

    async def set_commands(self):
        """PATCH /me/commands. Кириллицу API принимает не везде — есть латинский
        запасной набор, handlers понимает оба варианта."""
        # при выключенных картинках не удаляем строки из BOT_COMMANDS, а просто
        # не регистрируем их: иначе пользователь тыкает в команду без результата
        hidden = () if config.IMAGE_ENABLED else ("картинка", "image")

        def payload(rows):
            return {"commands": [{"name": n, "description": d}
                                 for n, d in rows if n not in hidden]}

        try:
            data = await self._request("PATCH", "/me/commands", json_body=payload(BOT_COMMANDS))
        except MaxApiError as exc:
            if exc.status not in (400, 422):
                raise
            log.warning("MAX не принял кириллические команды (%s), ставлю латиницу", exc)
            data = await self._request("PATCH", "/me/commands",
                                       json_body=payload(BOT_COMMANDS_LATIN))
        return data.get("commands") or []

    # ---------- long polling ----------

    async def get_updates(self, marker=None, timeout: int | None = None):
        timeout = config.MAX_POLL_TIMEOUT if timeout is None else timeout
        params = {
            "timeout": timeout,
            "limit": 100,
            "types": "message_created,message_callback,bot_started",
        }
        if marker is not None:
            params["marker"] = marker
        # read-таймаут должен быть больше timeout long polling
        data = await self._request("GET", "/updates", params=params,
                                   timeout=timeout + 30)
        return data.get("updates") or [], data.get("marker")

    # ---------- отправка ----------

    def keyboard(self, rows) -> list:
        """rows = [[(текст, payload_dict), ...], ...] → inline-кнопки MAX."""
        return [[{"type": "callback", "text": t,
                  "payload": json.dumps(p, ensure_ascii=False)} for t, p in row]
                for row in rows]

    def miniapp_button(self, payload="files"):
        if not config.MAX_WEBAPP_BOT:
            return None
        return {"type": "open_app", "text": "Открыть мини-приложение",
                "web_app": config.MAX_WEBAPP_BOT, "payload": payload}

    async def _send_message(self, chat_id, body: dict, *, retry_attachment: bool = False):
        params = _decode_address(chat_id)
        attempts = 6 if retry_attachment else 1
        delay = 0.7
        for attempt in range(attempts):
            try:
                return await self._request("POST", "/messages", params=params, json_body=body)
            except MaxApiError as exc:
                if (retry_attachment and exc.code == "attachment.not.ready"
                        and attempt < attempts - 1):
                    log.info("вложение ещё обрабатывается, повтор через %.1f c", delay)
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, 6.0)
                    continue
                raise

    async def send_text(self, chat_id, text: str, keyboard: list | None = None,
                        keyboard_last: bool = False, return_id: bool = False):
        # Отчёт агента о прочитанных страницах нужен для отладки, в MAX его не показываем.
        text = text.rsplit("\n\n---\nПрочитано:", 1)[0].rstrip()
        parts = _split_long(text)
        first_id = None
        for index, part in enumerate(parts):
            last = index == len(parts) - 1
            body: dict = {"text": part, "format": "markdown"}
            if keyboard and (last if keyboard_last else index == 0):
                body["attachments"] = [{"type": "inline_keyboard",
                                        "payload": {"buttons": keyboard}}]
            try:
                response = await self._send_message(chat_id, body)
            except MaxApiError as exc:
                # Некоторые конструкции markdown MAX не переваривает; текст
                # важнее форматирования — повторяем без разметки.
                if exc.status == 400 and body.get("format"):
                    log.warning("MAX отклонил markdown (%s) — отправляю простым текстом", exc)
                    body.pop("format", None)
                    response = await self._send_message(chat_id, body)
                else:
                    raise
            if first_id is None and return_id:
                first_id = (((response or {}).get("message") or {}).get("body") or {}).get("mid")
            if not last:
                await asyncio.sleep(0.55)   # не больше 2 сообщений в секунду
        return first_id

    async def edit_text(self, chat_id, message_id, text: str, keyboard: list | None = None,
                        notify: bool = False):
        """PUT /messages — обновляет сообщение бота. Прогресс идёт без push,
        а финальный итог — с notify=True, чтобы чат не заваливало, но результат
        не потерялся."""
        body: dict = {"text": text, "notify": notify}
        if keyboard:
            body["attachments"] = [{"type": "inline_keyboard",
                                    "payload": {"buttons": keyboard}}]
        data = await self._request("PUT", "/messages", params={"message_id": str(message_id)},
                                   json_body=body, timeout=30)
        if data.get("success") is False:
            raise MaxApiError(f"MAX не отредактировал сообщение: {data.get('message') or 'ошибка'}")

    async def delete_message(self, message_id):
        data = await self._request("DELETE", "/messages", params={"message_id": str(message_id)},
                                   timeout=30)
        if data.get("success") is False:
            raise MaxApiError(f"MAX не удалил сообщение: {data.get('message') or 'ошибка'}")

    async def _upload(self, kind: str, filename: str, data: bytes, mime: str) -> str:
        endpoint = await self._request("POST", "/uploads", params={"type": kind}, timeout=30)
        url = endpoint.get("url")
        if not url:
            raise RuntimeError(f"MAX /uploads не вернул URL для {kind}")
        files = {"data": (filename, data, mime or "application/octet-stream")}
        r = await self.http.post(url, files=files, timeout=120)
        if r.status_code in (401, 403) and _token_allowed(url):
            # часть CDN-хостов просит токен, часть ссылок уже подписана;
            # на чужой хост токен не отправляем никогда
            r = await self.http.post(url, files=files, timeout=120,
                                     headers={"Authorization": self.token})
        if r.status_code >= 400:
            code, message = self._error_body(r)
            raise MaxApiError(f"MAX загрузка {kind}: {message} (HTTP {r.status_code})",
                              r.status_code, code)
        try:
            payload = r.json()
        except ValueError:
            payload = {}
        token = _find_token(payload) or endpoint.get("token")
        if not token:
            raise RuntimeError(f"MAX загрузка {kind}: сервер не вернул token")
        return token

    async def send_image(self, chat_id, path: str, caption: str = ""):
        file_path = Path(path)
        data = file_path.read_bytes()
        if len(data) > MAX_IMAGE_BYTES:
            raise RuntimeError(f"картинка больше {MAX_IMAGE_BYTES // 1048576} МБ")
        mime = _mime_for(file_path.name, "image/png")
        token = await self._upload("image", file_path.name, data, mime)
        body = {
            "text": (caption or "")[:TEXT_LIMIT],
            "attachments": [{"type": "image", "payload": {"token": token}}],
        }
        return await self._send_message(chat_id, body, retry_attachment=True)

    async def answer_callback(self, callback_id):
        if not callback_id:
            return
        try:  # снимает «часики»; если сервер уже закрыл колбэк, это не критично
            await self._request("POST", "/answers", params={"callback_id": callback_id},
                                json_body={})
        except Exception:
            pass

    # ---------- скачивание ----------

    async def download(self, url: str, max_bytes: int | None = None) -> bytes:
        """Вложения приходят подписанной ссылкой; токен к CDN без нужды не
        отправляем — только если сервер явно ответил 401/403 и это хост MAX."""
        if not url:
            raise RuntimeError("пустая ссылка на вложение")
        limit = max_bytes or MAX_ATTACHMENT_BYTES
        headers = {}
        for attempt in range(2):
            buf = bytearray()
            async with self.http.stream("GET", url, headers=headers,
                                        timeout=httpx.Timeout(120.0, connect=20.0)) as r:
                if r.status_code in (401, 403) and attempt == 0 and _token_allowed(url):
                    headers = {"Authorization": self.token}
                    continue
                if r.status_code >= 400:
                    raise RuntimeError(f"не смог скачать вложение (HTTP {r.status_code})")
                async for chunk in r.aiter_bytes(1 << 16):
                    buf += chunk
                    if len(buf) > limit:
                        raise RuntimeError(
                            f"вложение больше {limit // 1048576} МБ — не качаю")
            if not buf:
                raise RuntimeError("вложение пришло пустым")
            return bytes(buf)
        raise RuntimeError("не смог скачать вложение")

    # ---------- апдейты → нейтральные события ----------

    def to_event(self, update: dict) -> dict | None:
        kind = update.get("update_type")
        if kind == "message_created":
            return self._message_event(update.get("message") or {})
        if kind == "message_callback":
            return self._callback_event(update)
        if kind == "bot_started":
            user = update.get("user") or {}
            user_id = user.get("user_id")
            if user_id is None:
                return None
            chat_id = update.get("chat_id")
            address = f"c:{chat_id}" if chat_id is not None else f"u:{user_id}"
            return {"kind": "message", "user_id": int(user_id), "chat_id": address,
                    "text": "/start", "attachments": []}
        return None

    def _message_event(self, msg: dict) -> dict | None:
        sender = msg.get("sender") or {}
        user_id = sender.get("user_id")
        if user_id is None or sender.get("is_bot"):
            return None
        body = msg.get("body") or {}
        text = (body.get("text") or "").strip()
        recipient = msg.get("recipient") or {}
        chat_id = recipient.get("chat_id")
        address = f"c:{chat_id}" if chat_id is not None else f"u:{user_id}"

        mid = body.get("mid")
        timestamp = msg.get("timestamp")
        if timestamp:
            stamp = datetime.fromtimestamp(timestamp / 1000).strftime("%d.%m %H:%M")
        else:
            stamp = datetime.now().strftime("%d.%m %H:%M")
        # подпись-вопрос («что тут главное?») — это вопрос, а не название материала:
        # handlers ответит на неё после индексации
        caption_is_title = 0 < len(text) <= 80 and not text.rstrip().endswith("?")
        image_title = " ".join(text.split())[:80] if caption_is_title \
            else f"фото {stamp}" + (f" · {mid}" if mid is not None else "")

        attachments = []
        for attachment in body.get("attachments") or []:
            kind = attachment.get("type")
            payload = attachment.get("payload") or {}
            if kind == "image":
                url = payload.get("url")
                if not url:
                    continue
                mime = _mime_for(urlparse(url).path, "image/jpeg")
                if not mime.startswith("image/"):
                    mime = "image/jpeg"
                # Фото одной пачки получают одинаковое имя: их OCR-текст ляжет
                # в один документ, а не в десяток отдельных страниц.
                attachments.append({"name": image_title, "handle": url,
                                    "mime": mime, "size": 0})
            elif kind == "file":
                url = payload.get("url")
                if not url:
                    continue
                name = attachment.get("filename") or "материал"
                attachments.append({
                    "name": name,
                    "handle": url,
                    "mime": _mime_for(name),
                    "size": attachment.get("size") or 0,
                })
            elif kind in ("video", "audio", "sticker", "contact", "location", "share"):
                # Отдаём в handlers как неподдерживаемое вложение: человек получит
                # понятный отказ, а не тишину.
                fake = {
                    "video": ("видео", "video/mp4"),
                    "audio": ("аудио", "audio/mpeg"),
                    "sticker": ("стикер", "application/x-sticker"),
                    "contact": ("контакт", "application/x-contact"),
                    "location": ("геолокация", "application/x-location"),
                    "share": ("ссылка", "application/x-share"),
                }[kind]
                attachments.append({"name": fake[0], "handle": "", "mime": fake[1], "size": 0})
        return {"kind": "message", "user_id": int(user_id), "chat_id": address,
                "text": text, "attachments": attachments}

    def _callback_event(self, update: dict) -> dict | None:
        callback = update.get("callback") or {}
        user = callback.get("user") or {}
        user_id = user.get("user_id")
        if user_id is None:
            return None
        message = update.get("message") or {}
        recipient = message.get("recipient") or {}
        chat_id = recipient.get("chat_id")
        address = f"c:{chat_id}" if chat_id is not None else f"u:{user_id}"
        raw_payload = callback.get("payload") or "{}"
        try:
            data = json.loads(raw_payload)
        except (TypeError, json.JSONDecodeError):
            data = {"action": str(raw_payload)}
        if not isinstance(data, dict):
            data = {"action": str(data)}
        return {"kind": "callback", "user_id": int(user_id), "chat_id": address,
                "callback_id": callback.get("callback_id"), "data": data}
