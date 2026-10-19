import hashlib
import logging
from datetime import datetime
from typing import Optional

from googleapiclient.errors import HttpError

from config import Settings
from gemini_service import CATEGORIES, GeminiService
from gmail_service import EmailMessage, GmailClient, is_relevant, parse_paynow_amount
from sheets_service import SheetsService, Transaction, parse_date

logger = logging.getLogger(__name__)

GMAIL_HISTORY_KEY = "gmail_history_id"
IOS_KEY_PREFIX = "ios:"
FALLBACK_CATEGORY = "Others"
PAYNOW_CATEGORY = "PAYNOW"
PAYNOW_CURRENCY = "SGD"
PAYNOW_CARD = "No Card/Bank Transfer"
PAYNOW_MERCHANT = "NoMerchant/Paynow"


def ios_dedupe_key(text: str, sender: str, day: str) -> str:
    """Stable key for one notification. Whitespace and case differences are ignored."""
    normalized = " ".join(text.lower().split())
    digest = hashlib.sha256(f"{sender}|{day}|{normalized}".encode()).hexdigest()[:16]
    return f"{IOS_KEY_PREFIX}{digest}"


def log_ios_expense(text: str, sender: str, gemini: GeminiService, sheets: SheetsService,
                    settings: Settings, now: datetime, card: str = "") -> Optional[Transaction]:
    """Parse an Apple Pay notification and append it.

    Returns None if the same notification was already logged today.
    Raises ValueError if no amount is found.
    """
    key = ios_dedupe_key(text, sender, now.date().isoformat())
    if any(t.raw.startswith(key) for t in sheets.load_transactions()):
        logger.info("Skipping duplicate iOS notification %s", key)
        return None
    fields = gemini.parse_expense(text, sender, now.date())
    txn = build_transaction(fields, sender, f"{key} | {text}", settings, now, card_override=card)
    sheets.append_transaction(txn)
    return txn


def normalize_category(value: str) -> str:
    for category in CATEGORIES:
        if value and value.strip().lower() == category.lower():
            return category
    return FALLBACK_CATEGORY


def build_transaction(fields: dict, sender: str, raw: str, settings: Settings,
                      now: datetime, card_override: str = "") -> Transaction:
    return Transaction(
        date=parse_date(fields.get("date", "")) or now.date(),
        time=fields.get("time") or now.strftime("%H:%M"),
        user=sender,
        category=normalize_category(fields.get("category", "")),
        amount=float(fields["amount"]),
        currency=fields.get("currency") or settings.default_currency,
        card=card_override.strip() or fields.get("card") or "Unknown",
        merchant=fields.get("merchant", ""),
        raw=raw,
        logged_at=now.isoformat(timespec="seconds"),
    )


def is_gmail_rate_limited(exc: HttpError) -> bool:
    if exc.resp.status == 429:
        return True
    return exc.resp.status == 403 and b"rateLimitExceeded" in (exc.content or b"")


def build_paynow_transaction(email: EmailMessage, amount: float, settings: Settings,
                             now: datetime) -> Transaction:
    when = email.sent_at.astimezone(settings.tz) if email.sent_at else now
    return Transaction(
        date=when.date(),
        time=when.strftime("%H:%M"),
        user=settings.gmail_owner_name,
        category=PAYNOW_CATEGORY,
        amount=amount,
        currency=PAYNOW_CURRENCY,
        card=PAYNOW_CARD,
        merchant=PAYNOW_MERCHANT,
        raw=f"gmail {email.subject} | {email.body}",
        logged_at=now.isoformat(timespec="seconds"),
    )


def sync_gmail(gmail: GmailClient, sheets: SheetsService,
               settings: Settings, push_history_id: str, now: datetime) -> list[Transaction]:
    """Log outgoing PayNow transfers since the last stored historyId. Safe to retry."""
    start = sheets.load_state(GMAIL_HISTORY_KEY)
    if not start:
        sheets.save_state(GMAIL_HISTORY_KEY, push_history_id)
        logger.warning("No Gmail historyId stored; starting from %s", push_history_id)
        return []

    try:
        message_ids, latest = gmail.new_message_ids(start)
    except HttpError as exc:
        if exc.resp.status == 404:
            logger.warning("Stored historyId %s is too old; resetting to %s", start, push_history_id)
            sheets.save_state(GMAIL_HISTORY_KEY, push_history_id)
            return []
        if is_gmail_rate_limited(exc):
            # Keep the baseline so the next push retries these messages; a 500 would make Pub/Sub retry and worsen the quota.
            logger.warning("Gmail quota exceeded; skipping push, baseline %s kept", start)
            return []
        raise

    logged_raw = {t.raw for t in sheets.load_transactions()}
    logged: list[Transaction] = []
    for message_id in dict.fromkeys(message_ids):
        email = gmail.get_email(message_id)
        if email is None or not is_relevant(email.subject, email.body):
            continue
        amount = parse_paynow_amount(email.body)
        if amount is None:
            logger.info("Gmail message %s is not an outgoing PayNow transfer; skipping", message_id)
            continue
        txn = build_paynow_transaction(email, amount, settings, now)
        if txn.raw in logged_raw:
            continue
        sheets.append_transaction(txn)
        logged_raw.add(txn.raw)
        logged.append(txn)

    sheets.save_state(GMAIL_HISTORY_KEY, latest)
    return logged
