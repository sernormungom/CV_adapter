import logging
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from backend.cv_assistant.chat_engine import chat
from backend.cv_assistant.profile_patcher import apply_profile_patch
from backend.cv_assistant.profile_utils import load_profile, parse_yaml

logger = logging.getLogger(__name__)

router = APIRouter()


def _normalize(username: str) -> str:
    return username.strip().split("@")[0].strip()


class ChatRequest(BaseModel):
    username: str
    message: str
    history: list[dict[str, Any]] = []
    cv_state: dict[str, Any] = {}
    job_description: str = ""


class ProfilePatchRequest(BaseModel):
    username: str
    patch: dict[str, Any]


@router.post("/api/chat")
async def chat_endpoint(req: ChatRequest) -> dict[str, Any]:
    if not req.username.strip() or not req.message.strip():
        raise HTTPException(status_code=400, detail="username and message are required")
    return await chat(
        username=_normalize(req.username),
        message=req.message,
        history=req.history,
        cv_state=req.cv_state,
        job_description=req.job_description,
    )


_SEVERITY_ORDER = {"blocking": 0, "valuable": 1, "minor": 2}


@router.get("/api/chat/gaps/{username}")
def get_profile_gaps(username: str) -> dict[str, Any]:
    """Return open gaps sorted by severity (blocking first), capped at 5."""
    yaml_text, _ = load_profile(_normalize(username))
    if not yaml_text:
        return {"gaps": []}
    profile = parse_yaml(yaml_text)
    if not profile:
        return {"gaps": []}
    open_gaps = [
        g for g in (profile.get("gaps") or [])
        if g.get("status") == "open" and g.get("suggested_question")
    ]
    open_gaps.sort(key=lambda g: _SEVERITY_ORDER.get(g.get("severity", "minor"), 3))
    return {
        "gaps": [
            {
                "gap_id": g.get("gap_id", ""),
                "severity": g.get("severity", ""),
                "suggested_question": g.get("suggested_question", ""),
            }
            for g in open_gaps[:5]
        ]
    }


@router.post("/api/chat/profile-patch")
async def profile_patch_endpoint(req: ProfilePatchRequest) -> dict[str, Any]:
    if not req.username.strip():
        raise HTTPException(status_code=400, detail="username is required")
    result = await apply_profile_patch(_normalize(req.username), req.patch)
    if not result.get("ok"):
        raise HTTPException(status_code=500, detail=result.get("error", "Unknown error"))
    return result
