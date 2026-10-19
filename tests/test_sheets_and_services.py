from datetime import date

import pytest

from chart_generator import render_dashboard
from gemini_service import GeminiService
from sheets_service import (
    Transaction,
    TransactionQuery,
    filter_transactions,
    parse_date,
    row_to_billing,
    row_to_transaction,
)


def _txn(day, category, card, amount, user="En Jie", merchant=""):
    return Transaction(date=day, time="12:00", user=user, category=category, amount=amount,
                       currency="SGD", card=card, merchant=merchant)


def test_row_to_transaction_accepts_formatted_amount_and_dd_mm_yyyy_dates():
    row = {"Date": "05/10/2026", "Time": "12:30", "User": "Jeslin", "Category": "Food",
           "Amount": "$1,234.50", "Currency": "SGD", "Card": "DBS", "Merchant/Notes": "Lunch"}
    t = row_to_transaction(row)
    assert t.date == date(2026, 10, 5)
    assert t.amount == 1234.50


def test_row_to_transaction_maps_merchant():
    row = {"Date": "05/10/2026", "Time": "12:30", "User": "Jeslin", "Category": "Dining",
           "Amount": "12", "Currency": "SGD", "Card": "DBS", "Merchant": "Koufu"}
    t = row_to_transaction(row)
    assert t.merchant == "Koufu"


def test_row_to_transaction_skips_rows_without_amount():
    assert row_to_transaction({"Date": "2026-10-05", "Amount": ""}) is None


def test_filter_by_date_range_and_merchant_keyword():
    txns = [
        _txn(date(2026, 10, 1), "Transport", "Citi", 12.0),
        _txn(date(2026, 10, 9), "Transport", "Citi", 20.0, merchant="Grab"),
        _txn(date(2026, 10, 9), "Food", "DBS", 5.0, merchant="Koufu"),
    ]
    query = TransactionQuery(start=date(2026, 10, 5), end=date(2026, 10, 11), merchant="grab")
    assert [t.amount for t in filter_transactions(txns, query)] == [20.0]


def test_filter_by_user_and_card_is_case_insensitive():
    txns = [_txn(date(2026, 10, 1), "Food", "DBS Altitude", 9, user="Jeslin"),
            _txn(date(2026, 10, 2), "Food", "Citi", 7, user="En Jie")]
    query = TransactionQuery(user="jeslin", card="altitude")
    assert len(filter_transactions(txns, query)) == 1


def test_parse_expense_rejects_zero_amount():
    service = GeminiService.__new__(GeminiService)
    service._json = lambda prompt, schema: {"amount": 0, "category": "Food"}
    with pytest.raises(ValueError):
        service.parse_expense("lunch", "En Jie", date(2026, 10, 17))


def test_parse_expense_returns_numeric_amount():
    service = GeminiService.__new__(GeminiService)
    service._json = lambda prompt, schema: {"amount": "15", "category": "Food"}
    assert service.parse_expense("lunch $15", "En Jie", date(2026, 10, 17))["amount"] == 15.0


def test_row_to_billing_rejects_invalid_days():
    assert row_to_billing({"Card": "Citi", "Statement Closing Day": 40, "Payment Due Day": 5}) is None
    assert parse_date("2026-10-05") == date(2026, 10, 5)


def test_render_dashboard_returns_png_and_none_when_empty():
    assert render_dashboard([], "October 2026") is None
    txns = [_txn(date(2026, 10, 1), "Food", "DBS", 15.0),
            _txn(date(2026, 10, 2), "Transport", "Citi", 12.0)]
    png = render_dashboard(txns, "October 2026")
    assert png.startswith(b"\x89PNG")
