"""Прокси для всего Telegram-трафика: VLESS / Hysteria2 → локальный SOCKS5.

В админке вставляется ссылка `vless://…` или `hysteria2://…` / `hy2://…`.
Поднимается локальный sing-box (mixed/SOCKS на 127.0.0.1), а Telethon и
aiogram ходят только через него.

Значения в runtime_settings (приоритет над .env):
  telegram_proxy_uri   — сырая ссылка
  telegram_proxy_enabled — bool
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

import runtime_settings
from config import DATABASE_PATH

logger = logging.getLogger(__name__)

LOCAL_HOST = "127.0.0.1"
LOCAL_PORT = int(os.getenv("TELEGRAM_PROXY_LOCAL_PORT", "17890") or "17890")
SINGBOX_BIN = os.getenv("SINGBOX_BIN", "sing-box").strip() or "sing-box"

_DATA_DIR = Path(DATABASE_PATH).resolve().parent
_CONFIG_PATH = _DATA_DIR / "singbox-telegram.json"
_PID_PATH = _DATA_DIR / "singbox-telegram.pid"
_LOG_PATH = _DATA_DIR / "singbox-telegram.log"

_proc: subprocess.Popen[str] | None = None
_lock = threading.Lock()
_last_error: str | None = None


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def get_proxy_uri() -> str:
    raw = runtime_settings.get("telegram_proxy_uri")
    if raw and str(raw).strip():
        return str(raw).strip()
    return os.getenv("TELEGRAM_PROXY_URI", "").strip()


def is_proxy_enabled() -> bool:
    flag = runtime_settings.get("telegram_proxy_enabled")
    if flag is not None:
        return _truthy(flag) and bool(get_proxy_uri())
    # .env без явного флага: достаточно URI
    return bool(get_proxy_uri())


def local_socks_url() -> str | None:
    if not is_proxy_enabled():
        return None
    return f"socks5://{LOCAL_HOST}:{LOCAL_PORT}"


def telethon_proxy() -> Any | None:
    """Кортеж для TelegramClient(proxy=…). None — без прокси."""
    if not is_proxy_enabled():
        return None
    try:
        from python_socks import ProxyType

        return (ProxyType.SOCKS5, LOCAL_HOST, LOCAL_PORT)
    except ImportError:
        # PySocks fallback
        try:
            import socks  # type: ignore

            return (socks.SOCKS5, LOCAL_HOST, LOCAL_PORT)
        except ImportError:
            logger.error(
                "Нужен python-socks или PySocks для Telethon через прокси"
            )
            return (2, LOCAL_HOST, LOCAL_PORT)  # ProxyType.SOCKS5 == 2


def find_singbox() -> str | None:
    path = shutil.which(SINGBOX_BIN) or shutil.which("sing-box")
    if path:
        return path
    for candidate in (
        "/usr/local/bin/sing-box",
        "/usr/bin/sing-box",
        str(Path.home() / "bin" / "sing-box"),
    ):
        if Path(candidate).is_file() and os.access(candidate, os.X_OK):
            return candidate
    return None


def _qs_first(qs: dict[str, list[str]], *names: str, default: str = "") -> str:
    for name in names:
        vals = qs.get(name)
        if vals and vals[0] != "":
            return unquote(vals[0])
    return default


def _as_bool(raw: str) -> bool:
    return raw.strip().lower() in ("1", "true", "yes", "on")


def detect_proxy_kind(uri: str) -> str | None:
    low = (uri or "").strip().lower()
    if low.startswith("vless://"):
        return "vless"
    if low.startswith("hysteria2://") or low.startswith("hy2://"):
        return "hysteria2"
    return None


def _parse_authority(netloc: str) -> tuple[str, str, int]:
    """userinfo@host:port → (userinfo, host, port)."""
    if "@" in netloc:
        userinfo, hostport = netloc.rsplit("@", 1)
    else:
        userinfo, hostport = "", netloc
    userinfo = unquote(userinfo)
    if hostport.startswith("["):
        # [ipv6]:port
        m = re.match(r"^\[([^\]]+)\](?::(\d+))?$", hostport)
        if not m:
            raise ValueError("Некорректный IPv6-адрес в ссылке")
        host = m.group(1)
        port = int(m.group(2) or "0")
    else:
        if ":" in hostport:
            host, port_s = hostport.rsplit(":", 1)
            port = int(port_s)
        else:
            host, port = hostport, 0
        host = unquote(host)
    if not host or port <= 0:
        raise ValueError("В ссылке нужны host и port")
    return userinfo, host, port


def parse_proxy_uri(uri: str) -> dict[str, Any]:
    """Разобрать vless:// или hysteria2:// → метаданные + outbound для sing-box."""
    raw = (uri or "").strip()
    if not raw:
        raise ValueError("Ссылка пустая")
    # Убрать пробелы/переносы из вставки
    raw = re.sub(r"\s+", "", raw)
    kind = detect_proxy_kind(raw)
    if not kind:
        raise ValueError(
            "Поддерживаются только ссылки vless:// и hysteria2:// (hy2://)"
        )

    parsed = urlparse(raw)
    userinfo, host, port = _parse_authority(parsed.netloc)
    qs = parse_qs(parsed.query, keep_blank_values=True)
    remark = unquote(parsed.fragment or "") or f"{kind}://{host}:{port}"

    if kind == "vless":
        outbound = _vless_outbound(userinfo, host, port, qs)
    else:
        outbound = _hysteria2_outbound(userinfo, host, port, qs)

    return {
        "kind": kind,
        "host": host,
        "port": port,
        "remark": remark,
        "outbound": outbound,
    }


def _tls_block(qs: dict[str, list[str]], *, security: str) -> dict[str, Any] | None:
    if security not in ("tls", "reality"):
        return None
    sni = _qs_first(qs, "sni", "peer", "host")
    insecure = _as_bool(_qs_first(qs, "allowInsecure", "insecure", "allow_insecure"))
    alpn_raw = _qs_first(qs, "alpn")
    fp = _qs_first(qs, "fp", "fingerprint") or "chrome"
    tls: dict[str, Any] = {
        "enabled": True,
        "insecure": insecure,
        "utls": {"enabled": True, "fingerprint": fp},
    }
    if sni:
        tls["server_name"] = sni
    if alpn_raw:
        tls["alpn"] = [p.strip() for p in alpn_raw.split(",") if p.strip()]
    if security == "reality":
        pbk = _qs_first(qs, "pbk", "publicKey", "public_key")
        if not pbk:
            raise ValueError("VLESS Reality: нужен параметр pbk (public key)")
        sid = _qs_first(qs, "sid", "shortId", "short_id")
        reality: dict[str, Any] = {
            "enabled": True,
            "public_key": pbk,
            "short_id": sid or "",
        }
        tls["reality"] = reality
    return tls


def _transport_block(qs: dict[str, list[str]]) -> dict[str, Any] | None:
    net = (_qs_first(qs, "type", "network") or "tcp").lower()
    if net in ("", "tcp", "raw"):
        return None
    if net == "ws":
        path = _qs_first(qs, "path") or "/"
        host_header = _qs_first(qs, "host", "Host")
        transport: dict[str, Any] = {"type": "ws", "path": path}
        if host_header:
            transport["headers"] = {"Host": host_header}
        early = _qs_first(qs, "ed", "earlyData", "max_early_data")
        if early.isdigit():
            transport["max_early_data"] = int(early)
        return transport
    if net == "grpc":
        service = _qs_first(qs, "serviceName", "service_name") or "GunService"
        return {"type": "grpc", "service_name": service}
    if net in ("http", "h2", "httpupgrade"):
        path = _qs_first(qs, "path") or "/"
        host_header = _qs_first(qs, "host")
        if net == "httpupgrade":
            t: dict[str, Any] = {"type": "httpupgrade", "path": path}
            if host_header:
                t["host"] = host_header
            return t
        t = {"type": "http", "path": [path] if path else ["/"]}
        if host_header:
            t["host"] = [host_header]
        return t
    if net == "quic":
        return {"type": "quic"}
    raise ValueError(f"Неподдерживаемый transport type={net}")


def _vless_outbound(
    uuid: str, host: str, port: int, qs: dict[str, list[str]]
) -> dict[str, Any]:
    if not uuid:
        raise ValueError("VLESS: в ссылке нет UUID")
    security = (_qs_first(qs, "security") or "none").lower()
    # encryption=none в ссылке — не security; Reality узнаём по pbk
    if security in ("", "none") and _qs_first(qs, "pbk"):
        security = "reality"
    flow = _qs_first(qs, "flow")
    packet = _qs_first(qs, "packetEncoding", "packetencoding") or "xudp"

    outbound: dict[str, Any] = {
        "type": "vless",
        "tag": "proxy",
        "server": host,
        "server_port": port,
        "uuid": uuid,
        "packet_encoding": packet if packet in ("packetaddr", "xudp") else "xudp",
    }
    if flow:
        outbound["flow"] = flow
    tls = _tls_block(qs, security=security)
    if tls:
        outbound["tls"] = tls
    transport = _transport_block(qs)
    if transport:
        outbound["transport"] = transport
    return outbound


def _hysteria2_outbound(
    password: str, host: str, port: int, qs: dict[str, list[str]]
) -> dict[str, Any]:
    if not password:
        raise ValueError("Hysteria2: в ссылке нет пароля")
    sni = _qs_first(qs, "sni", "peer") or host
    insecure = _as_bool(_qs_first(qs, "insecure", "allowInsecure"))
    obfs = _qs_first(qs, "obfs")
    obfs_password = _qs_first(qs, "obfs-password", "obfs_password", "obfsPassword")
    pin = _qs_first(qs, "pinSHA256", "pin_sha256", "pin")

    outbound: dict[str, Any] = {
        "type": "hysteria2",
        "tag": "proxy",
        "server": host,
        "server_port": port,
        "password": password,
        "tls": {
            "enabled": True,
            "server_name": sni,
            "insecure": insecure,
        },
    }
    if pin:
        outbound["tls"]["certificate_public_key_sha256"] = [pin]
    if obfs:
        outbound["obfs"] = {
            "type": obfs if obfs in ("salamander",) else "salamander",
            "password": obfs_password or password,
        }
    return outbound


def build_singbox_config(uri: str) -> dict[str, Any]:
    meta = parse_proxy_uri(uri)
    return {
        "log": {"level": "warn", "timestamp": True},
        "inbounds": [
            {
                "type": "mixed",
                "tag": "mixed-in",
                "listen": LOCAL_HOST,
                "listen_port": LOCAL_PORT,
            }
        ],
        "outbounds": [
            meta["outbound"],
            {"type": "direct", "tag": "direct"},
        ],
        "route": {"final": "proxy"},
    }


def _read_pid() -> int | None:
    try:
        raw = _PID_PATH.read_text(encoding="utf-8").strip()
        return int(raw) if raw else None
    except (OSError, ValueError):
        return None


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _stop_process_locked() -> None:
    global _proc
    pid = None
    if _proc is not None:
        pid = _proc.pid
        try:
            _proc.terminate()
            try:
                _proc.wait(timeout=4)
            except subprocess.TimeoutExpired:
                _proc.kill()
                _proc.wait(timeout=2)
        except Exception:
            logger.debug("sing-box terminate failed", exc_info=True)
        _proc = None
    file_pid = _read_pid()
    if file_pid and file_pid != pid and _pid_alive(file_pid):
        try:
            os.kill(file_pid, signal.SIGTERM)
            time.sleep(0.4)
            if _pid_alive(file_pid):
                os.kill(file_pid, signal.SIGKILL)
        except OSError:
            pass
    try:
        _PID_PATH.unlink(missing_ok=True)
    except OSError:
        pass


def stop_proxy() -> None:
    with _lock:
        _stop_process_locked()


def _wait_port(timeout: float = 8.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            import socket

            with socket.create_connection((LOCAL_HOST, LOCAL_PORT), timeout=0.4):
                return True
        except OSError:
            time.sleep(0.15)
    return False


def ensure_proxy_running(*, force_restart: bool = False) -> dict[str, Any]:
    """Запустить/перезапустить sing-box по текущим настройкам."""
    global _proc, _last_error
    with _lock:
        if not is_proxy_enabled():
            _stop_process_locked()
            _last_error = None
            return {"ok": True, "enabled": False, "running": False}

        uri = get_proxy_uri()
        try:
            meta = parse_proxy_uri(uri)
            config = build_singbox_config(uri)
        except ValueError as exc:
            _last_error = str(exc)
            _stop_process_locked()
            return {"ok": False, "enabled": True, "running": False, "error": str(exc)}

        binary = find_singbox()
        if not binary:
            _last_error = (
                "sing-box не найден. В Docker он ставится автоматически; "
                "локально установите: https://github.com/SagerNet/sing-box/releases"
            )
            return {
                "ok": False,
                "enabled": True,
                "running": False,
                "error": _last_error,
                "kind": meta["kind"],
            }

        config_text = json.dumps(config, ensure_ascii=False, indent=2)
        config_same = False
        try:
            config_same = _CONFIG_PATH.read_text(encoding="utf-8") == config_text
        except OSError:
            config_same = False

        # Уже жив с актуальным конфигом?
        if (
            not force_restart
            and config_same
            and _proc is not None
            and _proc.poll() is None
            and _wait_port(timeout=0.5)
        ):
            _last_error = None
            return {
                "ok": True,
                "enabled": True,
                "running": True,
                "kind": meta["kind"],
                "host": meta["host"],
                "port": meta["port"],
                "remark": meta["remark"],
                "local": f"{LOCAL_HOST}:{LOCAL_PORT}",
            }

        _stop_process_locked()
        _DATA_DIR.mkdir(parents=True, exist_ok=True)
        _CONFIG_PATH.write_text(config_text, encoding="utf-8")

        log_f = open(_LOG_PATH, "ab", buffering=0)
        try:
            _proc = subprocess.Popen(
                [binary, "run", "-c", str(_CONFIG_PATH)],
                stdout=log_f,
                stderr=subprocess.STDOUT,
                cwd=str(_DATA_DIR),
                start_new_session=True,
            )
        except OSError as exc:
            log_f.close()
            _last_error = f"Не удалось запустить sing-box: {exc}"
            return {
                "ok": False,
                "enabled": True,
                "running": False,
                "error": _last_error,
                "kind": meta["kind"],
            }

        _PID_PATH.write_text(str(_proc.pid), encoding="utf-8")
        # Не закрываем log_f — процесс пишет в него
        if not _wait_port(timeout=8.0):
            rc = _proc.poll()
            tail = ""
            try:
                tail = _LOG_PATH.read_text(encoding="utf-8", errors="replace")[-800:]
            except OSError:
                pass
            _last_error = (
                f"sing-box не открыл порт {LOCAL_PORT}"
                + (f" (exit={rc})" if rc is not None else "")
                + (f": {tail.strip()}" if tail.strip() else "")
            )
            _stop_process_locked()
            return {
                "ok": False,
                "enabled": True,
                "running": False,
                "error": _last_error,
                "kind": meta["kind"],
            }

        _last_error = None
        logger.info(
            "Telegram proxy up: %s → socks5://%s:%s (%s)",
            meta["remark"],
            LOCAL_HOST,
            LOCAL_PORT,
            meta["kind"],
        )
        return {
            "ok": True,
            "enabled": True,
            "running": True,
            "kind": meta["kind"],
            "host": meta["host"],
            "port": meta["port"],
            "remark": meta["remark"],
            "local": f"{LOCAL_HOST}:{LOCAL_PORT}",
        }


def proxy_status() -> dict[str, Any]:
    uri = get_proxy_uri()
    enabled = is_proxy_enabled()
    meta: dict[str, Any] | None = None
    parse_error: str | None = None
    if uri:
        try:
            meta = parse_proxy_uri(uri)
        except ValueError as exc:
            parse_error = str(exc)

    running = False
    if enabled:
        with _lock:
            if _proc is not None and _proc.poll() is None:
                running = True
            else:
                pid = _read_pid()
                running = bool(pid and _pid_alive(pid))
        if running:
            running = _wait_port(timeout=0.35)

    masked = ""
    if uri:
        kind = detect_proxy_kind(uri) or "proxy"
        host = (meta or {}).get("host") or "…"
        port = (meta or {}).get("port") or ""
        masked = f"{kind}://…@{host}:{port}"

    return {
        "enabled": enabled,
        "configured": bool(uri),
        "running": running,
        "kind": (meta or {}).get("kind"),
        "host": (meta or {}).get("host"),
        "port": (meta or {}).get("port"),
        "remark": (meta or {}).get("remark"),
        "uri_masked": masked,
        "uri_set": bool(uri),
        "local": f"{LOCAL_HOST}:{LOCAL_PORT}" if enabled else None,
        "singbox": find_singbox() is not None,
        "error": _last_error or parse_error,
    }


def save_proxy_settings(
    *,
    uri: str | None = None,
    enabled: bool | None = None,
    clear: bool = False,
) -> dict[str, Any]:
    if clear:
        runtime_settings.delete_keys("telegram_proxy_uri", "telegram_proxy_enabled")
        stop_proxy()
        return {"ok": True, **proxy_status()}

    updates: dict[str, Any] = {}
    if uri is not None:
        cleaned = re.sub(r"\s+", "", uri.strip())
        if cleaned:
            # валидация до записи
            parse_proxy_uri(cleaned)
            updates["telegram_proxy_uri"] = cleaned
        else:
            runtime_settings.delete_keys("telegram_proxy_uri")
    if enabled is not None:
        updates["telegram_proxy_enabled"] = bool(enabled)

    if updates:
        runtime_settings.set_many(updates)

    # Если URI есть, а enabled не трогали — включаем
    if uri is not None and uri.strip() and enabled is None:
        if runtime_settings.get("telegram_proxy_enabled") is None:
            runtime_settings.set_many({"telegram_proxy_enabled": True})

    result = ensure_proxy_running(force_restart=True)
    status = proxy_status()
    status["ok"] = bool(result.get("ok"))
    if not result.get("ok"):
        status["error"] = result.get("error") or status.get("error")
    return status


async def ensure_proxy_running_async(*, force_restart: bool = False) -> dict[str, Any]:
    return await asyncio.to_thread(ensure_proxy_running, force_restart=force_restart)


async def test_proxy_connectivity() -> dict[str, Any]:
    """Проверить, что через локальный SOCKS доступен api.telegram.org."""
    start = ensure_proxy_running()
    if not start.get("ok"):
        return {
            "ok": False,
            "error": start.get("error") or "proxy_not_running",
            **proxy_status(),
        }
    if not start.get("enabled"):
        return {"ok": False, "error": "proxy_disabled", **proxy_status()}

    proxy_url = local_socks_url()
    assert proxy_url

    def _check() -> dict[str, Any]:
        try:
            from python_socks.sync import Proxy
        except ImportError:
            return {
                "ok": False,
                "error": "python-socks не установлен — выполните pip install 'python-socks[asyncio]'",
            }
        try:
            proxy = Proxy.from_url(proxy_url)
            sock = proxy.connect(dest_host="api.telegram.org", dest_port=443, timeout=12)
            try:
                sock.close()
            except Exception:
                pass
            return {"ok": True, "detail": "TCP до api.telegram.org:443 через прокси OK"}
        except Exception as exc:
            return {"ok": False, "error": str(exc) or "proxy_test_failed"}

    result = await asyncio.to_thread(_check)
    status = proxy_status()
    status.update(result)
    return status
