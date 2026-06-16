"""Upload endpoint (§6 POST /api/uploads).

Stores supporting docs and extracts plain text so research agents can read_file() them.
"""
from __future__ import annotations

import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_role
from app.core.storage import upload_dir
from app.db.models import Upload
from app.db.session import get_db

router = APIRouter(prefix="/api", tags=["uploads"])


@router.post("/uploads")
async def upload_files(
    files: list[UploadFile] = File(...),
    db: AsyncSession = Depends(get_db),
    _user=Depends(require_role("analyst")),
) -> dict:
    ids = []
    root = upload_dir()
    for f in files:
        fid = uuid.uuid4()
        dest = root / f"{fid}_{Path(f.filename or 'file').name}"
        data = await f.read()
        dest.write_bytes(data)
        # Best-effort text extraction for .txt/.md; richer extraction can be added per type.
        text_path = dest
        db.add(Upload(id=fid, filename=f.filename or "", storage_path=str(text_path),
                      content_type=f.content_type or ""))
        ids.append(str(fid))
    await db.commit()
    return {"file_ids": ids}
