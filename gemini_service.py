import json
import logging
from datetime import date
from typing import Iterator

from google import genai
from google.genai import types

logger = logging.getLogger(__name__)

THOUGHT = "thought"
ANSWER = "answer"
StreamChunk = tuple[str, str]

CATEGORIES = ("Groceries", "Dining", "Transport", "Shopping", "Bills", "Others")

EXPENSE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "date": {"type": "STRING", "description": "YYYY-MM-DD, or empty if not stated"},
        "time": {"type": "STRING", "description": "HH:MM 24-hour, or empty if not stated"},
        "category": {"type": "STRING", "enum": list(CATEGORIES)},
        "amount": {"type": "NUMBER"},
        "currency": {"type": "STRING", "description": "ISO code, or empty if not stated"},
        "card": {"type": "STRING", "description": "Bank plus card name, or empty"},
        "merchant": {"type": "STRING", "description": "Merchant name, or empty"},
        "notes": {"type": "STRING", "description": "Short description, or empty"},
    },
    "required": ["date", "time", "category", "amount", "currency", "card", "merchant", "notes"],
}

EMAIL_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "direction": {"type": "STRING", "enum": ["outgoing", "incoming", "none"]},
        "date": {"type": "STRING", "description": "YYYY-MM-DD, or empty if not stated"},
        "time": {"type": "STRING", "description": "HH:MM 24-hour, or empty if not stated"},
        "category": {"type": "STRING", "enum": list(CATEGORIES)},
        "amount": {"type": "NUMBER"},
        "currency": {"type": "STRING", "description": "ISO code, or empty if not stated"},
        "card": {"type": "STRING", "description": "Payment method, or empty"},
        "merchant": {"type": "STRING", "description": "Merchant or recipient name"},
        "notes": {"type": "STRING", "description": "Short description, or empty"},
    },
    "required": ["direction", "date", "time", "category", "amount", "currency", "card",
                 "merchant", "notes"],
}

CATEGORY_RULE = ("- category is exactly one of: Groceries, Dining, Transport, Shopping, Bills, "
                 "Others.\n")

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

    def stream_turn(self, contents: list[types.Content], system: str,
                    tools: list[types.Tool], collected: list[types.Part]) -> Iterator[StreamChunk]:
        """Stream one model turn. Yields thoughts and answer text; appends the non-thought
        parts (including function calls and their signatures) to `collected`."""
        stream = self._client.models.generate_content_stream(
            model=self._model,
            contents=contents,
            config=types.GenerateContentConfig(
                system_instruction=system,
                tools=tools,
                temperature=0.2,
                thinking_config=types.ThinkingConfig(include_thoughts=True),
            ),
        )
        for chunk in stream:
            for candidate in chunk.candidates or []:
                if not (candidate.content and candidate.content.parts):
                    continue
                for part in candidate.content.parts:
                    if part.thought:
                        if part.text:
                            yield THOUGHT, part.text
                        continue
                    collected.append(part)
                    if part.text:
                        yield ANSWER, part.text

    def parse_expense(self, message: str, sender: str, today: date) -> dict:
        prompt = (
            "Extract one expense record from this message for a couple's shared finance tracker.\n"
            f"Today is {today.isoformat()}. The sender is {sender}.\n"
            "Rules:\n"
            "- amount is a plain number without currency symbols.\n"
            + CATEGORY_RULE +
            "- merchant is the shop, restaurant or payee name.\n"
            "- card is bank plus card name (e.g. 'DBS Altitude'). If only the bank is named, "
            "use the bank (e.g. 'DBS' from 'DBS: SGD 14.50 charged...'). Leave empty if unknown.\n"
            "- Leave date, time or currency empty if they are not stated.\n\n"
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
            "- merchant is the merchant or recipient name.\n"
            "- card is the payment method, e.g. 'PayNow DBS' or 'DBS card'.\n"
            + CATEGORY_RULE +
            "- Leave date, time, currency or card empty if they are not stated.\n\n"
            f'Subject: """{subject}"""\nFrom: """{sender}"""\nBody: """{body}"""'
        )
        return self._json(prompt, EMAIL_SCHEMA)



