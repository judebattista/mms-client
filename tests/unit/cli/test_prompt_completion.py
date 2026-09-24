"""Prompt (CLI-4, MOD-1) and tab completion over a model built offline (CLI-3)."""

from __future__ import annotations

from prompt_toolkit.document import Document

from mms_client.cli.completion import ShellCompleter, complete_reference
from mms_client.cli.shell import prompt_fragments, prompt_text
from mms_client.core.safety import Mode


def test_prompt_shows_device_path_orcat_and_mode():
    assert prompt_text("relay-F12", ("CTRL", "CSWI1"), Mode.STANDARD, 3) == "[std] relay-F12:/CTRL/CSWI1 [orCat=remote]> "
    assert prompt_text("relay-F12", (), Mode.STANDARD, None) == "[std] relay-F12:/> "
    assert prompt_text("relay-F12", ("CTRL",), Mode.EXPERT, 7) == "[EXPERT] relay-F12:/CTRL [orCat=maintenance]> "
    assert prompt_text(None, (), Mode.STANDARD, None) == "[std] (no device)> "
    assert "(not connected)" in prompt_text("relay-F12", (), Mode.STANDARD, None, connected=False)


def test_expert_mode_is_highlighted():
    frags = prompt_fragments("r", (), Mode.EXPERT, None)
    style, text = frags[0]
    assert text == "[EXPERT]" and "bg:ansired" in style


def names(cands):
    return [c[0] for c in cands]


def test_complete_from_root_and_relative(model):
    assert names(complete_reference(model, (), "")) == ["CTRL"]
    assert names(complete_reference(model, (), "CTRL/")) == ["CTRL/LLN0", "CTRL/CSWI1", "CTRL/GGIO1"]
    assert names(complete_reference(model, (), "CTRL/CS")) == ["CTRL/CSWI1"]
    assert names(complete_reference(model, (), "CTRL/CSWI1.")) == ["CTRL/CSWI1.Pos", "CTRL/CSWI1.Loc"]
    assert names(complete_reference(model, (), "CTRL/CSWI1.Pos.s")) == ["CTRL/CSWI1.Pos.stVal", "CTRL/CSWI1.Pos.sboTimeout"]
    # relative to the current location
    assert names(complete_reference(model, ("CTRL", "CSWI1"), "Po")) == ["Pos"]
    assert names(complete_reference(model, ("CTRL", "CSWI1"), "Pos.O")) == ["Pos.Oper"]
    assert names(complete_reference(model, ("CTRL", "CSWI1"), "../GG")) == ["../GGIO1"]
    # absolute shell paths
    assert names(complete_reference(model, ("CTRL", "GGIO1"), "/CTRL/CSWI1/P")) == ["/CTRL/CSWI1/Pos"]
    assert names(complete_reference(model, ("CTRL",), "/")) == ["/CTRL"]


def test_complete_kinds_and_fc(model):
    kinds = dict((c[1], c[2]) for c in complete_reference(model, ("CTRL", "CSWI1"), "Pos."))
    assert kinds["stVal"] == "attr" and kinds["Oper"] == "data"
    assert names(complete_reference(model, ("CTRL", "CSWI1"), "Pos[")) == ["Pos[ST]", "Pos[CO]", "Pos[CF]"]
    assert names(complete_reference(model, ("CTRL", "CSWI1"), "Pos[C")) == ["Pos[CO]", "Pos[CF]"]
    assert complete_reference(model, (), "NOPE/X.") == []


def test_completer_commands_subcommands_options_and_refs(model):
    c = ShellCompleter(lambda: model, lambda: ("CTRL", "CSWI1"), lambda: ["XCBR", "ctlModel", "object-access-denied"])

    def comp(text):
        return [x.text for x in c.get_completions(Document(text), None)]

    assert "read" in comp("re") and "restore" in comp("re")
    assert comp("files ") == ["get", "ls"]
    assert comp("set m") == ["mode"]
    assert comp("set mode ") == ["standard", "expert"]
    assert comp("setgroup a") == ["activate"]
    assert comp("read Pos.st") == ["Pos.stVal"]
    assert "--fc" in comp("read Pos.stVal --f")
    assert comp("explain XC") == ["XCBR"]
    assert comp("explain ") == []
    assert comp("files ls ") == []  # not an object reference
