from datetime import datetime
from types import SimpleNamespace

from google.genai import types

from assistant import Assistant
from gemini_service import ANSWER
from sheets_service import Transaction

NOW = datetime(2026, 10, 18, 18, 0)


class ScriptedGemini:
    """Each turn yields the next scripted function call, or a final text reply."""

    def __init__(self, script):
        self._script = list(script)

    def stream_turn(self, contents, system, tools, collected):
        step = self._script.pop(0)
        if isinstance(step, str):
            yield ANSWER, step
            return
        name, args = step
        collected.append(types.Part(function_call=types.FunctionCall(name=name, args=args)))
        return
        yield  # pragma: no cover


class FakeSheets:
    def __init__(self):
        self.appended: list[Transaction] = []

    def append_transaction(self, txn):
        self.appended.append(txn)


SETTINGS = SimpleNamespace(default_currency="SGD")


def _run(gemini, sheets, write):
    assistant = Assistant(gemini, sheets, SETTINGS, write=write)
    answer = "".join(chunk for kind, chunk in assistant.respond(1, "Ann", "lunch 12", NOW) if kind == ANSWER)
    return assistant, answer


def test_log_expense_waits_for_confirmation_before_writing():
    sheets = FakeSheets()
    gemini = ScriptedGemini([
        ("log_expense", {"category": "Food", "amount": 12, "notes": "lunch"}),
        "Prepared lunch $12. Confirm?",
    ])

    _, answer = _run(gemini, sheets, write=True)

    assert answer == "Prepared lunch $12. Confirm?"
    assert sheets.appended == []


def test_confirm_expense_true_writes_the_prepared_row():
    sheets = FakeSheets()
    gemini = ScriptedGemini([
        ("log_expense", {"category": "Food", "amount": 12, "notes": "lunch"}),
        ("confirm_expense", {"confirm": True}),
        "Saved.",
    ])

    _run(gemini, sheets, write=True)

    assert len(sheets.appended) == 1
    assert sheets.appended[0].amount == 12


def test_confirm_expense_false_discards_without_writing():
    sheets = FakeSheets()
    gemini = ScriptedGemini([
        ("log_expense", {"category": "Food", "amount": 12, "notes": "lunch"}),
        ("confirm_expense", {"confirm": False}),
        "Discarded.",
    ])

    assistant, _ = _run(gemini, sheets, write=True)

    assert sheets.appended == []
    assert 1 not in assistant._pending
