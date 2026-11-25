"""分区切换交接清单接口。

与通用资源路由并列，承载交接清单特有的动作：开立、迁移产物登记、临时引用、
确认切换、沿新分区读取、归档旧目录，以及演化详情页查询。

状态码约定：按 id 查询资源不存在返回 404；动作类 POST 的业务拒绝一律 400。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.services.partition_evolve import PartitionEvolveService
from app.services.partition_handoff import PartitionHandoffService, handoff_registry

router = APIRouter(prefix="/api/partition_evolve", tags=["分区切换交接清单"])

_handoff_service = PartitionHandoffService()
_evolve_service = PartitionEvolveService()


class OpenHandoffPayload(BaseModel):
    evolve_id: int


class MigrationPayload(BaseModel):
    source_file_id: str
    new_file_id: str


class BorrowedPayload(BaseModel):
    source_file_id: str


def _unwrap(result: tuple[dict[str, Any] | None, str]) -> dict[str, Any]:
    payload, error = result
    if payload is None:
        raise HTTPException(status_code=400, detail=error)
    return payload


def _require_handoff(handoff_id: str):
    handoff = handoff_registry.get(handoff_id)
    if handoff is None:
        raise HTTPException(status_code=404, detail="交接清单不存在")
    return handoff


@router.get("/handoffs")
def list_handoffs(lake_table: str | None = None) -> dict[str, Any]:
    items = _handoff_service.list_handoffs(lake_table)
    return {"items": items, "total": len(items)}


@router.get("/handoffs/{handoff_id}")
def get_handoff(handoff_id: str) -> dict[str, Any]:
    return _handoff_service.serialize(_require_handoff(handoff_id))


@router.get("/{entry_id}/detail")
def get_evolve_detail(entry_id: int) -> dict[str, Any]:
    detail, error = _evolve_service.evolve_detail(entry_id)
    if detail is None:
        raise HTTPException(status_code=404, detail=error)
    return detail


@router.post("/handoffs")
def open_handoff(payload: OpenHandoffPayload) -> dict[str, Any]:
    return _unwrap(_handoff_service.open_handoff(payload.evolve_id))


@router.post("/handoffs/{handoff_id}/migrations")
def register_migration(handoff_id: str, payload: MigrationPayload) -> dict[str, Any]:
    return _unwrap(
        _handoff_service.register_migration(
            handoff_id, payload.source_file_id, payload.new_file_id,
        )
    )


@router.post("/handoffs/{handoff_id}/borrowed")
def register_borrowed(handoff_id: str, payload: BorrowedPayload) -> dict[str, Any]:
    return _unwrap(
        _handoff_service.register_borrowed(handoff_id, payload.source_file_id)
    )


@router.get("/handoffs/{handoff_id}/progress")
def get_progress(handoff_id: str) -> dict[str, Any]:
    _require_handoff(handoff_id)
    return _unwrap(_handoff_service.get_coexist_progress(handoff_id))


@router.post("/handoffs/{handoff_id}/confirm")
def confirm_switch(handoff_id: str) -> dict[str, Any]:
    return _unwrap(_handoff_service.confirm_switch(handoff_id))


@router.get("/handoffs/{handoff_id}/read-new")
def read_new_partition(handoff_id: str) -> dict[str, Any]:
    _require_handoff(handoff_id)
    return _unwrap(_handoff_service.read_new_partition(handoff_id))


@router.post("/handoffs/{handoff_id}/archive")
def archive_old_directory(handoff_id: str) -> dict[str, Any]:
    return _unwrap(_handoff_service.archive_old_directory(handoff_id))
