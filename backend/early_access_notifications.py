"""Durable internal signup alerts. No mail is sent to the person signing up."""

import asyncio
import logging
import os
from contextlib import suppress
from datetime import datetime, timedelta, timezone
from uuid import uuid4
from zoneinfo import ZoneInfo

import httpx
from pymongo import ReturnDocument

logger = logging.getLogger(__name__)
RECIPIENT = "jw923@ic.ac.uk"
MAX_ATTEMPTS = 8
# Resend only deduplicates for 24 hours. Ambiguous older sends need review.
SAFE_RETRY_WINDOW = timedelta(hours=23)


def utc(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def email_payload(record, sender):
    created = utc(record["created_at"])
    seconds = record.get("browsing_seconds")
    duration = "Not available (older page or timing unavailable)"
    if seconds is not None:
        hours, remaining = divmod(seconds, 3600)
        minutes, remainder = divmod(remaining, 60)
        duration = f"{hours}h {minutes}m {remainder}s ({seconds} seconds)"
    text = (
        "A new email address joined the Rehyn early-access list.\n\n"
        f"Email: {record['email']}\n"
        f"Signup time (Europe/London): {created.astimezone(ZoneInfo('Europe/London')):%Y-%m-%d %H:%M:%S %Z}\n"
        f"Signup time (UTC): {created:%Y-%m-%d %H:%M:%S UTC}\n"
        f"Time with landing page visible before signup: {duration}\n"
        f"Form: {record['form_location']}\n"
        "Website: https://rehyn.com/\n\n"
        "Browsing time is a browser-reported estimate for this landing-page visit, "
        "not proof of attention or a total across visits. Hidden-tab time and the "
        "signup request's network wait are excluded. Values are capped at 24 hours. "
        "The form only collects an email address, not a name or patient status.\n"
    )
    return {"from": sender, "to": [RECIPIENT], "subject": "New Rehyn early-access signup", "text": text}


class DeliveryError(Exception):
    def __init__(self, code, retryable=True):
        super().__init__(code)
        self.code, self.retryable = code, retryable


class ResendSender:
    def __init__(self, api_key, sender, *, transport=None):
        self.api_key, self.sender, self.transport = api_key, sender, transport

    async def send(self, payload, key):
        async with httpx.AsyncClient(timeout=15, transport=self.transport) as client:
            response = await client.post(
                "https://api.resend.com/emails",
                headers={"Authorization": f"Bearer {self.api_key}", "Idempotency-Key": key},
                json=payload,
            )
        if not response.is_success:
            raise DeliveryError(f"provider_http_{response.status_code}",
                                response.status_code in (408, 429) or response.status_code >= 500)
        try:
            message_id = response.json().get("id")
        except (ValueError, AttributeError):
            message_id = None
        if not isinstance(message_id, str) or not message_id:
            raise DeliveryError("provider_unconfirmed")
        return message_id


class SignupNotifier:
    def __init__(self, collection, *, sender=None, clock=None):
        self.collection = collection
        key = os.environ.get("RESEND_API_KEY", "").strip()
        from_address = os.environ.get("EARLY_ACCESS_EMAIL_FROM", "").strip()
        self.sender = sender or (ResendSender(key, from_address) if key and from_address else None)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.event = asyncio.Event()
        self.task = None

    async def start(self):
        if self.sender is None:
            logger.warning("Early-access email is not configured; notifications remain queued in MongoDB")
            return
        self.task = asyncio.create_task(self.run())

    async def stop(self):
        if self.task:
            self.task.cancel()
            with suppress(asyncio.CancelledError):
                await self.task
            self.task = None

    def wake(self):
        self.event.set()

    async def run(self):
        while True:
            self.event.clear()
            try:
                # A bounded batch prevents a backlog from monopolising the API.
                for _ in range(20):
                    if not await self.deliver_one():
                        break
                    await asyncio.sleep(1)
            except Exception as exc:
                logger.warning("Early-access notification worker unavailable (%s)", type(exc).__name__)
            try:
                await asyncio.wait_for(self.event.wait(), timeout=60)
            except asyncio.TimeoutError:
                pass

    async def deliver_one(self):
        if self.sender is None:
            return False
        now, token = self.clock(), uuid4().hex
        record = await self.collection.find_one_and_update(
            {"status": "subscribed", "notification.status": {"$in": ["pending", "retry", "sending"]},
             "notification.next_attempt_at": {"$lte": now}},
            {"$set": {"notification.status": "sending", "notification.lease": token,
                      "notification.next_attempt_at": now + timedelta(minutes=2)},
             "$inc": {"notification.attempts": 1}},
            sort=[("notification.next_attempt_at", 1)], return_document=ReturnDocument.AFTER,
        )
        if record is None:
            return False
        query = {"_id": record["_id"], "notification.lease": token}
        notification = record["notification"]
        first_attempt = notification.get("first_attempt_at")
        if ((first_attempt and now - utc(first_attempt) >= SAFE_RETRY_WINDOW)
                or notification["attempts"] > MAX_ATTEMPTS):
            await self.update(query, {"status": "needs_review", "error_code": "retry_window_exhausted"})
            return True

        # Freeze the exact payload before sending, including across deployments/retries.
        if not first_attempt:
            payload = email_payload(record, self.sender.sender)
            result = await self.collection.update_one(query, {"$set": {
                "notification.payload": payload, "notification.first_attempt_at": now,
            }})
            if result.matched_count != 1:
                return True
        else:
            payload = notification["payload"]

        try:
            message_id = await self.sender.send(payload, f"early-access/{record['_id']}")
        except Exception as exc:
            retryable = not isinstance(exc, DeliveryError) or exc.retryable
            retryable = retryable and notification["attempts"] < MAX_ATTEMPTS
            code = exc.code if isinstance(exc, DeliveryError) else type(exc).__name__
            await self.update(query, {
                "status": "retry" if retryable else "needs_review", "error_code": code,
                "next_attempt_at": now + timedelta(seconds=min(3600, 30 * 2 ** (notification["attempts"] - 1))),
            })
            logger.warning("Early-access email not accepted (%s); record retained", code)
            return True
        # Accepted means the provider queued it, not that the inbox received it.
        await self.update(query, {"status": "accepted", "provider_id": message_id,
                                  "accepted_at": self.clock(), "error_code": None})
        return True

    async def update(self, query, values):
        await self.collection.update_one(query, {
            "$set": {f"notification.{key}": value for key, value in values.items()},
            "$unset": {"notification.lease": ""},
        })
