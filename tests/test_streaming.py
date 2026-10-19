import asyncio
from types import SimpleNamespace

from gemini_service import ANSWER, THOUGHT, GeminiService
from streaming import stream_reply


def _part(text, thought=False):
    return SimpleNamespace(text=text, thought=thought)


def _chunk(*parts):
    return SimpleNamespace(candidates=[SimpleNamespace(content=SimpleNamespace(parts=list(parts)))])


class FakeModels:
    def __init__(self, chunks):
        self._chunks = chunks

    def generate_content_stream(self, **kwargs):
        assert kwargs["config"].thinking_config.include_thoughts is True
        return iter(self._chunks)


def test_stream_separates_thoughts_from_answer():
    service = GeminiService.__new__(GeminiService)
    service._models = ("m",)
    service._client = SimpleNamespace(models=FakeModels([
        _chunk(_part("Checking rows", thought=True)),
        _chunk(_part("You spent $15."), _part("")),
    ]))

    collected = []
    chunks = list(service.stream_turn([], "system", [], collected))

    assert chunks == [(THOUGHT, "Checking rows"), (ANSWER, "You spent $15.")]


class FakeMessage:
    def __init__(self):
        self.edits = []

    async def edit_text(self, text):
        self.edits.append(text)


def test_stream_reply_ends_with_answer_and_footer():
    message = FakeMessage()
    chunks = iter([(THOUGHT, "thinking"), (ANSWER, "You spent "), (ANSWER, "$15.")])

    answer = asyncio.run(stream_reply(message, chunks, status="working", footer="\n\n(1 row)"))

    assert answer == "You spent $15."
    assert message.edits[-1] == "You spent $15.\n\n(1 row)"


def test_stream_falls_back_when_quota_error_raised_on_first_iteration():
    from google.genai import errors

    def stream_for(model, **kwargs):
        def chunks():
            if model == "first":
                raise errors.ClientError(429, {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED"}})
            yield _chunk(_part("You spent $15."))
        return chunks()

    service = GeminiService.__new__(GeminiService)
    service._models = ("first", "second")
    service._client = SimpleNamespace(models=SimpleNamespace(generate_content_stream=stream_for))

    collected = []
    chunks = list(service.stream_turn([], "system", [], collected))
    assert chunks == [(ANSWER, "You spent $15.")]
