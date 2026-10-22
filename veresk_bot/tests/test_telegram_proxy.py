"""Разбор ссылок VLESS / Hysteria2 и конфиг sing-box без живого туннеля."""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="veresk-proxy-")
os.environ["BOT_TOKEN"] = "123456789:AAE_test_token"
os.environ["POSIFLORA_USERNAME"] = "user"
os.environ["POSIFLORA_PASSWORD"] = "pass"
os.environ["POSIFLORA_STORE_ID"] = "1"
os.environ["DATABASE_PATH"] = str(Path(_TMP) / "veresk.db")

from telegram_proxy import build_singbox_config, parse_proxy_uri  # noqa: E402

UUID = "bf000d23-0752-40b4-affe-68f7707a9661"
PBK = "jNXHt1yRo0vDuchQlIP6Z0ZvjT3KtzVI-T4E7RoLJS0"
VLESS_REALITY = (
    f"vless://{UUID}@203.0.113.10:443?encryption=none&security=reality"
    f"&sni=www.microsoft.com&fp=chrome&pbk={PBK}&sid=0123456789abcdef"
    "&type=tcp&flow=xtls-rprx-vision#Amsterdam"
)


def _singbox() -> str | None:
    env = os.getenv("SINGBOX_BIN", "").strip()
    if env and Path(env).is_file():
        return env
    found = shutil.which("sing-box")
    if found:
        return found
    candidate = Path("/tmp/sing-box")
    if candidate.is_file() and os.access(candidate, os.X_OK):
        return str(candidate)
    return None


class ParseProxyTests(unittest.TestCase):
    def test_vless_reality(self) -> None:
        meta = parse_proxy_uri(VLESS_REALITY)
        out = meta["outbound"]
        self.assertEqual(meta["kind"], "vless")
        self.assertEqual(meta["remark"], "Amsterdam")
        self.assertEqual(out["uuid"], UUID)
        self.assertEqual(out["server"], "203.0.113.10")
        self.assertEqual(out["server_port"], 443)
        self.assertEqual(out["flow"], "xtls-rprx-vision")
        self.assertEqual(out["tls"]["reality"]["public_key"], PBK)
        self.assertEqual(out["tls"]["reality"]["short_id"], "0123456789abcdef")
        self.assertEqual(out["tls"]["utls"]["fingerprint"], "chrome")
        self.assertNotIn("transport", out)

    def test_old_flow_and_unknown_fingerprint(self) -> None:
        uri = (
            f"vless://{UUID}@203.0.113.10:443?security=reality&pbk={PBK}"
            "&fp=chrome_psk&flow=xtls-rprx-vision-udp443&sni=www.microsoft.com"
        )
        out = parse_proxy_uri(uri)["outbound"]
        self.assertEqual(out["flow"], "xtls-rprx-vision")
        self.assertEqual(out["tls"]["utls"]["fingerprint"], "chrome")

    def test_unknown_flow_dropped(self) -> None:
        uri = (
            f"vless://{UUID}@203.0.113.10:443?security=tls&sni=ex.com"
            "&flow=xtls-rprx-direct"
        )
        out = parse_proxy_uri(uri)["outbound"]
        self.assertNotIn("flow", out)

    def test_ws_and_http_transport(self) -> None:
        ws = parse_proxy_uri(
            f"vless://{UUID}@cdn.example.com:443?type=ws&security=tls"
            "&path=%2Fws&host=cdn.example.com&sni=cdn.example.com&fp=firefox&ed=2048"
        )["outbound"]
        self.assertEqual(ws["transport"]["type"], "ws")
        self.assertEqual(ws["transport"]["path"], "/ws")
        self.assertEqual(ws["transport"]["headers"]["Host"], "cdn.example.com")
        self.assertEqual(ws["transport"]["max_early_data"], 2048)
        self.assertEqual(ws["tls"]["utls"]["fingerprint"], "firefox")

        http = parse_proxy_uri(
            f"vless://{UUID}@203.0.113.10:8080?type=http&security=tls"
            "&path=%2Fsecret&host=ex.com&sni=ex.com"
        )["outbound"]
        self.assertEqual(http["transport"]["path"], "/secret")
        self.assertIsInstance(http["transport"]["path"], str)
        self.assertEqual(http["transport"]["host"], "ex.com")

    def test_ipv6_and_pasted_noise(self) -> None:
        uri = f"vless://{UUID}@[2001:db8::1]:443?security=tls&sni=example.com"
        meta = parse_proxy_uri("  ключ:\n" + uri + "  ")
        self.assertEqual(meta["host"], "2001:db8::1")
        self.assertEqual(meta["port"], 443)

    def test_base64_share(self) -> None:
        blob = base64.urlsafe_b64encode(VLESS_REALITY.encode()).decode().rstrip("=")
        meta = parse_proxy_uri(blob)
        self.assertEqual(meta["kind"], "vless")
        self.assertEqual(meta["outbound"]["uuid"], UUID)

    def test_hysteria2_obfs_and_hop(self) -> None:
        meta = parse_proxy_uri(
            "hysteria2://s3cret@hy.example.com:443/?sni=hy.example.com"
            "&insecure=1&obfs=salamander&obfs-password=obfs#home"
        )
        out = meta["outbound"]
        self.assertEqual(meta["kind"], "hysteria2")
        self.assertEqual(meta["remark"], "home")
        self.assertEqual(out["password"], "s3cret")
        self.assertTrue(out["tls"]["insecure"])
        self.assertEqual(out["obfs"]["password"], "obfs")

        hopped = parse_proxy_uri(
            "hy2://203.0.113.11:443?auth=p%40ss&sni=hy.example.com&mport=20000-50000"
        )["outbound"]
        self.assertEqual(hopped["password"], "p@ss")
        self.assertEqual(hopped["server_ports"], ["20000:50000"])
        self.assertEqual(hopped["hop_interval"], "30s")

    def test_pin_is_not_written_into_singbox(self) -> None:
        meta = parse_proxy_uri(
            "hysteria2://s3cret@203.0.113.11:443?sni=ex.com&pinSHA256=abcd"
        )
        self.assertIn("pinSHA256", meta["warning"] or "")
        self.assertNotIn("certificate_public_key_sha256", meta["outbound"]["tls"])

    def test_rejects_other_schemes_and_reality_without_key(self) -> None:
        with self.assertRaises(ValueError):
            parse_proxy_uri("socks5://127.0.0.1:1080")
        with self.assertRaises(ValueError):
            parse_proxy_uri(f"vless://{UUID}@203.0.113.10:443?security=reality")
        with self.assertRaises(ValueError):
            parse_proxy_uri("hysteria2://203.0.113.11:443?sni=ex.com")

    def test_dns_uses_tunnel_except_proxy_host(self) -> None:
        domain_cfg = build_singbox_config(
            f"vless://{UUID}@cdn.example.com:443?security=tls&sni=cdn.example.com"
        )
        self.assertEqual(domain_cfg["route"]["final"], "proxy")
        self.assertEqual(domain_cfg["dns"]["final"], "remote")
        self.assertEqual(domain_cfg["dns"]["servers"][0]["detour"], "direct")
        self.assertEqual(domain_cfg["dns"]["servers"][1]["detour"], "proxy")
        self.assertEqual(
            domain_cfg["dns"]["rules"],
            [{"domain": ["cdn.example.com"], "server": "bootstrap"}],
        )

        ip_cfg = build_singbox_config(VLESS_REALITY)
        self.assertNotIn("rules", ip_cfg["dns"])
        self.assertEqual(ip_cfg["inbounds"][0]["listen"], "127.0.0.1")
        self.assertEqual(ip_cfg["inbounds"][0]["type"], "mixed")

    def test_singbox_accepts_generated_config(self) -> None:
        binary = _singbox()
        if not binary:
            self.skipTest("sing-box binary is not installed")
        samples = [
            VLESS_REALITY,
            (
                f"vless://{UUID}@cdn.example.com:443?type=ws&security=tls"
                "&path=%2Fws&host=cdn.example.com&sni=cdn.example.com"
            ),
            (
                f"vless://{UUID}@203.0.113.10:8080?type=http&security=tls"
                "&path=%2Fsecret&host=ex.com&sni=ex.com"
            ),
            "hy2://203.0.113.11:443?auth=s3cret&sni=hy.example.com&mport=20000-50000",
            "hysteria2://s3cret@hy.example.com:443/?insecure=1&obfs=salamander&obfs-password=obfs",
        ]
        for uri in samples:
            cfg = build_singbox_config(uri)
            path = Path(_TMP) / "singbox-check.json"
            path.write_text(json.dumps(cfg), encoding="utf-8")
            proc = subprocess.run(
                [binary, "check", "-c", str(path)],
                capture_output=True,
                text=True,
            )
            self.assertEqual(
                proc.returncode,
                0,
                msg=f"{uri}\n{proc.stderr}\n{proc.stdout}",
            )


if __name__ == "__main__":
    unittest.main()
