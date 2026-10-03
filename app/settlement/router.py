from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from app.api.dependencies import current_principal
from app.core.security import Principal
from app.settlement.schemas import BatchImport, CaseCreate, NewVersionRequest, RevokeRequest
from app.settlement.service import SettlementService

router = APIRouter(prefix="/api/settlements", tags=["费用结算版本化"])


def service() -> SettlementService:
    return SettlementService()


def actor_of(principal: Principal) -> tuple[str, int]:
    return principal.display_name or principal.username, principal.user_id


@router.post("/cases", status_code=201)
def create_case(payload: CaseCreate, principal: Principal = Depends(current_principal)) -> dict:
    principal.require("settlements.write")
    actor, user_id = actor_of(principal)
    return service().create_case(payload.model_dump(), actor, user_id)


@router.get("/cases")
def list_cases(status: str | None = Query(default=None, pattern="^(draft|published|revoked)$"),
               limit: int = Query(default=100, ge=1, le=500),
               principal: Principal = Depends(current_principal)) -> dict:
    principal.require("settlements.read")
    return service().list_cases(status=status, limit=limit)


@router.get("/cases/{case_id}")
def get_case(case_id: int, principal: Principal = Depends(current_principal)) -> dict:
    principal.require("settlements.read")
    return service().get_case(case_id)


@router.get("/cases/{case_id}/versions")
def list_versions(case_id: int, principal: Principal = Depends(current_principal)) -> dict:
    principal.require("settlements.read")
    return service().list_versions(case_id)


@router.get("/cases/{case_id}/versions/{version_no}")
def get_version(case_id: int, version_no: int, principal: Principal = Depends(current_principal)) -> dict:
    principal.require("settlements.read")
    return service().get_version(case_id, version_no)


@router.get("/cases/{case_id}/audit")
def audit_chain(case_id: int, principal: Principal = Depends(current_principal)) -> dict:
    principal.require("settlements.read")
    return service().audit_chain(case_id)


@router.post("/cases/{case_id}/entries/import")
def import_entries(case_id: int, payload: BatchImport, principal: Principal = Depends(current_principal)) -> dict:
    principal.require("settlements.write")
    actor, user_id = actor_of(principal)
    return service().import_entries(case_id, payload.model_dump(), actor, user_id)


@router.post("/cases/{case_id}/recalculate")
def recalculate(case_id: int, principal: Principal = Depends(current_principal)) -> dict:
    principal.require("settlements.write")
    actor, user_id = actor_of(principal)
    return service().recalculate(case_id, actor, user_id)


@router.post("/cases/{case_id}/confirm")
def confirm(case_id: int, principal: Principal = Depends(current_principal)) -> dict:
    principal.require("settlements.write")
    actor, user_id = actor_of(principal)
    return service().confirm(case_id, actor, user_id)


@router.post("/cases/{case_id}/publish")
def publish(case_id: int, principal: Principal = Depends(current_principal)) -> dict:
    principal.require("settlements.publish")
    actor, user_id = actor_of(principal)
    return service().publish(case_id, actor, user_id)


@router.post("/cases/{case_id}/revoke")
def revoke(case_id: int, payload: RevokeRequest, principal: Principal = Depends(current_principal)) -> dict:
    principal.require("settlements.publish")
    actor, user_id = actor_of(principal)
    return service().revoke(case_id, payload.reason, actor, user_id)


@router.post("/cases/{case_id}/new-version", status_code=201)
def new_version(case_id: int, payload: NewVersionRequest, principal: Principal = Depends(current_principal)) -> dict:
    principal.require("settlements.write")
    actor, user_id = actor_of(principal)
    return service().new_version(case_id, payload.model_dump(), actor, user_id)
