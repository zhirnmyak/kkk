"""Authenticated API. Every material lookup is scoped to a verified owner."""
import asyncio
import logging
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

import config
import db
import rag
import runtime_settings
from admin_setup import get_admin_id
from bot import handlers
from .auth import Identity, current_identity, require_admin

router = APIRouter(prefix="/api")
log = logging.getLogger("neurotutor.miniapp")


class SettingsChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: str = Field(max_length=40)
    patch: dict[str, Any]


class StudyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["summary", "quiz", "cards"]
    count: int = Field(default=5, ge=1, le=20, strict=True)
    topic: str = Field(default="", max_length=500)


def owner_for(identity, owner_id):
    if owner_id is None or owner_id == identity.user_id:
        return identity.user_id
    if not identity.is_admin:
        raise HTTPException(403, "Материалы другого пользователя недоступны.")
    return owner_id


def document_for(document_id, owner_id):
    document = db.web_document(document_id, owner_id)
    if document is None:
        raise HTTPException(404, "Материал не найден. Обновите список.")
    return document


@router.get("/me")
async def me(identity: Identity = Depends(current_identity)):
    return {"id": identity.user_id, "first_name": identity.first_name,
            "last_name": identity.last_name, "is_admin": identity.is_admin,
            "bot_username": config.MAX_WEBAPP_BOT, "local_mode": identity.local_mode}


@router.get("/files")
async def files(owner_id: int | None = Query(default=None, gt=0),
                identity: Identity = Depends(current_identity)):
    owner = owner_for(identity, owner_id)
    rows = db.web_documents(owner)
    stats = await asyncio.to_thread(rag.document_stats, owner) if rows else {}
    processing = {title for uid, title in handlers._INDEXING if uid == owner}
    for row in rows:
        detail = stats.get(row["title"], {})
        row.update(fragments=detail.get("fragments", 0), pages=len(detail.get("pages", [])),
                   status="processing" if row["title"] in processing else "ready")
        processing.discard(row["title"])
    for title in sorted(processing):
        rows.insert(0, {"id": None, "title": title, "created_at": None, "fragments": 0,
                        "pages": 0, "status": "processing"})
    return {"files": rows, "owner_id": owner}


@router.get("/files/{document_id}")
async def preview(document_id: int, owner_id: int | None = Query(default=None, gt=0),
                  identity: Identity = Depends(current_identity)):
    owner = owner_for(identity, owner_id)
    document = document_for(document_id, owner)
    chunks = await asyncio.to_thread(rag.document_chunks, owner, document["title"])
    return {**document, "chunks": chunks[:200], "truncated": len(chunks) > 200}


@router.delete("/files/{document_id}")
async def delete(document_id: int, owner_id: int | None = Query(default=None, gt=0),
                 identity: Identity = Depends(current_identity)):
    owner = owner_for(identity, owner_id)
    async with handlers._user_lock(owner):
        document = document_for(document_id, owner)
        if (owner, document["title"]) in handlers._INDEXING:
            raise HTTPException(409, "Материал ещё обрабатывается. Дождитесь завершения.")
        await asyncio.to_thread(rag.remove_document, owner, document["title"])
        db.remove_document_rows(owner, document["title"])
    return {"ok": True, "message": "Материал и связанные учебные данные удалены."}


@router.post("/files/{document_id}/study", status_code=202)
async def study(document_id: int, body: StudyRequest, request: Request,
                identity: Identity = Depends(current_identity)):
    # Учебные действия работают только с собственными материалами и личным чатом.
    if identity.local_mode and get_admin_id() != identity.user_id:
        raise HTTPException(409, "Для отправки задания укажите свой ID администратора MAX "
                            "при запуске и перезапустите приложение.")
    document = document_for(document_id, identity.user_id)
    client = getattr(request.app.state, "bot_client", None)
    if client is None or config.MESSENGER != "max":
        raise HTTPException(503, "Бот сейчас не подключён к MAX. Попробуйте позже.")
    tasks = request.app.state.study_tasks
    if identity.user_id in tasks:
        raise HTTPException(409, "Предыдущее задание ещё готовится. Дождитесь ответа в чате.")
    if (identity.user_id, document["title"]) in handlers._INDEXING:
        raise HTTPException(409, "Материал ещё обрабатывается.")
    async def generate():
        try:
            async with handlers._user_lock(identity.user_id):
                document_for(document_id, identity.user_id)
                await handlers._execute_study(
                    client, identity.user_id, f"u:{identity.user_id}",
                    {"kind": body.kind, "count": body.count, "topic": body.topic,
                     "instructions": ""}, document["title"])
        except Exception:
            log.warning("Не удалось подготовить задание из мини-приложения")
            try:
                await client.send_text(f"u:{identity.user_id}",
                                       "Не удалось подготовить задание. Повторите запрос в чате.")
            except Exception:
                log.warning("Не удалось сообщить об ошибке задания в MAX")
        finally:
            tasks.pop(identity.user_id, None)
    tasks[identity.user_id] = asyncio.create_task(generate())
    return {"message": "Готовлю задание. Результат появится в личном чате с ботом."}


@router.get("/admin/users")
async def users(identity: Identity = Depends(require_admin)):
    rows = db.document_owners()
    for row in rows:
        row["label"] = ("Консоль" if row["user_id"] == config.CONSOLE_UID
                        else f"Пользователь {row['user_id']}")
    return {"users": rows}


@router.get("/admin/settings")
async def get_settings(identity: Identity = Depends(require_admin)):
    return await asyncio.to_thread(runtime_settings.public_snapshot)


@router.patch("/admin/settings")
async def patch_settings(body: SettingsChange, identity: Identity = Depends(require_admin)):
    try:
        return await asyncio.to_thread(runtime_settings.update, body.patch, body.revision)
    except runtime_settings.SettingsConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except OSError as exc:
        raise HTTPException(500, "Не удалось сохранить настройки на сервере.") from exc


@router.post("/admin/checks/{action}")
async def check(action: str, request: Request, identity: Identity = Depends(require_admin)):
    # Один дорогой сетевой тест за раз. Не запускаем очередь запросов к провайдеру.
    lock = request.app.state.check_lock
    if lock.locked():
        raise HTTPException(409, "Проверка уже выполняется.")
    async with lock:
        try:
            return await asyncio.to_thread(runtime_settings.check, action)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc
