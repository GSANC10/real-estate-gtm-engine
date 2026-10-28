from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID


def _default(value: Any) -> Any:
    if isinstance(value, (UUID, Decimal)):
        return str(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    raise TypeError(f"not JSON serializable: {type(value).__name__}")


def canonical_json(value: Any) -> str:
    """Stable JSON used for hashing and for exact, reproducible model inputs."""
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=_default
    )


def sha256_hex(value: str | bytes) -> str:
    data = value.encode("utf-8") if isinstance(value, str) else value
    return hashlib.sha256(data).hexdigest()


def json_sha256(value: Any) -> str:
    return sha256_hex(canonical_json(value))


def normalize_email(email: str) -> str:
    return email.strip().lower()


def email_identity_key(email: str) -> str:
    """M1 lead identity and suppression key: sha256 of the normalized supplied email."""
    return sha256_hex(normalize_email(email))


def estimate_tokens(value: Any) -> int:
    """Conservative token estimate (about 4 characters per token) for limits and reservations."""
    text = value if isinstance(value, str) else canonical_json(value)
    return max(1, (len(text) + 3) // 4)
