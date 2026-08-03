from senders.base import SendResult, Sender
from senders.max_bot import MaxBotSender
from senders.max_userbot import MaxUserbotSender
from senders.telegram_userbot import TelegramUserbotSender

__all__ = [
    "SendResult",
    "Sender",
    "TelegramUserbotSender",
    "MaxBotSender",
    "MaxUserbotSender",
]
