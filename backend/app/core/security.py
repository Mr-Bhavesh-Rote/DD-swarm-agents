"""Auth + minimal RBAC (§3.1, §10).

JWT bearer tokens; roles admin/analyst/viewer. Secrets come only from settings; tokens
carry the user id, email and role.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

import bcrypt
import jwt

from app.core.config import get_settings

ALGORITHM = "HS256"

# Role hierarchy for RBAC checks.
ROLE_RANK = {"viewer": 0, "analyst": 1, "admin": 2}

# bcrypt hashes at most the first 72 bytes of the password — truncate explicitly
# (bcrypt 4.x raises instead of silently truncating).
def _to_72(raw: str) -> bytes:
    return raw.encode("utf-8")[:72]


def hash_password(raw: str) -> str:
    return bcrypt.hashpw(_to_72(raw), bcrypt.gensalt()).decode("utf-8")


def verify_password(raw: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(_to_72(raw), hashed.encode("utf-8"))
    except Exception:
        return False


def create_access_token(*, user_id: str, email: str, role: str) -> str:
    settings = get_settings()
    now = datetime.now(timezone.utc)
    payload = {
        "sub": user_id,
        "email": email,
        "role": role,
        "iat": now,
        "exp": now + timedelta(minutes=settings.jwt_expiry_minutes),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=ALGORITHM)


def decode_token(token: str) -> Optional[Dict[str, Any]]:
    settings = get_settings()
    try:
        return jwt.decode(token, settings.jwt_secret, algorithms=[ALGORITHM])
    except jwt.PyJWTError:
        return None


def role_allows(user_role: str, required: str) -> bool:
    return ROLE_RANK.get(user_role, -1) >= ROLE_RANK.get(required, 99)
