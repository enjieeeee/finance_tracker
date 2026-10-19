from datetime import date

import pytest

from chart_generator import render_dashboard
from queries import (QueryError, format_billing, format_search, format_totals, parse_month,
                     parse_search)
from sheets_service import (
    BillingRule,
    Transaction,
    TransactionQuery,
    card_names_by_ending,
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


def test_row_to_billing_rejects_invalid_days():
    assert row_to_billing({"Card": "Citi", "Statement Closing Day": 40, "Payment Due Day": 5}) is None
    assert parse_date("2026-10-05") == date(2026, 10, 5)


def test_render_dashboard_returns_png_and_none_when_empty():
    assert render_dashboard([], "October 2026") is None
    txns = [_txn(date(2026, 10, 1), "Food", "DBS", 15.0),
            _txn(date(2026, 10, 2), "Transport", "Citi", 12.0)]
    png = render_dashboard(txns, "October 2026")
    assert png.startswith(b"\x89PNG")


def test_parse_search_splits_range_filters_and_keyword():
    query = parse_search(["grab", "2026-10-01..2026-10-31", 'card=UOB Card', "user=jeslin"])
    assert query.start == date(2026, 10, 1)
    assert query.end == date(2026, 10, 31)
    assert query.merchant == "grab"
    assert query.card == "UOB Card"
    assert query.user == "jeslin"


def test_parse_search_rejects_bad_range_and_unknown_filter():
    with pytest.raises(QueryError):
        parse_search(["2026-10-31..2026-10-01"])
    with pytest.raises(QueryError):
        parse_search(["color=red"])


def test_format_search_reports_count_total_and_latest_rows():
    txns = [_txn(date(2026, 10, d), "Food", "DBS", 10.0, merchant="Koufu") for d in range(1, 4)]
    text = format_search(txns, TransactionQuery(merchant="koufu"))
    assert text.startswith("3 transaction(s), total 30.00")
    assert "01 Oct 2026 | DBS | SGD 10.00 | Koufu" in text
    assert format_search(txns, TransactionQuery(merchant="nothing")) == "No matching transactions."


def test_parse_month_defaults_to_current_month_and_rejects_bad_input():
    assert parse_month("", date(2026, 10, 17)) == date(2026, 10, 1)
    assert parse_month("2026-09", date(2026, 10, 17)) == date(2026, 9, 1)
    with pytest.raises(QueryError):
        parse_month("October", date(2026, 10, 17))


def test_format_totals_groups_by_category_for_one_month_only():
    txns = [_txn(date(2026, 10, 1), "Food", "DBS", 10.0),
            _txn(date(2026, 10, 2), "Food", "DBS", 5.0),
            _txn(date(2026, 10, 3), "Transport", "Citi", 4.0),
            _txn(date(2026, 9, 30), "Food", "DBS", 99.0)]
    text = format_totals(txns, date(2026, 10, 1))
    assert "Food: 15.00" in text
    assert "Transport: 4.00" in text
    assert "Total: 19.00" in text
    assert "99" not in text


def test_format_billing_lists_each_rule():
    rules = [BillingRule(cardholder="En Jie", card="DBS Altitude", closing_day=5, due_day=25)]
    assert format_billing(rules) == "DBS Altitude (En Jie): statement closes day 5, due day 25"


def test_card_names_by_ending_keys_on_last_4_digits_and_skips_incomplete_rows():
    rows = [
        {"Card": "UOB Lady's Card", "Card Number": "4539 1234 5678 2881"},
        {"Card": "DBS Altitude", "Card Number": 1111},
        {"Card": "No number yet", "Card Number": ""},
    ]
    assert card_names_by_ending(rows) == {"2881": "UOB Lady's Card", "1111": "DBS Altitude"}
