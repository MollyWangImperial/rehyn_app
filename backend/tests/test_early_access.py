import copy
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pymongo.errors import DuplicateKeyError

from backend.early_access import NOTICE_TEXT, SignupRateLimiter, create_early_access_router


class Collection:
    def __init__(self):
        self.rows = {}
        self.failure = None
        self.acknowledged = True
        self.missing_read = False

    async def update_one(self, query, update, upsert):
        if self.failure:
            raise self.failure
        self.rows.setdefault(query["_id"], copy.deepcopy(update["$setOnInsert"]))
        return SimpleNamespace(acknowledged=self.acknowledged)

    async def find_one(self, query, projection):
        return None if self.missing_read else self.rows.get(query["_id"])


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
    assert original["notice_text"] == NOTICE_TEXT
    assert original["created_at"].tzinfo is not None
    assert original["status"] == "subscribed"
    client = make_client(collection)
    assert submit(client, email="example@example.com", form_location="footer").status_code == 200
    assert list(collection.rows.values()) == [original]
    assert "email" not in submit(client).text


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
