"""Клиент Telegram Bot API — второй транспорт поверх того же интерфейса, что и
bot/max_client.py, поэтому handlers.py работает с любым из них.

Назначение: проверить логику тьютора (RAG, карточки, тесты, план) в живом чате,
не дожидаясь токена Max.

Что специфично именно для Telegram и учтено здесь:
• callback_data — не больше 64 БАЙТ. JSON с кириллицей не влезает (кириллица в
  UTF-8 по 2 байта), поэтому payload сжимается в "action:key:value" и обратно;
• parse_mode требует разметки: MAX принимает «format: markdown», Telegram — HTML.
  Без экранирования <>& ответ LLM с «<...>» или «**тег**» роняет sendMessage в 400;
• жёсткий лимит сообщения 4096 символов — режем по границам строк, а не поперёк слова;
• вложения отдаются не ссылкой, а file_id → getFile → file_path;
• на 429 Telegram присылает retry_after — без паузы напоминания молча теряются;
• имена команд только латиница [a-zA-Z0-9_], кириллические /тест /карточки
  handlers.py понимает как обычный текст, но в меню Bot API их не зарегистрировать.
"""
import asyncio, html, json, logging, re
from datetime import datetime
from pathlib import Path
import httpx
import config

log = logging.getLogger("neurotutor.tg")

TEXT_LIMIT = 3900        # запас от жёстких 4096
CALLBACK_LIMIT = 64      # байт


class TgApiError(RuntimeError):
    """Ошибка Bot API с кодом: 401 (токен отозван) runtime считает фатальной."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


def encode_payload(payload: dict) -> str:
    """{"action": "card_good", "id": 12} → "card_good:id:12" («card_good:id:12» = 14 байт)."""
    parts = [str(payload.get("action", ""))]
    for k, v in payload.items():
        if k != "action":
            parts += [str(k), str(v)]
    s = ":".join(parts)
    raw = s.encode()
    if len(raw) > CALLBACK_LIMIT:
        log.warning("callback_data не влезает в %s байт: %s", CALLBACK_LIMIT, s)
        # режем БАЙТЫ, а не символы: 64 кириллических символа — это 128 байт,
        # и Telegram отклонял всю клавиатуру (BUTTON_DATA_INVALID)
        s = raw[:CALLBACK_LIMIT].decode("utf-8", errors="ignore")
    return s


def decode_payload(data: str) -> dict:
    parts = (data or "").split(":")
    out = {"action": parts[0]}
    for k, v in zip(parts[1::2], parts[2::2]):
        out[k] = int(v) if v.isdigit() else v
    return out


def to_html(text: str) -> str:
    """Экранируем всё, что Telegram принял бы за тег; из markdown оставляем
    только однозначные **полужирный** и `код`."""
    out = html.escape(text or "", quote=False)
    out = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", out, flags=re.S)
    return re.sub(r"`([^`\n]+)`", r"<code>\1</code>", out)


def _split(text: str) -> list[str]:
    text = text or ""
    parts, cur = [], ""
    for line in text.splitlines(keepends=True):
        if len(cur) + len(line) > TEXT_LIMIT:
            parts.append(cur)
            cur = ""
        cur += line
        while len(cur) > TEXT_LIMIT:      # одна строка длиннее лимита
            parts.append(cur[:TEXT_LIMIT])
            cur = cur[TEXT_LIMIT:]
    if cur.strip():
        parts.append(cur)
    return parts or [""]


class TgClient:
    def __init__(self, token: str | None = None):
        # токен читаем при вызове, а не в сигнатуре: значение по умолчанию
        # привязалось бы в момент импорта модуля
        self.token = token or config.TG_TOKEN
        if not self.token:
            raise RuntimeError(
                "TG_BOT_TOKEN пуст. Создайте бота у @BotFather (/newbot), скопируйте "
                "токен в .env (TG_BOT_TOKEN=...) и поставьте MESSENGER=tg.")
        self.base = f"{config.TG_API}/bot{self.token}"
        # read=90 c — long polling сидит с timeout=30; connect=15 c — иначе при
        # заблокированном api.telegram.org каждый прогон вешает старт на полторы минуты
        self.http = httpx.AsyncClient(
            timeout=httpx.Timeout(90.0, connect=15.0), proxy=config.TG_PROXY or None)

    async def close(self):
        await self.http.aclose()

    @staticmethod
    def _friendly(exc: Exception) -> Exception:
        """Сетевую проблему превращаем в одну понятную строку: иначе polling_loop
        печатает простыню трейсбеков каждые несколько секунд."""
        if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout)):
            return RuntimeError(
                f"Не достучаться до {config.TG_API}. Если api.telegram.org блокируется, "
                "нужен обычный HTTP/SOCKS5-прокси от VPN (TG_PROXY=http://127.0.0.1:порт "
                "или socks5://127.0.0.1:порт). MTProto-прокси для клиента Telegram не "
                "подойдёт — он не проксирует HTTPS Bot API.")
        if isinstance(exc, httpx.TransportError):
            return RuntimeError(f"соединение с {config.TG_API} оборвалось "
                                f"({type(exc).__name__}) — переподключаемся")
        return exc

    async def _call(self, method: str, **payload):
        for _ in range(5):
            try:
                r = await self.http.post(f"{self.base}/{method}", json=payload)
            except Exception as exc:
                raise self._friendly(exc) from exc
            if r.status_code == 429:
                try:
                    wait = int(r.json()["parameters"]["retry_after"])
                except Exception:
                    wait = 3
                log.warning("лимит Telegram, пауза %s c", wait)
                await asyncio.sleep(wait)
                continue
            try:
                data = r.json()
            except ValueError:   # прокси/воркер вернул HTML вместо JSON
                raise TgApiError(f"Telegram {method}: не-JSON (HTTP {r.status_code})",
                                 r.status_code) from None
            if data.get("ok"):
                return data["result"]
            raise TgApiError(f"Telegram {method}: {data.get('description')}",
                             data.get("error_code") or r.status_code)
        raise RuntimeError(f"Telegram {method}: не вышли из 429")

    async def get_me(self) -> dict:
        """getMe — проверка токена на старте (нужна bot/runtime.py)."""
        return await self._call("getMe")

    async def get_updates(self, offset=None, timeout: int | None = None):
        """Long polling — публичный URL не нужен. Возвращаем (updates, следующий offset).

        timeout по умолчанию читаем при вызове (в сигнатуре он привязался бы к
        значению на момент импорта). Каждое ожидание = 1 запрос к воркеру-ретранслятору,
        а у бесплатного Cloudflare Workers лимит 100 тыс. запросов/месяц: при 30 с
        это ~86 тыс. в месяц. 50-60 с срежут расход вдвое, Telegram столько и терпит."""
        if timeout is None:
            timeout = config.TG_POLL_TIMEOUT
        # edited_message не берём: правка старого сообщения приходила как новое,
        # и бот второй раз отвечал на вопрос или заново индексировал файл
        params = {"timeout": timeout, "limit": 50,
                  "allowed_updates": json.dumps(["message", "callback_query"])}
        if offset:
            params["offset"] = offset
        try:
            r = await self.http.get(f"{self.base}/getUpdates", params=params)
        except Exception as exc:
            raise self._friendly(exc) from exc
        try:
            data = r.json()
        except ValueError:  # DPI или прозрачный прокси мог вернуть HTML вместо JSON
            raise RuntimeError(f"getUpdates вернул не-JSON (HTTP {r.status_code})") from None
        if not data.get("ok"):
            raise TgApiError(f"Telegram getUpdates: {data.get('description')}",
                             data.get("error_code") or r.status_code)
        updates = data["result"]
        if not updates:
            return [], offset
        return updates, max(u["update_id"] for u in updates) + 1

    def keyboard(self, rows) -> list:
        """rows = [[(текст, payload_dict), ...], ...] → inline_keyboard Telegram."""
        return [[{"text": t, "callback_data": encode_payload(p)} for t, p in row]
                for row in rows]

    async def send_text(self, chat_id, text: str, keyboard: list | None = None,
                        keyboard_last: bool = False, return_id: bool = False):
        parts = _split(text)
        first_id = None
        for index, part in enumerate(parts):
            last = index == len(parts) - 1
            body: dict = {"chat_id": chat_id, "text": to_html(part),
                          "parse_mode": "HTML", "disable_web_page_preview": True}
            if keyboard and (last if keyboard_last else index == 0):
                body["reply_markup"] = {"inline_keyboard": keyboard}
            result = await self._call("sendMessage", **body)
            if first_id is None and return_id:
                first_id = (result or {}).get("message_id")
        return first_id

    async def send_image(self, chat_id, path: str, caption: str = ""):
        with open(path, "rb") as fh:
            try:
                r = await self.http.post(
                    f"{self.base}/sendPhoto",
                    data={"chat_id": str(chat_id), "caption": (caption or "")[:1000]},
                    files={"photo": (Path(path).name, fh, "image/png")},
                )
            except Exception as exc:
                raise self._friendly(exc) from exc
        data = r.json()
        if not data.get("ok"):
            raise RuntimeError(f"Telegram sendPhoto: {data.get('description')}")
        return data.get("result")

    async def answer_callback(self, callback_id):
        if not callback_id:
            return
        try:  # снимает «часики» на кнопке; не критично, если уже снялись
            await self._call("answerCallbackQuery", callback_query_id=callback_id)
        except Exception:
            pass

    async def download(self, file_id: str, attempts: int = 3,
                       max_bytes: int | None = None) -> bytes:
        path = (await self._call("getFile", file_id=file_id)).get("file_path")
        # Скачивание живёт НЕ внутри /bot<токен>/, а по отдельному пути:
        #   https://api.telegram.org/file/bot<токен>/<file_path>
        # Вариант «/bot<токен>/file/…» Telegram отвечает 404.
        url = f"{config.TG_API}/file/bot{self.token}/{path}"
        # read — таймаут на ОДНУ порцию данных, а не на файл целиком: поток, из
        # которого не пришло ни байта, надо замечать за минуту, а не за четыре
        timeout = httpx.Timeout(config.TG_FILE_TIMEOUT, connect=15.0, read=60.0)
        last: Exception | None = None
        for attempt in range(1, attempts + 1):
            buf, mark = bytearray(), 1 << 18
            try:
                async with self.http.stream("GET", url, timeout=timeout) as r:
                    r.raise_for_status()
                    async for chunk in r.aiter_bytes(1 << 16):
                        buf += chunk
                        if len(buf) >= mark:
                            log.info("  скачано %d КБ…", len(buf) // 1024)
                            mark += 1 << 18
                if max_bytes and len(buf) > max_bytes:
                    raise RuntimeError(f"файл больше {max_bytes // 1048576} МБ — не качаю")
                log.info("скачан файл %s: %.2f МБ", path, len(buf) / 1048576)
                return bytes(buf)
            except httpx.TransportError as exc:
                # обрыв на середине или тишина с самого начала — обычное дело для
                # файлового сервера Telegram и стриминга через прокси; без повторов
                # и без этих строк выглядит как зависший бот
                last = exc
                log.warning("скачивание %s оборвалось на %d КБ (%s), попытка %d/%d",
                            path, len(buf) // 1024, type(exc).__name__, attempt, attempts)
                await asyncio.sleep(3 * attempt)
        raise RuntimeError(f"не смог скачать файл за {attempts} попыток — {last}")

    async def set_commands(self):
        """Регистрирует меню команд. Латинские алиасы уже понятны handlers.py
        (/cards=/карточки, /quiz=/тест, /review=/повторение и т.д.)."""
        await self._call("setMyCommands", commands=[
            {"command": c, "description": d} for c, d in (
                ("start", "Приветствие и список возможностей"),
                ("upload", "Загрузить PDF или конспект"),
                ("cards", "Флеш-карточки по теме"),
                ("review", "Повторить карточки (Лейтнер)"),
                ("quiz", "Тест с разбором ошибок"),
                ("summary", "Выжимка последнего документа"),
                ("plan", "План подготовки к экзамену"),
                ("stats", "Мой прогресс"),
                ("reset", "Сбросить состояние"))])

    def to_event(self, update: dict) -> dict | None:
        """JSON апдейта Telegram → нейтральное событие того же вида, что у MaxClient."""
        cb = update.get("callback_query")
        if cb:
            return {"kind": "callback",
                    "user_id": (cb.get("from") or {}).get("id"),
                    "chat_id": ((cb.get("message") or {}).get("chat") or {}).get("id"),
                    "callback_id": cb.get("id"),
                    "data": decode_payload(cb.get("data") or "")}

        # edited_message сознательно игнорируем (см. get_updates)
        msg = update.get("message")
        if not msg or (msg.get("from") or {}).get("is_bot"):
            return None
        atts = []
        if doc := msg.get("document"):
            # file_size приходит в самом апдейте — по нему отсекаем файлы, которые
            # getFile всё равно не отдаст (лимит бота 5 МБ), не начиная качать
            atts.append({"name": doc.get("file_name") or "материал.pdf",
                         "handle": doc["file_id"],
                         "mime": doc.get("mime_type") or "",
                         "size": doc.get("file_size") or 0})
        if photo := msg.get("photo"):
            # Имя: подпись к сообщению, иначе метка времени + message_id.
            # Хвост file_id как уникальность не годится: у разных фото одного
            # альбома Telegram даёт id с одинаковым окончанием, и три страницы
            # легли в базу под одним именем. message_id в пределах чата различен.
            caption = (msg.get("caption") or "").strip()
            # подпись-вопрос — это вопрос к материалу, а не его название
            if caption.endswith("?") or len(caption) > 80:
                caption = ""
            atts.append({"name": caption
                                or f"фото {datetime.now():%d.%m %H:%M} · №{msg.get('message_id')}",
                         "handle": photo[-1]["file_id"],
                         "mime": "image/jpeg",
                         "size": photo[-1].get("file_size") or 0})
        return {"kind": "message",
                "user_id": (msg.get("from") or {}).get("id"),
                "chat_id": (msg.get("chat") or {}).get("id"),
                "text": (msg.get("text") or msg.get("caption") or "").strip(),
                "attachments": atts}
