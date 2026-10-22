from datetime import date

from sheets_service import (
    BillingRule,
    Transaction,
    TransactionQuery,
    filter_transactions,
    parse_date,
)

SEARCH_USAGE = (
    'Usage: /search <keyword> [YYYY-MM-DD..YYYY-MM-DD] [card="..."] [user=...] [category=...]'
)
STATEMENT_USAGE = "Usage: /statement [number of months]"
TOTAL_USAGE = "Usage: /total [number of months]"
MAX_TOTAL_MONTHS = 24
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


def parse_statement_start(value: str, today: date) -> date:
    """No argument: current month only. N: starts N-1 months back (N months including this one)."""
    return recent_months(today, parse_month_count(value, STATEMENT_USAGE))[0]


def format_statement(transactions: list[Transaction], start: date, today: date) -> str:
    matches = sorted(
        (t for t in transactions if start <= t.date <= today), key=lambda t: (t.date, t.time)
    )
    if not matches:
        return f"No transactions from {start:%b %Y} to {today:%d %b %Y}."
    total = sum(t.amount for t in matches)
    lines = [
        f"Statement {start:%b %Y} to {today:%d %b %Y}: "
        f"{len(matches)} transaction(s), total {total:,.2f}"
    ]
    for t in matches:
        lines.append(f"{t.date:%d %b} | {t.card} | {t.currency} {t.amount:,.2f} | {t.merchant}")
    return "\n".join(lines)


def parse_month_count(value: str, usage: str) -> int:
    if not value:
        return 1
    try:
        count = int(value)
    except ValueError:
        raise QueryError(usage)
    if not 1 <= count <= MAX_TOTAL_MONTHS:
        raise QueryError(f"Choose between 1 and {MAX_TOTAL_MONTHS} months. {usage}")
    return count


def recent_months(today: date, count: int) -> list[date]:
    """First day of each of the last `count` months including this one, oldest first."""
    months = []
    year, month = today.year, today.month
    for _ in range(count):
        months.append(date(year, month, 1))
        month -= 1
        if month == 0:
            month, year = 12, year - 1
    return list(reversed(months))


def format_totals(transactions: list[Transaction], months: list[date]) -> str:
    totals = {month: 0.0 for month in months}
    for t in transactions:
        key = t.date.replace(day=1)
        if key in totals:
            totals[key] += t.amount
    if len(months) == 1:
        return f"Total for {months[0]:%B %Y}: {totals[months[0]]:,.2f}"
    return "\n".join(f"{month:%b %Y}: {totals[month]:,.2f}" for month in months)


def _one_line(value) -> str:
    return " ".join(str(value or "").split())


def format_cards(rows: list[dict]) -> str:
    """One block per card: holder, category rules and exceptions. Duplicate card rows are merged."""
    cards: dict[str, dict[str, list[str]]] = {}
    for row in rows:
        name = _one_line(row.get("Card"))
        if not name:
            continue
        entry = cards.setdefault(name, {"holders": [], "rules": [], "exceptions": []})
        for key, value in (
            ("holders", _one_line(row.get("Card Holder"))),
            ("rules", _one_line(row.get("Category Rules"))),
            ("exceptions", _one_line(row.get("Key Exceptions & Traps to Avoid"))),
        ):
            if value and value not in entry[key]:
                entry[key].append(value)
    if not cards:
        return "No cards found."
    blocks = []
    for name, entry in cards.items():
        header = f"💳 {name}" + (f" ({', '.join(entry['holders'])})" if entry["holders"] else "")
        lines = [header] + [f"• {rule}" for rule in entry["rules"]]
        lines += [f"⚠ {exception}" for exception in entry["exceptions"]]
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def format_billing(rules: list[BillingRule]) -> str:
    if not rules:
        return "No billing rules found."
    lines = []
    for r in rules:
        holder = f" ({r.cardholder})" if r.cardholder else ""
        lines.append(f"{r.card}{holder}: statement closes day {r.closing_day}, due day {r.due_day}")
    return "\n".join(lines)
