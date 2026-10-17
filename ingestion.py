import logging
from datetime import datetime

from googleapiclient.errors import HttpError

from config import Settings
from gemini_service import GeminiService
from gmail_service import GmailClient, is_relevant
from sheets_service import SheetsService, Transaction, parse_date

logger = logging.getLogger(__name__)

GMAIL_HISTORY_KEY = "gmail_history_id"


def log_ios_expense(text: str, sender: str, gemini: GeminiService, sheets: SheetsService,
                    settings: Settings, now: datetime) -> Transaction:
    """Parse an Apple Pay notification and append it. Raises ValueError if no amount."""
    fields = gemini.parse_expense(text, sender, now.date())
    txn = build_transaction(fields, sender, text, settings, now)
    sheets.append_transaction(txn)
    return txn


def build_transaction(fields: dict, sender: str, raw: str, settings: Settings,
                      now: datetime) -> Transaction:
    return Transaction(
        date=parse_date(fields.get("date", "")) or now.date(),
        time=fields.get("time") or now.strftime("%H:%M"),
        user=sender,
        category=fields.get("category") or "Other",
        amount=float(fields["amount"]),
        currency=fields.get("currency") or settings.default_currency,
        card=fields.get("card") or "Unknown",
        notes=fields.get("notes", ""),
        raw=raw,
        logged_at=now.isoformat(timespec="seconds"),
    )


def sync_gmail(gmail: GmailClient, sheets: SheetsService, gemini: GeminiService,
               settings: Settings, push_history_id: str, now: datetime) -> list[Transaction]:
    """Log outgoing-money emails since the last stored historyId. Safe to retry."""
    start = sheets.load_state(GMAIL_HISTORY_KEY)
    if not start:
        sheets.save_state(GMAIL_HISTORY_KEY, push_history_id)
        logger.warning("No Gmail historyId stored; starting from %s", push_history_id)
        return []

    try:
        message_ids, latest = gmail.new_message_ids(start)
    except HttpError as exc:
        if exc.resp.status != 404:
            raise
        logger.warning("Stored historyId %s is too old; resetting to %s", start, push_history_id)
        sheets.save_state(GMAIL_HISTORY_KEY, push_history_id)
        return []

    logged_raw = [t.raw for t in sheets.load_transactions()]
    logged: list[Transaction] = []
    for message_id in dict.fromkeys(message_ids):
        tag = f"gmail:{message_id}"
        if any(raw.startswith(tag) for raw in logged_raw):
            continue
        email = gmail.get_email(message_id)
        if email is None or not is_relevant(email.subject, email.body):
            continue
        fields = gemini.parse_email(email.subject, email.sender, email.body, now.date())
        if fields.get("direction") != "outgoing" or float(fields.get("amount") or 0) <= 0:
            continue
        txn = build_transaction(fields, settings.gmail_owner_name, f"{tag} | {email.subject}",
                                settings, now)
        sheets.append_transaction(txn)
        logged_raw.append(txn.raw)
        logged.append(txn)

    sheets.save_state(GMAIL_HISTORY_KEY, latest)
    return logged
