from collections import defaultdict
from datetime import date, datetime

from sheets_service import BillingRule, Transaction, TransactionQuery, filter_transactions, parse_date

SEARCH_USAGE = ("Usage: /search <keyword> [YYYY-MM-DD..YYYY-MM-DD] "
                "[card=\"...\"] [user=...] [category=...]")
TOTAL_USAGE = "Usage: /total [YYYY-MM]"
RECENT_LIMIT = 10
FILTER_KEYS = {"card", "user", "category"}


class QueryError(ValueError):
    """Bad command arguments; the message is shown to the user as-is."""


def parse_search(args: list[str]) -> TransactionQuery:
    start = end = None
    fields: dict[str, str] = {}
    words: list[str] = []
    for token in args:
        if ".." in token:
            low, _, high = token.partition("..")
            start, end = parse_date(low), parse_date(high)
            if start is None or end is None or start > end:
                raise QueryError(f"Bad date range '{token}'. {SEARCH_USAGE}")
        elif "=" in token:
            key, _, value = token.partition("=")
            if key.lower() not in FILTER_KEYS or not value:
                raise QueryError(f"Unknown filter '{token}'. {SEARCH_USAGE}")
            fields[key.lower()] = value
        else:
            words.append(token)
    return TransactionQuery(start=start, end=end, merchant=" ".join(words), **fields)


def format_search(transactions: list[Transaction], query: TransactionQuery) -> str:
    matches = filter_transactions(transactions, query)
    if not matches:
        return "No matching transactions."
    total = sum(t.amount for t in matches)
    lines = [f"{len(matches)} transaction(s), total {total:,.2f}"]
    if len(matches) > RECENT_LIMIT:
        lines.append(f"Latest {RECENT_LIMIT}:")
    for t in matches[-RECENT_LIMIT:]:
        lines.append(f"{t.date:%d %b %Y} | {t.card} | {t.currency} {t.amount:,.2f} | {t.merchant}")
    return "\n".join(lines)


def parse_month(value: str, today: date) -> date:
    if not value:
        return today.replace(day=1)
    try:
        return datetime.strptime(value, "%Y-%m").date().replace(day=1)
    except ValueError:
        raise QueryError(TOTAL_USAGE)


def format_totals(transactions: list[Transaction], month: date) -> str:
    monthly = [t for t in transactions if (t.date.year, t.date.month) == (month.year, month.month)]
    if not monthly:
        return f"No transactions for {month:%B %Y}."
    by_category: dict[str, float] = defaultdict(float)
    for t in monthly:
        by_category[t.category or "Uncategorised"] += t.amount
    lines = [f"Spending for {month:%B %Y}:"]
    for category, amount in sorted(by_category.items(), key=lambda kv: -kv[1]):
        lines.append(f"{category}: {amount:,.2f}")
    lines.append(f"Total: {sum(by_category.values()):,.2f}")
    return "\n".join(lines)


def format_billing(rules: list[BillingRule]) -> str:
    if not rules:
        return "No billing rules found."
    lines = []
    for r in rules:
        holder = f" ({r.cardholder})" if r.cardholder else ""
        lines.append(f"{r.card}{holder}: statement closes day {r.closing_day}, due day {r.due_day}")
    return "\n".join(lines)
