"""Explicit, loopback-only assessment previews without a patient account."""
import os
from urllib.parse import urlsplit

from starlette.requests import Request


def is_local_assessment_preview(request: Request) -> bool:
    if os.environ.get("REHYN_LOCAL_ASSESSMENT_PREVIEW") != "1" or os.environ.get("RENDER"):
        return False
    if request.query_params.get("local_preview") != "1":
        return False
    if not request.client or request.client.host not in {"127.0.0.1", "::1"}:
        return False
    if request.url.hostname not in {"localhost", "127.0.0.1", "::1"}:
        return False
    if any(name in request.headers for name in ("forwarded", "x-forwarded-for", "x-forwarded-host")):
        return False
    if request.headers.get("x-user-id") or request.query_params.get("uid"):
        return False
    origin = request.headers.get("origin")
    if origin:
        try:
            if urlsplit(origin).hostname not in {"localhost", "127.0.0.1", "::1"}:
                return False
        except ValueError:
            return False
    return True
