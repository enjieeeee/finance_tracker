import asyncio
import json
from datetime import date

import httpx

from assistant import FALLBACK_REPLY, MAX_TOOL_ROUNDS, Assistant
from sheets_service import Transaction

TODAY = date(2026, 4, 15)


class FakeSheets:
    def load_transactions(self):
        return [
            Transaction(date(2026, 3, 5), "12:00", "Ann", "Food", 20.0, "SGD", "UOB", "Grab"),
            Transaction(date(2026, 4, 2), "09:00", "Ben", "Food", 7.5, "SGD", "DBS", "Kopitiam"),
        ]

    def load_cards_rows(self):
        return []

    def load_billing(self):
        return []


def _tool_call(call_id: str, name: str, arguments: dict) -> dict:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }


def _transport(replies: list[dict], sent: list[dict]) -> httpx.MockTransport:
    queue = list(replies)

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": queue.pop(0)}]})

    return httpx.MockTransport(handler)


def _ask(transport, text="q") -> str:
    assistant = Assistant(FakeSheets(), "key", "model", transport=transport)
    return asyncio.run(assistant.answer(text, TODAY))


def test_tool_call_result_is_fed_back_and_final_answer_returned():
    sent: list[dict] = []
    replies = [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                _tool_call(
                    "c1",
                    "search_transactions",
                    {"start_date": "2026-03-01", "end_date": "2026-03-31"},
                )
            ],
        },
        {"role": "assistant", "content": "You spent 20.00 SGD in March."},
    ]

    answer = _ask(_transport(replies, sent))

    assert answer == "You spent 20.00 SGD in March."
    tool_msg = sent[1]["messages"][-1]
    assert tool_msg["role"] == "tool" and tool_msg["tool_call_id"] == "c1"
    result = json.loads(tool_msg["content"])
    assert result["count"] == 1 and result["total"] == 20.0
    assert sent[0]["tools"][0]["function"]["name"] == "search_transactions"


def test_bad_date_returns_error_to_model_instead_of_dropping_filter():
    sent: list[dict] = []
    replies = [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [_tool_call("c1", "search_transactions", {"start_date": "last tuesday"})],
        },
        {"role": "assistant", "content": "Please give a date like 2026-03-01."},
    ]

    _ask(_transport(replies, sent))

    result = json.loads(sent[1]["messages"][-1]["content"])
    assert "error" in result and "start_date" in result["error"]


def test_gives_up_after_max_tool_rounds():
    sent: list[dict] = []
    looping = {
        "role": "assistant",
        "content": None,
        "tool_calls": [_tool_call("c", "get_cards", {})],
    }

    answer = _ask(_transport([looping] * MAX_TOOL_ROUNDS, sent))

    assert answer == FALLBACK_REPLY
    assert len(sent) == MAX_TOOL_ROUNDS
