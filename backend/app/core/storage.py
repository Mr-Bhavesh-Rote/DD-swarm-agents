"""Export/upload storage abstraction (§9, §10).

Supports `file://` (local) and `s3://` URIs from EXPORT_STORAGE_URI. Writes bytes and
returns a storage URI; `local_path` resolves a file:// URI back to a filesystem path for
streaming downloads.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit

from app.core.config import get_settings


def _base_uri() -> str:
    return get_settings().export_storage_uri.rstrip("/")


def store_bytes(rel_path: str, data: bytes) -> str:
    base = _base_uri()
    if base.startswith("file://"):
        root = Path(urlsplit(base).path)
        dest = root / rel_path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        return f"file://{dest}"
    if base.startswith("s3://"):
        try:
            import boto3  # optional dependency

            split = urlsplit(base)
            bucket = split.netloc
            prefix = split.path.lstrip("/")
            key = f"{prefix}/{rel_path}".lstrip("/")
            boto3.client("s3").put_object(Bucket=bucket, Key=key, Body=data)
            return f"s3://{bucket}/{key}"
        except Exception as e:  # pragma: no cover
            raise RuntimeError(f"S3 storage failed: {e}")
    raise RuntimeError(f"Unsupported EXPORT_STORAGE_URI scheme: {base}")


def local_path(storage_uri: str) -> Optional[str]:
    if storage_uri.startswith("file://"):
        return urlsplit(storage_uri).path
    return None


def upload_dir() -> Path:
    base = _base_uri()
    if base.startswith("file://"):
        root = Path(urlsplit(base).path).parent / "uploads"
    else:
        root = Path(os.getenv("TMPDIR", "/tmp")) / "deepdd-uploads"
    root.mkdir(parents=True, exist_ok=True)
    return root
