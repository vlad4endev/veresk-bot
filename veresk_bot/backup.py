"""Резервные копии магазина: база, сессии, фото рассылок, настройки.

Архив — zip с manifest.json. Снимок SQLite через sqlite3.backup(),
чтобы копия была цельной, пока админка работает.
"""

from __future__ import annotations

import json
import logging
import re
import secrets
import shutil
import sqlite3
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from config import DATABASE_PATH, SESSIONS_DIR

logger = logging.getLogger(__name__)

BACKUP_MAGIC = "veresk-backup"
BACKUP_FORMAT = 1
KEEP_MAX = 40
MAX_UPLOAD_BYTES = 200 * 1024 * 1024
TZ = ZoneInfo("Europe/Moscow")
_ID_RE = re.compile(r"^[0-9]{8}-[0-9]{6}-[a-f0-9]{6}$")
_SKIP_SUFFIXES = {".tmp", ".temp", ".db-journal", ".db-wal", ".db-shm", "-wal", "-shm"}
_MONTHS_RU = (
    "января",
    "февраля",
    "марта",
    "апреля",
    "мая",
    "июня",
    "июля",
    "августа",
    "сентября",
    "октября",
    "ноября",
    "декабря",
)


class BackupError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def data_dir() -> Path:
    return Path(DATABASE_PATH).resolve().parent


def backups_dir() -> Path:
    root = data_dir() / "backups"
    root.mkdir(parents=True, exist_ok=True)
    return root


def sessions_dir() -> Path:
    return Path(SESSIONS_DIR).resolve()


def runtime_settings_path() -> Path:
    return data_dir() / "runtime_settings.json"


def campaign_media_dir() -> Path:
    return data_dir() / "campaign_media"


def find_env_path() -> Path | None:
    data = data_dir()
    seen: set[Path] = set()
    for candidate in (
        data.parent / ".env",
        Path(__file__).resolve().parent / ".env",
        data.parent.parent / ".env",
    ):
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        if resolved.is_file():
            return resolved
    return None


def safe_backup_id(value: str) -> str:
    ident = (value or "").strip()
    if not _ID_RE.fullmatch(ident):
        raise BackupError("bad_id", "Некорректный идентификатор копии")
    return ident


def format_bytes(n: int) -> str:
    size = float(max(0, int(n)))
    units = ("Б", "КБ", "МБ", "ГБ")
    idx = 0
    while size >= 1024 and idx < len(units) - 1:
        size /= 1024
        idx += 1
    if idx == 0:
        return f"{int(size)} {units[idx]}"
    return f"{size:.1f} {units[idx]}".replace(".", ",")


def _now() -> datetime:
    return datetime.now(TZ)


def _label_for(dt: datetime) -> str:
    local = dt.astimezone(TZ)
    month = _MONTHS_RU[local.month - 1]
    return f"{local.day} {month} {local.year}, {local:%H:%M}"


def _new_id(now: datetime) -> str:
    stamp = now.strftime("%Y%m%d-%H%M%S")
    return f"{stamp}-{secrets.token_hex(3)}"


def _download_name(now: datetime) -> str:
    return f"veresk-kopiya-{now:%Y-%m-%d-%H%M}.zip"


def zip_path_for(backup_id: str) -> Path:
    return backups_dir() / f"{safe_backup_id(backup_id)}.zip"


def meta_path_for(backup_id: str) -> Path:
    return backups_dir() / f"{safe_backup_id(backup_id)}.meta.json"


def _dir_bytes(root: Path) -> int:
    if not root.is_dir():
        return 0
    total = 0
    for path in root.rglob("*"):
        if path.is_file():
            try:
                total += path.stat().st_size
            except OSError:
                continue
    return total


def _count_files(root: Path) -> int:
    if not root.is_dir():
        return 0
    return sum(1 for path in root.rglob("*") if path.is_file())


def _db_stats(db_path: Path) -> dict[str, int]:
    stats = {"customers": 0, "campaigns": 0, "accounts": 0}
    if not db_path.is_file():
        return stats
    try:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        try:
            tables = {
                str(row[0])
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            if "customers" in tables:
                stats["customers"] = int(
                    conn.execute("SELECT COUNT(*) FROM customers").fetchone()[0]
                )
            if "campaigns" in tables:
                stats["campaigns"] = int(
                    conn.execute("SELECT COUNT(*) FROM campaigns").fetchone()[0]
                )
            if "send_accounts" in tables:
                stats["accounts"] = int(
                    conn.execute("SELECT COUNT(*) FROM send_accounts").fetchone()[0]
                )
        finally:
            conn.close()
    except sqlite3.Error:
        logger.debug("Не удалось прочитать статистику БД %s", db_path, exc_info=True)
    return stats


def live_info() -> dict[str, Any]:
    db = Path(DATABASE_PATH)
    env_path = find_env_path()
    media = campaign_media_dir()
    sessions = sessions_dir()
    settings = runtime_settings_path()
    db_bytes = db.stat().st_size if db.is_file() else 0
    sessions_bytes = _dir_bytes(sessions)
    media_bytes = _dir_bytes(media)
    settings_bytes = settings.stat().st_size if settings.is_file() else 0
    env_bytes = env_path.stat().st_size if env_path else 0
    parts = [
        {
            "id": "database",
            "title": "База данных",
            "detail": "Клиенты, рассылки, сотрудники, Фортуна",
            "present": db.is_file(),
            "bytes": db_bytes,
            "size_label": format_bytes(db_bytes),
        },
        {
            "id": "sessions",
            "title": "Сессии Telegram и MAX",
            "detail": "Подключённые номера для рассылок",
            "present": sessions.is_dir() and _count_files(sessions) > 0,
            "bytes": sessions_bytes,
            "size_label": format_bytes(sessions_bytes),
        },
        {
            "id": "media",
            "title": "Фото рассылок",
            "detail": "Картинки из акций и писем",
            "present": media.is_dir() and _count_files(media) > 0,
            "bytes": media_bytes,
            "size_label": format_bytes(media_bytes),
        },
        {
            "id": "settings",
            "title": "Настройки панели",
            "detail": "Токены MAX, ИИ, автопоздравления",
            "present": settings.is_file(),
            "bytes": settings_bytes,
            "size_label": format_bytes(settings_bytes),
        },
        {
            "id": "env",
            "title": "Ключи .env",
            "detail": "Секреты сервера (бот, Posiflora, пароль)",
            "present": bool(env_path),
            "bytes": env_bytes,
            "size_label": format_bytes(env_bytes),
        },
    ]
    stats = _db_stats(db)
    total = db_bytes + sessions_bytes + media_bytes + settings_bytes + env_bytes
    return {
        "stats": stats,
        "size_bytes": total,
        "size_label": format_bytes(total),
        "parts": parts,
        "max_upload_bytes": MAX_UPLOAD_BYTES,
        "max_upload_label": format_bytes(MAX_UPLOAD_BYTES),
    }


def _public_item(meta: dict[str, Any]) -> dict[str, Any]:
    created = str(meta.get("created_at") or "")
    try:
        dt = datetime.fromisoformat(created)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=TZ)
        label = _label_for(dt)
    except ValueError:
        label = created or "—"
        dt = None
    size = int(meta.get("size_bytes") or 0)
    kind = str(meta.get("kind") or "manual")
    kind_label = "Перед восстановлением" if kind == "safety" else "Копия"
    contents = meta.get("contents") if isinstance(meta.get("contents"), dict) else {}
    stats = meta.get("stats") if isinstance(meta.get("stats"), dict) else {}
    backup_id = str(meta.get("id") or "")
    filename = str(meta.get("filename") or (f"veresk-kopiya-{backup_id}.zip" if backup_id else "veresk-kopiya.zip"))
    return {
        "id": backup_id,
        "kind": kind,
        "kind_label": kind_label,
        "created_at": created,
        "created_label": label,
        "label": str(meta.get("label") or f"{kind_label} от {label}"),
        "filename": filename,
        "size_bytes": size,
        "size_label": format_bytes(size),
        "stats": {
            "customers": int(stats.get("customers") or 0),
            "campaigns": int(stats.get("campaigns") or 0),
            "accounts": int(stats.get("accounts") or 0),
        },
        "contents": {
            "database": bool(contents.get("database")),
            "sessions": bool(contents.get("sessions")),
            "media": bool(contents.get("media")),
            "settings": bool(contents.get("settings")),
            "env": bool(contents.get("env")),
        },
        "note": str(meta.get("note") or ""),
    }


def _write_meta(backup_id: str, meta: dict[str, Any]) -> None:
    path = meta_path_for(backup_id)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _read_meta_file(backup_id: str) -> dict[str, Any] | None:
    path = meta_path_for(backup_id)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _read_manifest_from_zip(path: Path) -> dict[str, Any] | None:
    try:
        with zipfile.ZipFile(path, "r") as zf:
            if "manifest.json" not in zf.namelist():
                return None
            raw = zf.read("manifest.json")
        data = json.loads(raw.decode("utf-8"))
    except (OSError, zipfile.BadZipFile, json.JSONDecodeError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _load_backup_meta(backup_id: str) -> dict[str, Any] | None:
    zpath = backups_dir() / f"{backup_id}.zip"
    if not zpath.is_file():
        return None
    meta = _read_meta_file(backup_id)
    if not meta:
        manifest = _read_manifest_from_zip(zpath)
        if not manifest:
            return None
        meta = dict(manifest)
        meta["id"] = backup_id
        try:
            meta["size_bytes"] = zpath.stat().st_size
        except OSError:
            meta["size_bytes"] = 0
    meta["id"] = backup_id
    return meta


def list_backups() -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for path in backups_dir().glob("*.zip"):
        ident = path.stem
        if not _ID_RE.fullmatch(ident):
            continue
        meta = _load_backup_meta(ident)
        if not meta:
            continue
        items.append(_public_item(meta))
    items.sort(key=lambda row: str(row.get("created_at") or ""), reverse=True)
    return items


def get_backup(backup_id: str) -> dict[str, Any] | None:
    meta = _load_backup_meta(safe_backup_id(backup_id))
    return _public_item(meta) if meta else None


def resolve_backup_file(backup_id: str) -> Path | None:
    path = zip_path_for(backup_id)
    return path if path.is_file() else None


def _snapshot_sqlite(src: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        dest.unlink()
    src_conn = sqlite3.connect(str(src))
    try:
        dest_conn = sqlite3.connect(str(dest))
        try:
            src_conn.backup(dest_conn)
            dest_conn.commit()
        finally:
            dest_conn.close()
    finally:
        src_conn.close()


def _add_tree(zf: zipfile.ZipFile, root: Path, arc_prefix: str) -> int:
    if not root.exists():
        return 0
    added = 0
    if root.is_file():
        zf.write(root, arc_prefix)
        return 1
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if path.suffix.lower() in _SKIP_SUFFIXES:
            continue
        if any(path.name.endswith(suf) for suf in ("-wal", "-shm", "-journal")):
            continue
        rel = path.relative_to(root).as_posix()
        zf.write(path, f"{arc_prefix}/{rel}")
        added += 1
    return added


def _prune_old() -> None:
    items = list_backups()
    extra = items[KEEP_MAX:]
    for row in extra:
        try:
            delete_backup(str(row["id"]))
        except BackupError:
            continue


def create_backup(*, kind: str = "manual", note: str = "") -> dict[str, Any]:
    now = _now()
    backup_id = _new_id(now)
    filename = _download_name(now)
    work = Path(tempfile.mkdtemp(prefix="veresk-bak-", dir=str(backups_dir())))
    db_src = Path(DATABASE_PATH)
    db_snap = work / "veresk.db"
    try:
        if db_src.is_file():
            _snapshot_sqlite(db_src, db_snap)
        stats = _db_stats(db_snap if db_snap.is_file() else db_src)
        env_path = find_env_path()
        contents = {
            "database": db_snap.is_file(),
            "sessions": sessions_dir().is_dir() and _count_files(sessions_dir()) > 0,
            "media": campaign_media_dir().is_dir() and _count_files(campaign_media_dir()) > 0,
            "settings": runtime_settings_path().is_file(),
            "env": bool(env_path),
        }
        if not contents["database"]:
            raise BackupError("no_database", "База данных ещё не создана — копировать нечего")
        manifest = {
            "magic": BACKUP_MAGIC,
            "format": BACKUP_FORMAT,
            "id": backup_id,
            "kind": kind if kind in ("manual", "safety") else "manual",
            "created_at": now.isoformat(),
            "label": f"{'Перед восстановлением' if kind == 'safety' else 'Копия'} от {_label_for(now)}",
            "filename": filename,
            "note": (note or "").strip()[:240],
            "contents": contents,
            "stats": stats,
        }
        zip_dest = zip_path_for(backup_id)
        tmp_zip = work / "archive.zip"
        with zipfile.ZipFile(tmp_zip, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(
                "manifest.json",
                json.dumps(manifest, ensure_ascii=False, indent=2),
            )
            zf.write(db_snap, "veresk.db")
            if contents["settings"]:
                zf.write(runtime_settings_path(), "runtime_settings.json")
            if env_path:
                zf.write(env_path, ".env")
            _add_tree(zf, sessions_dir(), "sessions")
            _add_tree(zf, campaign_media_dir(), "campaign_media")
        shutil.move(str(tmp_zip), str(zip_dest))
        size = zip_dest.stat().st_size
        manifest["size_bytes"] = size
        _write_meta(backup_id, manifest)
        logger.info("Создана резервная копия %s (%s)", backup_id, format_bytes(size))
        _prune_old()
        return _public_item(manifest)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def delete_backup(backup_id: str) -> bool:
    ident = safe_backup_id(backup_id)
    zpath = zip_path_for(ident)
    mpath = meta_path_for(ident)
    if not zpath.is_file() and not mpath.is_file():
        raise BackupError("not_found", "Такой копии нет")
    if zpath.is_file():
        zpath.unlink()
    if mpath.is_file():
        mpath.unlink()
    return True


def _validate_manifest(data: dict[str, Any]) -> dict[str, Any]:
    if data.get("magic") != BACKUP_MAGIC:
        raise BackupError(
            "not_veresk",
            "Это не копия Veresk. Нужен файл, скачанный из Настройки → Копии",
        )
    try:
        fmt = int(data.get("format") or 1)
    except (TypeError, ValueError):
        fmt = 1
    if fmt > BACKUP_FORMAT:
        raise BackupError(
            "too_new",
            "Копия сделана в более новой версии панели. Обновите сервер и повторите",
        )
    return data


def _open_backup_zip(path: Path) -> zipfile.ZipFile:
    if not path.is_file():
        raise BackupError("not_found", "Файл копии не найден")
    if path.stat().st_size > MAX_UPLOAD_BYTES:
        raise BackupError(
            "file_too_large",
            f"Файл больше {format_bytes(MAX_UPLOAD_BYTES)}",
        )
    try:
        zf = zipfile.ZipFile(path, "r")
    except zipfile.BadZipFile as exc:
        raise BackupError("bad_zip", "Файл повреждён или это не zip") from exc
    names = zf.namelist()
    if "manifest.json" not in names or "veresk.db" not in names:
        zf.close()
        raise BackupError(
            "incomplete",
            "В архиве нет базы. Нужна полная копия из раздела «Копии»",
        )
    try:
        manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        zf.close()
        raise BackupError("bad_manifest", "Внутри копии повреждено описание") from exc
    if not isinstance(manifest, dict):
        zf.close()
        raise BackupError("bad_manifest", "Внутри копии повреждено описание")
    try:
        _validate_manifest(manifest)
    except BackupError:
        zf.close()
        raise
    return zf


def _extract_member(zf: zipfile.ZipFile, name: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with zf.open(name) as src, dest.open("wb") as out:
        shutil.copyfileobj(src, out, length=1024 * 256)


def _safe_zip_path(name: str, prefix: str) -> str | None:
    raw = name.replace("\\", "/").lstrip("/")
    if raw.endswith("/") or ".." in Path(raw).parts:
        return None
    if not raw.startswith(prefix + "/"):
        return None
    rel = raw[len(prefix) + 1 :]
    if not rel or rel.startswith("/") or ".." in Path(rel).parts:
        return None
    return rel


def _restore_tree(zf: zipfile.ZipFile, prefix: str, dest: Path) -> int:
    dest.mkdir(parents=True, exist_ok=True)
    restored = 0
    for name in zf.namelist():
        rel = _safe_zip_path(name, prefix)
        if not rel:
            continue
        target = (dest / rel).resolve()
        if not str(target).startswith(str(dest.resolve())):
            continue
        _extract_member(zf, name, target)
        restored += 1
    return restored


def _restore_sqlite_into_live(snapshot: Path) -> None:
    live = Path(DATABASE_PATH)
    live.parent.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(str(snapshot))
    try:
        dest = sqlite3.connect(str(live))
        try:
            dest.execute("PRAGMA foreign_keys = OFF")
            src.backup(dest)
            dest.commit()
            dest.execute("PRAGMA foreign_keys = ON")
        finally:
            dest.close()
    finally:
        src.close()


def restore_from_zip(path: Path) -> dict[str, Any]:
    """Восстанавливает данные из zip. Перед этим делает страховочную копию."""
    zf = _open_backup_zip(path)
    work = Path(tempfile.mkdtemp(prefix="veresk-rst-", dir=str(backups_dir())))
    try:
        snap = work / "veresk.db"
        _extract_member(zf, "veresk.db", snap)
        names = set(zf.namelist())
        _restore_sqlite_into_live(snap)
        if "runtime_settings.json" in names:
            _extract_member(zf, "runtime_settings.json", runtime_settings_path())
            try:
                import runtime_settings

                runtime_settings.invalidate_cache()
            except Exception:
                logger.debug("Не удалось сбросить кэш runtime_settings", exc_info=True)
        env_dest = find_env_path()
        if ".env" in names:
            if env_dest is None:
                env_dest = Path(__file__).resolve().parent / ".env"
            _extract_member(zf, ".env", env_dest)
        _restore_tree(zf, "sessions", sessions_dir())
        _restore_tree(zf, "campaign_media", campaign_media_dir())
        restored_from = _read_manifest_from_zip(path) or {}
        logger.info("Восстановлена копия %s", restored_from.get("id") or path.name)
        return {
            "ok": True,
            "restart_recommended": ".env" in names,
            "source": {
                "id": restored_from.get("id") or "",
                "label": restored_from.get("label") or path.name,
                "created_at": restored_from.get("created_at") or "",
            },
        }
    finally:
        zf.close()
        shutil.rmtree(work, ignore_errors=True)


def restore_backup(backup_id: str) -> dict[str, Any]:
    ident = safe_backup_id(backup_id)
    path = resolve_backup_file(ident)
    if not path:
        raise BackupError("not_found", "Такой копии нет")
    # Копия на время restore: prune после страховочного снимка не должен её стереть
    hold = backups_dir() / f".hold-{ident}.zip"
    shutil.copy2(path, hold)
    try:
        safety = create_backup(kind="safety", note=f"Перед восстановлением {ident}")
        result = restore_from_zip(hold)
        result["safety"] = safety
        return result
    finally:
        hold.unlink(missing_ok=True)


def import_uploaded_zip(src: Path, *, original_name: str = "") -> dict[str, Any]:
    """Проверяет загруженный zip и кладёт его в список копий."""
    zf = _open_backup_zip(src)
    try:
        raw = json.loads(zf.read("manifest.json").decode("utf-8"))
    finally:
        zf.close()
    if not isinstance(raw, dict):
        raise BackupError("bad_manifest", "Внутри копии повреждено описание")
    now = _now()
    backup_id = _new_id(now)
    filename = Path(original_name or "").name
    if not filename.lower().endswith(".zip"):
        filename = _download_name(now)
    filename = re.sub(r"[^a-zA-Z0-9._-]+", "-", filename)[:120] or _download_name(now)
    dest = zip_path_for(backup_id)
    shutil.copy2(src, dest)
    meta = {
        "magic": BACKUP_MAGIC,
        "format": int(raw.get("format") or 1),
        "id": backup_id,
        "kind": "manual",
        "created_at": str(raw.get("created_at") or now.isoformat()),
        "imported_at": now.isoformat(),
        "label": str(raw.get("label") or f"Загружена {filename}"),
        "filename": filename,
        "note": "Загружена с компьютера",
        "contents": raw.get("contents") if isinstance(raw.get("contents"), dict) else {},
        "stats": raw.get("stats") if isinstance(raw.get("stats"), dict) else {},
        "size_bytes": dest.stat().st_size,
        "source_id": str(raw.get("id") or ""),
    }
    _write_meta(backup_id, meta)
    _prune_old()
    return _public_item(meta)
