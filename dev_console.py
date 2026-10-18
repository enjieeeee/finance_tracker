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
import json
import logging
from datetime import datetime

from dotenv import load_dotenv

from config import load_settings
from gemini_service import THOUGHT, GeminiService
from reminders import billing_summary
from sheets_service import (
    SheetsService,
    Transaction,
    TransactionQuery,
    filter_transactions,
    parse_date,
)

SENDER = "DevConsole"


def print_stream(chunks):
    """Print thought summaries dimmed, then the answer, as they arrive."""
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


def run_message(text, settings, gemini, sheets, write):
    now = datetime.now(settings.tz)
    print("[status] classifying...")
    intent = gemini.classify_intent(text)
    print(f"[intent] {intent}")

    if intent == "expense":
        fields = gemini.parse_expense(text, SENDER, now.date())
        print(f"[parsed] {json.dumps(fields, ensure_ascii=False)}")
        txn = Transaction(
            date=parse_date(fields.get("date", "")) or now.date(),
            time=fields.get("time") or now.strftime("%H:%M"),
            user=SENDER,
            category=fields.get("category") or "Other",
            amount=fields["amount"],
            currency=fields.get("currency") or settings.default_currency,
            card=fields.get("card") or "Unknown",
            notes=fields.get("notes", ""),
            raw=text,
            logged_at=now.isoformat(timespec="seconds"),
        )
        if write:
            sheets.append_transaction(txn)
            print("[sheet] appended row")
        else:
            print("[sheet] dry run, row NOT written (use --write)")

    elif intent == "recommend":
        cards_text = sheets.load_cards_text()
        print(f"[cards] {len(cards_text.splitlines())} card rule line(s) loaded")
        print("[status] checking card rules...")
        print_stream(gemini.recommend_card_stream(text, cards_text))

    else:
        plan = gemini.plan_query(text, now.date())
        print(f"[query] {json.dumps(plan, ensure_ascii=False)}")
        query = TransactionQuery(
            start=parse_date(plan.get("start_date", "")),
            end=parse_date(plan.get("end_date", "")),
            category=plan.get("category", ""),
            card=plan.get("card", ""),
            merchant=plan.get("merchant", ""),
            user=plan.get("user", ""),
        )
        matches = filter_transactions(sheets.load_transactions(), query)
        print(f"[matches] {len(matches)} row(s)")
        for t in matches[-20:]:
            print(f"  {t.date} {t.time} | {t.user} | {t.category} | {t.amount:.2f} {t.currency} | {t.card} | {t.notes}")
        if matches:
            rows = [f"{t.date} {t.time} | {t.user} | {t.category} | {t.amount:.2f} {t.currency} | {t.card} | {t.notes}"
                    for t in matches[-200:]]
            print_stream(gemini.answer_question_stream(text, rows, now.date()))
        else:
            print("No matching transactions found.")


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
    print(f"Dev console ({mode}, model {settings.gemini_model}). Type a message, or 'billing' / 'quit'.")
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
        try:
            run_message(text, settings, gemini, sheets, args.write)
        except Exception as exc:  # keep the console alive so you can try the next message
            print(f"[error] {type(exc).__name__}: {exc}")


if __name__ == "__main__":
    main()
