"""Public, write-only early-access registration, separate from patient accounts."""

import asyncio
import hashlib
import json
import logging
import time
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, EmailStr, Field, ValidationError, field_validator
from pymongo.errors import DuplicateKeyError
from pymongo.read_concern import ReadConcern
from pymongo.write_concern import WriteConcern

NOTICE_VERSION = "early-access-v1"
NOTICE_TEXT = "We'll use your email for early-access updates. Contact info@rehyn.com to leave the list."
ALLOWED_ORIGINS = frozenset({
    "https://rehyn.com", "https://www.rehyn.com",
    "https://rehyn-website-static.onrender.com",
})
logger = logging.getLogger(__name__)


class EarlyAccessSignup(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: EmailStr = Field(max_length=254)
    form_location: Literal["hero", "footer"]
    notice_version: Literal["early-access-v1"]
    website: str = Field(default="", max_length=0)

    @field_validator("email", mode="before")
    @classmethod
    def normalize_email(cls, value):
        return value.strip().lower() if isinstance(value, str) else value


class SignupRateLimiter:
    """Bounded, process-local abuse protection; raw IPs are never stored."""

    def __init__(self, limit=20, window=3600, max_clients=4096, clock=time.monotonic):
        self.limit, self.window, self.max_clients = limit, window, max_clients
        self.clock = clock
        self.clients = OrderedDict()

    def check(self, address):
        now = self.clock()
        key = hashlib.sha256(address.encode()).hexdigest()
        started, count = self.clients.get(key, (now, 0))
        if now - started >= self.window:
            started, count = now, 0
        if count >= self.limit:
            raise HTTPException(429, "Please wait before trying again.",
                                headers={"Retry-After": str(max(1, int(self.window - (now - started))))})
        self.clients[key] = (started, count + 1)
        self.clients.move_to_end(key)
        while len(self.clients) > self.max_clients:
            self.clients.popitem(last=False)


async def save_signup(collection, signup):
    email = str(signup.email)
    record_id = hashlib.sha256(email.encode()).hexdigest()
    record = {
        "_id": record_id,
        "email": email,
        "created_at": datetime.now(timezone.utc),
        "source": "rehyn.com",
        "form_location": signup.form_location,
        "notice_version": NOTICE_VERSION,
        "notice_text": NOTICE_TEXT,
        "status": "subscribed",
    }
    try:
        result = await collection.update_one(
            {"_id": record_id}, {"$setOnInsert": record}, upsert=True,
        )
        if not result.acknowledged:
            raise RuntimeError("Unacknowledged waitlist write")
    except DuplicateKeyError:
        # Simultaneous retries can race on the unique _id. Confirm the saved row.
        pass
    stored = await collection.find_one({"_id": record_id}, {"email": 1})
    if not stored or stored.get("email") != email:
        raise RuntimeError("Waitlist read-back failed")


def create_early_access_router(database, *, allowed_origins=ALLOWED_ORIGINS, limiter=None):
    router = APIRouter(prefix="/api")
    limiter = limiter or SignupRateLimiter()
    # Use the actual Mongo database, never the app's development file fallback.
    collection = database.get_collection(
        "early_access_signups",
        write_concern=WriteConcern(w="majority", wtimeout=10000),
        read_concern=ReadConcern("majority"),
    )

    @router.post("/early-access")
    async def register(request: Request):
        if request.headers.get("origin") not in allowed_origins:
            raise HTTPException(403, "Please join through rehyn.com.")
        limiter.check(request.client.host if request.client else "unknown")
        if request.headers.get("content-type", "").split(";")[0].strip() != "application/json":
            raise HTTPException(415, "A JSON request is required.")
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 2048:
                raise HTTPException(413, "The submission is too large.")
        try:
            signup = EarlyAccessSignup.model_validate(json.loads(body))
        except (ValueError, UnicodeDecodeError, ValidationError):
            # Do not echo email addresses or arbitrary request bodies into errors.
            raise HTTPException(422, "Enter a valid email address and try again.") from None
        try:
            await asyncio.wait_for(save_signup(collection, signup), timeout=20)
        except Exception as exc:
            # Exception messages from database drivers can contain connection details.
            logger.warning("Early-access save unavailable (%s)", type(exc).__name__)
            raise HTTPException(503, "We couldn't save your email. Please try again.",
                                headers={"Retry-After": "5"}) from None
        # Same response for new and existing records; no public directory or lookup.
        return JSONResponse({"ok": True, "saved": True}, headers={"Cache-Control": "no-store"})

    return router
