"""SSL для MAX API (platform-api2.max.ru).

Сертификат MAX выдан Russian Trusted CA (Минцифры). Его нет в системных
корнях macOS и в Mozilla/certifi — без бандла проверка падает с
CERTIFICATE_VERIFY_FAILED.

Дополнительно: на системном Python macOS (LibreSSL) после успешной
проверки цепочки getpeercert() падает на OID 1.2.643.* — патчим возврат {}.
"""

from __future__ import annotations

import logging
import ssl
from pathlib import Path

logger = logging.getLogger(__name__)

_CERTS_DIR = Path(__file__).resolve().parent.parent / "certs"
_RU_BUNDLE = _CERTS_DIR / "russian_trusted_ca_bundle.pem"
_patched = False


def _normalize_pem(text: str) -> str:
    return text.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n").strip() + "\n"


def _patch_getpeercert() -> None:
    """Обход бага LibreSSL/macOS: binary getpeercert ок, parsed — SSLError."""
    global _patched
    if _patched:
        return

    def _wrap(orig):
        def getpeercert(self, binary_form=False):
            try:
                return orig(self, binary_form)
            except ssl.SSLError:
                if binary_form:
                    raise
                return {}

        return getpeercert

    try:
        ssl.SSLObject.getpeercert = _wrap(ssl.SSLObject.getpeercert)  # type: ignore[method-assign]
    except Exception:
        logger.debug("SSLObject.getpeercert patch skipped", exc_info=True)
    try:
        ssl.SSLSocket.getpeercert = _wrap(ssl.SSLSocket.getpeercert)  # type: ignore[method-assign]
    except Exception:
        logger.debug("SSLSocket.getpeercert patch skipped", exc_info=True)
    _patched = True


def _ca_cadata() -> str | None:
    parts: list[str] = []
    try:
        import certifi

        parts.append(_normalize_pem(Path(certifi.where()).read_text(encoding="utf-8")))
    except Exception:
        pass

    if _RU_BUNDLE.is_file():
        parts.append(_normalize_pem(_RU_BUNDLE.read_text(encoding="utf-8")))
    else:
        # fallback: отдельные файлы с gu-st.ru
        for name in ("russian_trusted_root_ca.cer", "russian_trusted_sub_ca.cer"):
            path = _CERTS_DIR / name
            if path.is_file():
                parts.append(_normalize_pem(path.read_text(encoding="utf-8")))

    if not parts:
        return None
    return "\n".join(parts)


def build_max_ssl_context() -> ssl.SSLContext | bool:
    """SSL-контекст для aiohttp TCPConnector.

    Возвращает SSLContext с Russian Trusted CA (+ certifi), либо True
    (дефолт aiohttp), если бандл недоступен.
    """
    _patch_getpeercert()
    cadata = _ca_cadata()
    if not cadata:
        logger.warning(
            "Нет certs/russian_trusted_ca_bundle.pem — SSL к MAX может не пройти на macOS"
        )
        return True
    ctx = ssl.create_default_context(cadata=cadata)
    return ctx
