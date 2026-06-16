"""FastAPI auth dependencies + RBAC guards."""
from __future__ import annotations

from typing import Any, Dict

from fastapi import Depends, Header, HTTPException, status

from app.core.security import decode_token, role_allows


async def current_user(authorization: str = Header(default="")) -> Dict[str, Any]:
    """Resolve the bearer token to a user claim dict. 401 on missing/invalid token."""
    if not authorization.lower().startswith("bearer "):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing bearer token")
    token = authorization.split(" ", 1)[1].strip()
    claims = decode_token(token)
    if not claims:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired token")
    return claims


def require_role(required: str):
    async def _guard(user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
        if not role_allows(user.get("role", ""), required):
            raise HTTPException(status.HTTP_403_FORBIDDEN, f"Requires role >= {required}")
        return user

    return _guard
