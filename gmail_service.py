import base64
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from email.utils import parsedate_to_datetime

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
TOKEN_URI = "https://oauth2.googleapis.com/token"
RELEVANT_KEYWORDS = re.compile(
    r"paynow|transfer|receipt|invoice|bill|payment|paid|charged|debit|transaction|"
    r"you made|account ending|a/c ending|"
    r"order|purchase|statement|grab|shopee|lazada|fairprice|ntuc|amount|\$",
    re.IGNORECASE,
)
PAYNOW_OUTGOING = re.compile(
    r"You made a PayNow transfer of (?:SGD|S\$)\s?([\d,]+(?:\.\d+)?)", re.IGNORECASE)
MAX_BODY_CHARS = 4000


@dataclass(frozen=True)
class EmailMessage:
    id: str
    subject: str
    sender: str
    body: str
    sent_at: datetime | None = None


def decode_push_history_id(push_body: dict) -> str:
    """Pub/Sub push body -> the historyId Gmail put in the notification."""
    data = json.loads(base64.b64decode(push_body["message"]["data"]).decode("utf-8"))
    return str(data["historyId"])


def is_relevant(subject: str, body: str) -> bool:
    return bool(RELEVANT_KEYWORDS.search(f"{subject} {body[:1500]}"))


def parse_paynow_amount(body: str) -> float | None:
    """Amount of an outgoing PayNow transfer, or None for any other email."""
    match = PAYNOW_OUTGOING.search(body)
    return float(match.group(1).replace(",", "")) if match else None


def _sent_at(headers: list[dict]) -> datetime | None:
    try:
        return parsedate_to_datetime(_header(headers, "Date"))
    except (TypeError, ValueError):
        return None


def _b64url_decode(data: str) -> str:
    padded = data + "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(padded).decode("utf-8", errors="replace")


def _header(headers: list[dict], name: str) -> str:
    for h in headers:
        if h.get("name", "").lower() == name.lower():
            return h.get("value", "")
    return ""


def _find_part(payload: dict, mime: str) -> str:
    if payload.get("mimeType") == mime and payload.get("body", {}).get("data"):
        return _b64url_decode(payload["body"]["data"])
    for part in payload.get("parts", []) or []:
        text = _find_part(part, mime)
        if text:
            return text
    return ""


def extract_body(payload: dict) -> str:
    text = _find_part(payload, "text/plain") or re.sub(
        r"<[^>]+>", " ", _find_part(payload, "text/html"))
    return re.sub(r"\s+", " ", text).strip()[:MAX_BODY_CHARS]


class GmailClient:
    def __init__(self, client_id: str, client_secret: str, refresh_token: str):
        creds = Credentials(
            token=None,
            refresh_token=refresh_token,
            token_uri=TOKEN_URI,
            client_id=client_id,
            client_secret=client_secret,
            scopes=SCOPES,
        )
        self._service = build("gmail", "v1", credentials=creds, cache_discovery=False)

    def watch(self, topic: str) -> str:
        """Start or renew push notifications to a Pub/Sub topic. Returns the current historyId."""
        response = self._service.users().watch(
            userId="me", body={"topicName": topic, "labelIds": ["INBOX"]}).execute()
        return str(response["historyId"])

    def new_message_ids(self, start_history_id: str) -> tuple[list[str], str]:
        """Message IDs added since start_history_id, plus the latest historyId."""
        ids: list[str] = []
        latest = start_history_id
        page_token = None
        while True:
            response = self._service.users().history().list(
                userId="me",
                startHistoryId=start_history_id,
                historyTypes=["messageAdded"],
                pageToken=page_token,
            ).execute()
            for record in response.get("history", []):
                for added in record.get("messagesAdded", []):
                    ids.append(added["message"]["id"])
            latest = str(response.get("historyId", latest))
            page_token = response.get("nextPageToken")
            if not page_token:
                return ids, latest

    def get_email(self, message_id: str) -> EmailMessage | None:
        try:
            message = self._service.users().messages().get(
                userId="me", id=message_id, format="full").execute()
        except HttpError as exc:
            if exc.resp.status != 404:
                raise
            logger.warning("Gmail message %s no longer exists; skipping", message_id)
            return None
        payload = message.get("payload", {})
        headers = payload.get("headers", [])
        return EmailMessage(
            id=message_id,
            subject=_header(headers, "Subject"),
            sender=_header(headers, "From"),
            body=extract_body(payload) or message.get("snippet", ""),
            sent_at=_sent_at(headers),
        )
