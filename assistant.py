"""Chat assistant: Gemini answers from the sheet by calling tools, and keeps per-chat memory."""

from collections import defaultdict, deque
from datetime import datetime
from typing import Iterator

from google.genai import types

from config import Settings
from gemini_service import ANSWER, THOUGHT, GeminiService, StreamChunk
from ingestion import build_transaction
from reminders import billing_summary
from sheets_service import (
    SheetsService,
    Transaction,
    TransactionQuery,
    filter_transactions,
    parse_date,
)

MAX_TOOL_ROUNDS = 5
MAX_HISTORY_TURNS = 20
MAX_ROWS_RETURNED = 200


def _string_arg(args: dict, key: str) -> str:
    return str(args.get(key) or "").strip()


def format_row(t: Transaction) -> str:
    return (f"{t.date.isoformat()} {t.time} | user={t.user} | category={t.category} | "
            f"amount={t.amount:.2f} {t.currency} | card={t.card} | merchant={t.merchant} | notes={t.notes}")


_SEARCH_PARAMS = {
    "type": "OBJECT",
    "properties": {
        "start_date": {"type": "STRING", "description": "YYYY-MM-DD, or empty"},
        "end_date": {"type": "STRING", "description": "YYYY-MM-DD, or empty"},
        "category": {"type": "STRING", "description": "Category keyword, or empty"},
        "card": {"type": "STRING", "description": "Card keyword, or empty"},
        "merchant": {"type": "STRING", "description": "Merchant keyword, e.g. Grab, or empty"},
        "user": {"type": "STRING", "description": "Person name, or empty"},
    },
}

_LOG_PARAMS = {
    "type": "OBJECT",
    "properties": {
        "date": {"type": "STRING", "description": "YYYY-MM-DD, or empty for today"},
        "time": {"type": "STRING", "description": "HH:MM 24-hour, or empty for now"},
        "category": {"type": "STRING", "description": "One of: Groceries, Dining, Transport, Shopping, Bills, Others"},
        "amount": {"type": "NUMBER", "description": "Plain number, no currency symbol"},
        "currency": {"type": "STRING", "description": "ISO code, or empty for the default"},
        "card": {"type": "STRING", "description": "Card or payment method, or empty"},
        "merchant": {"type": "STRING", "description": "Merchant or payee name, or empty"},
        "notes": {"type": "STRING", "description": "Short description, or empty"},
    },
    "required": ["category", "amount", "notes"],
}

TOOLS = [types.Tool(function_declarations=[
    types.FunctionDeclaration(
        name="search_transactions",
        description="Find logged transactions and their total. Only set the filters the question needs.",
        parameters=_SEARCH_PARAMS,
    ),
    types.FunctionDeclaration(
        name="log_expense",
        description="Prepare a purchase or payment for recording. Nothing is saved until confirm_expense "
                    "is called. Call only when the amount is known.",
        parameters=_LOG_PARAMS,
    ),
    types.FunctionDeclaration(
        name="confirm_expense",
        description="Save the prepared expense (confirm=true) or discard it (confirm=false). Call with "
                    "confirm=true only after the user explicitly agrees to the prepared expense.",
        parameters={
            "type": "OBJECT",
            "properties": {"confirm": {"type": "BOOLEAN", "description": "true to save, false to discard"}},
            "required": ["confirm"],
        },
    ),
    types.FunctionDeclaration(
        name="get_cards",
        description="Get the credit card names and their reward rules.",
    ),
    types.FunctionDeclaration(
        name="get_billing",
        description="Get each credit card's next statement closing date and payment due date.",
    ),
])]


def _system_prompt(sender: str, now: datetime, settings: Settings) -> str:
    return (
        "You are a personal finance assistant for a couple in Singapore, chatting with them on Telegram.\n"
        f"Today is {now:%A %d %B %Y}, the time is {now:%H:%M}. The person messaging you is {sender}. "
        f"The default currency is {settings.default_currency}.\n\n"
        "You can use tools to read and write their finance sheet. Rules:\n"
        "- For questions about past spending, call search_transactions with only the filters the "
        "question needs. Resolve relative dates such as 'last week' from today's date. Quote exact "
        "figures from the tool result and never guess.\n"
        "- To record a purchase or payment, call log_expense. If the amount is missing, ask for it "
        "instead of calling the tool. log_expense only prepares the expense: show the user the "
        "prepared details and ask them to confirm.\n"
        "- When the user agrees (yes, ok, confirm), call confirm_expense with confirm=true. When they "
        "decline or correct it, call confirm_expense with confirm=false, then log_expense again if "
        "they gave corrected details.\n"
        "- After a save, confirm what was recorded. If a tool says nothing was saved, say so plainly.\n"
        "- For which card to use, call get_cards and recommend the single best card with the rule it "
        "relies on and the expected return. If no rule applies, say so.\n"
        "- For credit card closing or due dates, call get_billing.\n"
        "- For anything else, reply conversationally. Keep replies short. Ask one short question when "
        "something is unclear."
    )


class Assistant:
    def __init__(self, gemini: GeminiService, sheets: SheetsService, settings: Settings, write: bool):
        self._gemini = gemini
        self._sheets = sheets
        self._settings = settings
        self._write = write
        self._history: dict[int, deque[tuple[str, str]]] = defaultdict(
            lambda: deque(maxlen=MAX_HISTORY_TURNS))
        self._pending: dict[int, dict] = {}

    def clear(self, chat_id: int) -> None:
        self._history.pop(chat_id, None)
        self._pending.pop(chat_id, None)

    def respond(self, chat_id: int, sender: str, text: str, now: datetime) -> Iterator[StreamChunk]:
        """Stream the reply to one message. Remembers the exchange for this chat."""
        contents: list[types.Content] = []
        for user_text, answer in self._history[chat_id]:
            contents.append(types.Content(role="user", parts=[types.Part(text=user_text)]))
            contents.append(types.Content(role="model", parts=[types.Part(text=answer)]))
        contents.append(types.Content(role="user", parts=[types.Part(text=text)]))

        answer_parts: list[str] = []
        system = _system_prompt(sender, now, self._settings)
        for kind, chunk in self._run(chat_id, contents, system, sender, text, now):
            if kind == ANSWER:
                answer_parts.append(chunk)
            yield kind, chunk

        answer = "".join(answer_parts).strip()
        if answer:
            self._history[chat_id].append((text, answer))

    def _run(self, chat_id: int, contents: list[types.Content], system: str, sender: str, text: str,
             now: datetime) -> Iterator[StreamChunk]:
        for _ in range(MAX_TOOL_ROUNDS):
            collected: list[types.Part] = []
            yield from self._gemini.stream_turn(contents, system, TOOLS, collected)
            calls = [part.function_call for part in collected if part.function_call]
            if not calls:
                return

            contents.append(types.Content(role="model", parts=collected))
            responses = []
            for call in calls:
                yield THOUGHT, f"\n🔧 {call.name}\n"
                result = self._call_tool(chat_id, call.name, dict(call.args or {}), sender, text, now)
                responses.append(types.Part.from_function_response(name=call.name, response=result))
            contents.append(types.Content(role="user", parts=responses))

        yield ANSWER, "I couldn't finish that. Please try a narrower question."

    def _call_tool(self, chat_id: int, name: str, args: dict, sender: str, text: str,
                   now: datetime) -> dict:
        try:
            if name == "search_transactions":
                return self._search(args)
            if name == "log_expense":
                return self._prepare(chat_id, args, sender, text, now)
            if name == "confirm_expense":
                return self._confirm(chat_id, bool(args["confirm"]))
            if name == "get_cards":
                return {"cards": self._sheets.load_cards_text() or "(no cards recorded)"}
            if name == "get_billing":
                return {"billing": billing_summary(self._sheets.load_billing(), now.date())}
        except (KeyError, TypeError, ValueError) as exc:
            return {"error": f"Invalid arguments for {name}: {exc}"}
        return {"error": f"Unknown tool {name}"}

    def _search(self, args: dict) -> dict:
        query = TransactionQuery(
            start=parse_date(_string_arg(args, "start_date")),
            end=parse_date(_string_arg(args, "end_date")),
            category=_string_arg(args, "category"),
            card=_string_arg(args, "card"),
            merchant=_string_arg(args, "merchant"),
            user=_string_arg(args, "user"),
        )
        matches = filter_transactions(self._sheets.load_transactions(), query)
        rows = [format_row(t) for t in matches[-MAX_ROWS_RETURNED:]]
        return {
            "count": len(matches),
            "total": round(sum(t.amount for t in matches), 2),
            "rows": rows,
            "rows_shown": len(rows),
        }

    def _prepare(self, chat_id: int, args: dict, sender: str, text: str, now: datetime) -> dict:
        fields = {**args, "amount": float(args["amount"])}
        if fields["amount"] <= 0:
            return {"saved": False, "reason": "amount must be positive"}
        txn = build_transaction(fields, sender, text, self._settings, now)
        self._pending[chat_id] = txn
        return {"status": "pending_confirmation", "row": format_row(txn),
                "instruction": "Ask the user to confirm these details before calling confirm_expense."}

    def _confirm(self, chat_id: int, confirmed: bool) -> dict:
        txn = self._pending.pop(chat_id, None)
        if txn is None:
            return {"saved": False, "reason": "no prepared expense to confirm"}
        if not confirmed:
            return {"saved": False, "reason": "discarded by the user"}
        if not self._write:
            return {"saved": False, "reason": "dry run: nothing was written", "row": format_row(txn)}
        self._sheets.append_transaction(txn)
        return {"saved": True, "row": format_row(txn)}
