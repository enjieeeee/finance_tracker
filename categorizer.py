import logging
import re

import httpx

logger = logging.getLogger(__name__)

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
CATEGORIES = ("Groceries", "Dining", "Transport", "Shopping", "Bills", "Others")
FALLBACK_CATEGORY = "Others"
LLM_TIMEOUT_SECONDS = 10.0

# Ordered: the first match wins, so specific food-delivery patterns sit above generic ones.
KEYWORD_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(
            r"grabfood|foodpanda|deliveroo|mcdonald|kfc|starbucks|koufu|restaurant|kopitiam|cafe|"
            r"7.?eleven|ajumma|breadtalk|crave|food republic|four leaves|fr nex|heytea|chagee|"
            r"hua dee|jollibee|nice pancakes|old chang kee|sunny island|yamazaki",
            re.I,
        ),
        "Dining",
    ),
    (
        re.compile(
            r"ntuc|fairprice|cold storage|sheng siong|giant|prime supermarket|surreyhills",
            re.I,
        ),
        "Groceries",
    ),
    (
        re.compile(
            r"grab|gojek|comfortdelgro|ezlink|ez-link|transitlink|smrt|sbs transit|getgo",
            re.I,
        ),
        "Transport",
    ),
    (
        re.compile(
            r"shopee|lazada|uniqlo|decathlon|amazon|courts|harvey norman|lovet",
            re.I,
        ),
        "Shopping",
    ),
    (
        re.compile(r"singtel|starhub|m1 |sp group|sp services|pub |sembcorp|town council", re.I),
        "Bills",
    ),
)

_llm_cache: dict[str, str] = {}


def categorize_merchant(merchant: str, api_key: str, model: str) -> str:
    """Keyword rules first, then the LLM; always returns one of CATEGORIES."""
    name = merchant.strip()
    if not name:
        return FALLBACK_CATEGORY
    for pattern, category in KEYWORD_RULES:
        if pattern.search(name):
            return category
    if name not in _llm_cache:
        _llm_cache[name] = _ask_llm(name, api_key, model) or FALLBACK_CATEGORY
    return _llm_cache[name]


def _ask_llm(merchant: str, api_key: str, model: str) -> str | None:
    if not api_key:
        return None
    prompt = (
        "Classify this merchant name from a Singapore bank transaction into exactly one of: "
        f"{', '.join(CATEGORIES)}. Reply with the category name only.\n"
        f"Merchant: {merchant}"
    )
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0,
        "max_tokens": 5,
    }
    headers = {"Authorization": f"Bearer {api_key}"}
    try:
        response = httpx.post(GROQ_URL, json=payload, headers=headers, timeout=LLM_TIMEOUT_SECONDS)
        response.raise_for_status()
        reply = response.json()["choices"][0]["message"]["content"]
    except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError) as exc:
        logger.warning("Categorisation LLM failed for %r: %s", merchant, exc)
        return None
    return _match_category(reply)


def _match_category(reply: str) -> str | None:
    cleaned = reply.strip().strip(".\"'").lower()
    for category in CATEGORIES:
        if cleaned == category.lower():
            return category
    return None
