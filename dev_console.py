"""Interactive terminal interface for the finance assistant."""

import asyncio
import os
from datetime import datetime
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from assistant import Assistant
from sheets_service import SheetsService


def _required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise SystemExit(f"Missing required environment variable: {name}")
    return value


async def _run_console() -> None:
    load_dotenv()
    api_key = _required_env("GROQ_API_KEY")
    sheets = SheetsService(
        _required_env("GOOGLE_SHEET_ID"),
        _required_env("GOOGLE_SERVICE_ACCOUNT_JSON"),
    )
    timezone = ZoneInfo(os.getenv("TIMEZONE", "Asia/Singapore").strip())
    assistant = Assistant(
        sheets,
        api_key,
        os.getenv("GROQ_MODEL", "openai/gpt-oss-120b").strip(),
    )

    print("Finance assistant console. Ask a question, or type /exit to quit.")
    while True:
        try:
            question = input("\nYou> ").strip()
        except EOFError:
            print()
            return
        if question.lower() in {"/exit", "/quit"}:
            return
        if not question:
            continue

        answer = await assistant.answer(question, datetime.now(timezone).date())
        print(f"\nAssistant> {answer}")


def main() -> None:
    try:
        asyncio.run(_run_console())
    except KeyboardInterrupt:
        print("\nGoodbye.")


if __name__ == "__main__":
    main()
