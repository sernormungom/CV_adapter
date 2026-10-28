"""Session 4 tests (Art 32): CORS allowlist + HSTS header.

`allow_origins=["*"]` was replaced by a config-driven allowlist
(`backend.config.CORS_ALLOWED_ORIGINS`), and the HSTS header now follows the
existing `SESSION_COOKIE_SECURE` flag (both signal "served over HTTPS").
"""

from backend import config
from backend.main import app


def test_cors_allowlist_is_not_wildcard():
    assert "*" not in config.CORS_ALLOWED_ORIGINS


def test_cors_preflight_from_disallowed_origin_is_rejected(client):
    """The config-value check above doesn't prove the *running middleware*
    enforces it — `CORSMiddleware` is constructed once at app-import time
    with a snapshot of `config.CORS_ALLOWED_ORIGINS`, so a regression here
    wouldn't be caught by asserting the config list alone."""
    r = client.options(
        "/health",
        headers={
            "Origin": "https://evil.example.com",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert r.status_code == 400


def test_cors_preflight_from_allowed_origin_succeeds(client):
    allowed = config.CORS_ALLOWED_ORIGINS[0]
    r = client.options(
        "/health",
        headers={"Origin": allowed, "Access-Control-Request-Method": "GET"},
    )
    assert r.status_code == 200
    assert r.headers["access-control-allow-origin"] == allowed


def test_cors_actual_request_from_disallowed_origin_gets_no_acao_header(client):
    r = client.get("/health", headers={"Origin": "https://evil.example.com"})
    assert r.status_code == 200  # CORS doesn't block the request itself...
    assert "access-control-allow-origin" not in r.headers  # ...just grants nothing


def test_hsts_header_absent_over_plain_http(client, monkeypatch):
    monkeypatch.setattr(config, "SESSION_COOKIE_SECURE", False)
    r = client.get("/health")
    assert "Strict-Transport-Security" not in r.headers


def test_hsts_header_present_when_cookie_secure_flag_is_on(client, monkeypatch):
    monkeypatch.setattr(config, "SESSION_COOKIE_SECURE", True)
    r = client.get("/health")
    assert r.headers["Strict-Transport-Security"] == "max-age=63072000; includeSubDomains"
