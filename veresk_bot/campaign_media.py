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
# Браузеры/ОС иногда шлют фото как octet-stream — не отвергаем, если расширение или сигнатура ок.
GENERIC_UPLOAD_MIME = {
    "",
    "application/octet-stream",
    "binary/octet-stream",
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


def guess_ext(filename: str, mime: str, data: bytes | None = None) -> str:
    # Сигнатура важнее имени: Windows часто шлёт .JPG при другом содержимом.
    if data:
        kind = sniff_image_kind(data)
        if kind == "png":
            return ".png"
        if kind == "webp":
            return ".webp"
        if kind == "gif":
            return ".gif"
        if kind == "jpeg":
            return ".jpg"
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


def sniff_image_kind(data: bytes) -> str | None:
    """Определяет тип по сигнатуре файла (без доверия к Content-Type)."""
    if not data:
        return None
    if data.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"GIF87a") or data.startswith(b"GIF89a"):
        return "gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


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
    ok_mime = mime_l in ALLOWED_IMAGE_MIME or (
        mime_l.startswith("image/") and mime_l != "image/svg+xml"
    )
    ok_ext = any(name_l.endswith(ext) for ext in ALLOWED_IMAGE_EXT)
    kind = sniff_image_kind(data)
    ok_magic = kind is not None
    generic_mime = mime_l in GENERIC_UPLOAD_MIME
    # image/* MIME или сигнатура файла — ок.
    # Пустой/octet-stream MIME — только с расширением фото или magic bytes.
    if not (
        ok_mime
        or ok_magic
        or ((generic_mime or not mime_l) and ok_ext)
    ):
        raise ValueError("only_images")

    ext = guess_ext(filename, mime_l, data)
    stored = f"{uuid.uuid4().hex}{ext}"
    dest = campaign_media_dir() / stored
    dest.write_bytes(data)
    if mime_l in ALLOWED_IMAGE_MIME:
        out_mime = "image/jpeg" if mime_l == "image/jpg" else mime_l
    elif kind == "jpeg":
        out_mime = "image/jpeg"
    elif kind:
        out_mime = f"image/{kind}"
    else:
        out_mime = "image/jpeg" if ext in (".jpg", ".jpeg") else f"image/{ext.lstrip('.')}"
    return {
        "media_path": stored,
        "media_kind": "photo",
        "media_filename": Path(filename or stored).name[:180] or stored,
        "media_mime": out_mime,
    }
