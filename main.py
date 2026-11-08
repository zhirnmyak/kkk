"""Точка входа: FastAPI + long polling (не требует публичного URL) + планировщик.

Режим приёма апдейтов — UPDATES_MODE в .env:
• polling (по умолчанию) — bot/runtime.py: marker переживает перезапуск, 401
  останавливает опрос, задачи-обработчики не теряются;
• webhook — только POST /webhook, и только с секретом WEBHOOK_SECRET
  (MAX: заголовок X-Max-Bot-Api-Secret, Telegram: X-Telegram-Bot-Api-Secret-Token).
  Без секрета или с чужим секретом запрос отклоняется 403: раньше любой, кто
  достучался до порта, мог прислать поддельный апдейт от имени любого ученика.
"""
import hmac
import asyncio
import logging
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

import config
import runtime_settings
import gateway_manager
from bot import create_client, handlers
from bot.runtime import BotRuntime
from miniapp.server import create_http_app, finish_tasks, enable_local_access
import scheduler

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("neurotutor")
# httpx логирует полный URL запроса, а в Telegram Bot API токен — часть URL.
# Без этого строка «POST …/bot8299…:AAFE…/getUpdates» уезжает в лог и в скриншоты.
logging.getLogger("httpx").setLevel(logging.WARNING)

# Один клиент на процесс: polling и webhook делят его httpx-соединения.
client = None
runtime: BotRuntime | None = None

SECRET_HEADERS = {
    "max": "X-Max-Bot-Api-Secret",
    "tg": "X-Telegram-Bot-Api-Secret-Token",
}


def webhook_authorized(headers) -> bool:
    """Запрос пришёл от мессенджера, а не от постороннего: секрет обязателен."""
    secret = config.WEBHOOK_SECRET
    if not secret:
        return False
    header = SECRET_HEADERS.get(config.MESSENGER)
    got = (headers.get(header) if header else None) or ""
    return hmac.compare_digest(got.encode("utf-8"), secret.encode("utf-8"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    global client, runtime
    settings = await asyncio.to_thread(runtime_settings.initialize)
    if config.LOCAL_MINIAPP:
        enable_local_access(app)
    await asyncio.to_thread(gateway_manager.ensure, settings)
    client = None
    runtime = None
    bot_enabled = config.MESSENGER != "max" or config.MAX_BOT_ENABLED
    if bot_enabled:
        client = create_client()
    app.state.bot_client = client
    if not bot_enabled:
        log.info("MAX-бот выключен: HTTP-интерфейс работает отдельно")
    elif config.UPDATES_MODE == "webhook":
        if not config.WEBHOOK_SECRET:
            log.error("UPDATES_MODE=webhook, но WEBHOOK_SECRET пуст — /webhook будет "
                      "отклонять все запросы. Задай секрет и укажи его в подписке.")
        if hasattr(client, "set_commands"):   # меню команд — косметика, на старте
            try:                              # оно не имеет права ронять приложение
                await client.set_commands()
            except Exception:
                log.warning("Меню команд не зарегистрировано — бот работает и так")
        log.info("Режим webhook, мессенджер: %s", config.MESSENGER)
    else:
        # меню команд, marker и повторы — внутри BotRuntime
        runtime = BotRuntime(client)
        await runtime.start()
        log.info("Long polling запущен, мессенджер: %s", config.MESSENGER)
    if bot_enabled:
        scheduler.start()
    try:
        yield
    finally:
        await finish_tasks(app)
        if bot_enabled:
            scheduler.sched.shutdown(wait=False)
        if runtime:
            await runtime.stop()              # гасит обработчики и закрывает клиента
        elif hasattr(client, "close"):
            await client.close()
        gateway_manager.stop()


app = create_http_app(lifespan=lifespan)


@app.post("/webhook")  # альтернатива polling, если есть публичный HTTPS
async def webhook(req: Request):
    if config.UPDATES_MODE != "webhook":
        return JSONResponse({"ok": False, "error": "webhook disabled"}, status_code=404)
    if not webhook_authorized(req.headers):
        log.warning("webhook: отклонён запрос без верного секрета (%s)",
                    req.client.host if req.client else "?")
        return JSONResponse({"ok": False}, status_code=403)
    try:
        update = await req.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "bad json"}, status_code=400)
    if not isinstance(update, dict):
        return JSONResponse({"ok": False, "error": "bad update"}, status_code=400)
    handlers.spawn_update(client, update)   # задача держится по ссылке до конца
    return {"ok": True}


if __name__ == "__main__":
    # по умолчанию только localhost: наружу — через обратный прокси с HTTPS
    host = "127.0.0.1" if config.LOCAL_MINIAPP else config.HTTP_HOST
    uvicorn.run("main:app", host=host, port=config.HTTP_PORT,
                proxy_headers=not config.LOCAL_MINIAPP)
