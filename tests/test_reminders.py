from datetime import date

from reminders import billing_summary, due_reminders, gmail_token_reminder, next_occurrence
from sheets_service import BillingRule


def test_next_occurrence_rolls_to_next_month_when_day_passed():
    assert next_occurrence(5, date(2026, 10, 12)) == date(2026, 11, 5)


def test_next_occurrence_clamps_day_to_month_length():
    assert next_occurrence(31, date(2026, 2, 1)) == date(2026, 2, 28)


def test_due_reminder_fires_in_window():
    rules = [BillingRule(card="DBS Altitude", closing_day=5, due_day=19)]
    messages = due_reminders(rules, date(2026, 10, 12))
    assert len(messages) == 1
    assert "DBS Altitude" in messages[0]
    assert "in 7 day(s)" in messages[0]


def test_no_reminder_outside_windows():
    rules = [BillingRule(card="Citi", closing_day=5, due_day=25)]
    assert due_reminders(rules, date(2026, 10, 12)) == []


def test_gmail_token_reminder_is_sent_one_day_before_seven_day_expiry():
    refreshed_at = date(2026, 10, 26)

    message = gmail_token_reminder(refreshed_at, date(2026, 11, 1))

    assert "expire tomorrow (02 Nov 2026)" in message


def test_gmail_token_reminder_is_not_sent_on_other_days_or_without_refresh_date():
    refreshed_at = date(2026, 10, 26)

    assert gmail_token_reminder(refreshed_at, date(2026, 10, 31)) is None
    assert gmail_token_reminder(refreshed_at, date(2026, 11, 2)) is None
    assert gmail_token_reminder(None, date(2026, 11, 1)) is None


def test_billing_summary_tells_same_card_apart_by_cardholder():
    rules = [
        BillingRule(card="Preferred Platinum", closing_day=12, due_day=2, cardholder="En Jie"),
        BillingRule(card="Preferred Platinum", closing_day=12, due_day=2, cardholder="Jeslin"),
    ]
    summary = billing_summary(rules, date(2026, 10, 12))
    assert "Preferred Platinum (En Jie)" in summary
    assert "Preferred Platinum (Jeslin)" in summary
