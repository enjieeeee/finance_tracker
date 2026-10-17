# Couple Finance Agent

A Telegram bot for a couple. It logs expenses to a shared Google Sheet, parses free text with Gemini, answers history questions, recommends credit cards, sends billing reminders, and draws monthly spending charts.

## Features
- **Log expenses**: "Lunch at hawker center $15, DBS card, En Jie" is parsed and appended to the `Transactions` tab.
- **History questions**: "How much did I spend on Grab last week?" Filters run in Python and Gemini answers from those rows only.
- **Card recommendations**: "Buying flight tickets on SIA website, which card?" Gemini uses the `Cards` tab rules.
- **Reminders**: daily 09:00 (`TIMEZONE`) alerts from the `Billing` tab, plus `/reminders`.
- **Dashboard**: `/dashboard 2026-10` sends bar charts by category and by card.

## 1. Google Sheet
Create a spreadsheet with three tabs and these headers in row 1:

| Tab | Headers |
|---|---|
| `Transactions` | `Date, Time, User, Category, Amount, Currency, Card, Merchant/Notes, Raw Message, Logged At` |
| `Cards` | `Card, Issuer, Reward Type, Category Rules, Min Spend, Notes` |
| `Billing` | `Card, Statement Closing Day, Payment Due Day, Min Payment, Notes` |

Example `Cards` row: `DBS Altitude | DBS | miles | dining 3mpd; online 3mpd; other 1.2mpd | - | SIA partner`.
`Billing` uses plain day numbers (e.g. closing `5`, due `25`).

Copy the sheet ID from its URL: `docs.google.com/spreadsheets/d/<SHEET_ID>/edit`.

## 2. Google service account
1. In Google Cloud Console, create a project and enable the **Google Sheets API**.
2. Create a service account under *IAM & Admin → Service Accounts*, then add a JSON key.
3. Save the key as `service_account.json` in the project folder (git-ignored).
4. Share the spreadsheet with the service account's email (`...@...iam.gserviceaccount.com`) as **Editor**.

## 3. Telegram bot and Gemini key
- Message `@BotFather`, send `/newbot`, and copy the token.
- Get each person's numeric user ID (e.g. from `@userinfobot`) for `ALLOWED_USER_IDS`.
- Create a Gemini API key in Google AI Studio.

## 4. Environment variables
Copy `.env.example` to `.env` and fill it in. Never commit `.env`.

| Variable | Purpose |
|---|---|
| `TELEGRAM_BOT_TOKEN` | BotFather token |
| `TELEGRAM_WEBHOOK_SECRET` | Random string; Telegram sends it back in a header |
| `WEBHOOK_URL` | Public HTTPS URL of the service (set after the first deploy) |
| `ALLOWED_USER_IDS` | Comma-separated Telegram user IDs allowed to use the bot |
| `REMINDER_CHAT_ID` | Chat that receives scheduled reminders |
| `GEMINI_API_KEY` | Gemini key |
| `GEMINI_MODEL` | Defaults to `gemini-3.5-flash`; verify the model ID is available to your key |
| `GOOGLE_SHEET_ID` | Spreadsheet ID |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | Path to the key file, or the JSON inline |
| `DEFAULT_CURRENCY` | Default `SGD` |
| `TIMEZONE` | Default `Asia/Singapore` |

## 5. Run locally
The bot uses webhooks, so for local testing expose it with a tunnel (e.g. `ngrok http 8080`) and set `WEBHOOK_URL` to that HTTPS URL.

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn main:app --port 8080
```

Run tests with `pytest`.

### Debug the AI locally (no Telegram needed)
```bash
python dev_console.py          # dry run: shows intent, parsed fields, matches, answers; writes nothing
python dev_console.py --write  # also appends expenses to the real sheet
python dev_console.py --debug  # also prints full Gemini prompts and raw JSON responses
```
Type `billing` to see the reminder summary, `quit` to exit. Use this to tune prompts and schemas before deploying.

## 6. Deploy to Cloud Run
```bash
gcloud run deploy finance-agent --source . --region asia-southeast1 \
  --allow-unauthenticated --min-instances 1 \
  --set-env-vars "TELEGRAM_BOT_TOKEN=...,TELEGRAM_WEBHOOK_SECRET=...,WEBHOOK_URL=https://<service-url>,ALLOWED_USER_IDS=...,REMINDER_CHAT_ID=...,GEMINI_API_KEY=...,GOOGLE_SHEET_ID=...,GOOGLE_SERVICE_ACCOUNT_JSON=<paste JSON>"
```
- Use `--set-secrets` with Secret Manager for the token and API keys in production.
- `--min-instances 1` keeps the built-in reminder scheduler running. It is not free-tier, so review the cost.
- After the first deploy, set `WEBHOOK_URL` to the service URL and redeploy. The bot registers its webhook on startup.
- To see the AI responses in production, tail the logs: `gcloud run services logs tail finance-agent --region asia-southeast1`. Set `LOG_LEVEL=DEBUG` on the service to include full Gemini prompts. Note that prompts contain your expense text, so turn it off when you're done.

## Commands
- `/start` or `/help`: show usage
- `/dashboard [YYYY-MM]`: monthly spending charts
- `/reminders`: billing cycle summary for all cards
