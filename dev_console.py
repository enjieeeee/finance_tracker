"""Local console to test the bot logic without Telegram.

Each message runs the same steps as the bot and prints what Gemini returned
and what would be (or was) written to the sheet. Nothing is written unless
you pass --write.

Usage:
    python dev_console.py            # dry run, no sheet writes
    python dev_console.py --write    # actually append expenses to the sheet
    python dev_console.py --debug    # also print full prompts and raw responses
"""

import argparse
import logging
from datetime import datetime

from dotenv import load_dotenv

from assistant import Assistant
from config import load_settings
from gemini_service import THOUGHT, GeminiService
from reminders import billing_summary
from sheets_service import SheetsService

SENDER = "En Jie"
CHAT_ID = 0


def print_stream(chunks):
    """Print thoughts and tool calls dimmed, then the answer, as they arrive."""
    announced_thinking = announced_answer = False
    for kind, text in chunks:
        if kind == THOUGHT:
            if not announced_thinking:
                print("[thinking]")
                announced_thinking = True
            print(f"\033[2m{text}\033[0m", end="", flush=True)
        else:
            if not announced_answer:
                print("\n[answer]")
                announced_answer = True
            print(text, end="", flush=True)
    print()


def run_message(text, settings, assistant):
    print_stream(assistant.respond(CHAT_ID, SENDER, text, datetime.now(settings.tz)))



def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--write", action="store_true", help="append expenses to the sheet")
    parser.add_argument("--debug", action="store_true", help="print full Gemini prompts and responses")
    args = parser.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.debug else logging.WARNING, format="%(message)s")
    load_dotenv()
    settings = load_settings()
    gemini = GeminiService(settings.gemini_api_key, settings.gemini_model)
    sheets = SheetsService(settings.google_sheet_id, settings.service_account_json)

    mode = "WRITE mode" if args.write else "dry run"
    assistant = Assistant(gemini, sheets, settings, write=args.write)
    print(f"Dev console ({mode}, model {settings.gemini_model}). Type a message, or 'billing' / 'clear' / 'quit'.")
    while True:
        try:
            text = input("\n> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not text:
            continue
        if text.lower() in {"quit", "exit"}:
            break
        if text.lower() == "billing":
            print(billing_summary(sheets.load_billing(), datetime.now(settings.tz).date()))
            continue
        if text.lower() == "clear":
            assistant.clear(CHAT_ID)
            print("Conversation cleared.")
            continue
        try:
            run_message(text, settings, assistant)
        except Exception as exc:  # keep the console alive so you can try the next message
            print(f"[error] {type(exc).__name__}: {exc}")



if __name__ == "__main__":
    main()
