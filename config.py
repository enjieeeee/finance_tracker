import os
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Settings:
    telegram_bot_token: str
    webhook_url: str
    webhook_secret: str
    allowed_user_ids: frozenset[int]
    reminder_chat_id: int | None
    google_sheet_id: str
    service_account_json: str
    default_currency: str
    timezone: str
    gmail_client_id: str = ""
    gmail_client_secret: str = ""
    gmail_refresh_token: str = ""
    gmail_push_token: str = ""
    gmail_pubsub_topic: str = ""
    gmail_owner_name: str = "Email"
    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @property
    def gmail_enabled(self) -> bool:
        return bool(self.gmail_refresh_token and self.gmail_client_id and self.gmail_client_secret)


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def load_settings() -> Settings:
    reminder_chat = os.getenv("REMINDER_CHAT_ID", "").strip()
    allowed = {int(x) for x in _required("ALLOWED_USER_IDS").split(",") if x.strip()}
    return Settings(
        telegram_bot_token=_required("TELEGRAM_BOT_TOKEN"),
        webhook_url=_required("WEBHOOK_URL").rstrip("/"),
        webhook_secret=_required("TELEGRAM_WEBHOOK_SECRET"),
        allowed_user_ids=frozenset(allowed),
        reminder_chat_id=int(reminder_chat) if reminder_chat else None,
        google_sheet_id=_required("GOOGLE_SHEET_ID"),
        service_account_json=_required("GOOGLE_SERVICE_ACCOUNT_JSON"),
        default_currency=os.getenv("DEFAULT_CURRENCY", "SGD").strip(),
        timezone=os.getenv("TIMEZONE", "Asia/Singapore").strip(),
        gmail_client_id=os.getenv("GMAIL_CLIENT_ID", "").strip(),
        gmail_client_secret=os.getenv("GMAIL_CLIENT_SECRET", "").strip(),
        gmail_refresh_token=os.getenv("GMAIL_REFRESH_TOKEN", "").strip(),
        gmail_push_token=os.getenv("GMAIL_PUSH_TOKEN", "").strip(),
        gmail_pubsub_topic=os.getenv("GMAIL_PUBSUB_TOPIC", "").strip(),
        gmail_owner_name=os.getenv("GMAIL_OWNER_NAME", "Email").strip(),
    )
