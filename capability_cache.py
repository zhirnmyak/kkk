"""Кэш возможностей на 7 дней. На диск попадают только отпечатки подключения."""
import hashlib
import json
import os
import threading
import time
from pathlib import Path
import config
import model_checks

CACHE_PATH = Path(config.DATA_DIR) / "model-capabilities.json"
TTL = 7 * 24 * 3600
_lock = threading.RLock()


def _fingerprint(kind, key):
    return hashlib.sha256(json.dumps([kind, *key], ensure_ascii=False).encode()).hexdigest()


def _read():
    try:
        data = json.loads(CACHE_PATH.read_text("utf-8"))
        if data.get("version") == 1 and isinstance(data.get("items"), dict):
            return data["items"]
    except (OSError, ValueError, AttributeError):
        pass
    return {}


def get(kind, key):
    with _lock:
        item = _read().get(_fingerprint(kind, key))
    if not isinstance(item, dict) or not isinstance(item.get("saved_at"), (int, float)):
        return None
    if not 0 <= time.time() - item["saved_at"] < TTL:
        return None
    info = item.get("info")
    if not isinstance(info, dict) or not isinstance(info.get("supported"), bool):
        return None
    if kind == "reasoning" and not isinstance(info.get("efforts"), (list, tuple)):
        return None
    return dict(info)


def memory_get(cache, key):
    item = cache.get(key)
    if item and 0 <= time.time() - item.get("checked_at", 0) < TTL:
        return item
    cache.pop(key, None)
    return None


def put(kind, key, info):
    model_checks.check_cancelled()
    fingerprint = _fingerprint(kind, key)
    with _lock:
        items = _read()
        if info.get("supported") is None:
            items.pop(fingerprint, None)
        else:
            items[fingerprint] = {"saved_at": time.time(), "info": info}
        now = time.time()
        items = {k: v for k, v in items.items() if isinstance(v, dict)
                 and isinstance(v.get("saved_at"), (int, float)) and 0 <= now - v["saved_at"] < TTL}
        try:
            CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
            temp = CACHE_PATH.with_suffix(".tmp")
            temp.write_text(json.dumps({"version": 1, "items": items}, ensure_ascii=False), "utf-8")
            os.replace(temp, CACHE_PATH)
        except OSError:
            pass  # кэш не имеет права ломать ответы при недоступном диске
