import base64
import json
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from googleapiclient.errors import HttpError

from config import Settings
from gmail_service import (EmailMessage, decode_push_history_id, extract_body, is_relevant,
                           parse_paynow_amount)
from ingestion import GMAIL_HISTORY_KEY, log_ios_expense, sync_gmail
from sheets_service import Transaction

NOW = datetime(2026, 10, 17, 21, 0)


def make_settings(**overrides) -> Settings:
    base = dict(
        telegram_bot_token="t", webhook_url="https://x", webhook_secret="s",
        allowed_user_ids=frozenset({1}), reminder_chat_id=None, gemini_api_key="k",
        gemini_model="m", google_sheet_id="id", service_account_json="{}",
        default_currency="SGD", timezone="Asia/Singapore", gmail_owner_name="En Jie",
    )
    base.update(overrides)
    return Settings(**base)


class FakeSheets:
    def __init__(self, transactions=None, state=None):
        self.transactions = list(transactions or [])
        self.state = dict(state or {})

    def append_transaction(self, txn):
        self.transactions.append(txn)

    def load_transactions(self):
        return list(self.transactions)

    def load_state(self, key):
        return self.state.get(key)

    def save_state(self, key, value):
        self.state[key] = value


class FakeGemini:
    def __init__(self, expense=None, emails=None, paynow_merchant="Town Council"):
        self.expense = expense
        self.emails = emails or {}
        self.email_calls = []
        self.paynow_merchant = paynow_merchant

    def parse_expense(self, text, sender, today):
        if self.expense is None:
            raise ValueError("no amount")
        return self.expense

    def parse_paynow_merchant(self, subject, body):
        if self.paynow_merchant is None:
            raise RuntimeError("gemini down")
        return self.paynow_merchant

class FakeGmail:
    def __init__(self, ids=(), latest="200", messages=None, history_error=None):
        self.ids = list(ids)
        self.latest = latest
        self.messages = messages or {}
        self.history_error = history_error

    def new_message_ids(self, start):
        if self.history_error:
            raise self.history_error
        return self.ids, self.latest

    def get_email(self, message_id):
        return self.messages.get(message_id)


def email(message_id, subject, body="You made a PayNow transfer of SGD 42.50 to Town Council"):
    return EmailMessage(id=message_id, subject=subject, sender="uob@uob.com.sg", body=body,
                        sent_at=datetime(2026, 10, 1, 2, 25, tzinfo=timezone.utc))


def test_ios_expense_is_appended_with_defaults():
    sheets = FakeSheets()
    gemini = FakeGemini(expense={"date": "", "time": "", "category": "Groceries", "amount": 14.5,
                                 "currency": "", "card": ""})
    txn = log_ios_expense("DBS: SGD 14.50 charged at NTUC via Apple Pay", "En Jie",
                          gemini, sheets, make_settings(), NOW)
    assert sheets.transactions == [txn]
    assert txn.user == "En Jie"
    assert txn.amount == 14.5
    assert txn.currency == "SGD"
    assert txn.card == "Unknown"
    assert txn.raw.startswith("ios:")
    assert txn.raw.endswith("| DBS: SGD 14.50 charged at NTUC via Apple Pay")
    assert txn.date == date(2026, 10, 17)


def test_ios_expense_without_amount_appends_nothing():
    sheets = FakeSheets()
    with pytest.raises(ValueError):
        log_ios_expense("Your OTP is 123456", "En Jie", FakeGemini(), sheets, make_settings(), NOW)
    assert sheets.transactions == []


def test_ios_duplicate_notification_is_skipped():
    gemini = FakeGemini(expense={"amount": 14.5, "category": "Groceries", "merchant": "NTUC"})
    sheets = FakeSheets()
    first = log_ios_expense("DBS: SGD 14.50 at NTUC", "En Jie", gemini, sheets, make_settings(), NOW)
    second = log_ios_expense("dbs:  SGD 14.50 at ntuc", "En Jie", gemini, sheets, make_settings(), NOW)
    assert first is not None
    assert second is None
    assert len(sheets.transactions) == 1


def test_ios_payload_card_overrides_gemini_card_and_unknown_category_maps_to_others():
    gemini = FakeGemini(expense={"amount": 9, "category": "Food", "card": "Citi",
                                 "merchant": "Koufu"})
    txn = log_ios_expense("Citi: SGD 9 at Koufu", "En Jie", gemini, FakeSheets(),
                          make_settings(), NOW, card="DBS Altitude")
    assert txn.card == "DBS Altitude"
    assert txn.category == "Others"
    assert txn.merchant == "Koufu"


def test_gmail_first_push_only_stores_history_id():
    sheets = FakeSheets()
    gmail = FakeGmail(ids=["m1"], messages={"m1": email("m1", "PayNow sent")})
    logged = sync_gmail(gmail, sheets, make_settings(), "100", NOW)
    assert logged == []
    assert sheets.state[GMAIL_HISTORY_KEY] == "100"


def test_gmail_logs_paynow_transfer_with_fixed_values_and_advances_history():
    sheets = FakeSheets(state={GMAIL_HISTORY_KEY: "100"})
    gmail = FakeGmail(ids=["m1"], latest="200", messages={"m1": email("m1", "PayNow sent")})
    logged = sync_gmail(gmail, sheets, make_settings(), "999", NOW)

    assert len(logged) == 1
    txn = logged[0]
    assert txn.amount == 42.5
    assert txn.category == "PAYNOW"
    assert txn.currency == "SGD"
    assert txn.card == "No Card/Bank Transfer"
    assert txn.merchant == "NoMerchant/Paynow"
    assert txn.user == "En Jie"
    assert txn.raw == "gmail PayNow sent | You made a PayNow transfer of SGD 42.50 to Town Council"
    assert txn.date == date(2026, 10, 1)
    assert txn.time == "10:25"
    assert sheets.state[GMAIL_HISTORY_KEY] == "200"


def test_gmail_paynow_merchant_comes_from_gemini_and_falls_back_on_error():
    sheets = FakeSheets(state={GMAIL_HISTORY_KEY: "100"})
    gmail = FakeGmail(ids=["m1"], messages={"m1": email("m1", "PayNow sent")})
    logged = sync_gmail(gmail, sheets, make_settings(), "999", NOW, gemini=FakeGemini())
    assert logged[0].merchant == "Town Council"

    sheets = FakeSheets(state={GMAIL_HISTORY_KEY: "100"})
    gmail = FakeGmail(ids=["m1"], messages={"m1": email("m1", "PayNow sent")})
    logged = sync_gmail(gmail, sheets, make_settings(), "999", NOW,
                        gemini=FakeGemini(paynow_merchant=None))
    assert len(logged) == 1
    assert logged[0].merchant == "NoMerchant/Paynow"


def test_gmail_skips_email_without_paynow_amount():
    sheets = FakeSheets(state={GMAIL_HISTORY_KEY: "100"})
    gmail = FakeGmail(ids=["m1"], messages={"m1": email(
        "m1", "PayNow received", body="You have received SGD 118.29 from a friend")})
    assert sync_gmail(gmail, sheets, make_settings(), "999", NOW) == []
    assert sheets.transactions == []


def test_gmail_does_not_log_same_message_twice():
    existing = Transaction(date=date(2026, 10, 1), time="09:00", user="En Jie",
                           category="PAYNOW", amount=42.5, currency="SGD",
                           card="No Card/Bank Transfer", merchant="NoMerchant/Paynow",
                           raw="gmail PayNow sent | You made a PayNow transfer of SGD 42.50 to Town Council")
    sheets = FakeSheets(transactions=[existing], state={GMAIL_HISTORY_KEY: "100"})
    gmail = FakeGmail(ids=["m1"], messages={"m1": email("m1", "PayNow sent")})
    assert sync_gmail(gmail, sheets, make_settings(), "999", NOW) == []
    assert len(sheets.transactions) == 1


def test_gmail_skips_irrelevant_email():
    sheets = FakeSheets(state={GMAIL_HISTORY_KEY: "100"})
    gmail = FakeGmail(ids=["m1"], messages={"m1": email("m1", "Hello friend", body="Lunch?")})
    assert sync_gmail(gmail, sheets, make_settings(), "999", NOW) == []
    assert sheets.transactions == []


def test_gmail_rate_limit_skips_without_advancing_history():
    quota = HttpError(SimpleNamespace(status=403, reason="Forbidden"),
                      b'{"error": {"errors": [{"reason": "rateLimitExceeded"}]}}')
    sheets = FakeSheets(state={GMAIL_HISTORY_KEY: "1"})
    gmail = FakeGmail(history_error=quota)
    assert sync_gmail(gmail, sheets, make_settings(), "500", NOW) == []
    assert sheets.state[GMAIL_HISTORY_KEY] == "1"


def test_gmail_resets_when_stored_history_is_expired():
    not_found = HttpError(SimpleNamespace(status=404, reason="Not Found"), b"gone")
    sheets = FakeSheets(state={GMAIL_HISTORY_KEY: "1"})
    gmail = FakeGmail(history_error=not_found)
    assert sync_gmail(gmail, sheets, make_settings(), "500", NOW) == []
    assert sheets.state[GMAIL_HISTORY_KEY] == "500"


def test_push_body_decodes_history_id():
    payload = json.dumps({"emailAddress": "me@gmail.com", "historyId": 12345})
    body = {"message": {"data": base64.b64encode(payload.encode()).decode()}}
    assert decode_push_history_id(body) == "12345"


def test_relevance_keywords():
    assert is_relevant("Your PayNow transfer", "")
    assert is_relevant("Receipt", "Total $12.00")
    assert not is_relevant("Weekly newsletter", "Hello there")


def _b64url(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


def test_body_prefers_plain_text_over_html():
    payload = {"mimeType": "multipart/alternative", "parts": [
        {"mimeType": "text/html", "body": {"data": _b64url("<p>HTML version</p>")}},
        {"mimeType": "text/plain", "body": {"data": _b64url("Plain   version")}},
    ]}
    assert extract_body(payload) == "Plain version"


def test_body_strips_html_when_no_plain_text():
    payload = {"mimeType": "text/html", "body": {"data": _b64url("<p>You paid <b>$5</b></p>")}}
    assert extract_body(payload) == "You paid $5"


REFERENCE = Path(__file__).parent / "reference"


def load_reference(name):
    return json.loads((REFERENCE / name).read_text())


def test_uob_paynow_outbound_template_is_parsed_and_logged():
    template = load_reference("Paynow_outboundtransfer.json")
    assert is_relevant(template["subject"], template["raw_content"])
    assert parse_paynow_amount(template["raw_content"]) == 200.0
    sheets = FakeSheets(state={GMAIL_HISTORY_KEY: "100"})
    gmail = FakeGmail(ids=["m1"], latest="200",
                      messages={"m1": email("m1", template["subject"], template["raw_content"])})
    logged = sync_gmail(gmail, sheets, make_settings(), "999", NOW)
    assert len(logged) == 1
    assert logged[0].amount == 200.0
    assert logged[0].category == "PAYNOW"


def test_uob_paynow_inbound_template_is_not_logged():
    template = load_reference("Paynow_Inboundtransfer.json")
    assert is_relevant(template["subject"], template["raw_content"])
    assert parse_paynow_amount(template["raw_content"]) is None
    sheets = FakeSheets(state={GMAIL_HISTORY_KEY: "100"})
    gmail = FakeGmail(ids=["m1"], latest="200",
                      messages={"m1": email("m1", template["subject"], template["raw_content"])})
    assert sync_gmail(gmail, sheets, make_settings(), "999", NOW) == []
    assert sheets.transactions == []
