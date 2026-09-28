import asyncio
import copy
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from backend.early_access_notifications import DeliveryError, ResendSender, SignupNotifier, email_payload
from backend.tests.test_early_access import Collection, make_client, submit


class Sender:
    sender = "Rehyn <notifications@rehyn.com>"

    def __init__(self):
        self.calls = []
        self.failure = None

    async def send(self, payload, key):
        self.calls.append((copy.deepcopy(payload), key))
        if self.failure:
            raise self.failure
        return "provider-message-123"


@pytest.fixture
def notification(monkeypatch):
    monkeypatch.delenv("RESEND_API_KEY", raising=False)
    monkeypatch.delenv("EARLY_ACCESS_EMAIL_FROM", raising=False)
    collection = Collection()
    client = make_client(collection)
    assert submit(client, notice_version="early-access-v2", browsing_seconds=83).status_code == 200
    sender = Sender()
    row = next(iter(collection.rows.values()))
    clock = [row["created_at"]]
    worker = SignupNotifier(collection, sender=sender, clock=lambda: clock[0])
    return collection, client, sender, row, clock, worker


def test_new_signup_sends_once_and_duplicate_never_requeues(notification):
    collection, client, sender, row, _, worker = notification
    async def run():
        assert await worker.deliver_one()
        assert not await worker.deliver_one()
    asyncio.run(run())
    assert submit(client).status_code == 200
    assert not asyncio.run(worker.deliver_one())
    assert len(sender.calls) == 1
    payload, key = sender.calls[0]
    assert payload["to"] == ["jw923@ic.ac.uk"]
    assert "example@example.com" in payload["text"]
    assert "1m 23s (83 seconds)" in payload["text"]
    assert "browser-reported estimate" in payload["text"]
    assert key == f"early-access/{row['_id']}"
    assert row["notification"]["status"] == "accepted"
    assert row["notification"]["provider_id"] == "provider-message-123"
    assert len(collection.rows) == 1


def test_transient_failure_keeps_signup_and_retries_same_payload(notification):
    _, client, sender, row, clock, worker = notification
    sender.failure = httpx.ReadTimeout("secret provider details")
    assert asyncio.run(worker.deliver_one())
    assert row["notification"]["status"] == "retry"
    assert row["status"] == "subscribed"
    assert submit(client).json() == {"ok": True, "saved": True}
    assert not asyncio.run(worker.deliver_one())
    clock[0] += timedelta(minutes=1)
    sender.failure = None
    sender.sender = "Changed <different@rehyn.com>"
    assert asyncio.run(worker.deliver_one())
    assert sender.calls[0] == sender.calls[1]
    assert row["notification"]["status"] == "accepted"
    assert "secret" not in str(row)


def test_expired_lease_resumes_after_restart_without_concurrent_send(notification):
    collection, _, sender, row, clock, worker = notification
    row["notification"].update({"status": "sending", "lease": "old-process",
                                "next_attempt_at": clock[0] + timedelta(minutes=2)})
    assert not asyncio.run(worker.deliver_one())
    clock[0] += timedelta(minutes=3)
    other = SignupNotifier(collection, sender=sender, clock=lambda: clock[0])
    async def race():
        await asyncio.gather(worker.deliver_one(), other.deliver_one())
    asyncio.run(race())
    assert len(sender.calls) == 1


def test_provider_acceptance_then_db_failure_recovers_with_same_idempotency_key(notification):
    collection, _, sender, row, clock, worker = notification
    original_send = sender.send
    async def crash_after_send(payload, key):
        result = await original_send(payload, key)
        collection.failure = RuntimeError("database unavailable")
        return result
    sender.send = crash_after_send
    with pytest.raises(RuntimeError):
        asyncio.run(worker.deliver_one())
    assert row["notification"]["status"] == "sending"
    collection.failure = None
    sender.send = original_send
    clock[0] += timedelta(minutes=3)
    assert asyncio.run(worker.deliver_one())
    assert sender.calls[0] == sender.calls[1]


def test_ambiguous_attempt_older_than_provider_window_is_not_resent(notification):
    _, _, sender, row, clock, worker = notification
    sender.failure = httpx.ReadTimeout("unknown outcome")
    asyncio.run(worker.deliver_one())
    clock[0] += timedelta(hours=24)
    asyncio.run(worker.deliver_one())
    assert len(sender.calls) == 1
    assert row["notification"]["status"] == "needs_review"


def test_permanent_error_and_exhausted_retries_are_retained_for_review(notification):
    _, _, sender, row, _, worker = notification
    sender.failure = DeliveryError("provider_http_403", retryable=False)
    asyncio.run(worker.deliver_one())
    assert row["notification"]["status"] == "needs_review"
    assert not asyncio.run(worker.deliver_one())


def test_max_attempts(notification):
    _, _, sender, row, clock, worker = notification
    sender.failure = DeliveryError("provider_http_429")
    for _ in range(8):
        asyncio.run(worker.deliver_one())
        clock[0] += timedelta(hours=1)
    assert len(sender.calls) == 8
    assert row["notification"]["status"] == "needs_review"


def test_historic_records_and_unsubscribed_records_are_not_notified(notification):
    _, _, sender, row, _, worker = notification
    notification_state = row.pop("notification")
    assert not asyncio.run(worker.deliver_one())
    row["notification"] = notification_state
    row["status"] = "unsubscribed"
    assert not asyncio.run(worker.deliver_one())
    assert not sender.calls


def test_no_configuration_preserves_queue(notification):
    collection, _, sender, row, _, _ = notification
    worker = SignupNotifier(collection)
    async def run():
        await worker.start()
        assert not await worker.deliver_one()
        await worker.stop()
    asyncio.run(run())
    assert worker.task is None
    assert not sender.calls
    assert row["notification"]["status"] == "pending"


def test_router_lifecycle_runs_worker(notification):
    collection, _, sender, row, _, worker = notification
    with make_client(collection, notifier_factory=lambda _: worker) as client:
        # The TestClient portal waits for the worker without a timing-based sleep.
        async def done():
            for _ in range(1000):
                if sender.calls:
                    return
                await asyncio.sleep(0)
            raise AssertionError("Notification worker did not start")
        # Worker starts on application startup, including when the API wakes again.
        assert worker.task is not None
        worker.task.get_loop().call_soon_threadsafe(worker.wake)
        client.portal.call(done)
    assert worker.task is None
    assert len(sender.calls) == 1
    assert row["notification"]["status"] == "accepted"


def test_london_dst_and_naive_mongo_utc_timestamps(notification):
    _, _, _, row, _, _ = notification
    row["created_at"] = datetime(2026, 9, 28, 12, 30)
    row["browsing_seconds"] = None
    text = email_payload(row, "notifications@rehyn.com")["text"]
    assert "2026-09-28 13:30:00 BST" in text
    assert "2026-09-28 12:30:00 UTC" in text
    assert "Not available" in text
    row["created_at"] = datetime(2026, 12, 1, 12, 30, tzinfo=timezone.utc)
    assert "2026-12-01 12:30:00 GMT" in email_payload(row, "sender")["text"]


def test_resend_http_contract():
    def handle(request):
        assert str(request.url) == "https://api.resend.com/emails"
        assert request.headers["Authorization"] == "Bearer test-key"
        assert request.headers["Idempotency-Key"] == "early-access/test"
        assert json.loads(request.content)["to"] == ["jw923@ic.ac.uk"]
        return httpx.Response(200, json={"id": "mail-id"})
    sender = ResendSender("test-key", "sender", transport=httpx.MockTransport(handle))
    assert asyncio.run(sender.send({"to": ["jw923@ic.ac.uk"]}, "early-access/test")) == "mail-id"


@pytest.mark.parametrize("status, body, retryable", [
    (429, {}, True), (500, {}, True), (403, {}, False), (409, {}, False),
    (200, {}, True), (200, [], True),
])
def test_resend_requires_confirmed_acceptance(status, body, retryable):
    sender = ResendSender("test-key", "sender", transport=httpx.MockTransport(
        lambda _: httpx.Response(status, json=body)))
    with pytest.raises(DeliveryError) as error:
        asyncio.run(sender.send({}, "test"))
    assert error.value.retryable is retryable
