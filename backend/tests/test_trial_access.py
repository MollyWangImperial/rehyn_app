from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from backend.trial_access import DESTINATION, create_trial_access_router


def client():
    def check(code):
        if code.strip() != "test-invitation":
            raise HTTPException(403, "The trial code is not valid.")
    app = FastAPI()
    app.include_router(create_trial_access_router(check))
    return TestClient(app)


def test_code_is_checked_before_returning_only_fixed_destination():
    api = client()
    headers = {"Origin": "https://rehyn.com"}
    wrong = api.post("/api/trial-access/verify", headers=headers, json={"trial_code": "wrong"})
    assert wrong.status_code == 403
    assert DESTINATION not in wrong.text
    response = api.post("/api/trial-access/verify", headers=headers, json={"trial_code": "test-invitation"})
    assert response.json() == {"ok": True, "redirect_url": DESTINATION}
    assert response.headers["cache-control"] == "no-store"
    assert "test-invitation" not in response.text
    assert "set-cookie" not in response.headers


def test_bad_requests_rate_limits_and_no_open_redirect():
    api = client()
    path = "/api/trial-access/verify"
    headers = {"Origin": "https://rehyn.com"}
    assert api.post(path, json={"trial_code": "test-invitation"}).status_code == 403
    assert api.post(path, headers=headers, json={"trial_code": "test-invitation", "redirect_url": "https://evil.example"}).status_code == 422
    assert api.post(path, headers=headers, json={"trial_code": 12}).status_code == 422
    assert api.post(path, headers=headers, json={"trial_code": "x" * 300}).status_code == 422
    assert api.post(path, headers={**headers, "Content-Type": "application/json"}, content="x" * 2049).status_code == 413
    for _ in range(6):
        api.post(path, headers=headers, json={"trial_code": "wrong"})
    assert api.post(path, headers=headers, json={"trial_code": "wrong"}).status_code == 429
