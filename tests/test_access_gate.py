from __future__ import annotations

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI
from starlette.testclient import TestClient

from app.web.access_gate import ACCESS_JWT_HEADER, AUD_ENV, TEAM_ENV, install_access_gate

TEAM = "carbonpaper"
AUD = "the-aud-tag-of-this-tenant"
ISSUER = f"https://{TEAM}.cloudflareaccess.com"
HEADER_NAME = ACCESS_JWT_HEADER.decode()


@pytest.fixture
def signing_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def other_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def mint_token(key: rsa.RSAPrivateKey, *, audience: str = AUD, issuer: str = ISSUER) -> str:
    return jwt.encode(
        {"aud": audience, "iss": issuer, "email": "reader@example.com",
         "exp": 4_102_444_800},
        key, algorithm="RS256",
    )


def build_client(monkeypatch, served_by: rsa.RSAPrivateKey | None, **env: str) -> TestClient:
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    if served_by is not None:
        monkeypatch.setattr(
            jwt, "PyJWKClient", lambda _url: _StubKeys(served_by.public_key())
        )
    app = FastAPI()

    @app.get("/")
    def read_root() -> dict[str, str]:
        return {"ok": "reached the app"}

    install_access_gate(app)
    return TestClient(app)


class _StubKeys:
    """Stands in for PyJWKClient so no test reaches Cloudflare for a public key."""

    def __init__(self, public_key: object) -> None:
        self.key = public_key

    def get_signing_key_from_jwt(self, _token: str) -> "_StubKeys":
        return self


def test_env_unset_leaves_the_app_unguarded(monkeypatch):
    monkeypatch.delenv(TEAM_ENV, raising=False)
    monkeypatch.delenv(AUD_ENV, raising=False)
    client = build_client(monkeypatch, served_by=None)
    assert client.get("/").status_code == 200


def test_configured_gate_refuses_a_request_with_no_token(monkeypatch, signing_key):
    client = build_client(
        monkeypatch, signing_key, **{TEAM_ENV: TEAM, AUD_ENV: AUD}
    )
    assert client.get("/").status_code == 403


def test_valid_token_reaches_the_app(monkeypatch, signing_key):
    client = build_client(monkeypatch, signing_key, **{TEAM_ENV: TEAM, AUD_ENV: AUD})
    response = client.get("/", headers={HEADER_NAME: mint_token(signing_key)})
    assert response.status_code == 200
    assert response.json() == {"ok": "reached the app"}


def test_token_signed_by_another_key_is_refused(monkeypatch, signing_key, other_key):
    client = build_client(monkeypatch, signing_key, **{TEAM_ENV: TEAM, AUD_ENV: AUD})
    response = client.get("/", headers={HEADER_NAME: mint_token(other_key)})
    assert response.status_code == 403


def test_token_for_another_access_application_is_refused(monkeypatch, signing_key):
    """The `aud` check: same team, same key, different app — the one a naive gate lets through."""
    client = build_client(monkeypatch, signing_key, **{TEAM_ENV: TEAM, AUD_ENV: AUD})
    borrowed = mint_token(signing_key, audience="the-aud-tag-of-a-different-app")
    assert client.get("/", headers={HEADER_NAME: borrowed}).status_code == 403


def test_token_from_another_team_is_refused(monkeypatch, signing_key):
    client = build_client(monkeypatch, signing_key, **{TEAM_ENV: TEAM, AUD_ENV: AUD})
    foreign = mint_token(signing_key, issuer="https://someone-else.cloudflareaccess.com")
    assert client.get("/", headers={HEADER_NAME: foreign}).status_code == 403
