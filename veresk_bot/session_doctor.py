#!/usr/bin/env python3
"""
Доктор сессий Telegram (Telethon) и MAX (PyMax).

Главное правило надёжности: один процесс — один .session / max_acc_*.db.
Второй Telethon на тот же файл даёт AuthKeyDuplicated, и Telegram отзывает вход.
Поэтому пока жив bot API, живой коннект делает только он (HTTP keepalive).
Локальный connect — только если бот лежит, или явно --force-local.

Примеры:
  python session_doctor.py doctor
  python session_doctor.py heal
  python session_doctor.py recover
  python session_doctor.py watch
  ./sessions.sh doctor
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

# чтобы `python session_doctor.py` находил mailing_db / config
sys.path.insert(0, str(Path(__file__).resolve().parent))

API_BASE = os.getenv("SESSION_DOCTOR_API", "http://127.0.0.1:3005").rstrip("/")
STALE_OK_MIN = max(15, int(os.getenv("SESSION_DOCTOR_STALE_MIN", "25") or "25"))
KEEPALIVE_HTTP_TIMEOUT = max(
    60, int(os.getenv("SESSION_DOCTOR_KEEPALIVE_TIMEOUT", "180") or "180")
)

_CONF: dict[str, Any] | None = None


def _conf() -> dict[str, Any]:
    global _CONF
    if _CONF is None:
        from config import ADMIN_PASSWORD, ADMIN_USERNAME, SESSIONS_DIR

        _CONF = {
            "admin_user": ADMIN_USERNAME,
            "admin_password": ADMIN_PASSWORD,
            "sessions_dir": SESSIONS_DIR,
        }
    return _CONF


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _sessions_dir() -> Path:
    path = Path(_conf()["sessions_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def _digits(phone: str) -> str:
    d = re.sub(r"\D", "", phone or "")
    if len(d) == 11 and d[0] in "78":
        d = d[1:]
    return d


def _phone_plus(digits: str) -> str:
    d = re.sub(r"\D", "", digits or "")
    if len(d) == 10:
        d = "7" + d
    if len(d) == 11 and d[0] == "8":
        d = "7" + d[1:]
    return f"+{d}" if d else ""


def _phone_from_session_path(path: Path, kind: str) -> str:
    stem = path.stem
    if kind == "max_userbot":
        raw = stem.replace("max_acc_", "")
    else:
        raw = stem.replace("acc_", "").replace("qr_", "")
    return _phone_plus(raw)


def bot_is_up(timeout: float = 2.5) -> bool:
    try:
        with urllib.request.urlopen(f"{API_BASE}/api/health", timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8") or "{}")
            return bool(data.get("ok"))
    except Exception:
        return False


def _http_json(
    method: str,
    path: str,
    *,
    token: str | None = None,
    body: dict[str, Any] | None = None,
    timeout: float = 30,
) -> tuple[int, dict[str, Any]]:
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(
        f"{API_BASE}{path}",
        data=data,
        headers=headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8") or "{}"
            payload = json.loads(raw) if raw.strip() else {}
            return int(resp.status), payload if isinstance(payload, dict) else {"data": payload}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
        try:
            payload = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            payload = {"error": raw or str(exc)}
        if not isinstance(payload, dict):
            payload = {"error": str(payload)}
        return int(exc.code), payload
    except Exception as exc:
        return 0, {"error": str(exc)}


def admin_login(user: str, password: str) -> str | None:
    code, payload = _http_json(
        "POST",
        "/api/admin/login",
        body={"username": user, "password": password},
        timeout=15,
    )
    if code == 200 and payload.get("token"):
        return str(payload["token"])
    return None


def _sqlite_ro(path: Path) -> sqlite3.Connection | None:
    uri = f"file:{path.resolve()}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=2.5)
        conn.execute("PRAGMA busy_timeout=2000")
        return conn
    except sqlite3.Error:
        return None


def _integrity_ok(path: Path) -> tuple[bool, str]:
    conn = _sqlite_ro(path)
    if conn is None:
        return False, "sqlite_locked_or_unreadable"
    try:
        row = conn.execute("PRAGMA integrity_check").fetchone()
        msg = str(row[0]) if row else "empty"
        return msg.lower() == "ok", msg
    except sqlite3.Error as exc:
        return False, str(exc)
    finally:
        conn.close()


def inspect_telegram_file(path: Path) -> dict[str, Any]:
    info: dict[str, Any] = {
        "path": str(path),
        "exists": path.is_file(),
        "size": path.stat().st_size if path.is_file() else 0,
        "kind": "tg_userbot",
        "has_auth_key": False,
        "sqlite_ok": False,
        "journal": (Path(str(path) + "-journal")).is_file(),
        "mtime": datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds")
        if path.is_file()
        else None,
    }
    if not path.is_file():
        info["error"] = "missing"
        return info
    ok, msg = _integrity_ok(path)
    info["sqlite_ok"] = ok
    info["sqlite_msg"] = msg
    conn = _sqlite_ro(path)
    if conn is None:
        info["error"] = "locked"
        return info
    try:
        tables = {
            str(r[0])
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        if "sessions" in tables:
            row = conn.execute(
                "SELECT length(auth_key) FROM sessions WHERE auth_key IS NOT NULL LIMIT 1"
            ).fetchone()
            info["has_auth_key"] = bool(row and int(row[0] or 0) > 16)
    except sqlite3.Error as exc:
        info["error"] = str(exc)
    finally:
        conn.close()
    return info


def inspect_max_file(path: Path) -> dict[str, Any]:
    info: dict[str, Any] = {
        "path": str(path),
        "exists": path.is_file(),
        "size": path.stat().st_size if path.is_file() else 0,
        "kind": "max_userbot",
        "has_auth_key": False,
        "sqlite_ok": False,
        "wal": Path(str(path) + "-wal").is_file(),
        "shm": Path(str(path) + "-shm").is_file(),
        "journal": Path(str(path) + "-journal").is_file(),
        "mtime": datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds")
        if path.is_file()
        else None,
    }
    if not path.is_file():
        info["error"] = "missing"
        return info
    ok, msg = _integrity_ok(path)
    info["sqlite_ok"] = ok
    info["sqlite_msg"] = msg
    conn = _sqlite_ro(path)
    if conn is None:
        # MAX часто держит WAL — файл живой, просто занят
        info["busy"] = True
        info["has_auth_key"] = info["size"] > 8_000
        return info
    try:
        tables = {
            str(r[0]).lower()
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        info["tables"] = sorted(tables)
        authish = [
            t
            for t in tables
            if any(x in t for x in ("session", "auth", "token", "login", "user"))
        ]
        if authish or info["size"] > 8_000:
            info["has_auth_key"] = True
    except sqlite3.Error as exc:
        info["error"] = str(exc)
        info["has_auth_key"] = info["size"] > 8_000
    finally:
        conn.close()
    return info


def file_holders(path: Path) -> list[dict[str, str]]:
    """Кто держит файл (Linux /proc). Помогает ловить второй процесс на сессии."""
    holders: list[dict[str, str]] = []
    proc = Path("/proc")
    if not proc.is_dir() or not path.exists():
        return holders
    try:
        target = str(path.resolve())
    except OSError:
        return holders
    for pid_dir in proc.iterdir():
        if not pid_dir.name.isdigit():
            continue
        fd_dir = pid_dir / "fd"
        try:
            for fd in fd_dir.iterdir():
                try:
                    if os.path.realpath(fd) == target:
                        cmd = ""
                        try:
                            cmd = (pid_dir / "cmdline").read_bytes().replace(b"\x00", b" ").decode(
                                "utf-8", errors="replace"
                            ).strip()
                        except OSError:
                            pass
                        holders.append({"pid": pid_dir.name, "cmd": cmd[:160]})
                        break
                except OSError:
                    continue
        except OSError:
            continue
    return holders


def _parse_dt(raw: Any) -> datetime | None:
    if not raw:
        return None
    text = str(raw).strip()
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f"):
        try:
            return datetime.strptime(text[:26], fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def _age_minutes(raw: Any) -> int | None:
    dt = _parse_dt(raw)
    if not dt:
        return None
    return max(0, int((datetime.now() - dt).total_seconds() // 60))


def _scan_files() -> tuple[list[Path], list[Path]]:
    root = _sessions_dir()
    tg = sorted(root.glob("acc_*.session")) + sorted(root.glob("qr_*.session"))
    mx = sorted(root.glob("max_acc_*.db"))
    return tg, mx


async def doctor_report() -> dict[str, Any]:
    from mailing_db import init_mailing_db, list_send_accounts

    await init_mailing_db()
    rows = await list_send_accounts()
    tg_files, max_files = _scan_files()
    known_files: set[str] = set()
    for row in rows:
        sf = str(row.get("session_file") or "").strip()
        if sf:
            known_files.add(str(Path(sf).resolve()) if sf else sf)
            known_files.add(sf)

    def _known(path: Path) -> bool:
        try:
            res = str(path.resolve())
        except OSError:
            res = str(path)
        return str(path) in known_files or res in known_files

    accounts: list[dict[str, Any]] = []
    errors: list[str] = []
    warnings: list[str] = []
    hints: list[str] = []

    bot_up = bot_is_up()
    if not bot_up:
        warnings.append("Bot API не отвечает — живой коннект через HTTP недоступен")

    userbots = [r for r in rows if r.get("kind") in ("tg_userbot", "max_userbot")]
    phones: dict[tuple[str, str], list[int]] = {}

    for row in userbots:
        kind = str(row.get("kind"))
        sf = str(row.get("session_file") or "").strip()
        path = Path(sf) if sf else None
        phone = str(row.get("phone") or "")
        key = (kind, _digits(phone))
        if key[1]:
            phones.setdefault(key, []).append(int(row["id"]))

        item: dict[str, Any] = {
            "id": row.get("id"),
            "kind": kind,
            "phone": phone,
            "label": row.get("label"),
            "status": row.get("status"),
            "last_ok_at": row.get("last_ok_at"),
            "last_checked_at": row.get("last_checked_at"),
            "last_error": row.get("last_error"),
            "fail_streak": int(row.get("fail_streak") or 0),
            "session_file": sf,
        }
        if not path or not path.is_file():
            item["file"] = {"exists": False}
            item["severity"] = "error"
            errors.append(
                f"{kind} id={row.get('id')} {phone or 'без номера'}: нет файла сессии"
            )
        else:
            file_info = (
                inspect_max_file(path)
                if kind == "max_userbot"
                else inspect_telegram_file(path)
            )
            holders = file_holders(path)
            file_info["holders"] = holders
            item["file"] = file_info
            if len(holders) > 1:
                warnings.append(
                    f"{kind} {phone}: файл открыт {len(holders)} процессами — риск AuthKeyDuplicated"
                )
            if kind == "tg_userbot" and not file_info.get("has_auth_key"):
                item["severity"] = "error"
                errors.append(
                    f"Telegram {phone}: в файле нет auth_key — сессия не авторизована"
                )
            elif not file_info.get("sqlite_ok") and not file_info.get("busy"):
                item["severity"] = "warn"
                warnings.append(
                    f"{kind} {phone}: SQLite {file_info.get('sqlite_msg')}"
                )
            elif row.get("status") == "unavailable":
                item["severity"] = "error"
                errors.append(
                    f"{kind} {phone}: статус unavailable — {row.get('last_error') or 'нужен повторный вход'}"
                )
            else:
                age = _age_minutes(row.get("last_ok_at"))
                if age is None:
                    item["severity"] = "warn"
                    warnings.append(f"{kind} {phone}: ещё ни разу не было успешного last_ok_at")
                elif age > STALE_OK_MIN:
                    item["severity"] = "warn"
                    warnings.append(
                        f"{kind} {phone}: последний успешный коннект {age} мин назад"
                    )
                else:
                    item["severity"] = "ok"
        accounts.append(item)

    for key, ids in phones.items():
        if len(ids) > 1:
            warnings.append(
                f"дубликаты {key[0]} на номер …{key[1][-4:]}: id {ids} — оставьте один (--dedupe)"
            )

    orphans: list[dict[str, Any]] = []
    for path in tg_files:
        if _known(path):
            continue
        info = inspect_telegram_file(path)
        info["phone"] = _phone_from_session_path(path, "tg_userbot")
        orphans.append(info)
        if path.name.startswith("qr_") and not info.get("has_auth_key"):
            continue
        if info.get("has_auth_key"):
            warnings.append(
                f"сирота Telegram {path.name} (есть auth_key) — recover подхватит в БД"
            )
        elif path.name.startswith("acc_"):
            warnings.append(f"сирота {path.name} без auth_key — незавершённый вход")

    for path in max_files:
        if _known(path):
            continue
        info = inspect_max_file(path)
        info["phone"] = _phone_from_session_path(path, "max_userbot")
        orphans.append(info)
        warnings.append(f"сирота MAX {path.name} — recover подхватит в БД")

    if not userbots:
        hints.append("Нет userbot-номеров в БД. Подключите Telegram/MAX в Настройках.")
    if errors:
        hints.append("Нажмите «Переподключить» в админке или: python session_doctor.py heal")
    elif warnings:
        hints.append("heal через API безопасен при живом боте (не открывает второй Telethon).")
    else:
        hints.append("Сессии выглядят живыми. watch можно поставить рядом как страховку.")

    if bot_up:
        hints.append("Bot API жив — heal пойдёт через него, без второго коннекта к файлам.")
    else:
        hints.append("Бот лежит — heal --force-local сам откроет сессии (это безопасно только пока бот выключен).")

    severity = "ok"
    if errors:
        severity = "error"
    elif warnings:
        severity = "warn"

    return {
        "ok": severity == "ok",
        "severity": severity,
        "at": _now(),
        "bot_up": bot_up,
        "api": API_BASE,
        "sessions_dir": str(_sessions_dir()),
        "accounts": accounts,
        "orphans": orphans,
        "errors": errors,
        "warnings": warnings,
        "hints": hints,
        "counts": {
            "accounts": len(accounts),
            "errors": len(errors),
            "warnings": len(warnings),
            "orphans": len(orphans),
        },
    }


def _print_report(report: dict[str, Any], *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return
    sev = report.get("severity")
    mark = {"ok": "OK", "warn": "WARN", "error": "FAIL"}.get(sev, "?")
    print(f"[{mark}] сессии  bot_api={'up' if report.get('bot_up') else 'down'}  {report.get('at')}")
    print(f"  dir  {report.get('sessions_dir')}")
    print(f"  api  {report.get('api')}")
    for acc in report.get("accounts") or []:
        tag = {"ok": "+", "warn": "!", "error": "x"}.get(acc.get("severity"), ".")
        extra = acc.get("last_error") or acc.get("status") or ""
        print(
            f"  [{tag}] {acc.get('kind'):12}  {acc.get('phone') or '—':16}  "
            f"status={acc.get('status') or '—'}  last_ok={acc.get('last_ok_at') or '—'}"
            + (f"  {extra}" if acc.get("severity") != "ok" and extra else "")
        )
    for err in report.get("errors") or []:
        print(f"  ERROR  {err}")
    for warn in report.get("warnings") or []:
        print(f"  WARN   {warn}")
    for hint in report.get("hints") or []:
        print(f"  hint   {hint}")


async def recover_orphans(*, dry_run: bool = False) -> dict[str, Any]:
    """Зарегистрировать сиротские файлы в БД без второго MTProto-коннекта."""
    from mailing_db import (
        create_send_account,
        find_send_account_by_kind_phone,
        init_mailing_db,
        normalize_phone_db,
        update_send_account,
    )

    await init_mailing_db()
    report = await doctor_report()
    attached: list[dict[str, Any]] = []
    skipped: list[str] = []
    for orphan in report.get("orphans") or []:
        path = Path(str(orphan.get("path") or ""))
        kind = str(orphan.get("kind") or "")
        phone = str(orphan.get("phone") or "")
        if not path.is_file() or not kind:
            continue
        if kind == "tg_userbot" and path.name.startswith("qr_") and not orphan.get("has_auth_key"):
            skipped.append(f"пропуск незавершённого {path.name}")
            continue
        if kind == "tg_userbot" and not orphan.get("has_auth_key"):
            skipped.append(f"пропуск {path.name}: нет auth_key")
            continue
        if not phone:
            skipped.append(f"пропуск {path.name}: не разобрать телефон")
            continue
        phone = normalize_phone_db(phone) or phone
        existing = await find_send_account_by_kind_phone(kind, phone)
        if dry_run:
            attached.append(
                {
                    "would": "update" if existing else "create",
                    "kind": kind,
                    "phone": phone,
                    "path": str(path),
                }
            )
            continue
        if existing:
            await update_send_account(
                int(existing["id"]),
                session_file=str(path),
            )
            attached.append(
                {
                    "action": "updated",
                    "id": existing["id"],
                    "kind": kind,
                    "phone": phone,
                }
            )
        else:
            acc_id = await create_send_account(
                kind=kind,
                label=phone,
                phone=phone,
                session_file=str(path),
                daily_limit=200 if kind == "tg_userbot" else 150,
                status="warmup",
                warmup_until=(datetime.now() + timedelta(days=4)).date().isoformat(),
            )
            attached.append(
                {"action": "created", "id": acc_id, "kind": kind, "phone": phone}
            )
    return {"attached": attached, "skipped": skipped, "dry_run": dry_run}


async def dedupe_accounts(*, dry_run: bool = False) -> dict[str, Any]:
    from mailing_db import delete_send_account, init_mailing_db, list_send_accounts

    await init_mailing_db()
    rows = [
        r
        for r in await list_send_accounts()
        if r.get("kind") in ("tg_userbot", "max_userbot")
    ]
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in rows:
        key = (str(row.get("kind")), _digits(str(row.get("phone") or "")))
        if not key[1]:
            continue
        groups.setdefault(key, []).append(row)
    removed: list[dict[str, Any]] = []
    for (_kind, _digs), items in groups.items():
        if len(items) < 2:
            continue

        def _score(item: dict[str, Any]) -> tuple[int, int, int]:
            has_file = 1 if Path(str(item.get("session_file") or "")).is_file() else 0
            ready = 1 if item.get("status") in ("ready", "warmup") else 0
            return (has_file, ready, int(item.get("id") or 0))

        keep = sorted(items, key=_score, reverse=True)[0]
        for item in items:
            if int(item["id"]) == int(keep["id"]):
                continue
            rec = {
                "id": item["id"],
                "kind": item.get("kind"),
                "phone": item.get("phone"),
                "keep_id": keep["id"],
            }
            if not dry_run:
                # файл не трогаем, если он тот же, что у keep
                same_file = str(item.get("session_file") or "") == str(
                    keep.get("session_file") or ""
                )
                await delete_send_account(int(item["id"]))
                rec["file_kept"] = same_file
            removed.append(rec)
    return {"removed": removed, "dry_run": dry_run}


def heal_via_api(user: str, password: str) -> dict[str, Any]:
    token = admin_login(user, password)
    if not token:
        return {
            "ok": False,
            "error": "login_failed",
            "detail": "Не удалось войти в админ API. Проверьте ADMIN_USERNAME / ADMIN_PASSWORD.",
        }
    # GET accounts подхватывает qr-сирот в том же процессе бота
    _http_json("GET", "/api/admin/accounts", token=token, timeout=60)
    code, payload = _http_json(
        "POST",
        "/api/admin/accounts/telegram/keepalive",
        token=token,
        body={},
        timeout=KEEPALIVE_HTTP_TIMEOUT,
    )
    list_code, listed = _http_json("GET", "/api/admin/accounts", token=token, timeout=45)
    dead = listed.get("dead_sessions") if isinstance(listed, dict) else None
    return {
        "ok": code == 200 and bool(payload.get("ok") or payload.get("skipped")),
        "http": code,
        "keepalive": payload,
        "list_http": list_code,
        "dead_sessions": dead or [],
        "via": "api",
    }


async def heal_local() -> dict[str, Any]:
    from mailing_db import init_mailing_db
    from senders.session_keepalive import keepalive_all_telegram_sessions
    from senders.telegram_userbot import recover_authorized_qr_sessions

    await init_mailing_db()
    recovered = []
    try:
        recovered = await recover_authorized_qr_sessions()
    except Exception as exc:
        recovered = [{"error": str(exc)}]
    result = await keepalive_all_telegram_sessions()
    result["recovered"] = recovered
    result["via"] = "local"
    return result


async def cmd_heal(args: argparse.Namespace) -> int:
    bot_up = bot_is_up()
    rec = await recover_orphans(dry_run=False)
    if args.json:
        pass
    else:
        if rec.get("attached"):
            print(f"recover: {len(rec['attached'])} сирот в БД")
        for skip in rec.get("skipped") or []:
            print(f"  skip  {skip}")

    if bot_up and not args.force_local:
        user = args.user or _conf()["admin_user"]
        password = args.password or _conf()["admin_password"]
        result = heal_via_api(user, password)
        if args.json:
            print(json.dumps({"recover": rec, "heal": result}, ensure_ascii=False, indent=2))
        else:
            ka = result.get("keepalive") or {}
            if result.get("ok"):
                print(
                    f"heal API: checked={ka.get('checked')} ok={ka.get('ok_count')} "
                    f"reconnect={ka.get('reconnect_count', ka.get('bad_count'))}"
                )
            else:
                print(f"heal API FAIL: {result.get('detail') or result.get('error') or ka}")
            for dead in result.get("dead_sessions") or []:
                print(
                    f"  DEAD  {dead.get('kind')} {dead.get('phone_masked') or dead.get('phone')} "
                    f"— {dead.get('last_error') or 'переподключите в админке'}"
                )
        dead_n = len(result.get("dead_sessions") or [])
        if not result.get("ok"):
            return 2
        return 2 if dead_n else 0

    if bot_up and args.force_local:
        print(
            "WARN: --force-local при живом боте может отозвать Telegram-сессию "
            "(два клиента на один файл).",
            file=sys.stderr,
        )
    result = await heal_local()
    if args.json:
        print(json.dumps({"recover": rec, "heal": result}, ensure_ascii=False, indent=2))
    else:
        print(
            f"heal local: checked={result.get('checked')} ok={result.get('ok_count')} "
            f"bad={result.get('bad_count')}"
        )
        for item in result.get("items") or []:
            if not item.get("ok"):
                print(f"  DEAD  {item.get('kind')} id={item.get('id')} — {item.get('error')}")
    return 0 if result.get("ok") and not result.get("bad_count") else 2


async def cmd_watch(args: argparse.Namespace) -> int:
    interval = max(60, int(args.interval or 180))
    print(f"watch каждые {interval}с  api={API_BASE}", flush=True)
    while True:
        report = await doctor_report()
        _print_report(report, as_json=False)
        if report.get("severity") != "ok":
            ns = argparse.Namespace(
                json=False,
                force_local=False,
                user=args.user,
                password=args.password,
            )
            await cmd_heal(ns)
        await asyncio.sleep(interval)


def _exit_from_report(report: dict[str, Any]) -> int:
    sev = report.get("severity")
    if sev == "error":
        return 2
    if sev == "warn":
        return 1
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Диагностика и лечение сессий Telegram/MAX без второго Telethon."
    )
    p.add_argument("--json", action="store_true", help="вывод JSON")
    p.add_argument("--user", default="", help="логин админки (по умолчанию ADMIN_USERNAME)")
    p.add_argument("--password", default="", help="пароль админки (по умолчанию ADMIN_PASSWORD)")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("doctor", help="проверка файлов + БД, без второго коннекта")
    sub.add_parser("status", help="то же, что doctor")
    h = sub.add_parser("heal", help="recover сирот + keepalive (через API, если бот жив)")
    h.add_argument(
        "--force-local",
        action="store_true",
        help="открыть сессии из этого процесса (только если бот выключен)",
    )
    r = sub.add_parser("recover", help="сиротские acc_*/max_acc_* прописать в БД")
    r.add_argument("--dry-run", action="store_true")
    d = sub.add_parser("dedupe", help="убрать дубликаты номеров в send_accounts")
    d.add_argument("--dry-run", action="store_true")
    w = sub.add_parser("watch", help="цикл doctor+heal")
    w.add_argument("--interval", type=int, default=180, help="секунды между кругами")
    return p


async def amain(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd in ("doctor", "status"):
        report = await doctor_report()
        _print_report(report, as_json=args.json)
        return _exit_from_report(report)
    if args.cmd == "recover":
        result = await recover_orphans(dry_run=args.dry_run)
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            for row in result.get("attached") or []:
                print(f"  {row}")
            for skip in result.get("skipped") or []:
                print(f"  skip {skip}")
            if not result.get("attached"):
                print("сирот с auth_key нет")
        return 0
    if args.cmd == "dedupe":
        result = await dedupe_accounts(dry_run=args.dry_run)
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            if not result.get("removed"):
                print("дубликатов нет")
            for row in result.get("removed") or []:
                print(f"  remove id={row['id']} keep={row['keep_id']} {row.get('phone')}")
        return 0
    if args.cmd == "heal":
        return await cmd_heal(args)
    if args.cmd == "watch":
        return await cmd_watch(args)
    return 2


def main() -> None:
    try:
        code = asyncio.run(amain())
    except KeyboardInterrupt:
        code = 130
    sys.exit(code)


if __name__ == "__main__":
    main()
