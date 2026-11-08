"""HTTP server shared by main.py and the interactive console."""
import asyncio
from contextlib import asynccontextmanager
import socket
import secrets
import time

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

import config
from .api import router
from admin_setup import get_admin_id

WEB_DIST = config.BASE_DIR / "webapp" / "dist"


def enable_local_access(app, *, output=print):
    """Opt-in, random process-local credential; never enabled by a browser request."""
    token = secrets.token_urlsafe(32)
    app.state.local_access = {"token": token, "user_id": get_admin_id() or config.CONSOLE_UID,
                              "expires": time.time() + 8 * 3600}
    app.state.local_url = f"http://127.0.0.1:{config.HTTP_PORT}/app/#LocalToken={token}"
    output("\nЛокальная админка (ссылка действует 8 часов, до перезапуска):")
    output(app.state.local_url)
    output("Открывайте эту ссылку только на этом компьютере.\n")


async def finish_tasks(app):
    app.state.local_access = None
    app.state.local_url = None
    tasks = list(app.state.study_tasks.values())
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


@asynccontextmanager
async def passive_lifespan(app):
    try:
        yield
    finally:
        await finish_tasks(app)


def create_http_app(*, lifespan=None, bot_client=None):
    app = FastAPI(title="МаксИИмка", lifespan=lifespan or passive_lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.state.bot_client = bot_client
    app.state.study_tasks = {}
    app.state.check_lock = asyncio.Lock()
    app.state.local_access = None
    app.include_router(router)

    @app.middleware("http")
    async def headers(request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request, exc):
        return JSONResponse({"detail": "Проверьте поля запроса."}, status_code=422)

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.get("/")
    async def index():
        return RedirectResponse("/app/")

    if WEB_DIST.is_dir() and (WEB_DIST / "index.html").is_file():
        app.mount("/app", StaticFiles(directory=WEB_DIST, html=True), name="miniapp")
    else:
        @app.get("/app/")
        async def not_built():
            return JSONResponse({"detail": "Интерфейс ещё не собран. Выполните build-miniapp.cmd."},
                                status_code=503)
    return app


async def start_console_server(bot_client=None):
    import uvicorn
    # Заранее связываем сокет: занятый порт не должен завершать консоль через SystemExit.
    host = "127.0.0.1" if config.LOCAL_MINIAPP else config.HTTP_HOST
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind((host, config.HTTP_PORT))
        sock.listen(128)
        sock.setblocking(False)
    except OSError:
        sock.close()
        raise
    app = create_http_app(bot_client=bot_client)
    if config.LOCAL_MINIAPP:
        enable_local_access(app)
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning", access_log=False,
                                         host=host, port=config.HTTP_PORT, proxy_headers=False))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    return server, task, sock
