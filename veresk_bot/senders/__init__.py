from senders.base import SendResult, Sender
from senders.max_bot import MaxBotSender
from senders.telegram_userbot import TelegramUserbotSender

__all__ = [
    "SendResult",
    "Sender",
    "TelegramUserbotSender",
    "MaxBotSender",
]
