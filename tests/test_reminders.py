from datetime import date

from reminders import due_reminders, next_occurrence
from sheets_service import BillingRule


def test_next_occurrence_rolls_to_next_month_when_day_passed():
    assert next_occurrence(5, date(2026, 10, 12)) == date(2026, 11, 5)


def test_next_occurrence_clamps_day_to_month_length():
    assert next_occurrence(31, date(2026, 2, 1)) == date(2026, 2, 28)


def test_due_reminder_fires_in_window():
    rules = [BillingRule(card="DBS Altitude", closing_day=5, due_day=19, min_payment="$50")]
    messages = due_reminders(rules, date(2026, 10, 12))
    assert len(messages) == 1
    assert "DBS Altitude" in messages[0]
    assert "in 7 day(s)" in messages[0]
    assert "$50" in messages[0]


def test_no_reminder_outside_windows():
    rules = [BillingRule(card="Citi", closing_day=5, due_day=25)]
    assert due_reminders(rules, date(2026, 10, 12)) == []
