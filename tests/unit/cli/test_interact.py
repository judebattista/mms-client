"""Interactive confirmations (SAF-2/3) and questions with a default (IDN-4)."""

from __future__ import annotations

import io

from mms_client.cli.interact import PromptInteraction, TextNonInteractive
from mms_client.cli.render import Output


def ui(answers):
    buf = io.StringIO()
    it = iter(answers)
    return PromptInteraction(Output.for_stream(buf), prompt_fn=lambda q: next(it)), buf


def test_confirm_y_n_and_default():
    u, buf = ui(["y", "", "no", "YES"])
    assert u.confirm("Write?") is True
    assert u.confirm("Write?") is False  # default no
    assert u.confirm("Write?") is False
    assert u.confirm("Write?", default=False) is True
    assert "Write?" in buf.getvalue()


def test_typed_confirmation_must_match_exactly():
    u, buf = ui(["CSWI1.Pos", "cswi1.pos"])
    assert u.confirm_typed("Type the object name (CSWI1.Pos) to confirm:", "CSWI1.Pos") is True
    assert u.confirm_typed("again", "CSWI1.Pos") is False
    assert "nothing was sent" in buf.getvalue()


def test_choose_by_number_key_label_and_default():
    opts = [("Ed1", "Edition 1 (2003)"), ("Ed2", "Edition 2"), ("unknown", "Don't know")]
    u, buf = ui(["2", "ed1", "", "7", "x", "Don't know"])
    assert u.choose("Which edition?", opts, default="unknown") == "Ed2"
    assert u.choose("Which edition?", opts, default="unknown") == "Ed1"
    assert u.choose("Which edition?", opts, default="unknown") == "unknown"  # Enter = don't know
    assert u.choose("Which edition?", opts, default="unknown") == "unknown"  # 7, x, then the label
    assert "1) Edition 1 (2003)" in buf.getvalue()


def test_eof_declines():
    def eof(q):
        raise EOFError

    u = PromptInteraction(Output.for_stream(io.StringIO()), prompt_fn=eof)
    assert u.confirm("x") is False and u.confirm_typed("x", "y") is False and u.choose("x", [("a", "A")]) is None


def test_non_interactive_never_blocks_and_shows_notes():
    buf = io.StringIO()
    n = TextNonInteractive(Output.for_stream(buf))
    assert n.interactive is False
    assert n.confirm("x") is False and n.confirm_typed("x", "x") is False
    assert n.choose("x", [("a", "A")], default="a") == "a"
    n.notify("summary\n  WARNING: careful")
    assert "careful" in buf.getvalue() and n.messages
