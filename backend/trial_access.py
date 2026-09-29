"""Landing-page trial-code check. This does not authenticate patient accounts."""

import json

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

try:
    from backend.early_access import ALLOWED_ORIGINS, SignupRateLimiter
except ModuleNotFoundError:
    from early_access import ALLOWED_ORIGINS, SignupRateLimiter

DESTINATION = "https://rehyn-recovery-companion.onrender.com/"


class TrialCode(BaseModel):
    model_config = ConfigDict(extra="forbid")
    trial_code: str = Field(min_length=1, max_length=256)


def create_trial_access_router(require_code, *, limiter=None, allowed_origins=ALLOWED_ORIGINS):
    router = APIRouter(prefix="/api")
    limiter = limiter or SignupRateLimiter(limit=10, window=600)

    @router.post("/trial-access/verify")
    async def verify(request: Request):
        if request.headers.get("origin") not in allowed_origins:
            raise HTTPException(403, "Please sign in through rehyn.com.")
        limiter.check(request.client.host if request.client else "unknown")
        if request.headers.get("content-type", "").split(";")[0].strip() != "application/json":
            raise HTTPException(415, "A JSON request is required.")
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 2048:
                raise HTTPException(413, "The submission is too large.")
        try:
            payload = TrialCode.model_validate(json.loads(body))
        except (ValueError, UnicodeDecodeError):
            raise HTTPException(422, "Enter your trial code.") from None
        require_code(payload.trial_code)
        return JSONResponse({"ok": True, "redirect_url": DESTINATION},
                            headers={"Cache-Control": "no-store"})

    return router
