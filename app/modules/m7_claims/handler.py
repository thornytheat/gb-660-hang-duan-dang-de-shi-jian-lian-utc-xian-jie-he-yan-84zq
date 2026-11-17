"""M7 误机索赔结算 HTTP 接口。

所有写接口接受可选请求幂等键 X-Request-Key（或 body.request_key）：
同键同载荷返回原回执，同键异载荷返回 409，无键请求正常执行。
"""
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app.web import page
from .service import ClaimError, ClaimService

router = APIRouter(prefix="/api/claims", tags=["m7-claims"])
page_router = APIRouter(tags=["m7-claims"])

ROWS = [
    {"key": "ITN-701|JFK|MU5101|UA857", "status": "待结算", "value": "30000"},
    {"key": "ITN-702|LHR|BA178|AF001", "status": "证据不全拒付", "value": "0"},
    {"key": "ITN-703|PVG|CA1858|MU5101", "status": "已结算+补赔", "value": "45000"},
]


@page_router.get("/claims", response_class=HTMLResponse)
def view(request: Request):
    return page(request, "m7.html", "索赔结算", "/api/claims/register", ROWS)


def _service(request: Request) -> ClaimService:
    service = getattr(request.app.state, "m7_service", None)
    if service is None:
        service = ClaimService()
        request.app.state.m7_service = service
    return service


def _key(request: Request, payload: dict) -> str | None:
    return payload.pop("request_key", None) or request.headers.get("X-Request-Key")


def _error(exc: ClaimError) -> JSONResponse:
    return JSONResponse(status_code=exc.status,
                        content={"error_code": exc.code, "message": exc.message})


@router.post("/register")
async def register(request: Request):
    payload = await request.json()
    try:
        return _service(request).register(payload, _key(request, payload))
    except ClaimError as exc:
        return _error(exc)


@router.post("/revise")
async def revise(request: Request):
    payload = await request.json()
    try:
        return _service(request).revise(payload, _key(request, payload))
    except ClaimError as exc:
        return _error(exc)


@router.post("/settle")
async def settle(request: Request):
    payload = await request.json()
    try:
        return _service(request).settle(payload, _key(request, payload))
    except ClaimError as exc:
        return _error(exc)


@router.post("/settle-batch")
async def settle_batch(request: Request):
    payload = await request.json()
    try:
        return _service(request).settle_batch(payload, _key(request, payload))
    except ClaimError as exc:
        return _error(exc)


@router.post("/withdraw")
async def withdraw(request: Request):
    payload = await request.json()
    try:
        return _service(request).withdraw(payload, _key(request, payload))
    except ClaimError as exc:
        return _error(exc)


@router.get("/{claim_id}")
def readback(claim_id: str, request: Request):
    try:
        return _service(request).readback(claim_id)
    except ClaimError as exc:
        return _error(exc)


@router.get("")
def list_claims(request: Request):
    return {"claims": _service(request).list_claims()}
