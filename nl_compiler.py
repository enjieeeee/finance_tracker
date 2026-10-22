"""Turns a free-text Telegram message into a bot command. The LLM never answers questions itself."""

import json
import logging
from dataclasses import dataclass
from datetime import date

import httpx

logger = logging.getLogger(__name__)

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
MAX_ARG_CHARS = 100
COMMANDS = {"search", "statement", "total", "dashboard", "cards", "billing", "help"}

SYSTEM_PROMPT = """You convert a user's message into one command for a personal card-spending bot.
Do not answer questions yourself. Reply with JSON only:
{"command": "<name>", "args": ["<arg>", ...]}

Commands:
- search: find transactions and their total. Args: optional keywords (merchant words), then any of
  "YYYY-MM-DD..YYYY-MM-DD" (date range), "card=<card name>", "user=<holder name>",
  "category=<name>". Use search with a date range for questions about a specific month or period.
- statement: list transactions. Args: [] for this month, or ["N"] for the last N months (1-24).
- total: spending per month. Args: [] for this month, or ["N"] for the last N months (1-24).
- dashboard: spending charts. Args: [] for this month, or ["YYYY-MM"].
- cards: card rules and exceptions. Args: [].
- billing: statement closing and due days. Args: [].
- help: show the command list. Args: [].

If the message is not about these, or is unclear, use command "help" with args [].
Today is {today}. Resolve relative dates such as "last month" or "in March" using today's date.
Output only the JSON object."""


@dataclass(frozen=True)
class CompiledCommand:
    command: str
    args: list[str]


def parse_llm_reply(content: str) -> CompiledCommand | None:
    """Validate the model's JSON. Anything malformed or unknown returns None."""
    try:
        data = json.loads(content)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    command = data.get("command")
    args = data.get("args", [])
    if command not in COMMANDS or not isinstance(args, list):
        return None
    if not all(isinstance(a, str) and len(a) <= MAX_ARG_CHARS for a in args):
        return None
    return CompiledCommand(command=command, args=[a.strip() for a in args if a.strip()])


async def compile_command(
    text: str,
    today: date,
    api_key: str,
    model: str,
    transport: httpx.AsyncBaseTransport | None = None,
) -> CompiledCommand | None:
    payload = {
        "model": model,
        "temperature": 0,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT.replace("{today}", today.isoformat())},
            {"role": "user", "content": text},
        ],
    }
    headers = {"Authorization": f"Bearer {api_key}"}
    try:
        async with httpx.AsyncClient(timeout=15, transport=transport) as client:
            response = await client.post(GROQ_URL, json=payload, headers=headers)
            response.raise_for_status()
            content = response.json()["choices"][0]["message"]["content"]
    except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError):
        logger.exception("Groq request failed")
        return None
    compiled = parse_llm_reply(content)
    if compiled is None:
        logger.warning("Groq returned an invalid command: %r", content)
    return compiled
