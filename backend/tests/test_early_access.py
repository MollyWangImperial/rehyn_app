import copy
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pymongo.errors import DuplicateKeyError

from backend.early_access import LEGACY_NOTICE_TEXT, NOTICE_TEXT, SignupRateLimiter, create_early_access_router


class Collection:
    def __init__(self):
        self.rows = {}
        self.failure = None
        self.acknowledged = True
        self.missing_read = False

    @staticmethod
    def matches(row, query):
        for key, expected in query.items():
            value = row
            for part in key.split("."):
                value = value.get(part) if isinstance(value, dict) else None
            if isinstance(expected, dict):
                if "$in" in expected and value not in expected["$in"]:
                    return False
                if "$lte" in expected and (value is None or value > expected["$lte"]):
                    return False
            elif value != expected:
                return False
        return True

    @staticmethod
    def apply(row, update):
        for operation, values in update.items():
            if operation == "$setOnInsert":
                continue
            for key, value in values.items():
                target = row
                parts = key.split(".")
                for part in parts[:-1]:
                    target = target.setdefault(part, {})
                if operation == "$set":
                    target[parts[-1]] = copy.deepcopy(value)
                elif operation == "$inc":
                    target[parts[-1]] = target.get(parts[-1], 0) + value
                elif operation == "$unset":
                    target.pop(parts[-1], None)

    async def update_one(self, query, update, upsert=False):
        if self.failure:
            raise self.failure
        row = next((row for row in self.rows.values() if self.matches(row, query)), None)
        if row is None and upsert:
            row = copy.deepcopy(update["$setOnInsert"])
            self.rows[query["_id"]] = row
        if row is not None:
            self.apply(row, update)
        return SimpleNamespace(acknowledged=self.acknowledged, matched_count=int(row is not None))

    async def find_one(self, query, projection):
        return None if self.missing_read else self.rows.get(query["_id"])

    async def find_one_and_update(self, query, update, **kwargs):
        if self.failure:
            raise self.failure
        for row in self.rows.values():
            if self.matches(row, query):
                self.apply(row, update)
                return copy.deepcopy(row)
        return None


class Database:
    def __init__(self, collection):
        self.collection = collection

    def get_collection(self, name, **options):
        assert name == "early_access_signups"
        assert options["write_concern"].document == {"wtimeout": 10000, "w": "majority"}
        assert options["read_concern"].document == {"level": "majority"}
        return self.collection


def make_client(collection, **kwargs):
    app = FastAPI()
    app.include_router(create_early_access_router(Database(collection), **kwargs))
    return TestClient(app)


@pytest.fixture
def setup():
    collection = Collection()
    return collection, make_client(collection)


@pytest.fixture(autouse=True)
def no_real_email(monkeypatch):
    monkeypatch.delenv("RESEND_API_KEY", raising=False)
    monkeypatch.delenv("EARLY_ACCESS_EMAIL_FROM", raising=False)


def payload(**overrides):
    return {"email": "Example@example.com", "form_location": "hero",
            "notice_version": "early-access-v1", "website": "", **overrides}


def submit(client, **overrides):
    return client.post("/api/early-access", headers={"Origin": "https://rehyn.com"},
                       json=payload(**overrides))


def test_saves_normalized_email_and_notice_once_across_forms_and_restarts(setup):
    collection, client = setup
    assert submit(client, email="  Example@EXAMPLE.com  ").json() == {"ok": True, "saved": True}
    original = copy.deepcopy(next(iter(collection.rows.values())))
    assert original["email"] == "example@example.com"
    assert original["notice_text"] == LEGACY_NOTICE_TEXT
    assert original["created_at"].tzinfo is not None
    assert original["status"] == "subscribed"
    assert original["notification"]["status"] == "pending"
    assert original["browsing_seconds"] is None
    client = make_client(collection)
    assert submit(client, email="example@example.com", form_location="footer").status_code == 200
    assert list(collection.rows.values()) == [original]
    assert "email" not in submit(client).text


def test_v2_timing_and_notice_are_persisted_without_overwriting_on_retry(setup):
    collection, client = setup
    assert submit(client, notice_version="early-access-v2", browsing_seconds=83).status_code == 200
    original = copy.deepcopy(next(iter(collection.rows.values())))
    assert original["browsing_seconds"] == 83
    assert original["browsing_measurement"] == "visible-page-v1"
    assert original["notice_text"] == NOTICE_TEXT
    assert submit(client, notice_version="early-access-v2", browsing_seconds=92).status_code == 200
    assert list(collection.rows.values()) == [original]


@pytest.mark.parametrize("seconds", [-1, 86401, 1.2, "12", True, {"$ne": None}])
def test_rejects_invalid_durations(setup, seconds):
    collection, client = setup
    assert submit(client, notice_version="early-access-v2", browsing_seconds=seconds).status_code == 422
    assert not collection.rows


def test_old_notice_cannot_collect_new_telemetry(setup):
    collection, client = setup
    assert submit(client, browsing_seconds=12).status_code == 422
    assert not collection.rows


@pytest.mark.parametrize("seconds", [None, 0, 86400])
def test_timing_boundary_or_unavailable_never_blocks_signup(setup, seconds):
    _, client = setup
    assert submit(client, notice_version="early-access-v2", browsing_seconds=seconds).status_code == 200


@pytest.mark.parametrize("changes", [
    {"email": "invalid"}, {"email": {"$ne": None}}, {"email": "a" * 260 + "@example.com"},
    {"form_location": "admin"}, {"notice_version": "old"}, {"website": "spam"}, {"extra": "data"},
])
def test_invalid_requests_do_not_write(setup, changes):
    collection, client = setup
    assert submit(client, **changes).status_code == 422
    assert not collection.rows


@pytest.mark.parametrize("origin", [None, "https://evil.example", "null", "https://rehyn.com.evil.example"])
def test_unapproved_origins_cannot_write(setup, origin):
    collection, client = setup
    headers = {"Origin": origin} if origin else {}
    assert client.post("/api/early-access", headers=headers, json=payload()).status_code == 403
    assert not collection.rows


@pytest.mark.parametrize("origin", ["https://www.rehyn.com", "https://rehyn-website-static.onrender.com"])
def test_other_production_origins(setup, origin):
    _, client = setup
    assert client.post("/api/early-access", headers={"Origin": origin}, json=payload()).json()["saved"]


@pytest.mark.parametrize("failure", ["write", "read", "unacknowledged"])
def test_failure_never_reports_success_and_retry_works(setup, failure):
    collection, client = setup
    if failure == "write":
        collection.failure = RuntimeError("secret connection details")
    if failure == "read":
        collection.missing_read = True
    if failure == "unacknowledged":
        collection.acknowledged = False
    response = submit(client)
    assert response.status_code == 503
    assert "secret" not in response.text
    assert response.headers["retry-after"] == "5"
    collection.failure = None
    collection.missing_read = False
    collection.acknowledged = True
    assert submit(client).json()["saved"]
    assert len(collection.rows) == 1


def test_duplicate_key_race_requires_readback(setup):
    collection, client = setup
    assert submit(client).status_code == 200
    collection.failure = DuplicateKeyError("duplicate")
    assert submit(client).status_code == 200
    assert submit(client, email="other@example.com").status_code == 503


def test_body_limit_content_type_and_no_public_read(setup):
    collection, client = setup
    headers = {"Origin": "https://rehyn.com", "Content-Type": "application/json"}
    assert client.post("/api/early-access", headers=headers, content="x" * 2049).status_code == 413
    assert client.post("/api/early-access", headers=headers, content="{").status_code == 422
    assert client.post("/api/early-access", headers={"Origin": "https://rehyn.com"}, content="email=a").status_code == 415
    assert client.get("/api/early-access").status_code == 405
    assert not collection.rows


def test_rate_limit_is_bounded_and_recovers():
    clock = [0]
    limiter = SignupRateLimiter(limit=2, window=10, max_clients=2, clock=lambda: clock[0])
    client = make_client(Collection(), limiter=limiter)
    assert submit(client).status_code == 200
    assert submit(client).status_code == 200
    assert submit(client).status_code == 429
    clock[0] = 11
    assert submit(client).status_code == 200
    limiter.check("another")
    limiter.check("third")
    assert len(limiter.clients) == 2
