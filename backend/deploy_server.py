"""Production entry point that serves the Expo web build and FastAPI together."""

from pathlib import Path
import os

from fastapi import HTTPException
from fastapi.responses import FileResponse, RedirectResponse

# The hosted assessment uses the same designed Alira voice as the companion.
# Explicit environment settings still take precedence. Packed lines work
# without a provider credential; unrecorded speech requires the key.
os.environ.setdefault("INSTRUCTION_TTS_PROVIDER", "elevenlabs")
os.environ.setdefault("ELEVENLABS_VOICE_SCOPE", "all")

from backend.server import app, ASSESSMENT_RUBRICS, EXERCISE_LIBRARY, _survey_candidate_is_eligible
from backend.companion_review_testing import create_review_testing_router
from backend.local_assessment_recordings import router as local_recordings_router
from backend.testing_reach_voice import router as testing_reach_voice_router
from backend.testing_mouth_voice import router as testing_mouth_voice_router

app.include_router(local_recordings_router)
app.include_router(testing_reach_voice_router)
app.include_router(testing_mouth_voice_router)
app.include_router(create_review_testing_router(ASSESSMENT_RUBRICS, EXERCISE_LIBRARY, _survey_candidate_is_eligible))


WEB_DIST = Path(__file__).resolve().parents[1] / "frontend" / "dist"
APP_SHELL_HEADERS = {
    "Cache-Control": "no-store, max-age=0, must-revalidate",
    "Pragma": "no-cache",
    "Expires": "0",
}
SERVICE_WORKER_HEADERS = {
    "Cache-Control": "no-cache, no-store, max-age=0, must-revalidate",
    "Pragma": "no-cache",
    "Expires": "0",
    "Service-Worker-Allowed": "/",
}
IMMUTABLE_ASSET_HEADERS = {
    "Cache-Control": "public, max-age=31536000, immutable",
}


@app.get("/", include_in_schema=False)
async def web_index() -> FileResponse:
    index = WEB_DIST / "index.html"
    if not index.is_file():
        raise HTTPException(status_code=503, detail="Web application has not been built")
    return FileResponse(index, headers=APP_SHELL_HEADERS)


@app.get("/testing/seated-forward-reach", include_in_schema=False)
async def direct_seated_forward_reach(affected_side: str = "right"):
    side = "left" if affected_side == "left" else "right"
    return RedirectResponse(f"/assessment?package=upper_limb&start_task=T1&task_ids=T1&library_test=1&affected_side={side}", status_code=307)


@app.get("/testing/hand-to-mouth", include_in_schema=False)
async def direct_hand_to_mouth(affected_side: str = "right"):
    side = "left" if affected_side == "left" else "right"
    return RedirectResponse(f"/assessment?package=upper_limb&start_task=T3&task_ids=T3&library_test=1&affected_side={side}", status_code=307)


@app.get("/testing/trunk-lean-comparison", include_in_schema=False)
async def direct_trunk_lean_comparison() -> FileResponse:
    return FileResponse(
        Path(__file__).resolve().parents[1] / "testing" / "trunk-lean-comparison" / "trunk_lean_comparison.html",
        media_type="text/html",
        headers=APP_SHELL_HEADERS,
    )


@app.get("/{full_path:path}", include_in_schema=False)
async def web_assets_or_spa(full_path: str) -> FileResponse:
    # Unknown API routes must remain API 404s instead of returning index.html.
    if full_path == "api" or full_path.startswith("api/"):
        raise HTTPException(status_code=404, detail="API route not found")

    requested = (WEB_DIST / full_path).resolve()
    try:
        requested.relative_to(WEB_DIST.resolve())
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="File not found") from exc

    if requested.is_file():
        if full_path == "sw.js":
            return FileResponse(requested, headers=SERVICE_WORKER_HEADERS)
        if full_path.startswith("_expo/static/"):
            return FileResponse(requested, headers=IMMUTABLE_ASSET_HEADERS)
        return FileResponse(requested)

    index = WEB_DIST / "index.html"
    if not index.is_file():
        raise HTTPException(status_code=503, detail="Web application has not been built")
    return FileResponse(index, headers=APP_SHELL_HEADERS)
