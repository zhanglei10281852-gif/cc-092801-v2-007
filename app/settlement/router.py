from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from app.api.dependencies import current_principal
from app.core.security import Principal
from app.settlement.schemas import (
    ImportBatchRequest,
    NoteRequest,
    PublishRequest,
    ReasonRequest,
    ResolveConflictRequest,
    SettlementCreate,
)
from app.settlement.service import SettlementService

router = APIRouter(prefix="/api/settlements", tags=["结算版本化"])


def service() -> SettlementService:
    return SettlementService()


@router.post("", status_code=201)
def create_settlement(payload: SettlementCreate, principal: Principal = Depends(current_principal)):
    principal.require("settlements.write")
    return service().create_settlement(payload.model_dump(), principal.username)


@router.get("")
def list_settlements(status: str | None = None, limit: int = Query(default=100, ge=1, le=500), principal: Principal = Depends(current_principal)):
    principal.require("settlements.read")
    return {"items": service().list_settlements(status=status, limit=limit)}


@router.get("/{settlement_id}")
def get_settlement(settlement_id: int, principal: Principal = Depends(current_principal)):
    principal.require("settlements.read")
    return service().get_settlement(settlement_id)


@router.post("/{settlement_id}/imports")
def import_batch(settlement_id: int, payload: ImportBatchRequest, principal: Principal = Depends(current_principal)):
    principal.require("settlements.write")
    return service().import_batch(settlement_id, payload.model_dump(), principal.username)


@router.post("/{settlement_id}/recalculate")
def recalculate(settlement_id: int, payload: NoteRequest | None = None, principal: Principal = Depends(current_principal)):
    principal.require("settlements.write")
    return service().recalculate(settlement_id, principal.username, payload.note if payload else "")


@router.post("/{settlement_id}/confirm")
def confirm(settlement_id: int, payload: NoteRequest | None = None, principal: Principal = Depends(current_principal)):
    principal.require("settlements.write")
    return service().confirm(settlement_id, principal.username, payload.note if payload else "")


@router.post("/{settlement_id}/versions", status_code=201)
def create_version(settlement_id: int, payload: NoteRequest | None = None, principal: Principal = Depends(current_principal)):
    principal.require("settlements.write")
    return service().create_version(settlement_id, principal.username, payload.note if payload else "")


@router.post("/{settlement_id}/items/{item_id}/void")
def void_item(settlement_id: int, item_id: int, payload: ReasonRequest, principal: Principal = Depends(current_principal)):
    principal.require("settlements.write")
    return service().void_item(settlement_id, item_id, payload.reason, principal.username)


@router.post("/{settlement_id}/items/{item_id}/resolve")
def resolve_conflict(settlement_id: int, item_id: int, payload: ResolveConflictRequest, principal: Principal = Depends(current_principal)):
    principal.require("settlements.write")
    return service().resolve_conflict(settlement_id, item_id, payload.action, payload.reason, principal.username)


@router.post("/{settlement_id}/publish")
def publish(settlement_id: int, payload: PublishRequest | None = None, principal: Principal = Depends(current_principal)):
    principal.require("settlements.review")
    return service().publish(settlement_id, principal.username, payload.version if payload else None, payload.note if payload else "")


@router.post("/{settlement_id}/revoke")
def revoke(settlement_id: int, payload: ReasonRequest, principal: Principal = Depends(current_principal)):
    principal.require("settlements.review")
    return service().revoke(settlement_id, payload.reason, principal.username)


@router.get("/{settlement_id}/versions/{version}")
def get_version(settlement_id: int, version: int, principal: Principal = Depends(current_principal)):
    principal.require("settlements.read")
    return service().get_version(settlement_id, version)


@router.get("/{settlement_id}/audit")
def audit_trail(settlement_id: int, principal: Principal = Depends(current_principal)):
    principal.require("settlements.read")
    return {"items": service().audit_trail(settlement_id)}
