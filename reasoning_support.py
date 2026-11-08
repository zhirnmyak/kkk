"""Возможности текущего API/модели. Ключи и ответы провайдера не сохраняются на диск."""
import hashlib
import capability_cache
import model_checks
import time

_cache = {}


def key(url, api_key, model):
    return (url.rstrip("/"), hashlib.sha256(api_key.encode()).hexdigest(), model)


def get(url, api_key, model):
    cache_key = key(url, api_key, model)
    return capability_cache.memory_get(_cache, cache_key) or capability_cache.get("reasoning", cache_key)


def remember(url, api_key, model, supported, efforts=(), source="probe"):
    model_checks.check_cancelled()
    info = {"supported": supported, "efforts": tuple(efforts), "source": source, "checked_at": time.time()}
    _cache[key(url, api_key, model)] = info
    capability_cache.put("reasoning", key(url, api_key, model), info)
    return info


def rejected(message):
    message = message.lower()
    return "reasoning_effort" in message and any(marker in message for marker in (
        "unrecognized request argument", "unknown parameter", "unsupported parameter",
        "not supported", "unrecognized parameter"))
