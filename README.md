# Couple Finance Agent

A Telegram bot for a couple. It logs expenses to a shared Google Sheet, parses free text with Gemini, answers history questions, recommends credit cards, sends billing reminders, and draws monthly spending charts.

## Features
- **Log expenses**: "Lunch at hawker center $15, DBS card, En Jie" is parsed and appended to the `Transactions` tab.
- **Chat assistant**: free-form conversation. Gemini decides what to do by calling tools: `search_transactions` (reads the sheet), `log_expense` plus `confirm_expense` (an expense is shown for confirmation before it is saved), `get_cards` (card rules), and `get_billing`. Replies stream in live, with the thinking shown first. Each chat keeps its recent history (20 turns); `/clear` resets it.
- **Reminders**: daily 09:00 (`TIMEZONE`) alerts from the `Billing` tab, plus `/reminders`.
- **Dashboard**: `/dashboard 2026-10` sends bar charts by category and by card.

## 1. Google Sheet
Create a spreadsheet with three tabs and these headers in row 1:

| Tab | Headers |
|---|---|
| `Transactions` | `Date, Time, User, Category, Amount, Currency, Card, Merchant, Raw Message, Logged At` |
| `Cards` | `Card Holder, Card, Issuer, Reward Type, Category Rules, Min Spend, Key Exceptions & Traps to Avoid` |
| `Billing` | `Cardholder, Card, Statement Closing Day, Payment Due Day, Notes` |

Example `Cards` row: `En Jie | DBS Altitude | DBS | miles | dining 3mpd; online 3mpd; other 1.2mpd | - | SIA partner`.
`Billing` uses plain day numbers (e.g. closing `5`, due `25`).

Copy the sheet ID from its URL: `docs.google.com/spreadsheets/d/<SHEET_ID>/edit`.

**Migrating an existing sheet:** rename the old `Merchant/Notes` header to `Merchant` and delete any `Notes` column in the Transactions tab, so the header matches the table above. Categories must be one of `Groceries, Dining, Transport, Shopping, Bills, Others`; older rows are left as-is.

## iOS Shortcut payload
`POST /webhook/ios` with header `X-Webhook-Secret` and JSON body:
```json
{"text": "DBS: SGD 14.50 charged at NTUC via Apple Pay", "user": "En Jie", "card": "DBS Altitude"}
```
`card` is optional; if omitted, the card is parsed from `text`. Identical notifications on the same day are logged once and return `{"ok": true, "duplicate": true}`.

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
python dev_console.py          # dry run: shows thinking, tool calls, and the answer; writes nothing
python dev_console.py --write  # also appends expenses to the real sheet
python dev_console.py --debug  # also prints full Gemini prompts and raw JSON responses
```
Type `billing` to see the reminder summary, `clear` to reset the conversation, `quit` to exit. Use this to tune the system prompt and tools before deploying.

## 6. Deploy to Cloud Run
Option A: pass the variables inline.
```bash
gcloud run deploy finance-agent --source . --region asia-southeast1 \
  --allow-unauthenticated --min-instances 1 \
  --set-env-vars "TELEGRAM_BOT_TOKEN=...,TELEGRAM_WEBHOOK_SECRET=...,WEBHOOK_URL=https://<service-url>,ALLOWED_USER_IDS=...,REMINDER_CHAT_ID=...,GEMINI_API_KEY=...,GOOGLE_SHEET_ID=...,GOOGLE_SERVICE_ACCOUNT_JSON=<paste JSON>,IOS_WEBHOOK_SECRET=..."
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

- Use `--set-secrets` with Secret Manager for the token and API keys in production.
- `--min-instances 1` keeps the built-in reminder scheduler running. It is not free-tier, so review the cost.
- After the first deploy, set `WEBHOOK_URL` to the service URL and redeploy. The bot registers its webhook on startup.
- To see the AI responses in production, tail the logs: `gcloud run services logs tail finance-agent --region asia-southeast1`. Set `LOG_LEVEL=DEBUG` on the service to include full Gemini prompts. Note that prompts contain your expense text, so turn it off when you're done.

## Commands
- `/start` or `/help`: show usage
- `/clear`: forget this chat's conversation history
- `/dashboard [YYYY-MM]`: monthly spending charts
- `/reminders`: billing cycle summary for all cards
