"""Проверка приёма изображений для конкретного подключения и модели."""
from reasoning_support import key
import capability_cache
import model_checks
import time

_cache = {}


def get(url, api_key, model):
    cache_key = key(url, api_key, model)
    return capability_cache.memory_get(_cache, cache_key) or capability_cache.get("vision", cache_key)


def remember(url, api_key, model, supported, source="probe"):
    model_checks.check_cancelled()
    info = {"supported": supported, "source": source, "checked_at": time.time()}
    _cache[key(url, api_key, model)] = info
    capability_cache.put("vision", key(url, api_key, model), info)
    return info


def read_metadata(url, api_key, item):
    modalities = item.get("input_modalities")
    architecture = item.get("architecture")
    if modalities is None and isinstance(architecture, dict):
        modalities = architecture.get("input_modalities")
    capabilities = item.get("capabilities")
    if isinstance(modalities, list) and modalities:
        remember(url, api_key, item["id"], "image" in modalities, "metadata")
    elif isinstance(capabilities, dict) and isinstance(capabilities.get("vision"), bool):
        remember(url, api_key, item["id"], capabilities["vision"], "metadata")


def rejected(message):
    text = message.lower()
    return any(term in text for term in ("image", "vision", "multimodal")) and any(
        term in text for term in ("not support", "unsupported", "does not support",
                                  "only support text", "only supports text", "not allowed"))


def probe_image():
    """Код есть только на изображении: текстовый шлюз не сможет его угадать."""
    import base64
    import secrets
    from io import BytesIO
    from PIL import Image, ImageDraw, ImageFont
    code = secrets.token_hex(3).upper()
    image = Image.new("RGB", (240, 80), "white")
    ImageDraw.Draw(image).text((20, 20), code, fill="black", font=ImageFont.load_default(size=36))
    out = BytesIO()
    image.save(out, "PNG")
    return code, "data:image/png;base64," + base64.b64encode(out.getvalue()).decode()
