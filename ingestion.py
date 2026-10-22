import logging
from datetime import datetime

from googleapiclient.errors import HttpError

from config import Settings
from gmail_service import (
    CardAlert,
    EmailMessage,
    GmailClient,
    is_relevant,
    parse_card_alert,
    parse_paynow_amount,
    parse_paynow_recipient,
    parse_qr_payment,
    parse_transit_charge,
)
from sheets_service import SheetsService, Transaction

logger = logging.getLogger(__name__)

GMAIL_HISTORY_KEY = "gmail_history_id"
FALLBACK_CATEGORY = "Others"
PAYNOW_CATEGORY = "PAYNOW"
PAYNOW_CURRENCY = "SGD"
PAYNOW_CARD = "No Card/Bank Transfer"
PAYNOW_MERCHANT = "NoMerchant/Paynow"


def is_gmail_rate_limited(exc: HttpError) -> bool:
    if exc.resp.status == 429:
        return True
    return exc.resp.status == 403 and b"rateLimitExceeded" in (exc.content or b"")


def build_paynow_transaction(
    email: EmailMessage,
    amount: float,
    settings: Settings,
    now: datetime,
    merchant: str = "",
    category: str = PAYNOW_CATEGORY,
) -> Transaction:
    when = email.sent_at.astimezone(settings.tz) if email.sent_at else now
    return Transaction(
        date=when.date(),
        time=when.strftime("%H:%M"),
        user=settings.gmail_owner_name,
        category=category,
        amount=amount,
        currency=PAYNOW_CURRENCY,
        card=PAYNOW_CARD,
        merchant=merchant or PAYNOW_MERCHANT,
        raw=f"gmail {email.subject} | {email.body}",
        logged_at=now.isoformat(timespec="seconds"),
    )


def build_card_transaction(
    email: EmailMessage, alert: CardAlert, card: str, settings: Settings, now: datetime
) -> Transaction:
    when = email.sent_at.astimezone(settings.tz) if email.sent_at else now
    return Transaction(
        date=alert.day,
        time=when.strftime("%H:%M"),
        user=settings.gmail_owner_name,
        category=alert.category or FALLBACK_CATEGORY,
        currency=PAYNOW_CURRENCY,
        card=card,
        merchant=alert.merchant,
        raw=f"gmail {email.subject} | {email.body}",
        logged_at=now.isoformat(timespec="seconds"),
    )


def sync_gmail(
    gmail: GmailClient,
    sheets: SheetsService,
    settings: Settings,
    push_history_id: str,
    now: datetime,
) -> list[Transaction]:
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
            logger.warning(
                "Stored historyId %s is too old; resetting to %s", start, push_history_id
            )
            sheets.save_state(GMAIL_HISTORY_KEY, push_history_id)
            return []
        if is_gmail_rate_limited(exc):
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
        qr = parse_qr_payment(email.body) if amount is None else None
        if amount is not None:
            recipient = parse_paynow_recipient(email.body)
            txn = build_paynow_transaction(email, amount, settings, now, merchant=recipient)
        elif qr is not None:
            qr_amount, payee = qr
            txn = build_paynow_transaction(
                email, qr_amount, settings, now, merchant=payee, category=FALLBACK_CATEGORY
            )
        else:
            alert = parse_card_alert(email.body) or parse_transit_charge(email.body)
            if alert is None:
                logger.info(
                    "Gmail message %s is not a known transaction format; skipping", message_id
                )
                continue
            card = sheets.load_card_names_by_ending().get(alert.ending, alert.card)
            txn = build_card_transaction(email, alert, card, settings, now)
        if txn.raw in logged_raw:
            continue
        sheets.append_transaction(txn)
        logged_raw.add(txn.raw)
        logged.append(txn)

    sheets.save_state(GMAIL_HISTORY_KEY, latest)
    return logged
