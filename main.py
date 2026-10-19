import asyncio
import functools
import hmac
import logging
import os
import shlex
from contextlib import asynccontextmanager
from datetime import datetime, time

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

from chart_generator import render_dashboard
from config import Settings, load_settings
from gmail_service import GmailClient, decode_push_history_id
from ingestion import sync_gmail
from queries import (SEARCH_USAGE, QueryError, format_billing, format_search, format_totals,
                     parse_month, parse_search)
from reminders import billing_summary, due_reminders
from sheets_service import SheetsService, Transaction

logger = logging.getLogger(__name__)
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
WEBHOOK_PATH = "/telegram/webhook"
TELEGRAM_MAX_CHARS = 4096
GMAIL_WEBHOOK_PATH = "/webhook/gmail"
HELP_TEXT = (
    "Transactions are logged automatically from Gmail (PayNow and card alerts).\n\n"
    "Commands:\n"
    "/search <keyword> [YYYY-MM-DD..YYYY-MM-DD] [card=\"...\"] [user=...] [category=...] - find transactions\n"
    "/total [YYYY-MM] - spending by category for a month\n"
    "/dashboard [YYYY-MM] - spending charts for a month\n"
    "/cards - card rules and benefits\n"
    "/billing - statement closing and due days\n"
    "/reminders - upcoming card closing and due dates\n"
    "/help - this list\n"
    "/clear - nothing to clear (no chat memory)"
)


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
    await update.effective_message.reply_text("Nothing to clear. Commands don't keep a conversation.")


@allowed_only
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text("Free-text chat is off. Send /help to see the commands.")


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
    await message.reply_text(text[:TELEGRAM_MAX_CHARS])


@allowed_only
async def search_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    sheets: SheetsService = context.bot_data["sheets"]
    message = update.effective_message
    try:
        args = _command_args(message.text)
        if not args:
            raise QueryError(SEARCH_USAGE)
        query = parse_search(args)
    except QueryError as exc:
        await message.reply_text(str(exc))
        return
    transactions = await asyncio.to_thread(sheets.load_transactions)
    await _reply(message, format_search(transactions, query))


@allowed_only
async def total_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.bot_data["settings"]
    sheets: SheetsService = context.bot_data["sheets"]
    message = update.effective_message
    try:
        args = _command_args(message.text)
        month = parse_month(args[0] if args else "", datetime.now(settings.tz).date())
    except QueryError as exc:
        await message.reply_text(str(exc))
        return
    transactions = await asyncio.to_thread(sheets.load_transactions)
    await _reply(message, format_totals(transactions, month))


@allowed_only
async def cards_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    sheets: SheetsService = context.bot_data["sheets"]
    text = await asyncio.to_thread(sheets.load_cards_text)
    await _reply(update.effective_message, text or "No cards found.")


@allowed_only
async def billing_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    sheets: SheetsService = context.bot_data["sheets"]
    rules = await asyncio.to_thread(sheets.load_billing)
    await _reply(update.effective_message, format_billing(rules))


def _format_saved(txn: Transaction) -> str:
    return (f"{txn.date:%d %b %Y} {txn.time} |{txn.card}  "
            f"{txn.currency} {txn.amount:,.2f}| {txn.merchant}")


async def _notify_allowed_users(bot, settings: Settings, text: str) -> None:
    for user_id in settings.allowed_user_ids:
        try:
            await bot.send_message(chat_id=user_id, text=text)
        except Exception:
            logger.exception("Could not notify user %s", user_id)


@allowed_only
async def dashboard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.bot_data["settings"]
    sheets: SheetsService = context.bot_data["sheets"]
    today = datetime.now(settings.tz).date()
    arg = context.args[0] if context.args else today.strftime("%Y-%m")
    try:
        month = datetime.strptime(arg, "%Y-%m").date()
    except ValueError:
        await update.effective_message.reply_text("Usage: /dashboard YYYY-MM")
        return

    label = month.strftime("%B %Y")
    transactions = await asyncio.to_thread(sheets.load_transactions)
    monthly = [t for t in transactions if (t.date.year, t.date.month) == (month.year, month.month)]
    png = await asyncio.to_thread(render_dashboard, monthly, label)
    if png is None:
        await update.effective_message.reply_text(f"No transactions recorded for {label}.")
        return
    await update.effective_message.reply_photo(photo=png, caption=f"Spending for {label}")


@allowed_only
async def reminders_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.bot_data["settings"]
    sheets: SheetsService = context.bot_data["sheets"]
    rules = await asyncio.to_thread(sheets.load_billing)
    await update.effective_message.reply_text(
        billing_summary(rules, datetime.now(settings.tz).date()))


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
    for text in messages:
        await context.bot.send_message(chat_id=settings.reminder_chat_id, text=text)


def build_application(settings: Settings) -> Application:
    application = ApplicationBuilder().token(settings.telegram_bot_token).updater(None).build()
    sheets = SheetsService(settings.google_sheet_id, settings.service_account_json)
    application.bot_data.update(
        settings=settings,
        sheets=sheets,
        gmail=(GmailClient(settings.gmail_client_id, settings.gmail_client_secret,
                           settings.gmail_refresh_token) if settings.gmail_enabled else None),
        gmail_lock=asyncio.Lock(),
    )
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", start))
    application.add_handler(CommandHandler("clear", clear_chat))
    application.add_handler(CommandHandler("search", search_command))
    application.add_handler(CommandHandler("total", total_command))
    application.add_handler(CommandHandler("cards", cards_command))
    application.add_handler(CommandHandler("billing", billing_command))
    application.add_handler(CommandHandler("dashboard", dashboard))
    application.add_handler(CommandHandler("reminders", reminders_command))
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
                request.query_params.get("token", ""), settings.gmail_push_token):
            raise HTTPException(status_code=403, detail="Invalid push token")
        gmail: GmailClient | None = telegram_app.bot_data["gmail"]
        if gmail is None:
            raise HTTPException(status_code=503, detail="Gmail not configured")

        push_history_id = decode_push_history_id(await request.json())
        now = datetime.now(settings.tz)
        async with telegram_app.bot_data["gmail_lock"]:
            logged = await asyncio.to_thread(
                sync_gmail, gmail, telegram_app.bot_data["sheets"],
                settings, push_history_id, now)
        for txn in logged:
            await _notify_allowed_users(telegram_app.bot, settings,
                                        f"📧 Transaction logged: {_format_saved(txn)}")
        return {"ok": True, "logged": len(logged)}

    @api.get("/health")
    async def health():
        return {"status": "ok"}

    return api


app = create_app()
