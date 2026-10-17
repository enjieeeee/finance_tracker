import base64
import json
from datetime import date, datetime
from types import SimpleNamespace

import pytest
from googleapiclient.errors import HttpError

from config import Settings
from gmail_service import EmailMessage, decode_push_history_id, extract_body, is_relevant
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
    def __init__(self, expense=None, emails=None):
        self.expense = expense
        self.emails = emails or {}
        self.email_calls = []

    def parse_expense(self, text, sender, today):
        if self.expense is None:
            raise ValueError("no amount")
        return self.expense

    def parse_email(self, subject, sender, body, today):
        self.email_calls.append(subject)
        return self.emails[subject]


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


def email(message_id, subject, body="Payment details"):
    return EmailMessage(id=message_id, subject=subject, sender="dbs@dbs.com", body=body)


OUTGOING = {"direction": "outgoing", "date": "", "time": "", "category": "Bills",
            "amount": 42.5, "currency": "SGD", "card": "PayNow DBS", "notes": "Town Council"}


def test_ios_expense_is_appended_with_defaults():
    sheets = FakeSheets()
    gemini = FakeGemini(expense={"date": "", "time": "", "category": "Groceries", "amount": 14.5,
                                 "currency": "", "card": "", "notes": "NTUC"})
    txn = log_ios_expense("DBS: SGD 14.50 charged at NTUC via Apple Pay", "En Jie",
                          gemini, sheets, make_settings(), NOW)
    assert sheets.transactions == [txn]
    assert txn.user == "En Jie"
    assert txn.amount == 14.5
    assert txn.currency == "SGD"
    assert txn.card == "Unknown"
    assert txn.raw == "DBS: SGD 14.50 charged at NTUC via Apple Pay"
    assert txn.date == date(2026, 10, 17)


def test_ios_expense_without_amount_appends_nothing():
    sheets = FakeSheets()
    with pytest.raises(ValueError):
        log_ios_expense("Your OTP is 123456", "En Jie", FakeGemini(), sheets, make_settings(), NOW)
    assert sheets.transactions == []


def test_gmail_first_push_only_stores_history_id():
    sheets = FakeSheets()
    gmail = FakeGmail(ids=["m1"], messages={"m1": email("m1", "PayNow sent")})
    logged = sync_gmail(gmail, sheets, FakeGemini(), make_settings(), "100", NOW)
    assert logged == []
    assert sheets.state[GMAIL_HISTORY_KEY] == "100"


def test_gmail_logs_outgoing_email_and_advances_history():
    sheets = FakeSheets(state={GMAIL_HISTORY_KEY: "100"})
    gmail = FakeGmail(ids=["m1"], latest="200", messages={"m1": email("m1", "Bill paid")})
    gemini = FakeGemini(emails={"Bill paid": OUTGOING})
    logged = sync_gmail(gmail, sheets, gemini, make_settings(), "999", NOW)

    assert len(logged) == 1
    assert logged[0].user == "En Jie"
    assert logged[0].card == "PayNow DBS"
    assert logged[0].raw == "gmail:m1 | Bill paid"
    assert sheets.state[GMAIL_HISTORY_KEY] == "200"


def test_gmail_skips_incoming_and_none_directions():
    sheets = FakeSheets(state={GMAIL_HISTORY_KEY: "100"})
    gmail = FakeGmail(ids=["m1", "m2"], messages={
        "m1": email("m1", "Salary credited"), "m2": email("m2", "Promo")})
    gemini = FakeGemini(emails={
        "Salary credited": {**OUTGOING, "direction": "incoming"},
        "Promo": {**OUTGOING, "direction": "none"},
    })
    assert sync_gmail(gmail, sheets, gemini, make_settings(), "999", NOW) == []
    assert sheets.transactions == []


def test_gmail_does_not_log_same_message_twice():
    existing = Transaction(date=date(2026, 10, 1), time="09:00", user="En Jie", category="Bills",
                           amount=42.5, currency="SGD", card="PayNow DBS", notes="x",
                           raw="gmail:m1 | Bill paid")
    sheets = FakeSheets(transactions=[existing], state={GMAIL_HISTORY_KEY: "100"})
    gmail = FakeGmail(ids=["m1"], messages={"m1": email("m1", "Bill paid")})
    gemini = FakeGemini(emails={"Bill paid": OUTGOING})
    assert sync_gmail(gmail, sheets, gemini, make_settings(), "999", NOW) == []
    assert gemini.email_calls == []


def test_gmail_skips_irrelevant_email_without_calling_gemini():
    sheets = FakeSheets(state={GMAIL_HISTORY_KEY: "100"})
    gmail = FakeGmail(ids=["m1"], messages={"m1": email("m1", "Hello friend", body="Lunch?")})
    gemini = FakeGemini()
    assert sync_gmail(gmail, sheets, gemini, make_settings(), "999", NOW) == []
    assert gemini.email_calls == []


def test_gmail_resets_when_stored_history_is_expired():
    not_found = HttpError(SimpleNamespace(status=404, reason="Not Found"), b"gone")
    sheets = FakeSheets(state={GMAIL_HISTORY_KEY: "1"})
    gmail = FakeGmail(history_error=not_found)
    assert sync_gmail(gmail, sheets, FakeGemini(), make_settings(), "500", NOW) == []
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
