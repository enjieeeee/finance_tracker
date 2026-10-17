import json
import logging
from datetime import date

from google import genai
from google.genai import types

logger = logging.getLogger(__name__)

INTENTS = ("expense", "history", "recommend")

INTENT_SCHEMA = {
    "type": "OBJECT",
    "properties": {"intent": {"type": "STRING", "enum": list(INTENTS)}},
    "required": ["intent"],
}

EXPENSE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "date": {"type": "STRING", "description": "YYYY-MM-DD, or empty if not stated"},
        "time": {"type": "STRING", "description": "HH:MM 24-hour, or empty if not stated"},
        "category": {"type": "STRING"},
        "amount": {"type": "NUMBER"},
        "currency": {"type": "STRING", "description": "ISO code, or empty if not stated"},
        "card": {"type": "STRING", "description": "Card name as written, or empty"},
        "notes": {"type": "STRING", "description": "Merchant or short description"},
    },
    "required": ["date", "time", "category", "amount", "currency", "card", "notes"],
}

EMAIL_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "direction": {"type": "STRING", "enum": ["outgoing", "incoming", "none"]},
        "date": {"type": "STRING", "description": "YYYY-MM-DD, or empty if not stated"},
        "time": {"type": "STRING", "description": "HH:MM 24-hour, or empty if not stated"},
        "category": {"type": "STRING"},
        "amount": {"type": "NUMBER"},
        "currency": {"type": "STRING", "description": "ISO code, or empty if not stated"},
        "card": {"type": "STRING", "description": "Payment method, or empty"},
        "notes": {"type": "STRING", "description": "Merchant or recipient"},
    },
    "required": ["direction", "date", "time", "category", "amount", "currency", "card", "notes"],
}

QUERY_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "start_date": {"type": "STRING", "description": "YYYY-MM-DD or empty"},
        "end_date": {"type": "STRING", "description": "YYYY-MM-DD or empty"},
        "category": {"type": "STRING", "description": "Category keyword or empty"},
        "card": {"type": "STRING", "description": "Card keyword or empty"},
        "merchant": {"type": "STRING", "description": "Merchant or notes keyword, e.g. Grab, or empty"},
        "user": {"type": "STRING", "description": "Person name or empty"},
    },
    "required": ["start_date", "end_date", "category", "card", "merchant", "user"],
}


class GeminiService:
    def __init__(self, api_key: str, model: str):
        self._client = genai.Client(api_key=api_key)
        self._model = model

    def _json(self, prompt: str, schema: dict) -> dict:
        logger.debug("Gemini JSON prompt:\n%s", prompt)
        response = self._client.models.generate_content(
            model=self._model,
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=schema,
                temperature=0,
            ),
        )
        logger.debug("Gemini JSON response: %s", response.text)
        return json.loads(response.text)

    def _text(self, prompt: str) -> str:
        logger.debug("Gemini text prompt:\n%s", prompt)
        response = self._client.models.generate_content(
            model=self._model,
            contents=prompt,
            config=types.GenerateContentConfig(temperature=0.2),
        )
        text = (response.text or "").strip()
        logger.debug("Gemini text response: %s", text)
        return text

    def classify_intent(self, message: str) -> str:
        prompt = (
            "Classify this Telegram message from a couple's finance bot.\n"
            "- expense: they are logging a purchase or payment to record\n"
            "- history: they are asking about past transactions or spending\n"
            "- recommend: they ask which credit card to use for a purchase\n\n"
            f'Message: """{message}"""'
        )
        intent = self._json(prompt, INTENT_SCHEMA).get("intent", "history")
        return intent if intent in INTENTS else "history"

    def parse_expense(self, message: str, sender: str, today: date) -> dict:
        prompt = (
            "Extract one expense record from this message for a couple's shared finance tracker.\n"
            f"Today is {today.isoformat()}. The sender is {sender}.\n"
            "Rules:\n"
            "- amount is a plain number without currency symbols.\n"
            "- category is one of: Food, Transport, Groceries, Utilities, Shopping, "
            "Travel, Entertainment, Health, Bills, Other.\n"
            "- Leave date, time, currency or card empty if they are not stated.\n"
            "- Keep card names as the user wrote them (e.g. 'DBS card').\n\n"
            f'Message: """{message}"""'
        )
        data = self._json(prompt, EXPENSE_SCHEMA)
        try:
            amount = float(data.get("amount") or 0)
        except (TypeError, ValueError):
            amount = 0
        if amount <= 0:
            raise ValueError("No valid amount found in message")
        data["amount"] = amount
        return data

    def parse_email(self, subject: str, sender: str, body: str, today: date) -> dict:
        prompt = (
            "Decide whether this email is a money-OUT transaction for a couple's expense tracker "
            "(a purchase, bill, PayNow or bank transfer they sent, or an e-commerce receipt), "
            "and if so extract it.\n"
            f"Today is {today.isoformat()}.\n"
            "Rules:\n"
            "- direction: 'outgoing' for money the couple paid out, 'incoming' for money received, "
            "'none' if it is not a transaction (promotions, statements without a charge, OTPs).\n"
            "- amount is a plain number without currency symbols.\n"
            "- notes is the merchant or recipient name.\n"
            "- card is the payment method, e.g. 'PayNow DBS' or 'DBS card'.\n"
            "- category is one of: Food, Transport, Groceries, Utilities, Shopping, "
            "Travel, Entertainment, Health, Bills, Other.\n"
            "- Leave date, time, currency or card empty if they are not stated.\n\n"
            f'Subject: """{subject}"""\nFrom: """{sender}"""\nBody: """{body}"""'
        )
        return self._json(prompt, EMAIL_SCHEMA)

    def plan_query(self, question: str, today: date) -> dict:
        prompt = (
            "Convert this question about a couple's past transactions into search filters.\n"
            f"Today is {today.isoformat()}. Resolve relative dates such as 'last week' "
            "or 'this month' into exact YYYY-MM-DD start_date and end_date.\n"
            "Leave a field empty if the question does not restrict it.\n\n"
            f'Question: """{question}"""'
        )
        return self._json(prompt, QUERY_SCHEMA)

    def answer_question(self, question: str, rows: list[str], today: date) -> str:
        prompt = (
            f"Today is {today.isoformat()}. Answer the question using ONLY the transactions below.\n"
            "Quote exact dates, times, amounts and card names from the rows. "
            "If the rows do not answer it, say so. Be brief.\n\n"
            "Transactions:\n" + "\n".join(rows) + f'\n\nQuestion: """{question}"""'
        )
        return self._text(prompt)

    def recommend_card(self, question: str, cards_text: str) -> str:
        prompt = (
            "You are a credit card rewards advisor for a couple in Singapore.\n"
            "Using ONLY the card rules below, recommend the single best card for the purchase "
            "to maximise cashback or miles. Give the card, the rule it relies on, and the "
            "expected return. If no card rule applies, say which is the safest default and "
            "that the rule is unknown.\n\n"
            f"Card rules:\n{cards_text or '(no cards recorded)'}\n\n"
            f'Purchase: """{question}"""'
        )
        return self._text(prompt)
