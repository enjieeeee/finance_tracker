import asyncio
import functools
import hmac
import logging
import os
import shlex
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta

from fastapi import FastAPI, HTTPException, Request
from telegram import Update
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from assistant import Assistant
from chart_generator import render_dashboard
from config import Settings, load_settings
from gmail_service import GmailClient, decode_push_history_id
from ingestion import recategorize_fallback, sync_gmail
from queries import (
    SEARCH_USAGE,
    STATEMENT_USAGE,
    TOTAL_USAGE,
    QueryError,
    format_billing,
    format_cards,
    format_search,
    format_statement,
    format_totals,
    parse_month_count,
    parse_search,
    parse_statement_start,
    recent_months,
)
from reminders import (
    GMAIL_TOKEN_VALIDITY_DAYS,
    billing_summary,
    due_reminders,
    gmail_token_reminder,
)
from sheets_service import SheetsService, Transaction

logger = logging.getLogger(__name__)
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
WEBHOOK_PATH = "/telegram/webhook"
TELEGRAM_MAX_CHARS = 4096
GMAIL_WEBHOOK_PATH = "/webhook/gmail"
GMAIL_TOKEN_REFRESHED_KEY = "gmail_token_refreshed_at"
HELP_TEXT = (
    "Transactions are logged automatically from Gmail (PayNow, QR, card and transit alerts).\n\n"
    "Commands:\n"
    '/search <keyword> [YYYY-MM-DD..YYYY-MM-DD] [card="..."] [user=...] [category=...]'
    " - find transactions\n"
    "/statement [months] - transactions for this month, or from N months back to now\n"
    "/total [months] - spending for this month, or per month for the last N months\n"
    "/dashboard [YYYY-MM] - spending charts for a month\n"
    "/cards - card rules and exceptions\n"
    "/billing - statement closing and due days\n"
    "/recategorize [apply] - re-sort 'Others' transactions by merchant (preview unless 'apply')\n"
    "/gmailrefreshed - record that the Gmail refresh token was refreshed today\n"
    "/help - this list\n"
    "/clear - nothing to clear (no chat memory)"
)
RECATEGORIZE_USAGE = "Usage: /recategorize [apply]"


def allowed_only(handler):
    @functools.wraps(handler)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        settings: Settings = context.bot_data["settings"]
        user = update.effective_user
        if user is None or user.id not in settings.allowed_user_ids:
            logger.warning("Rejected message from unauthorised user %s", user.id if user else None)
            if update.effective_message:
                await update.effective_message.reply_text("Sorry, this bot is private.")
            return
        return await handler(update, context)

    return wrapper


@allowed_only
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(HELP_TEXT)


@allowed_only
async def clear_chat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "Nothing to clear. Commands don't keep a conversation."
    )


@allowed_only
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.bot_data["settings"]
    message = update.effective_message
    assistant: Assistant | None = context.bot_data["assistant"]
    if assistant is None:
        await message.reply_text("Free-text chat is off. Send /help to see the commands.")
        return
    await context.bot.send_chat_action(chat_id=message.chat_id, action="typing")
    reply = await assistant.answer(message.text, datetime.now(settings.tz).date())
    await _reply(message, reply)


def _command_args(message_text: str) -> list[str]:
    """Arguments after the command name, honouring quotes (e.g. card="UOB Card")."""
    parts = (message_text or "").split(maxsplit=1)
    if len(parts) < 2:
        return []
    try:
        return shlex.split(parts[1])
    except ValueError as exc:
        raise QueryError(f"Could not read arguments: {exc}")


async def _reply(message, text: str) -> None:
    for chunk in split_message(text):
        await message.reply_text(chunk)


def split_message(text: str, limit: int = TELEGRAM_MAX_CHARS) -> list[str]:
    """Split on line breaks so each chunk fits Telegram's message limit."""
    chunks: list[str] = []
    current = ""
    for line in text.splitlines():
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > limit and current:
            chunks.append(current)
            current = line
        else:
            current = candidate
    chunks.append(current)
    return chunks


async def run_search(message, context, args: list[str]) -> None:
    sheets: SheetsService = context.bot_data["sheets"]
    try:
        if not args:
            raise QueryError(SEARCH_USAGE)
        query = parse_search(args)
    except QueryError as exc:
        await message.reply_text(str(exc))
        return
    transactions = await asyncio.to_thread(sheets.load_transactions)
    await _reply(message, format_search(transactions, query))


async def run_statement(message, context, args: list[str]) -> None:
    settings: Settings = context.bot_data["settings"]
    sheets: SheetsService = context.bot_data["sheets"]
    try:
        if len(args) > 1:
            raise QueryError(STATEMENT_USAGE)
        today = datetime.now(settings.tz).date()
        start = parse_statement_start(args[0] if args else "", today)
    except QueryError as exc:
        await message.reply_text(str(exc))
        return
    transactions = await asyncio.to_thread(sheets.load_transactions)
    await _reply(message, format_statement(transactions, start, today))


async def run_total(message, context, args: list[str]) -> None:
    settings: Settings = context.bot_data["settings"]
    sheets: SheetsService = context.bot_data["sheets"]
    try:
        if len(args) > 1:
            raise QueryError(TOTAL_USAGE)
        count = parse_month_count(args[0] if args else "", TOTAL_USAGE)
    except QueryError as exc:
        await message.reply_text(str(exc))
        return
    months = recent_months(datetime.now(settings.tz).date(), count)
    transactions = await asyncio.to_thread(sheets.load_transactions)
    await _reply(message, format_totals(transactions, months))


async def run_cards(message, context, args: list[str]) -> None:
    sheets: SheetsService = context.bot_data["sheets"]
    rows = await asyncio.to_thread(sheets.load_cards_rows)
    await _reply(message, format_cards(rows))


async def run_billing(message, context, args: list[str]) -> None:
    sheets: SheetsService = context.bot_data["sheets"]
    rules = await asyncio.to_thread(sheets.load_billing)
    await _reply(message, format_billing(rules))


async def run_recategorize(message, context, args: list[str]) -> None:
    settings: Settings = context.bot_data["settings"]
    sheets: SheetsService = context.bot_data["sheets"]
    if args not in ([], ["apply"]):
        await message.reply_text(RECATEGORIZE_USAGE)
        return
    apply = args == ["apply"]
    changes = await asyncio.to_thread(
        recategorize_fallback, sheets, settings.groq_api_key, settings.groq_model, apply
    )
    if not changes:
        await message.reply_text("No 'Others' transactions to recategorise.")
        return
    lines = [f"{txn.date:%d %b %Y} | {txn.merchant} -> {category}" for txn, category in changes]
    verb = "Updated" if apply else "Would update"
    header = f"{verb} {len(changes)} transaction(s)."
    if not apply:
        header += " Send /recategorize apply to write these changes."
    await _reply(message, "\n".join([header, *lines]))


def _format_saved(txn: Transaction) -> str:
    return (
        f"{txn.date:%d %b %Y} {txn.time} |{txn.card}  "
        f"{txn.currency} {txn.amount:,.2f}| {txn.merchant}"
    )


async def _notify_allowed_users(bot, settings: Settings, text: str) -> None:
    for user_id in settings.allowed_user_ids:
        try:
            await bot.send_message(chat_id=user_id, text=text)
        except Exception:
            logger.exception("Could not notify user %s", user_id)


async def run_dashboard(message, context, args: list[str]) -> None:
    settings: Settings = context.bot_data["settings"]
    sheets: SheetsService = context.bot_data["sheets"]
    today = datetime.now(settings.tz).date()
    arg = args[0] if args else today.strftime("%Y-%m")
    try:
        month = datetime.strptime(arg, "%Y-%m").date()
    except ValueError:
        await message.reply_text("Usage: /dashboard YYYY-MM")
        return

    label = month.strftime("%B %Y")
    transactions = await asyncio.to_thread(sheets.load_transactions)
    monthly = [t for t in transactions if (t.date.year, t.date.month) == (month.year, month.month)]
    png = await asyncio.to_thread(render_dashboard, monthly, label)
    if png is None:
        await message.reply_text(f"No transactions recorded for {label}.")
        return
    await message.reply_photo(photo=png, caption=f"Spending for {label}")


def _command_handler(runner):
    """Slash-command handler that passes the parsed arguments to a runner."""

    @allowed_only
    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        message = update.effective_message
        try:
            args = _command_args(message.text)
        except QueryError as exc:
            await message.reply_text(str(exc))
            return
        await runner(message, context, args)

    handler.__name__ = runner.__name__.replace("run_", "") + "_command"
    return handler


search_command = _command_handler(run_search)
statement_command = _command_handler(run_statement)
total_command = _command_handler(run_total)
cards_command = _command_handler(run_cards)
billing_command = _command_handler(run_billing)
dashboard_command = _command_handler(run_dashboard)
recategorize_command = _command_handler(run_recategorize)


async def send_daily_reminders(context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.bot_data["settings"]
    sheets: SheetsService = context.bot_data["sheets"]
    if settings.reminder_chat_id is None:
        return
    today = datetime.now(settings.tz).date()
    rules = await asyncio.to_thread(sheets.load_billing)
    messages = due_reminders(rules, today)
    if today.day == 1:
        messages.insert(0, billing_summary(rules, today))
    if settings.gmail_enabled:
        refreshed_at_value = await asyncio.to_thread(sheets.load_state, GMAIL_TOKEN_REFRESHED_KEY)
        refreshed_at = date.fromisoformat(refreshed_at_value) if refreshed_at_value else None
        token_message = gmail_token_reminder(refreshed_at, today)
        if token_message:
            messages.insert(0, token_message)
    for text in messages:
        await context.bot.send_message(chat_id=settings.reminder_chat_id, text=text)


@allowed_only
async def gmail_refreshed(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.bot_data["settings"]
    sheets: SheetsService = context.bot_data["sheets"]
    refreshed_at = datetime.now(settings.tz).date()
    await asyncio.to_thread(sheets.save_state, GMAIL_TOKEN_REFRESHED_KEY, refreshed_at.isoformat())
    reminder_date = refreshed_at + timedelta(days=GMAIL_TOKEN_VALIDITY_DAYS - 1)
    expiry_date = refreshed_at + timedelta(days=GMAIL_TOKEN_VALIDITY_DAYS)
    message = (
        f"Recorded Gmail token refresh for {refreshed_at:%d %b %Y}. "
        f"I'll remind you on {reminder_date:%d %b %Y}, one day before the assumed "
        f"expiry on {expiry_date:%d %b %Y}."
    )
    if settings.reminder_chat_id is None:
        message += " Set REMINDER_CHAT_ID to receive scheduled reminders."
    await update.effective_message.reply_text(message)


def build_application(settings: Settings) -> Application:
    application = ApplicationBuilder().token(settings.telegram_bot_token).updater(None).build()
    sheets = SheetsService(settings.google_sheet_id, settings.service_account_json)
    application.bot_data.update(
        settings=settings,
        sheets=sheets,
        gmail=(
            GmailClient(
                settings.gmail_client_id, settings.gmail_client_secret, settings.gmail_refresh_token
            )
            if settings.gmail_enabled
            else None
        ),
        gmail_lock=asyncio.Lock(),
        assistant=(
            Assistant(sheets, settings.groq_api_key, settings.groq_model)
            if settings.groq_api_key
            else None
        ),
    )
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", start))
    application.add_handler(CommandHandler("clear", clear_chat))
    application.add_handler(CommandHandler("search", search_command))
    application.add_handler(CommandHandler("statement", statement_command))
    application.add_handler(CommandHandler("total", total_command))
    application.add_handler(CommandHandler("cards", cards_command))
    application.add_handler(CommandHandler("billing", billing_command))
    application.add_handler(CommandHandler("dashboard", dashboard_command))
    application.add_handler(CommandHandler("recategorize", recategorize_command))
    application.add_handler(CommandHandler("gmailrefreshed", gmail_refreshed))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    application.job_queue.run_daily(
        send_daily_reminders,
        time=time(hour=9, minute=0, tzinfo=settings.tz),
        name="daily-reminders",
    )
    application.job_queue.run_daily(
        renew_gmail_watch,
        time=time(hour=9, minute=30, tzinfo=settings.tz),
        name="gmail-watch-renewal",
    )
    return application


async def renew_gmail_watch(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Gmail watches expire after 7 days, so renew daily."""
    settings: Settings = context.bot_data["settings"]
    gmail: GmailClient | None = context.bot_data["gmail"]
    if gmail is None:
        return
    if settings.gmail_pubsub_topic:
        await asyncio.to_thread(gmail.watch, settings.gmail_pubsub_topic)
        logger.info("Renewed Gmail watch")


def create_app() -> FastAPI:
    settings = load_settings()
    telegram_app = build_application(settings)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        await telegram_app.initialize()
        await telegram_app.start()
        await telegram_app.bot.set_webhook(
            url=settings.webhook_url + WEBHOOK_PATH,
            secret_token=settings.webhook_secret,
            allowed_updates=Update.ALL_TYPES,
        )
        yield
        await telegram_app.stop()
        await telegram_app.shutdown()

    api = FastAPI(lifespan=lifespan)

    @api.post(WEBHOOK_PATH)
    async def telegram_webhook(request: Request):
        if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != settings.webhook_secret:
            raise HTTPException(status_code=403, detail="Invalid secret token")
        update = Update.de_json(await request.json(), telegram_app.bot)
        await telegram_app.process_update(update)
        return {"ok": True}

    @api.post(GMAIL_WEBHOOK_PATH)
    async def gmail_webhook(request: Request):
        if not settings.gmail_push_token or not hmac.compare_digest(
            request.query_params.get("token", ""), settings.gmail_push_token
        ):
            raise HTTPException(status_code=403, detail="Invalid push token")
        gmail: GmailClient | None = telegram_app.bot_data["gmail"]
        if gmail is None:
            raise HTTPException(status_code=503, detail="Gmail not configured")

        push_history_id = decode_push_history_id(await request.json())
        now = datetime.now(settings.tz)
        async with telegram_app.bot_data["gmail_lock"]:
            logged = await asyncio.to_thread(
                sync_gmail, gmail, telegram_app.bot_data["sheets"], settings, push_history_id, now
            )
        for txn in logged:
            await _notify_allowed_users(
                telegram_app.bot, settings, f"📧 Transaction logged: {_format_saved(txn)}"
            )
        return {"ok": True, "logged": len(logged)}

    @api.get("/health")
    async def health():
        return {"status": "ok"}

    return api


app = create_app()
