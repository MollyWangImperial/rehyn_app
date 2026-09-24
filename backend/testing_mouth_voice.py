"""Private, authenticated Molly clips for Hand to Mouth Testing."""
import base64
import hashlib
import json
import os
from functools import lru_cache
from pathlib import Path
from fastapi import APIRouter, HTTPException, Request
from backend import server
from backend.testing_reach_voice import CueRequest
from backend.testing_mouth_voice_lines import LINES

router = APIRouter(prefix="/api/testing/mouth/voice")
HASHES = {hashlib.sha256(text.encode()).hexdigest() for text in LINES}

@lru_cache(maxsize=1)
def bundle():
    local = Path(__file__).resolve().parent / "voice_samples" / "mouth-molly-bundle.json"
    path = Path(os.environ.get("TESTING_MOUTH_VOICE_BUNDLE") or (local if local.is_file() else "/etc/secrets/mouth-molly-bundle.json"))
    try:
        if path.stat().st_size > 1_000_000:
            return {}
        data = json.loads(path.read_text(encoding="ascii"))
        entries = data.get("entries", {})
        if data.get("version") != 1 or data.get("voice") != "Molly" or set(entries) != HASHES:
            return {}
        for encoded in entries.values():
            audio = base64.b64decode(encoded, validate=True)
            if not 1000 <= len(audio) <= 200_000 or not (audio.startswith(b"ID3") or audio[:1] == b"\xff"):
                return {}
        return entries
    except (OSError, ValueError, TypeError):
        return {}

@router.get("/health")
async def health():
    entries = bundle()
    return {"ready": bool(entries), "voice": "Molly" if entries else None,
            "provider": "prepared-private-clone" if entries else "unavailable",
            "required_cues": len(LINES), "available_cues": len(entries)}

@router.post("")
async def cue(request: Request, cue: CueRequest):
    if not await server._task_video_user(request):
        raise HTTPException(401, "Sign in required")
    if cue.text not in LINES:
        raise HTTPException(403, "Only authored Hand to Mouth instructions can use this voice")
    encoded = bundle().get(hashlib.sha256(cue.text.encode()).hexdigest())
    if not encoded:
        raise HTTPException(503, "Molly voice is unavailable")
    return {"audio_b64": encoded, "text": cue.text}
