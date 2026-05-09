"""
FastAPI Router
All REST endpoints for the AP Policy Engine.
"""
from __future__ import annotations
from typing import Annotated
from fastapi import APIRouter, HTTPException, Query, UploadFile, File, Depends, Body
from fastapi.responses import JSONResponse, PlainTextResponse

from app.models.schemas import (
    ExtractionRequest, ExtractionResponse,
    ValidationRequest, ValidationResponse,
    ExtractedRule, Conflict,
)
from app.api.service import PolicyEngineService, get_service

router = APIRouter(prefix="/api/v1", tags=["AP Policy Engine"])
ServiceDep = Annotated[PolicyEngineService, Depends(get_service)]


# ---------------------------------------------------------------------------
# Extraction endpoints
# ---------------------------------------------------------------------------

@router.post(
    "/extract",
    response_model=ExtractionResponse,
    summary="Extract rules from a policy document (text/JSON body)",
)
async def extract_rules(request: ExtractionRequest, svc: ServiceDep):
    """
    Upload policy text and run the full extraction pipeline:
    parse → LLM extract → validate → conflict detect → persist.
    """
    try:
        return svc.extract_from_text(request)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post(
    "/extract/upload",
    response_model=ExtractionResponse,
    summary="Extract rules from an uploaded policy file (.md / .txt / .pdf)",
)
async def extract_rules_upload(
    svc: ServiceDep,
    file: UploadFile = File(...),
    policy_name: str = Query(default="uploaded_policy"),
    confidence_threshold: float = Query(default=0.6, ge=0.0, le=1.0),
):
    """Upload a .md, .txt, or .pdf policy file for rule extraction."""
    content = await file.read()
    if file.filename and file.filename.endswith(".pdf"):
        try:
            import pypdf, io
            reader = pypdf.PdfReader(io.BytesIO(content))
            text = "\n".join(p.extract_text() or "" for p in reader.pages)
        except ImportError:
            raise HTTPException(status_code=400, detail="pypdf required for PDF upload")
    else:
        text = content.decode("utf-8", errors="replace")

    request = ExtractionRequest(
        policy_text=text,
        policy_name=policy_name,
        confidence_threshold=confidence_threshold,
    )
    try:
        return svc.extract_from_text(request)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Rules endpoints
# ---------------------------------------------------------------------------

@router.get(
    "/rules",
    response_model=list[ExtractedRule],
    summary="List all extracted rules with optional filters",
)
async def list_rules(
    svc: ServiceDep,
    policy_name: str | None = Query(default=None),
    category: str | None = Query(default=None, description="e.g. THREE_WAY_MATCH"),
    min_confidence: float = Query(default=0.0, ge=0.0, le=1.0),
):
    rules = svc.get_rules(policy_name=policy_name, category=category, min_confidence=min_confidence)
    return rules


@router.get(
    "/rules/{rule_id}",
    response_model=ExtractedRule,
    summary="Get a specific rule by ID",
)
async def get_rule(rule_id: str, svc: ServiceDep):
    from app.utils.rules_store import get_store
    rule = get_store().get_rule(rule_id)
    if not rule:
        raise HTTPException(status_code=404, detail=f"Rule '{rule_id}' not found")
    return rule


# ---------------------------------------------------------------------------
# Validation endpoint
# ---------------------------------------------------------------------------

@router.post(
    "/validate",
    response_model=ValidationResponse,
    summary="Validate an invoice against all extracted rules",
)
async def validate_invoice(request: ValidationRequest, svc: ServiceDep):
    """
    Submit an invoice JSON and receive a deterministic execution report
    including verdict, all triggered rules, deviations, and audit trail.
    """
    try:
        return svc.validate_invoice(request)
    except Exception as e:
        raise HTTPException(status_code=422, detail=str(e))


# ---------------------------------------------------------------------------
# Conflicts endpoint
# ---------------------------------------------------------------------------

@router.get(
    "/conflicts",
    response_model=list[Conflict],
    summary="List detected rule conflicts",
)
async def list_conflicts(
    svc: ServiceDep,
    policy_name: str | None = Query(default=None),
    severity: str | None = Query(default=None, description="LOW | MEDIUM | HIGH | CRITICAL"),
):
    return svc.get_conflicts(policy_name=policy_name, severity=severity)


# ---------------------------------------------------------------------------
# Diagrams endpoint
# ---------------------------------------------------------------------------

@router.get(
    "/diagrams",
    summary="Get Mermaid diagram strings for all rule categories",
)
async def get_diagrams(
    svc: ServiceDep,
    policy_name: str | None = Query(default=None),
    diagram: str | None = Query(default=None, description="e.g. approval_matrix, conflicts, full_pipeline"),
):
    """Returns Mermaid diagram strings. Paste into any Mermaid renderer."""
    all_diagrams = svc.get_diagrams(policy_name)
    if diagram:
        if diagram not in all_diagrams:
            raise HTTPException(
                status_code=404,
                detail=f"Diagram '{diagram}' not found. Available: {list(all_diagrams.keys())}",
            )
        return PlainTextResponse(all_diagrams[diagram], media_type="text/plain")
    return JSONResponse(all_diagrams)


# ---------------------------------------------------------------------------
# Health & meta endpoints
# ---------------------------------------------------------------------------

@router.get("/health", summary="Health check")
async def health():
    return {"status": "ok", "service": "AP Policy Engine"}


@router.get("/policies", summary="List all ingested policy names")
async def list_policies(svc: ServiceDep):
    from app.utils.rules_store import get_store
    store = get_store()
    return {
        "policies": [
            {"name": p, **store.get_policy_meta(p)}
            for p in store.list_policies()
        ]
    }


@router.delete("/policies/{policy_name}", summary="Delete all rules for a policy")
async def delete_policy(policy_name: str, svc: ServiceDep):
    from app.utils.rules_store import get_store
    get_store().clear(policy_name)
    return {"deleted": policy_name}