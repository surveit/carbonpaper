"""Refuses requests that did not come through Cloudflare Access. See docs/per-tenant-deploy.md."""
from __future__ import annotations

import os

import jwt
from jwt.exceptions import PyJWKClientError, PyJWTError
from fastapi import FastAPI
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Receive, Scope, Send

# Set by Cloudflare's edge. The CF_Authorization cookie may not reach the origin.
ACCESS_JWT_HEADER = b"cf-access-jwt-assertion"

TEAM_ENV = "CARBON_PAPER_ACCESS_TEAM"
AUD_ENV = "CARBON_PAPER_ACCESS_AUD"


def install_access_gate(app: FastAPI) -> bool:
    """Returns whether the gate was installed, so the caller can log which mode it booted in."""
    team = os.environ.get(TEAM_ENV, "").strip()
    audience = os.environ.get(AUD_ENV, "").strip()
    if not team or not audience:
        return False
    app.add_middleware(CloudflareAccessGate, team=team, audience=audience)
    return True


class CloudflareAccessGate:
    def __init__(self, app: ASGIApp, *, team: str, audience: str) -> None:
        self._app = app
        self._audience = audience
        self._issuer = f"https://{team}.cloudflareaccess.com"
        # Cached by PyJWKClient; Cloudflare rotates these every 6 weeks.
        self._keys = jwt.PyJWKClient(f"{self._issuer}/cdn-cgi/access/certs")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            await self._app(scope, receive, send)
            return
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        refusal = self._find_token_refusal(read_access_token(scope))
        if refusal is None:
            await self._app(scope, receive, send)
            return
        status, text = refusal
        await PlainTextResponse(text, status_code=status)(scope, receive, send)

    def _find_token_refusal(self, token: str | None) -> tuple[int, str] | None:
        """None when the token verifies. Every other path refuses — fail closed."""
        if token is None:
            return 403, "This deployment is reachable only through Cloudflare Access."
        try:
            key = self._keys.get_signing_key_from_jwt(token).key
        except PyJWKClientError:
            # A key we cannot fetch is a key we cannot trust.
            return 503, "Cannot reach Cloudflare Access to verify this request."
        try:
            # `audience` pins this to THIS app; without it a sibling app's token verifies.
            jwt.decode(
                token, key, algorithms=["RS256"],
                audience=self._audience, issuer=self._issuer,
            )
        except PyJWTError:
            return 403, "Cloudflare Access token rejected."
        return None


def read_access_token(scope: Scope) -> str | None:
    for name, value in scope.get("headers", ()):
        if name.lower() == ACCESS_JWT_HEADER:
            return value.decode("latin-1")
    return None
