"""The `local` commands (FLD-1 … FLD-6) without a device: status, edit, export, import, prune."""

from __future__ import annotations

import io
import json
import stat
from pathlib import Path

import pytest

from ied_client import localdata
from ied_client.cli import runner
from ied_client.cli.context import CliContext
from ied_client.cli.main import main
from ied_client.cli.options import Options
from ied_client.cli.render import Output
from ied_client.core.safety import Scripted
from ied_client.explain import default_catalogue, reset_default_catalogue

VALID = """\
version: 1
entries:
- id: rack.thing
  terms: [rack thing]
  title: Rack thing
  summary: Something about this rack.
"""


@pytest.fixture
def home(tmp_path, monkeypatch):
    user = tmp_path / "home"
    monkeypatch.setenv("IED_CLIENT_LOCAL_DIRS", f"{tmp_path / 'etc'}:{user}")
    reset_default_catalogue()
    yield user
    reset_default_catalogue()


def run_json(capsys, *argv):
    code = main(["--json", "--no-log", *argv])
    return code, json.loads(capsys.readouterr().out)


def run_ui(argv: list[str], answers: list) -> tuple[object, str]:
    """Run a command in-process with scripted answers (an interactive operator)."""
    buf = io.StringIO()
    ctx = CliContext(Options(no_log=True), out=Output.for_stream(buf, width=200), stdout=buf, ui=Scripted(list(answers)))
    cmd, rest = runner.lookup(argv, in_shell=False)
    result = runner.call(ctx, cmd, runner.parse(cmd, rest, oneshot=True))
    runner.emit(ctx, cmd.name, result)
    return result, buf.getvalue()


def editor(tmp_path: Path, monkeypatch, *contents: str) -> Path:
    """An EDITOR that writes contents[0], then contents[1], … on successive runs ("" = leave the file)."""
    for i, c in enumerate(contents):
        (tmp_path / f"edit{i}.yaml").write_text(c)
    script = tmp_path / "fake-editor"
    script.write_text(
        "#!/bin/sh\n"
        f'n=$(cat "{tmp_path}/count" 2>/dev/null || echo 0)\n'
        f'echo $((n + 1)) > "{tmp_path}/count"\n'
        f'src="{tmp_path}/edit$n.yaml"\n'
        'if [ -s "$src" ]; then cp "$src" "$1"; fi\n'
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("EDITOR", str(script))
    monkeypatch.delenv("VISUAL", raising=False)
    return script


def test_status_empty_and_invalid(home, capsys):
    code, env = run_json(capsys, "local", "status")
    assert code == 0 and env["result"]["files"] == []
    (home / "hints").mkdir(parents=True)
    (home / "hints" / "bad.yaml").write_text("version: 2\n")
    code, env = run_json(capsys, "local", "status")
    assert code == 1 and env["error"]["key"] == "tool:local-data-invalid"
    assert env["result"]["files"][0]["valid"] is False


def test_edit_validates_before_saving(home, tmp_path, monkeypatch):
    editor(tmp_path, monkeypatch, "version: 1\nentries:\n- id: Not Valid\n", VALID)
    result, out = run_ui(["local", "edit", "hints"], [True])  # invalid first: "edit again?" yes
    assert result.ok, out
    saved = home / "hints" / "local.yaml"
    assert saved.read_text() == VALID
    assert "lower-case identifier" in out  # the first attempt's problem was shown
    assert "rack.thing" in default_catalogue()  # and the saved entry is live at once


def test_edit_discard_leaves_file_alone(home, tmp_path, monkeypatch):
    (home / "hints").mkdir(parents=True)
    (home / "hints" / "local.yaml").write_text(VALID)
    editor(tmp_path, monkeypatch, "not: [valid\n")
    result, _ = run_ui(["local", "edit", "hints"], [False])  # "edit again?" no
    assert not result.ok and result.code == 4
    assert (home / "hints" / "local.yaml").read_text() == VALID


def test_edit_from_builtin_entry(home, tmp_path, monkeypatch):
    editor(tmp_path, monkeypatch, "")  # save as it is
    result, _ = run_ui(["local", "edit", "hints", "--from", "tool.refused-standard-mode"], [])
    assert result.ok
    text = (home / "hints" / "local.yaml").read_text()
    assert "# copied from ied_client/data/hints/tool.yaml" in text and "- id: tool.refused-standard-mode" in text
    exp = default_catalogue().explain("tool.refused-standard-mode")
    assert exp.source == str(home / "hints" / "local.yaml")


def test_edit_new_quirks_file_has_the_format(home, tmp_path, monkeypatch):
    new = localdata.quirks_template(Path("x"), "mms") + "  - match: {vendor: V, model: M}\n    notes: n\n    source: s\n"
    editor(tmp_path, monkeypatch, new)
    result, _ = run_ui(["local", "edit", "quirks"], [])
    assert result.ok
    text = (home / "quirks" / "mms" / "local.yaml").read_text()
    assert "Local quirks for protocol module 'mms'" in text


def test_edit_needs_a_terminal(home, capsys):
    code, env = run_json(capsys, "local", "edit", "hints")
    assert code == 2 and "terminal" in env["error"]["message"]


def test_export_import_prune(home, tmp_path, capsys, monkeypatch):
    (home / "hints").mkdir(parents=True)
    (home / "hints" / "local.yaml").write_text(VALID)
    out = tmp_path / "bundle.json"
    code, env = run_json(capsys, "local", "export", "--out", str(out))
    assert code == 0 and env["result"]["entries"] == 1 and out.exists()
    code, env = run_json(capsys, "local", "export", "--out", str(out))
    assert code != 0  # exists

    other = tmp_path / "other"
    monkeypatch.setenv("IED_CLIENT_LOCAL_DIRS", str(other))
    code, env = run_json(capsys, "local", "import", str(out))
    assert code == 0 and len(env["result"]["written"]) == 1

    # the package's ledger now says the entry was accepted: prune removes it, after confirmation
    fp = localdata.fingerprint(localdata.raw_entries(VALID, "hints")[0])
    monkeypatch.setattr(localdata, "ledger", lambda: {fp: localdata.LedgerRow(fp, "hint", "rack.thing", "accepted")})
    code, env = run_json(capsys, "local", "prune")
    assert code == 4  # not confirmed (non-interactive, no --yes)
    code, env = run_json(capsys, "--yes", "local", "prune")
    assert code == 0 and env["result"]["removed"] == 1
    assert all(not fs.entries for fs in localdata.status())


def test_explain_warns_about_a_local_file_left_out(home, capsys):
    (home / "hints").mkdir(parents=True)
    (home / "hints" / "bad.yaml").write_text("version: 1\nentries:\n- id: Bad Id\n")
    code, env = run_json(capsys, "explain", "XCBR")
    assert code == 0 and env["result"]["explanation"]["id"]
    assert any("bad.yaml" in w and "local status" in w for w in env["warnings"])
