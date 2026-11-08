"""MAX WebAppData HMAC validation, following dev.max.ru/docs/webapps/validation."""
from dataclasses import dataclass
import hashlib
import hmac
import ipaddress
import json
import os
import re
import time
from urllib.parse import parse_qsl, urlsplit

from fastapi import HTTPException, Request

import config
from admin_setup import get_admin_id


@dataclass(frozen=True)
class Identity:
    user_id: int
    first_name: str
    last_name: str
    is_admin: bool
    local_mode: bool = False


def validate_init_data(raw: str, token: str, *, now=None, ttl=3600) -> dict:
    if not token or not raw or len(raw) > 16384:
        raise ValueError("Не удалось проверить вход в MAX.")
    try:
        if re.search(r"%(?![0-9a-fA-F]{2})", raw):
            raise ValueError("Malformed encoding")
        pairs = parse_qsl(raw, keep_blank_values=True, strict_parsing=True,
                          encoding="utf-8", errors="strict", max_num_fields=32)
        keys = [key for key, _ in pairs]
        if len(keys) != len(set(keys)) or any(not re.fullmatch(r"[a-z_]+", key) for key in keys):
            raise ValueError("Duplicate or invalid keys")
        data = dict(pairs)
        signature = data.pop("hash")
        if not re.fullmatch(r"[0-9a-fA-F]{64}", signature):
            raise ValueError("Invalid signature")
        check_string = "\n".join(f"{key}={value}" for key, value in sorted(data.items()))
        secret = hmac.new(b"WebAppData", token.encode("utf-8"), hashlib.sha256).digest()
        expected = hmac.new(secret, check_string.encode("utf-8"), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, signature.lower()):
            raise ValueError("Invalid signature")
        auth_date = int(data["auth_date"])
        current = time.time() if now is None else now
        if auth_date > current + 30 or current - auth_date > ttl:
            raise ValueError("Expired signature")
        user = json.loads(data["user"])
        if (not isinstance(user, dict) or type(user.get("id")) is not int
                or not 0 < user["id"] < 2**63):
            raise ValueError("Invalid user")
        return user
    except (KeyError, TypeError, UnicodeError, ValueError) as exc:
        raise ValueError("Откройте мини-приложение заново из MAX: данные входа недействительны или устарели.") from exc


def current_identity(request: Request) -> Identity:
    # Только заголовок: подписанные данные не попадают в URL/логи/referrer.
    scheme, _, raw = request.headers.get("Authorization", "").partition(" ")
    if scheme.lower() == "local":
        access = getattr(request.app.state, "local_access", None)
        loopbacks = {"127.0.0.1", "::1", "localhost"}
        peer_is_local = request.client is not None and request.client.host in loopbacks
        if request.client and os.environ.get("MAXIMKA_CONTAINER") == "1":
            # Docker forwards a localhost-published port through its private bridge.
            # Host, Origin, random token and forwarded-header checks still apply.
            try:
                peer = ipaddress.ip_address(request.client.host)
                peer_is_local = peer_is_local or any(peer in ipaddress.ip_network(network) for network in
                    ("172.16.0.0/12", "192.168.0.0/16", "10.0.0.0/8"))
            except ValueError:
                pass
        origin = request.headers.get("origin")
        local_request = (peer_is_local
                         and request.url.hostname in loopbacks
                         and not any(key in request.headers for key in
                                     ("forwarded", "x-forwarded-for", "x-forwarded-host")))
        if origin:
            try:
                parsed = urlsplit(origin)
                local_request = local_request and parsed.netloc == request.url.netloc and parsed.scheme == request.url.scheme
            except ValueError:
                local_request = False
        if (not access or not local_request or time.time() > access["expires"]
                or not raw or not raw.isascii() or not hmac.compare_digest(raw, access["token"])):
            raise HTTPException(401, "Откройте локальную ссылку из терминала заново.")
        return Identity(access["user_id"], "Администратор", "", True, local_mode=True)
    if scheme.lower() != "max":
        raise HTTPException(401, "Откройте мини-приложение из чата с ботом в MAX.")
    try:
        user = validate_init_data(raw, config.MAX_TOKEN, ttl=config.MAX_INIT_DATA_TTL)
        admin_id = get_admin_id()
    except ValueError as exc:
        raise HTTPException(401, str(exc)) from exc
    return Identity(user["id"], str(user.get("first_name") or "")[:100],
                    str(user.get("last_name") or "")[:100], user["id"] == admin_id)


def require_admin(request: Request) -> Identity:
    identity = current_identity(request)
    if not identity.is_admin:
        raise HTTPException(403, "Этот раздел доступен только администратору.")
    return identity
