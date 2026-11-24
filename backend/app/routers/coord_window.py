"""格时窗口接口：登记窗口、巡检领取、独立审核、正式版本与逐点复算。

和通用资源路由分开，是因为这些接口围绕"窗口 / 领取号 / 版本"三个标识
组织，而不是围绕单条登记记录。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, HTTPException, Query

from app.schemas import WindowBuildPayload, WindowClaimPayload, WindowConfirmPayload
from app.services.coord_window import CoordWindowError, CoordWindowService

router = APIRouter(prefix="/api/coord-windows", tags=["坐标格时窗口"])


def service() -> CoordWindowService:
    return CoordWindowService()


def _fail(error: Exception) -> HTTPException:
    return HTTPException(status_code=400, detail=str(error))


@router.get("")
def list_windows(
    batch_key: str | None = Query(default=None),
    sample_eligibility: str | None = Query(default=None),
    observation_status: str | None = Query(default=None),
    include_detail: bool = Query(default=False),
) -> dict[str, Any]:
    return service().list_windows(
        batch_key=batch_key,
        sample_eligibility=sample_eligibility,
        observation_status=observation_status,
        include_detail=include_detail,
    )


@router.post("/build")
def build_windows(payload: WindowBuildPayload) -> dict[str, Any]:
    try:
        return service().build_windows(
            payload.batch_key,
            grid_precision=payload.grid_precision,
            bucket_sec=payload.bucket_sec,
            expected_windows=payload.expected_windows,
            limit=payload.limit,
        )
    except CoordWindowError as error:
        raise _fail(error) from error


@router.get("/{window_id}/detail")
def window_detail(window_id: str) -> dict[str, Any]:
    svc = service()
    window = svc.get_window(window_id)
    if window is None:
        raise HTTPException(status_code=404, detail=f"格时窗口 {window_id} 不存在")
    return {"ok": True, "window": window, "detail": svc.get_window_detail(window_id)}


@router.post("/{window_id}/recompute")
def recompute_window(window_id: str) -> dict[str, Any]:
    try:
        return service().recompute_window(window_id)
    except CoordWindowError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@router.post("/claims/inspection")
def claim_inspection(payload: WindowClaimPayload) -> dict[str, Any]:
    try:
        return service().claim_inspection(payload.batch_key, payload.window_ids)
    except CoordWindowError as error:
        raise _fail(error) from error


@router.post("/claims/formal")
def claim_formal(payload: WindowClaimPayload) -> dict[str, Any]:
    try:
        return service().claim_formal(
            payload.batch_key, payload.window_ids, scope=payload.scope)
    except CoordWindowError as error:
        raise _fail(error) from error


@router.get("/claims")
def list_claims(
    batch_key: str | None = Query(default=None),
    claim_kind: str | None = Query(default=None),
) -> dict[str, Any]:
    return service().list_claims(batch_key=batch_key, claim_kind=claim_kind)


@router.get("/claims/{claim_id}")
def get_claim(claim_id: str) -> dict[str, Any]:
    try:
        return service().get_claim(claim_id)
    except CoordWindowError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@router.get("/claims/{claim_id}/replay")
def replay_claim(claim_id: str) -> dict[str, Any]:
    try:
        return service().replay_claim(claim_id)
    except CoordWindowError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@router.post("/claims/{claim_id}/confirm")
def confirm_claim(claim_id: str, payload: WindowConfirmPayload | None = Body(default=None)) -> dict[str, Any]:
    try:
        return service().confirm_claim(
            claim_id, formal_version=payload.formal_version if payload else None)
    except CoordWindowError as error:
        raise _fail(error) from error


@router.post("/audits")
def register_audit(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    batch_key = str(payload.get("batch_key") or "").strip()
    if not batch_key:
        raise HTTPException(status_code=400, detail="审核必须带 batch_key")
    try:
        return service().audit_windows(batch_key, payload)
    except CoordWindowError as error:
        raise _fail(error) from error


@router.get("/formal/{batch_key}")
def formal_result(
    batch_key: str,
    version: int | None = Query(default=None),
    include_detail: bool = Query(default=False),
) -> dict[str, Any]:
    try:
        return service().get_formal_result(batch_key, version, include_detail)
    except CoordWindowError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@router.get("/formal/{batch_key}/versions")
def formal_versions(batch_key: str) -> dict[str, Any]:
    return service().list_formal_versions(batch_key)


@router.get("/heatmap/{batch_key}")
def window_heatmap(batch_key: str) -> dict[str, Any]:
    """热力侧按格取数：临时估算、正式统计与空窗分列。"""
    from app.services.heatmap_aggregate import HeatmapAggregateService
    return HeatmapAggregateService().get_window_heatmap(batch_key)
