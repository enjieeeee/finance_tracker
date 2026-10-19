# Couple Finance Agent

A Telegram bot for a couple. It records expenses from Gmail alerts into a shared Google Sheet, answers history queries with slash commands, recommends credit cards, sends billing reminders, and draws monthly spending charts. It makes no paid LLM API calls.

## Features
- **Gmail ingestion**: PayNow outgoing transfers and UOB card transaction alerts are parsed with regex and appended to the `Transactions` tab. Unrecognised emails are skipped and logged.
- **Query commands**: `/search`, `/total`, `/cards`, `/billing` read the sheet directly.
- **Reminders**: daily 09:00 (`TIMEZONE`) alerts from the `Billing` tab, plus `/reminders`.
- **Dashboard**: `/dashboard 2026-10` sends bar charts by category and by card.

Free-text messages get a short help reply. Use the commands below.

## 1. Google Sheet
Create a spreadsheet with three tabs and these headers in row 1:

| Tab | Headers |
|---|---|
| `Transactions` | `Date, Time, User, Category, Amount, Currency, Card, Merchant, Raw Message, Logged At` |
| `Cards` | `Card Holder, Card, Card Number, Issuer, Reward Type, Category Rules, Min Spend, Key Exceptions & Traps to Avoid` |
| `Billing` | `Cardholder, Card, Statement Closing Day, Payment Due Day, Notes` |

Example `Cards` row: `En Jie | UOB Lady's Card | 2881 | UOB | ...`. `Card Number` should hold the last 4 digits only; a Gmail card alert ending in `2881` is then logged with the `Card` name (`UOB Lady's Card`) instead of `UOB Card ending 2881`. If no row matches, the alert keeps the `ending 2881` label.
`Billing` uses plain day numbers (e.g. closing `5`, due `25`).

Copy the sheet ID from its URL: `docs.google.com/spreadsheets/d/<SHEET_ID>/edit`.

**Migrating an existing sheet:** rename the old `Merchant/Notes` header to `Merchant` and delete any `Notes` column in the Transactions tab, so the header matches the table above. Categories must be one of `Groceries, Dining, Transport, Shopping, Bills, Others`; older rows are left as-is.

## 2. Google service account
1. In Google Cloud Console, create a project and enable the **Google Sheets API**.
2. Create a service account under *IAM & Admin → Service Accounts*, then add a JSON key.
3. Save the key as `service_account.json` in the project folder (git-ignored).
4. Share the spreadsheet with the service account's email (`...@...iam.gserviceaccount.com`) as **Editor**.

## 3. Telegram bot
- Message `@BotFather`, send `/newbot`, and copy the token.
- Get each person's numeric user ID (e.g. from `@userinfobot`) for `ALLOWED_USER_IDS`.

## 4. Environment variables
Copy `.env.example` to `.env` and fill it in. Never commit `.env`.

| Variable | Purpose |
|---|---|
| `TELEGRAM_BOT_TOKEN` | BotFather token |
| `TELEGRAM_WEBHOOK_SECRET` | Random string; Telegram sends it back in a header |
| `WEBHOOK_URL` | Public HTTPS URL of the service (set after the first deploy) |
| `ALLOWED_USER_IDS` | Comma-separated Telegram user IDs allowed to use the bot |
| `REMINDER_CHAT_ID` | Chat that receives scheduled reminders |
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

## 6. Deploy to Cloud Run
Option A: pass the variables inline.
```bash
gcloud run deploy finance-agent --source . --region asia-southeast1 \
  --allow-unauthenticated --min-instances 1 \
  --set-env-vars "TELEGRAM_BOT_TOKEN=...,TELEGRAM_WEBHOOK_SECRET=...,WEBHOOK_URL=https://<service-url>,ALLOWED_USER_IDS=...,REMINDER_CHAT_ID=...,GOOGLE_SHEET_ID=...,GOOGLE_SERVICE_ACCOUNT_JSON=<paste JSON>"
```

Option B: keep the variables in `env.yaml` and deploy with `--env-vars-file`. Create the file in the project root with one `KEY: "value"` per line (quote every value, so tokens and JSON parse correctly), then run:
```bash
gcloud run deploy finance-agent --source . --region asia-southeast1 \
  --allow-unauthenticated --min-instances 1 \
  --env-vars-file env.yaml
```
- `env.yaml` is git-ignored. Do not commit it.
- Option B keeps secrets out of your shell history.
- To change one variable without redeploying, use `gcloud run services update finance-agent --region asia-southeast1 --update-env-vars KEY=value`.

- Use `--set-secrets` with Secret Manager for the token and keys in production.
- `--min-instances 1` keeps the built-in reminder scheduler running. It is not free-tier, so review the cost.
- After the first deploy, set `WEBHOOK_URL` to the service URL and redeploy. The bot registers its webhook on startup.
- To check logs: `gcloud run services logs tail finance-agent --region asia-southeast1`.

## Commands
- `/start` or `/help`: show usage
- `/search <keyword> [YYYY-MM-DD..YYYY-MM-DD] [card=<name>] [user=<name>] [category=<name>]`: total, count, and the last 10 matching transactions. Keywords match the merchant name. Quote multi-word values, e.g. `/search "heart beat" card="UOB Card"`.
- `/total [YYYY-MM]`: totals per category for a month (defaults to the current month).
- `/cards`: card rules from the `Cards` tab.
- `/billing`: billing cycle summary from the `Billing` tab.
- `/dashboard [YYYY-MM]`: monthly spending charts.
- `/reminders`: billing cycle summary for all cards.
- `/clear`: no-op, kept for compatibility (the bot keeps no chat history).
