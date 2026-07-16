"""Интерфейсы отправки сообщений."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass
class SendResult:
    ok: bool
    status: str = "sent"  # sent | delivered | failed
    error: str | None = None


class Sender(Protocol):
    async def send(self, *, phone: str, name: str, text: str) -> SendResult: ...

    @property
    def available(self) -> bool: ...
