"""Interactive terminal interface for the finance assistant."""

import asyncio
import os
from datetime import datetime
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from assistant import Assistant
from ingestion import recategorize_fallback
from sheets_service import SheetsService

RECATEGORIZE_USAGE = (
    "Usage: /recategorize to preview, /recategorize apply to write changes to the sheet."
)


def _required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise SystemExit(f"Missing required environment variable: {name}")
    return value


async def _run_recategorize(sheets: SheetsService, api_key: str, model: str, apply: bool) -> str:
    changes = await asyncio.to_thread(recategorize_fallback, sheets, api_key, model, apply)
    if not changes:
        return "No 'Others' transactions to recategorise."
    lines = [f"{txn.date:%d %b %Y} | {txn.merchant} -> {category}" for txn, category in changes]
    verb = "Updated" if apply else "Would update"
    header = f"{verb} {len(changes)} transaction(s)."
    if not apply:
        header += " Run /recategorize apply to write these changes."
    return "\n".join([header, *lines])


async def _run_console() -> None:
    load_dotenv()
    api_key = _required_env("GROQ_API_KEY")
    model = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b").strip()
    sheets = SheetsService(
        _required_env("GOOGLE_SHEET_ID"),
        _required_env("GOOGLE_SERVICE_ACCOUNT_JSON"),
    )
    timezone = ZoneInfo(os.getenv("TIMEZONE", "Asia/Singapore").strip())
    assistant = Assistant(sheets, api_key, model)

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
        if question.split()[0].lower() == "/recategorize":
            args = question.split()[1:]
            if args not in ([], ["apply"]):
                print(f"\n{RECATEGORIZE_USAGE}")
                continue
            apply = args == ["apply"]
            print(f"\n{await _run_recategorize(sheets, api_key, model, apply)}")
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
