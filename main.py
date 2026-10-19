import asyncio
import functools
import hmac
import logging
import os
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

from assistant import Assistant
from chart_generator import render_dashboard
from config import Settings, load_settings
from gemini_service import GeminiService
from gmail_service import GmailClient, decode_push_history_id
from ingestion import log_ios_expense, sync_gmail
from reminders import billing_summary, due_reminders
from streaming import stream_reply
from sheets_service import SheetsService, Transaction

logger = logging.getLogger(__name__)
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
WEBHOOK_PATH = "/telegram/webhook"
IOS_WEBHOOK_PATH = "/webhook/ios"
GMAIL_WEBHOOK_PATH = "/webhook/gmail"
HELP_TEXT = (
    "Chat with me naturally. I can:\n"
    "• Log spending: \"Lunch at hawker center $15 on DBS\"\n"
    "• Answer questions: \"How much did I spend on Grab last week?\"\n"
    "• Suggest a card: \"Buying flight tickets on SIA website, which card?\"\n"
    "• Check billing: \"When is my next card payment due?\"\n\n"
    "Commands:\n"
    "/dashboard [YYYY-MM] - spending charts for a month\n"
    "/reminders - credit card closing and due dates\n"
    "/clear - forget this conversation"
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


def _sender(update: Update) -> str:
    user = update.effective_user
    return (user.first_name or str(user.id)) if user else "Unknown"


@allowed_only
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(HELP_TEXT)


@allowed_only
async def clear_chat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.bot_data["assistant"].clear(update.effective_chat.id)
    await update.effective_message.reply_text("Conversation cleared.")


@allowed_only
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.bot_data["settings"]
    assistant: Assistant = context.bot_data["assistant"]
    message = update.effective_message
    text = message.text.strip()

    placeholder = await message.reply_text("🧠 Thinking…")
    chunks = assistant.respond(update.effective_chat.id, _sender(update), text,
                               datetime.now(settings.tz))
    try:
        await stream_reply(placeholder, chunks, status="🧠 Thinking…")
    except Exception:
        logger.exception("Assistant failed")
        await placeholder.edit_text("Sorry, I couldn't process that. Please try again.")


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
    gemini = GeminiService(settings.gemini_api_key, settings.gemini_model)
    application.bot_data.update(
        settings=settings,
        sheets=sheets,
        gemini=gemini,
        assistant=Assistant(gemini, sheets, settings, write=True),
        gmail=(GmailClient(settings.gmail_client_id, settings.gmail_client_secret,
                           settings.gmail_refresh_token) if settings.gmail_enabled else None),
        gmail_lock=asyncio.Lock(),
    )
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", start))
    application.add_handler(CommandHandler("clear", clear_chat))
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

    @api.post(IOS_WEBHOOK_PATH)
    async def ios_webhook(request: Request):
        if not settings.ios_webhook_secret:
            raise HTTPException(status_code=503, detail="iOS webhook not configured")
        if not hmac.compare_digest(request.headers.get("X-Webhook-Secret", ""),
                                   settings.ios_webhook_secret):
            raise HTTPException(status_code=403, detail="Invalid webhook secret")
        payload = await request.json()
        text = str(payload.get("text", "")).strip()
        sender = str(payload.get("user", "")).strip() or "iOS"
        if not text:
            raise HTTPException(status_code=422, detail="Missing 'text'")

        now = datetime.now(settings.tz)
        try:
            txn = await asyncio.to_thread(
                log_ios_expense, text, sender, telegram_app.bot_data["gemini"],
                telegram_app.bot_data["sheets"], settings, now)
        except ValueError:
            raise HTTPException(status_code=422, detail="No amount found in notification")
        await _notify_allowed_users(telegram_app.bot, settings, f"💳 Apple Pay logged: {_format_saved(txn)}")
        return {"ok": True, "saved": _format_saved(txn)}

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
                settings, push_history_id, now, gemini=telegram_app.bot_data["gemini"])
        for txn in logged:
            await _notify_allowed_users(telegram_app.bot, settings,
                                        f"📧 Transaction logged: {_format_saved(txn)}")
        return {"ok": True, "logged": len(logged)}

    @api.get("/health")
    async def health():
        return {"status": "ok"}

    return api


app = create_app()
