import base64
import json
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from googleapiclient.errors import HttpError

from config import Settings
from gmail_service import (
    EmailMessage,
    decode_push_history_id,
    extract_body,
    is_relevant,
    parse_card_alert,
    parse_paynow_amount,
    parse_paynow_recipient,
    parse_qr_payment,
)
from ingestion import GMAIL_HISTORY_KEY, sync_gmail
from sheets_service import Transaction

NOW = datetime(2026, 10, 17, 21, 0)


def make_settings(**overrides) -> Settings:
    base = dict(
        telegram_bot_token="t",
        webhook_url="https://x",
        webhook_secret="s",
        allowed_user_ids=frozenset({1}),
        reminder_chat_id=None,
        google_sheet_id="id",
        service_account_json="{}",
        default_currency="SGD",
        timezone="Asia/Singapore",
        gmail_owner_name="En Jie",
    )
    base.update(overrides)
    return Settings(**base)


class FakeSheets:
    def __init__(self, transactions=None, state=None, card_names=None):
        self.transactions = list(transactions or [])
        self.state = dict(state or {})
        self.card_names = dict(card_names or {})

    def append_transaction(self, txn):
        self.transactions.append(txn)

    def load_transactions(self):
        return list(self.transactions)

    def load_state(self, key):
        return self.state.get(key)

    def save_state(self, key, value):
        self.state[key] = value

    def load_card_names_by_ending(self):
        return dict(self.card_names)


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


def email(
    message_id,
    subject,
    body="You made a PayNow transfer of SGD 42.50 to Town Council on your a/c ending 3606",
):
    return EmailMessage(
        id=message_id,
        subject=subject,
        sender="uob@uob.com.sg",
        body=body,
        sent_at=datetime(2026, 10, 1, 2, 25, tzinfo=timezone.utc),
    )


def test_card_alert_is_logged_under_others_with_parsed_fields():
    sheets = FakeSheets(state={GMAIL_HISTORY_KEY: "100"})
    body = (
        "A transaction of SGD 1,047.60 was made with your UOB Card ending 2881 on 19/10/26 "
        "at PHS HEARTBEAT@BEDOK #2. If unauthorised, call 24/7 Fraud Hotline now"
    )
    gmail = FakeGmail(
        ids=["m1"], latest="200", messages={"m1": email("m1", "UOB - Transaction Alert", body)}
    )
    logged = sync_gmail(gmail, sheets, make_settings(), "999", NOW)
    assert len(logged) == 1
    txn = logged[0]
    assert txn.amount == 1047.60
    assert txn.card == "UOB Card ending 2881"
    assert txn.merchant == "PHS HEARTBEAT@BEDOK #2"
    assert txn.date == date(2026, 10, 19)
    assert txn.category == "Others"
    assert txn.user == "En Jie"


def test_card_alert_regex_rejects_other_emails():
    assert parse_card_alert("Your OTP is 123456") is None


def test_paynow_recipient_is_extracted_without_uen_suffix():
    body = (
        "You made a PayNow transfer of SGD 200.00 to YOU TECHNOLOGIES GRO (UEN ending CFAS) "
        "on your a/c ending 3606"
    )
    assert parse_paynow_recipient(body) == "YOU TECHNOLOGIES GRO"


def test_paynow_without_recipient_falls_back_to_default_merchant():
    sheets = FakeSheets(state={GMAIL_HISTORY_KEY: "100"})
    gmail = FakeGmail(
        ids=["m1"],
        messages={"m1": email("m1", "PayNow sent", body="You made a PayNow transfer of SGD 42.50")},
    )
    logged = sync_gmail(gmail, sheets, make_settings(), "999", NOW)
    assert logged[0].merchant == "NoMerchant/Paynow"

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
    assert txn.merchant == "Town Council"
    assert txn.user == "En Jie"
    assert txn.raw == (
        "gmail PayNow sent | You made a PayNow transfer of SGD 42.50 to Town Council "
        "on your a/c ending 3606"
    )
    assert txn.date == date(2026, 10, 1)
    assert txn.time == "10:25"
    assert sheets.state[GMAIL_HISTORY_KEY] == "200"


def test_gmail_skips_email_without_paynow_amount():
    sheets = FakeSheets(state={GMAIL_HISTORY_KEY: "100"})
    gmail = FakeGmail(
        ids=["m1"],
        messages={
            "m1": email("m1", "PayNow received", body="You have received SGD 118.29 from a friend")
        },
    )
    assert sync_gmail(gmail, sheets, make_settings(), "999", NOW) == []
    assert sheets.transactions == []


def test_gmail_does_not_log_same_message_twice():
    existing = Transaction(
        date=date(2026, 10, 1),
        time="09:00",
        user="En Jie",
        category="PAYNOW",
        amount=42.5,
        currency="SGD",
        card="No Card/Bank Transfer",
        merchant="NoMerchant/Paynow",
        raw=(
            "gmail PayNow sent | You made a PayNow transfer of SGD 42.50 to Town Council "
            "on your a/c ending 3606"
        ),
    )
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
    quota = HttpError(
        SimpleNamespace(status=403, reason="Forbidden"),
        b'{"error": {"errors": [{"reason": "rateLimitExceeded"}]}}',
    )
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
    payload = {
        "mimeType": "multipart/alternative",
        "parts": [
            {"mimeType": "text/html", "body": {"data": _b64url("<p>HTML version</p>")}},
            {"mimeType": "text/plain", "body": {"data": _b64url("Plain   version")}},
        ],
    }
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
    gmail = FakeGmail(
        ids=["m1"],
        latest="200",
        messages={"m1": email("m1", template["subject"], template["raw_content"])},
    )
    logged = sync_gmail(gmail, sheets, make_settings(), "999", NOW)
    assert len(logged) == 1
    assert logged[0].amount == 200.0
    assert logged[0].category == "PAYNOW"
    assert logged[0].merchant == "YOU TECHNOLOGIES GRO"


def test_uob_card_alert_template_is_logged():
    template = load_reference("Creditcard_payment.json")
    assert is_relevant(template["subject"], template["raw_content"])
    sheets = FakeSheets(state={GMAIL_HISTORY_KEY: "100"})
    gmail = FakeGmail(
        ids=["m1"],
        latest="200",
        messages={"m1": email("m1", template["subject"], template["raw_content"])},
    )
    logged = sync_gmail(gmail, sheets, make_settings(), "999", NOW)
    assert len(logged) == 1
    assert logged[0].amount == 47.6
    assert logged[0].card == "UOB Card ending 2881"
    assert logged[0].merchant == "PHS HEARTBEAT@BEDOK #2"


def test_card_alert_uses_cards_tab_name_matched_by_last_4_digits():
    template = load_reference("Creditcard_payment.json")
    sheets = FakeSheets(state={GMAIL_HISTORY_KEY: "100"}, card_names={"2881": "UOB Lady's Card"})
    gmail = FakeGmail(
        ids=["m1"],
        latest="200",
        messages={"m1": email("m1", template["subject"], template["raw_content"])},
    )
    logged = sync_gmail(gmail, sheets, make_settings(), "999", NOW)
    assert logged[0].card == "UOB Lady's Card"


def test_uob_paynow_inbound_template_is_not_logged():
    template = load_reference("Paynow_Inboundtransfer.json")
    assert is_relevant(template["subject"], template["raw_content"])
    assert parse_paynow_amount(template["raw_content"]) is None
    sheets = FakeSheets(state={GMAIL_HISTORY_KEY: "100"})
    gmail = FakeGmail(
        ids=["m1"],
        latest="200",
        messages={"m1": email("m1", template["subject"], template["raw_content"])},
    )
    assert sync_gmail(gmail, sheets, make_settings(), "999", NOW) == []
    assert sheets.transactions == []


def test_uob_qr_payment_template_is_logged_as_bank_transfer():
    template = load_reference("QRpayment_outboundtransfer.json")
    assert is_relevant(template["subject"], template["raw_content"])
    assert parse_qr_payment(template["raw_content"]) == (5.0, "HUA DEE-BEDOK")
    sheets = FakeSheets(state={GMAIL_HISTORY_KEY: "100"})
    gmail = FakeGmail(
        ids=["m1"],
        latest="200",
        messages={"m1": email("m1", template["subject"], template["raw_content"])},
    )
    logged = sync_gmail(gmail, sheets, make_settings(), "999", NOW)
    assert len(logged) == 1
    assert logged[0].amount == 5.0
    assert logged[0].category == "Others"
    assert logged[0].card == "No Card/Bank Transfer"
    assert logged[0].merchant == "HUA DEE-BEDOK"


def test_uob_transit_template_is_logged_on_card_with_billing_date():
    template = load_reference("schedulepayment_outboundtransfer.json")
    assert is_relevant(template["subject"] or "", template["raw_content"])
    sheets = FakeSheets(state={GMAIL_HISTORY_KEY: "100"}, card_names={"2881": "UOB Lady's Card"})
    gmail = FakeGmail(
        ids=["m1"],
        latest="200",
        messages={"m1": email("m1", template["subject"] or "", template["raw_content"])},
    )
    logged = sync_gmail(gmail, sheets, make_settings(), "999", NOW)
    assert len(logged) == 1
    assert logged[0].amount == 16.79
    assert logged[0].card == "UOB Lady's Card"
    assert logged[0].merchant == "SMRT CORPORATION LTD"
    assert logged[0].category == "transport"
    assert logged[0].date == date(2026, 10, 13)
