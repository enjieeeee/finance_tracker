"""Chat assistant: the LLM picks finance tools on Groq, the bot runs them against the sheet."""

import asyncio
import json
import logging
from datetime import date
from typing import Callable

import httpx

from queries import format_billing, format_cards, recent_months
from sheets_service import (
    SheetsService,
    Transaction,
    TransactionQuery,
    filter_transactions,
    parse_date,
)

logger = logging.getLogger(__name__)

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
MAX_TOOL_ROUNDS = 5
MAX_ROWS_RETURNED = 200
MAX_MONTHS = 24
FALLBACK_REPLY = "I couldn't finish that. Please try a narrower question."


def _string(args: dict, key: str) -> str:
    return str(args.get(key) or "").strip()


def _date_arg(args: dict, key: str) -> date | None:
    value = _string(args, key)
    if not value:
        return None
    parsed = parse_date(value)
    if parsed is None:
        raise ValueError(f"'{key}' must be a date like YYYY-MM-DD, got '{value}'")
    return parsed


def _format_row(t: Transaction) -> dict:
    return {
        "date": t.date.isoformat(),
        "time": t.time,
        "user": t.user,
        "category": t.category,
        "amount": round(t.amount, 2),
        "currency": t.currency,
        "card": t.card,
        "merchant": t.merchant,
    }


class FinanceTools:
    """Read-only tools the model can call. Each returns a JSON-serialisable dict."""

    def __init__(self, sheets: SheetsService):
        self._sheets = sheets

    def search_transactions(self, args: dict, today: date) -> dict:
        query = TransactionQuery(
            start=_date_arg(args, "start_date"),
            end=_date_arg(args, "end_date"),
            category=_string(args, "category"),
            card=_string(args, "card"),
            merchant=_string(args, "merchant"),
            user=_string(args, "user"),
        )
        matches = filter_transactions(self._sheets.load_transactions(), query)
        shown = matches[-MAX_ROWS_RETURNED:]
        return {
            "count": len(matches),
            "total": round(sum(t.amount for t in matches), 2),
            "rows": [_format_row(t) for t in shown],
            "rows_shown": len(shown),
        }

    def get_monthly_totals(self, args: dict, today: date) -> dict:
        months = int(args.get("months") or 1)
        if not 1 <= months <= MAX_MONTHS:
            raise ValueError(f"'months' must be between 1 and {MAX_MONTHS}")
        periods = recent_months(today, months)
        totals = {month: 0.0 for month in periods}
        for t in self._sheets.load_transactions():
            key = t.date.replace(day=1)
            if key in totals:
                totals[key] += t.amount
        return {"totals": [{"month": f"{m:%Y-%m}", "total": round(totals[m], 2)} for m in periods]}

    def get_cards(self, args: dict, today: date) -> dict:
        return {"cards": format_cards(self._sheets.load_cards_rows())}

    def get_billing(self, args: dict, today: date) -> dict:
        return {"billing": format_billing(self._sheets.load_billing())}


TOOL_SPECS = [
    {
        "type": "function",
        "function": {
            "name": "search_transactions",
            "description": (
                "Find logged transactions and their total. Set only the filters the question "
                "needs. Use a date range for a specific month or period, e.g. March 2026 is "
                "2026-03-01 to 2026-03-31."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "start_date": {"type": "string", "description": "YYYY-MM-DD, or empty"},
                    "end_date": {"type": "string", "description": "YYYY-MM-DD, or empty"},
                    "category": {"type": "string", "description": "Category keyword, or empty"},
                    "card": {"type": "string", "description": "Card name keyword, or empty"},
                    "merchant": {"type": "string", "description": "Merchant keyword, e.g. Grab"},
                    "user": {"type": "string", "description": "Person name, or empty"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_monthly_totals",
            "description": "Spending total for each of the last N months, including this month.",
            "parameters": {
                "type": "object",
                "properties": {
                    "months": {
                        "type": "integer",
                        "description": f"Number of months, 1 to {MAX_MONTHS}",
                    }
                },
                "required": ["months"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_cards",
            "description": "Credit card names, holders, category reward rules and exceptions.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_billing",
            "description": "Each card's statement closing day and payment due day.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]


def _system_prompt(today: date) -> str:
    return (
        "You are a personal finance assistant for a couple, replying on Telegram.\n"
        f"Today is {today:%A %d %B %Y}. Resolve relative dates such as 'last week' from today.\n\n"
        "Rules:\n"
        "- Answer questions about spending, cards and billing only by calling the tools. "
        "Never guess figures.\n"
        "- Quote totals and amounts exactly as the tool returned them.\n"
        "- For a specific month, use search_transactions with that month's date range. "
        "Use get_monthly_totals for trends over several months.\n"
        "- For which card to use, call get_cards and recommend the single best card with the rule "
        "it relies on. If no rule applies, say so.\n"
        "- If a tool returns an error, fix the arguments and retry once, or tell the user.\n"
        "- Reply in plain text, no markdown. Keep replies short."
    )


ToolRunner = Callable[[dict, date], dict]


class Assistant:
    def __init__(
        self,
        sheets: SheetsService,
        api_key: str,
        model: str,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self._tools = FinanceTools(sheets)
        self._api_key = api_key
        self._model = model
        self._transport = transport
        self._runners: dict[str, ToolRunner] = {
            spec["function"]["name"]: getattr(self._tools, spec["function"]["name"])
            for spec in TOOL_SPECS
        }

    async def answer(self, text: str, today: date) -> str:
        messages: list[dict] = [
            {"role": "system", "content": _system_prompt(today)},
            {"role": "user", "content": text},
        ]
        async with httpx.AsyncClient(timeout=30, transport=self._transport) as client:
            for _ in range(MAX_TOOL_ROUNDS):
                message = await self._chat(client, messages)
                calls = message.get("tool_calls") or []
                if not calls:
                    return (message.get("content") or "").strip() or FALLBACK_REPLY
                messages.append(
                    {
                        "role": "assistant",
                        "content": message.get("content") or "",
                        "tool_calls": calls,
                    }
                )
                for call in calls:
                    result = await asyncio.to_thread(self._run_tool, call, today)
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call["id"],
                            "content": json.dumps(result, default=str),
                        }
                    )
        return FALLBACK_REPLY

    async def _chat(self, client: httpx.AsyncClient, messages: list[dict]) -> dict:
        payload = {
            "model": self._model,
            "messages": messages,
            "tools": TOOL_SPECS,
            "tool_choice": "auto",
            "temperature": 0,
        }
        response = await client.post(
            GROQ_URL, json=payload, headers={"Authorization": f"Bearer {self._api_key}"}
        )
        response.raise_for_status()
        return response.json()["choices"][0]["message"]

    def _run_tool(self, call: dict, today: date) -> dict:
        name = call["function"]["name"]
        runner = self._runners.get(name)
        if runner is None:
            return {"error": f"Unknown tool {name}"}
        try:
            args = json.loads(call["function"].get("arguments") or "{}")
            if not isinstance(args, dict):
                raise ValueError("arguments must be a JSON object")
            return runner(args, today)
        except (ValueError, TypeError) as exc:
            logger.warning("Tool %s failed: %s", name, exc)
            return {"error": f"Invalid arguments for {name}: {exc}"}
