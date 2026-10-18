"""Show Gemini's progress in a Telegram message by editing it in place.

Telegram cannot stream tokens, so the placeholder message is edited as thought
summaries and answer text arrive. Edits are throttled to stay under Telegram's
per-chat edit limits.
"""

import asyncio
import logging
from collections.abc import Iterator

from telegram import Message
from telegram.error import BadRequest

from gemini_service import ANSWER, THOUGHT, StreamChunk

logger = logging.getLogger(__name__)

EDIT_INTERVAL_SECONDS = 1.2
MAX_MESSAGE_CHARS = 4096
THOUGHT_TAIL_CHARS = 500
CURSOR = " ▌"


async def _iter_chunks(chunks: Iterator[StreamChunk]):
    """Consume a blocking generator in a worker thread and yield its items here."""
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()
    done = object()

    def pump():
        try:
            for item in chunks:
                loop.call_soon_threadsafe(queue.put_nowait, item)
        except Exception as exc:
            loop.call_soon_threadsafe(queue.put_nowait, exc)
        finally:
            loop.call_soon_threadsafe(queue.put_nowait, done)

    loop.run_in_executor(None, pump)
    while True:
        item = await queue.get()
        if item is done:
            return
        if isinstance(item, Exception):
            raise item
        yield item


def _live_text(thoughts: str, answer: str, status: str) -> str:
    if answer:
        return answer[-MAX_MESSAGE_CHARS + len(CURSOR):] + CURSOR
    tail = thoughts[-THOUGHT_TAIL_CHARS:].strip()
    return f"💭 {tail}\n\n{status}" if tail else status


async def _edit(message: Message, text: str) -> None:
    try:
        await message.edit_text(text[:MAX_MESSAGE_CHARS])
    except BadRequest as exc:
        # Telegram rejects edits that do not change the text; anything else is worth logging.
        if "not modified" not in str(exc):
            logger.warning("Could not edit message: %s", exc)


async def set_status(message: Message, status: str) -> None:
    await _edit(message, status)


async def stream_reply(placeholder: Message, chunks: Iterator[StreamChunk], status: str, footer: str = "") -> str:
    """Stream Gemini output into `placeholder`, then replace it with the final answer.

    Returns the final answer text (without footer).
    """
    loop = asyncio.get_running_loop()
    thoughts, answer = "", ""
    last_edit = 0.0
    last_text = status

    async for kind, text in _iter_chunks(chunks):
        if kind == THOUGHT:
            thoughts += text
        else:
            answer += text
        now = loop.time()
        live = _live_text(thoughts, answer, status)
        if now - last_edit >= EDIT_INTERVAL_SECONDS and live != last_text:
            last_edit = now
            last_text = live
            await _edit(placeholder, live)

    answer = answer.strip() or "(no answer)"
    final = answer[: MAX_MESSAGE_CHARS - len(footer)] + footer
    await _edit(placeholder, final)
    return answer
