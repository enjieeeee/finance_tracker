import json
import os
from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional

import gspread
from google.oauth2.service_account import Credentials

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

TRANSACTIONS_TAB = "Transactions"
CARDS_TAB = "Cards"
BILLING_TAB = "Billing"
STATE_TAB = "State"

TRANSACTION_HEADERS = [
    "Date", "Time", "User", "Category", "Amount", "Currency",
    "Card", "Merchant/Notes", "Raw Message", "Logged At",
]
CARD_HEADERS = ["Card", "Issuer", "Reward Type", "Category Rules", "Min Spend", "Notes"]
BILLING_HEADERS = ["Card", "Statement Closing Day", "Payment Due Day", "Min Payment", "Notes"]
STATE_HEADERS = ["Key", "Value"]


@dataclass(frozen=True)
class Transaction:
    date: date
    time: str
    user: str
    category: str
    amount: float
    currency: str
    card: str
    notes: str
    raw: str = ""
    logged_at: str = ""


@dataclass(frozen=True)
class BillingRule:
    card: str
    closing_day: int
    due_day: int
    min_payment: str = ""


@dataclass(frozen=True)
class TransactionQuery:
    start: Optional[date] = None
    end: Optional[date] = None
    category: str = ""
    card: str = ""
    merchant: str = ""
    user: str = ""


def parse_date(value) -> Optional[date]:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d %b %Y", "%d %B %Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _to_float(value) -> Optional[float]:
    try:
        return float(str(value).replace(",", "").replace("$", "").strip())
    except ValueError:
        return None


def row_to_transaction(row: dict) -> Optional[Transaction]:
    parsed_date = parse_date(row.get("Date", ""))
    amount = _to_float(row.get("Amount", ""))
    if parsed_date is None or amount is None:
        return None
    return Transaction(
        date=parsed_date,
        time=str(row.get("Time", "")),
        user=str(row.get("User", "")),
        category=str(row.get("Category", "")),
        amount=amount,
        currency=str(row.get("Currency", "")),
        card=str(row.get("Card", "")),
        notes=str(row.get("Merchant/Notes", "")),
        raw=str(row.get("Raw Message", "")),
        logged_at=str(row.get("Logged At", "")),
    )


def row_to_billing(row: dict) -> Optional[BillingRule]:
    card = str(row.get("Card", "")).strip()
    try:
        closing = int(float(row.get("Statement Closing Day", "")))
        due = int(float(row.get("Payment Due Day", "")))
    except (TypeError, ValueError):
        return None
    if not card or not (1 <= closing <= 31 and 1 <= due <= 31):
        return None
    return BillingRule(
        card=card,
        closing_day=closing,
        due_day=due,
        min_payment=str(row.get("Min Payment", "")),
    )


def filter_transactions(transactions: list[Transaction], query: TransactionQuery) -> list[Transaction]:
    def contains(field: str, needle: str) -> bool:
        return not needle or needle.lower() in field.lower()

    matches = []
    for t in transactions:
        if query.start and t.date < query.start:
            continue
        if query.end and t.date > query.end:
            continue
        if not (contains(t.category, query.category)
                and contains(t.card, query.card)
                and contains(t.user, query.user)):
            continue
        if not contains(f"{t.notes} {t.raw}", query.merchant):
            continue
        matches.append(t)
    return sorted(matches, key=lambda t: (t.date, t.time))


def cards_as_text(rows: list[dict]) -> str:
    lines = []
    for row in rows:
        parts = [f"{k}: {v}" for k, v in row.items() if str(v).strip()]
        if parts:
            lines.append(" | ".join(parts))
    return "\n".join(lines)


class SheetsService:
    def __init__(self, sheet_id: str, service_account_json: str):
        if os.path.isfile(service_account_json):
            creds = Credentials.from_service_account_file(service_account_json, scopes=SCOPES)
        elif service_account_json.lstrip().startswith("{"):
            creds = Credentials.from_service_account_info(json.loads(service_account_json), scopes=SCOPES)
        else:
            raise RuntimeError(f"GOOGLE_SERVICE_ACCOUNT_JSON is neither an existing file nor JSON: {service_account_json}")
        self._spreadsheet = gspread.authorize(creds).open_by_key(sheet_id)

    def load_transactions(self) -> list[Transaction]:
        rows = self._spreadsheet.worksheet(TRANSACTIONS_TAB).get_all_records()
        return [t for t in (row_to_transaction(r) for r in rows) if t is not None]

    def append_transaction(self, t: Transaction) -> None:
        self._spreadsheet.worksheet(TRANSACTIONS_TAB).append_row(
            [t.date.isoformat(), t.time, t.user, t.category, t.amount, t.currency,
             t.card, t.notes, t.raw, t.logged_at],
            value_input_option="RAW",
        )

    def load_billing(self) -> list[BillingRule]:
        rows = self._spreadsheet.worksheet(BILLING_TAB).get_all_records()
        return [r for r in (row_to_billing(row) for row in rows) if r is not None]

    def load_cards_text(self) -> str:
        return cards_as_text(self._spreadsheet.worksheet(CARDS_TAB).get_all_records())

    def load_state(self, key: str) -> Optional[str]:
        for row in self._state_sheet().get_all_records():
            if str(row.get("Key", "")) == key:
                return str(row.get("Value", ""))
        return None

    def save_state(self, key: str, value: str) -> None:
        sheet = self._state_sheet()
        keys = sheet.col_values(1)
        if key in keys:
            sheet.update_cell(keys.index(key) + 1, 2, value)
        else:
            sheet.append_row([key, value], value_input_option="RAW")

    def _state_sheet(self):
        try:
            return self._spreadsheet.worksheet(STATE_TAB)
        except gspread.WorksheetNotFound:
            sheet = self._spreadsheet.add_worksheet(title=STATE_TAB, rows=50, cols=2)
            sheet.append_row(STATE_HEADERS, value_input_option="RAW")
            return sheet

