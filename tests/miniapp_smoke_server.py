"""Изолированный HTTP-сервер для сквозной проверки браузера. Данные временные."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests import TEST_ROOT
import asyncio
import json
import os
import socket

os.environ["MAX_ADMIN_ID"] = "8800099"
os.environ["MAX_BOT_TOKEN"] = "offline-test-token"
os.environ["MAX_BOT_ENABLED"] = "0"
os.environ["MAXIMKA_QODER_EXPLICIT"] = ""
os.environ["MAXIMKA_MODEL_EXPLICIT"] = ""

import config
import console_settings
import db
import rag
import uvicorn
from miniapp.server import create_http_app, enable_local_access

console_settings.SETTINGS_PATH = TEST_ROOT / "settings.json"
console_settings.MODEL_HINT_PATH = TEST_ROOT / "model.txt"
config.LLM_PROVIDER = "openai"
config.LLM_BASE_URL = "https://offline.example.test/v1"
config.LLM_API_KEY = "offline-original-secret"
config.LLM_MODEL = "offline-model"
db.touch_user(8800099, "u:8800099")
rag.add_document(8800099, "Локальный конспект.pdf",
                 pages=["Клетка хранит наследственную информацию в ядре. Этот материал читается из локального индекса."])
db.add_document_row(8800099, "Локальный конспект.pdf")


async def main():
    app = create_http_app()
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen(128)
    sock.setblocking(False)
    config.HTTP_PORT = sock.getsockname()[1]
    enable_local_access(app, output=lambda _: None)
    server = uvicorn.Server(uvicorn.Config(app, access_log=False, log_level="error", proxy_headers=False))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    while not server.started:
        if task.done():
            await task
            raise RuntimeError("HTTP-сервер не запустился")
        await asyncio.sleep(.02)
    print(json.dumps({"url": app.state.local_url}), flush=True)
    await asyncio.to_thread(sys.stdin.readline)
    server.should_exit = True
    await task
    sock.close()


if __name__ == "__main__":
    asyncio.run(main())
