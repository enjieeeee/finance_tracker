import asyncio
import json
from datetime import date

import httpx
import pytest

from nl_compiler import CompiledCommand, compile_command, parse_llm_reply


def test_accepts_known_command_and_strips_blank_args():
    reply = json.dumps({"command": "search", "args": ["coffee", " ", "card=UOB Card"]})
    assert parse_llm_reply(reply) == CompiledCommand("search", ["coffee", "card=UOB Card"])


@pytest.mark.parametrize(
    "reply",
    [
        "not json",
        json.dumps(["search"]),
        json.dumps({"command": "delete_all", "args": []}),
        json.dumps({"command": "total", "args": "3"}),
        json.dumps({"command": "total", "args": [3]}),
    ],
)
def test_rejects_malformed_or_unknown_replies(reply):
    assert parse_llm_reply(reply) is None


def test_compile_command_posts_prompt_and_returns_command():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers["Authorization"]
        seen["body"] = json.loads(request.content)
        content = json.dumps({"command": "statement", "args": ["3"]})
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    result = asyncio.run(
        compile_command(
            "last 3 months",
            date(2026, 10, 22),
            "gsk_test",
            "test-model",
            transport=httpx.MockTransport(handler),
        )
    )

    assert result == CompiledCommand("statement", ["3"])
    assert seen["auth"] == "Bearer gsk_test"
    assert seen["body"]["model"] == "test-model"
    assert "2026-10-22" in seen["body"]["messages"][0]["content"]


def test_compile_command_returns_none_on_api_error():
    transport = httpx.MockTransport(lambda request: httpx.Response(500))
    result = asyncio.run(compile_command("hi", date(2026, 10, 22), "k", "m", transport=transport))
    assert result is None
