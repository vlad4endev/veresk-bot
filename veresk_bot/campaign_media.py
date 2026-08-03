"""Хранение фото для рассылок: data/campaign_media/."""

from __future__ import annotations

import re
import uuid
from pathlib import Path

from config import DATABASE_PATH

# Одно фото к тексту — достаточно для промо-букетов
CAMPAIGN_MEDIA_MAX_BYTES = 10 * 1024 * 1024
ALLOWED_IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
ALLOWED_IMAGE_MIME = {
    "image/jpeg",
    "image/jpg",
    "image/png",
    "image/webp",
    "image/gif",
}


def campaign_media_dir() -> Path:
    root = Path(DATABASE_PATH).resolve().parent / "campaign_media"
    root.mkdir(parents=True, exist_ok=True)
    return root


def safe_media_name(name: str) -> bool:
    return bool(re.fullmatch(r"[a-zA-Z0-9._-]{1,120}", name or ""))


def resolve_campaign_media(stored: str | None) -> Path | None:
    """stored — имя файла внутри campaign_media (без путей)."""
    name = (stored or "").strip()
    if not name or not safe_media_name(name):
        return None
    path = (campaign_media_dir() / name).resolve()
    root = campaign_media_dir().resolve()
    if not str(path).startswith(str(root)):
        return None
    if not path.is_file():
        return None
    return path


def guess_ext(filename: str, mime: str) -> str:
    suf = Path(filename or "").suffix.lower()
    if suf in ALLOWED_IMAGE_EXT:
        return suf
    mime_l = (mime or "").lower()
    if "png" in mime_l:
        return ".png"
    if "webp" in mime_l:
        return ".webp"
    if "gif" in mime_l:
        return ".gif"
    return ".jpg"


def save_campaign_photo(
    data: bytes,
    *,
    filename: str = "",
    mime: str = "",
) -> dict:
    """Сохраняет фото, возвращает метаданные для campaigns.*."""
    if not data:
        raise ValueError("empty_file")
    if len(data) > CAMPAIGN_MEDIA_MAX_BYTES:
        raise ValueError("file_too_large")
    mime_l = (mime or "").lower().split(";")[0].strip()
    name_l = (filename or "").lower()
    ok_mime = mime_l in ALLOWED_IMAGE_MIME or mime_l.startswith("image/")
    ok_ext = any(name_l.endswith(ext) for ext in ALLOWED_IMAGE_EXT)
    if not (ok_mime or ok_ext):
        raise ValueError("only_images")
    if mime_l and mime_l not in ALLOWED_IMAGE_MIME and not mime_l.startswith("image/"):
        raise ValueError("only_images")

    ext = guess_ext(filename, mime_l)
    stored = f"{uuid.uuid4().hex}{ext}"
    dest = campaign_media_dir() / stored
    dest.write_bytes(data)
    return {
        "media_path": stored,
        "media_kind": "photo",
        "media_filename": Path(filename or stored).name[:180] or stored,
        "media_mime": mime_l if mime_l in ALLOWED_IMAGE_MIME else f"image/{ext.lstrip('.')}",
    }
