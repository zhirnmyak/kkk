"""Генерация картинок по текстовому описанию.

Два бэкенда, выбираются по адресу LLM_BASE_URL (или явно через IMAGE_BACKEND):

• cloudflare — нативный POST /accounts/<id>/ai/run/@cf/black-forest-labs/flux-1-schnell.
  Нужен, потому что у Workers AI нет OpenAI-совместимого /images/generations:
  запрос туда отвечает 400 code 7000 «No route for that URI».
• gateway — /v1/images/generations локального openai-gateway (Node), как задумано
  в архитектуре напарника; используется, когда база не Cloudflare.
"""
import base64
import logging
import time
import uuid
from pathlib import Path

import httpx

import config
import llm

log = logging.getLogger("neurotutor.imagegen")
_client = None   # OpenAI-совместимый клиент создаётся лениво: при cloudflare-бэкенде он не нужен

CF_MARK = "api.cloudflare.com"


def _backend() -> str:
    forced = (config.IMAGE_BACKEND or "").strip().lower()
    if forced:
        return forced
    return "cloudflare" if CF_MARK in (config.LLM_BASE_URL or "") else "gateway"


def _cf_root() -> str:
    """…/accounts/<id>/ai — нативный префикс Workers AI (без /v1)."""
    base = (config.VISION_BASE_URL or config.LLM_BASE_URL).rstrip("/")
    return base[:-3] if base.endswith("/v1") else base


async def _to_english(prompt: str) -> str:
    """flux обучен на английских подписях: «мужик поймал рыбу» на русском он
    превращает в случайную картинку. Просим LLM перевести коротко и предметно."""
    if config.LLM_MOCK or not (config.LLM_API_KEY or config.VISION_API_KEY):
        return prompt
    try:
        out = await llm.chat(
            "Переведи запрос к генератору картинок на английский: 5–15 слов, "
            "только существительные, действия и детали сцены, без пояснений и кавычек. "
            "Верни одной строкой.", prompt, temperature=0.2, max_tokens=60)
        en = (out or "").strip().splitlines()[0].strip().strip('"') if out else ""
        return en or prompt
    except Exception as exc:
        log.warning("перевод запроса не удался (%s) — шлю как есть", type(exc).__name__)
        return prompt


async def _generate_cloudflare(prompt: str) -> bytes:
    key = config.VISION_API_KEY or config.LLM_API_KEY
    payload = {"prompt": await _to_english(prompt), "steps": config.IMAGE_STEPS}
    async with httpx.AsyncClient(timeout=config.IMAGE_TIMEOUT) as http:
        r = await http.post(f"{_cf_root()}/run/{config.IMAGE_MODEL}",
                            headers={"Authorization": f"Bearer {key}"}, json=payload)
    try:
        data = r.json()
    except ValueError:
        raise RuntimeError(f"модель вернула не-JSON (HTTP {r.status_code})")
    if not data.get("success"):
        err = (data.get("errors") or [{}])[0].get("message") or f"HTTP {r.status_code}"
        raise RuntimeError(f"Cloudflare AI: {err}")
    image = (data.get("result") or {}).get("image")
    if not image:
        raise RuntimeError("Cloudflare AI вернул пустое изображение")
    return base64.b64decode(image)


async def _generate_gateway(prompt: str, size: str) -> bytes:
    global _client
    if _client is None:
        from openai import AsyncOpenAI
        _client = AsyncOpenAI(base_url=config.LLM_BASE_URL,
                              api_key=config.LLM_API_KEY or "empty")
    request_client = _client.with_options(base_url=config.LLM_BASE_URL,
                                         api_key=config.LLM_API_KEY or "empty")
    resp = await request_client.images.generate(model=config.LLM_MODEL, prompt=prompt,
                                         size=size, n=1, response_format="b64_json")
    item = resp.data[0]
    if not item.b64_json:
        raise RuntimeError("шлюз вернул пустое изображение")
    return base64.b64decode(item.b64_json)


def _suffix(raw: bytes) -> str:
    """flux отдаёт JPEG, хотя расширение в имени было бы png — а по суффиксу
    max_client определяет MIME для вложения, так что берём формат из байтов."""
    if raw[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if raw[:4] == b"\x89PNG":
        return ".png"
    if raw[4:8] == b"WEBP":
        return ".webp"
    return ".png"


async def generate(prompt: str, size: str = "1024x1024") -> Path:
    started = time.monotonic()
    backend = _backend()
    raw = await (_generate_cloudflare(prompt) if backend == "cloudflare"
                 else _generate_gateway(prompt, size))
    out_dir = config.DATA_DIR / "images"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}{_suffix(raw)}"
    path.write_bytes(raw)
    log.info("картинка %s (%s, %.0f КБ) за %.1f c", path.name, backend,
             len(raw) / 1024, time.monotonic() - started)
    return path
