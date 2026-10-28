from datetime import date

import httpx
import pytest

import categorizer
from categorizer import categorize_merchant
from config import Settings
from ingestion import recategorize_fallback
from sheets_service import Transaction

MODEL = "test-model"


def settings(api_key: str = "key") -> Settings:
    return Settings(
        telegram_bot_token="t",
        webhook_url="https://x",
        webhook_secret="s",
        allowed_user_ids=frozenset({1}),
        reminder_chat_id=None,
        google_sheet_id="id",
        service_account_json="{}",
        default_currency="SGD",
        timezone="Asia/Singapore",
        groq_api_key=api_key,
    )


@pytest.fixture(autouse=True)
def clear_cache(monkeypatch):
    monkeypatch.setattr(categorizer, "_llm_cache", {})


def fake_groq(monkeypatch, reply: str | Exception, calls: list):
    def post(url, json, headers, timeout):
        calls.append(json)
        if isinstance(reply, Exception):
            raise reply
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": reply}}]},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(categorizer.httpx, "post", post)


def test_keyword_rule_maps_known_merchant_without_calling_llm(monkeypatch):
    calls = []
    fake_groq(monkeypatch, "Shopping", calls)
    assert categorize_merchant("NTUC FairPrice Bedok", "key", MODEL) == "Groceries"
    assert calls == []


def test_first_matching_keyword_wins_for_food_delivery():
    assert categorize_merchant("GRABFOOD*KOUFU", "", MODEL) == "Dining"


def test_llm_answers_unknown_merchant_and_result_is_cached(monkeypatch):
    calls = []
    fake_groq(monkeypatch, "Health.\n", calls)
    assert categorize_merchant("Unknown Clinic", "key", MODEL) == "Others"
    fake_groq(monkeypatch, "Shopping", calls)
    assert categorize_merchant("Unknown Clinic", "key", MODEL) == "Others"
    assert len(calls) == 1


def test_llm_valid_category_is_used(monkeypatch):
    calls = []
    fake_groq(monkeypatch, "shopping", calls)
    assert categorize_merchant("Random Boutique", "key", MODEL) == "Shopping"
    assert calls[0]["model"] == MODEL


@pytest.mark.parametrize("reply", ["Health", "I think it is Dining because..."])
def test_llm_reply_outside_allowed_categories_falls_back_to_others(monkeypatch, reply):
    fake_groq(monkeypatch, reply, [])
    assert categorize_merchant("Random Boutique", "key", MODEL) == "Others"


def test_llm_error_falls_back_to_others(monkeypatch):
    fake_groq(monkeypatch, httpx.ConnectError("down"), [])
    assert categorize_merchant("Random Boutique", "key", MODEL) == "Others"


def test_no_api_key_skips_llm(monkeypatch):
    calls = []
    fake_groq(monkeypatch, "Dining", calls)
    assert categorize_merchant("Random Boutique", "", MODEL) == "Others"
    assert calls == []


class FakeRecategorizeSheets:
    def __init__(self, rows):
        self.rows = rows
        self.updates = []

    def load_transactions_with_rows(self):
        return list(self.rows)

    def update_transaction_category(self, row_number, category):
        self.updates.append((row_number, category))


def txn(merchant: str, category: str) -> Transaction:
    return Transaction(
        date=date(2026, 10, 1),
        time="10:00",
        user="En Jie",
        category=category,
        amount=5.0,
        currency="SGD",
        card="UOB Card ending 2881",
        merchant=merchant,
    )


def test_recategorize_only_touches_others_rows_and_writes_when_applied():
    sheets = FakeRecategorizeSheets(
        [
            (2, txn("NTUC FairPrice", "Others")),
            (3, txn("NTUC FairPrice", "Dining")),
            (4, txn("PayNow Friend", "PAYNOW")),
            (5, txn("Starbucks", "Others")),
        ]
    )
    changes = recategorize_fallback(sheets, "", MODEL, apply=False)
    assert [(t.merchant, c) for t, c in changes] == [
        ("NTUC FairPrice", "Groceries"),
        ("Starbucks", "Dining"),
    ]
    assert sheets.updates == []

    recategorize_fallback(sheets, "", MODEL, apply=True)
    assert sheets.updates == [(2, "Groceries"), (5, "Dining")]
