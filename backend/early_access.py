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
from pydantic import BaseModel, ConfigDict, EmailStr, Field, ValidationError, field_validator, model_validator
from pymongo.errors import DuplicateKeyError
from pymongo.read_concern import ReadConcern
from pymongo.write_concern import WriteConcern

try:
    from backend.early_access_notifications import SignupNotifier
except ModuleNotFoundError:
    from early_access_notifications import SignupNotifier

NOTICE_VERSION = "early-access-v2"
LEGACY_NOTICE_TEXT = "We'll use your email for early-access updates. Contact info@rehyn.com to leave the list."
NOTICE_TEXT = (
    "We'll use your email for early-access updates. On signup, we record this visit's "
    "visible-page time and notify our team with your email, signup time and browsing-time "
    "estimate. No tracking cookies. Contact info@rehyn.com to leave the list."
)
ALLOWED_ORIGINS = frozenset({
    "https://rehyn.com", "https://www.rehyn.com",
    "https://rehyn-website-static.onrender.com",
})
logger = logging.getLogger(__name__)


class EarlyAccessSignup(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: EmailStr = Field(max_length=254)
    form_location: Literal["hero", "footer"]
    notice_version: Literal["early-access-v1", "early-access-v2"]
    browsing_seconds: int | None = Field(default=None, strict=True, ge=0, le=86400)
    website: str = Field(default="", max_length=0)

    @field_validator("email", mode="before")
    @classmethod
    def normalize_email(cls, value):
        return value.strip().lower() if isinstance(value, str) else value

    @model_validator(mode="after")
    def timing_requires_notice(self):
        if self.browsing_seconds is not None and self.notice_version != NOTICE_VERSION:
            raise ValueError("Page timing requires the updated signup notice")
        return self


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
    now = datetime.now(timezone.utc)
    record = {
        "_id": record_id,
        "email": email,
        "created_at": now,
        "source": "rehyn.com",
        "form_location": signup.form_location,
        "notice_version": signup.notice_version,
        "notice_text": NOTICE_TEXT if signup.notice_version == NOTICE_VERSION else LEGACY_NOTICE_TEXT,
        "browsing_seconds": signup.browsing_seconds,
        "browsing_measurement": "visible-page-v1" if signup.browsing_seconds is not None else None,
        "status": "subscribed",
        # The notification is committed atomically with the registration.
        "notification": {"status": "pending", "attempts": 0, "next_attempt_at": now},
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


def create_early_access_router(database, *, allowed_origins=ALLOWED_ORIGINS, limiter=None, notifier_factory=SignupNotifier):
    router = APIRouter(prefix="/api")
    limiter = limiter or SignupRateLimiter()
    # Use the actual Mongo database, never the app's development file fallback.
    collection = database.get_collection(
        "early_access_signups",
        write_concern=WriteConcern(w="majority", wtimeout=10000),
        read_concern=ReadConcern("majority"),
    )
    notifier = notifier_factory(collection)
    router.add_event_handler("startup", notifier.start)
    router.add_event_handler("shutdown", notifier.stop)

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
        notifier.wake()
        return JSONResponse({"ok": True, "saved": True}, headers={"Cache-Control": "no-store"})

    return router
