import json
import logging
from datetime import date
from typing import Callable, Iterable, Iterator, Sequence, TypeVar

from google import genai
from google.genai import errors, types

logger = logging.getLogger(__name__)

T = TypeVar("T")
FALLBACK_CODES = (404, 429)

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
    },
    "required": ["date", "time", "category", "amount", "currency", "card", "merchant"],
}

PAYNOW_MERCHANT_SCHEMA = {
    "type": "OBJECT",
    "properties": {"merchant": {"type": "STRING", "description": "Recipient name, or empty"}},
    "required": ["merchant"],
}

CATEGORY_RULE = ("- category is exactly one of: Groceries, Dining, Transport, Shopping, Bills, "
                 "Others.\n")

class GeminiService:
    def __init__(self, api_key: str, models: Sequence[str]):
        if not models:
            raise ValueError("At least one Gemini model is required")
        self._client = genai.Client(api_key=api_key)
        self._models = tuple(models)

    def _with_fallback(self, call: Callable[[str], T]) -> T:
        """Run call(model) for each model in priority order. Move to the next model when the
        current one has hit its quota (429) or does not exist for this key (404); any other
        error is raised immediately."""
        for index, model in enumerate(self._models):
            try:
                return call(model)
            except errors.ClientError as exc:
                is_last = index == len(self._models) - 1
                if exc.code not in FALLBACK_CODES or is_last:
                    raise
                logger.warning("Gemini %s for %s; falling back to %s",
                               exc.code, model, self._models[index + 1])
        raise AssertionError("unreachable")

    def _json(self, prompt: str, schema: dict) -> dict:
        logger.debug("Gemini JSON prompt:\n%s", prompt)
        response = self._with_fallback(lambda model: self._client.models.generate_content(
            model=model,
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=schema,
                temperature=0,
            ),
        ))
        logger.debug("Gemini JSON response: %s", response.text)
        return json.loads(response.text)

    def _stream_with_fallback(self, open_stream: Callable[[str], Iterable[T]]) -> Iterator[T]:
        """Stream from the first model that accepts the request. The request is sent on first
        iteration (not when the stream is opened), so fallback is only possible before any
        chunk has been yielded."""
        for index, model in enumerate(self._models):
            started = False
            try:
                for item in open_stream(model):
                    started = True
                    yield item
                return
            except errors.ClientError as exc:
                is_last = index == len(self._models) - 1
                if started or exc.code not in FALLBACK_CODES or is_last:
                    raise
                logger.warning("Gemini %s for %s; falling back to %s",
                               exc.code, model, self._models[index + 1])

    def stream_turn(self, contents: list[types.Content], system: str,
                    tools: list[types.Tool], collected: list[types.Part]) -> Iterator[StreamChunk]:
        """Stream one model turn. Yields thoughts and answer text; appends the non-thought
        parts (including function calls and their signatures) to `collected`."""
        stream = self._stream_with_fallback(lambda model: self._client.models.generate_content_stream(
            model=model,
            contents=contents,
            config=types.GenerateContentConfig(
                system_instruction=system,
                tools=tools,
                temperature=0.2,
                thinking_config=types.ThinkingConfig(include_thoughts=True),
            ),
        ))
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

    def parse_paynow_merchant(self, subject: str, body: str) -> str:
        prompt = (
            "This is a bank email about a PayNow transfer the couple sent. "
            "Return the recipient's name, exactly as written in the email. "
            "Return an empty string if no recipient is named.\n\n"
            f'Subject: """{subject}"""\nBody: """{body}"""'
        )
        return (self._json(prompt, PAYNOW_MERCHANT_SCHEMA).get("merchant") or "").strip()



