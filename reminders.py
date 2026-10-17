import calendar
from datetime import date, timedelta

from sheets_service import BillingRule

REMINDER_WINDOWS = (7, 3, 1, 0)


def _date_in_month(year: int, month: int, day: int) -> date:
    """Clamp the day to the month's length, e.g. day 31 in February becomes 28/29."""
    return date(year, month, min(day, calendar.monthrange(year, month)[1]))


def next_occurrence(day: int, today: date) -> date:
    """First date on or after `today` that falls on `day` of the month."""
    candidate = _date_in_month(today.year, today.month, day)
    if candidate >= today:
        return candidate
    following = (today.replace(day=1) + timedelta(days=32)).replace(day=1)
    return _date_in_month(following.year, following.month, day)


def due_reminders(rules: list[BillingRule], today: date) -> list[str]:
    messages = []
    for rule in rules:
        due = next_occurrence(rule.due_day, today)
        days_to_due = (due - today).days
        if days_to_due in REMINDER_WINDOWS:
            when = "today" if days_to_due == 0 else f"in {days_to_due} day(s)"
            messages.append(f"💳 {rule.card}: payment due {when} ({due:%d %b %Y}).")

        closing = next_occurrence(rule.closing_day, today)
        days_to_closing = (closing - today).days
        if days_to_closing in REMINDER_WINDOWS:
            when = "today" if days_to_closing == 0 else f"in {days_to_closing} day(s)"
            messages.append(f"🧾 {rule.card}: statement closes {when} ({closing:%d %b %Y}).")
    return messages


def billing_summary(rules: list[BillingRule], today: date) -> str:
    if not rules:
        return "No cards found in the Billing tab."
    lines = ["Credit card billing cycle:"]
    for rule in rules:
        closing = next_occurrence(rule.closing_day, today)
        due = next_occurrence(rule.due_day, today)
        lines.append(
            f"• {rule.card}: closes {closing:%d %b} ({(closing - today).days}d), "
            f"due {due:%d %b} ({(due - today).days}d)"
        )
    return "\n".join(lines)
