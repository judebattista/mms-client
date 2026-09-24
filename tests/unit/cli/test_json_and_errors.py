"""JSON envelope shape (ARC-3), exit codes and error mapping (CLI-7, EXP-1) without a device."""

from __future__ import annotations

import io
import json

import pytest

from mms_client import codes
from mms_client.adapter import ConnectError, EncodeError, NotConnectedError, ServiceError
from mms_client.cli import errors, registry
from mms_client.cli.context import CliContext
from mms_client.cli.main import main
from mms_client.cli.options import Options
from mms_client.cli.registry import UsageError
from mms_client.cli.render import Output
from mms_client.cli.result import CommandResult
from mms_client.cli.shell import Shell
from mms_client.core.model import AmbiguousFcError, NotInModelError
from mms_client.core.refs import ObjectRef, RefError
from mms_client.core.safety import ConfirmationDeclined, NonInteractive, PolicyError, Scripted
from mms_client.inventory import InventoryError


def run_json(capsys, argv):
    code = main(argv)
    out = capsys.readouterr().out
    return code, json.loads(out)


def assert_envelope(env, command, ok):
    assert env["schema_version"] == "1.0"
    assert env["tool"]["name"] == "mms-client" and "version" in env["tool"] and "libiec61850" in env["tool"]
    assert env["command"] == command
    assert env["ok"] is ok
    assert "generated_at" in env and "result" in env and "exit_code" in env


def test_explain_json(capsys):
    code, env = run_json(capsys, ["explain", "object-access-denied", "--json"])
    assert code == 0
    assert_envelope(env, "explain", True)
    assert env["result"]["explanation"]["id"]
    assert env["device"] is None


def test_help_json_lists_every_command(capsys):
    code, env = run_json(capsys, ["--json", "help"])
    assert code == 0
    names = {c["name"] for c in env["result"]["commands"]}
    assert {"read", "files ls", "inventory validate", "origin"} <= names
    assert "pwd" not in names  # shell only


def test_usage_error_envelope_and_exit_2(capsys):
    code, env = run_json(capsys, ["--json", "read", "relay"])
    assert code == 2
    assert_envelope(env, "read", False)
    assert env["error"]["key"] == "tool:usage-error"
    assert "required" in env["error"]["message"]
    code, env = run_json(capsys, ["--json", "raed", "relay", "x"])
    assert code == 2 and env["ok"] is False


def test_set_one_shot_points_to_the_option(capsys):
    code, env = run_json(capsys, ["set", "mode", "expert", "--json"])
    assert code == 0 and env["result"] == {"key": "mode", "applied": False, "equivalent_option": "--expert"}


def test_no_arguments_prints_help_and_exits_2(capsys):
    assert main([]) == 2
    assert "usage: mms-client" in capsys.readouterr().out
    assert main(["--help"]) == 0
    out = capsys.readouterr().out
    assert "GOOSE" in out and "exit codes" in out and "--orcat" in out


def test_version(capsys):
    assert main(["--version"]) == 0
    assert capsys.readouterr().out.startswith("mms-client ")


def test_bad_inventory_is_a_usage_error(tmp_path, capsys):
    bad = tmp_path / "inv.yaml"
    bad.write_text("devices: [{name: x}]\n")
    code, env = run_json(capsys, ["--json", "-i", str(bad), "--no-log", "read", "x", "A/B.c"])
    assert code == 2 and env["error"]["key"] == "tool:inventory-invalid"


def test_help_for_one_command(capsys):
    assert main(["read", "-h"]) == 0
    out = capsys.readouterr().out
    assert "usage: mms-client read" in out and "device" in out


# ---------------------------------------------------------------------------------- error mapping
@pytest.mark.parametrize(
    "exc,exit_code,key",
    [
        (UsageError("x"), 2, "tool:usage-error"),
        (AmbiguousFcError(ObjectRef("LD", "LN", ("DO",)), ["ST", "CF"]), 2, "tool:invalid-reference"),
        (NotInModelError("nope"), 1, "tool:object-not-in-model"),
        (RefError("bad"), 2, "tool:invalid-reference"),
        (EncodeError("bad value"), 2, "tool:usage-error"),
        (ConnectError("associate", "h:102", codes.ied_error(5)), 3, "ied:connection-rejected"),
        (NotConnectedError("gone"), 3, "tool:not-connected"),
        (ServiceError("read", "X", codes.data_access_error(3)), 1, "data-access:object-access-denied"),
        (PolicyError(codes.tool("refused-standard-mode"), "expert"), 4, "tool:refused-standard-mode"),
        (ConfirmationDeclined("no"), 4, "tool:non-interactive-no-prompt"),
        (InventoryError("bad"), 2, "tool:inventory-invalid"),
        (FileExistsError("f"), 2, "tool:local-file-error"),
        (ZeroDivisionError("boom"), 1, "tool:internal-error"),
    ],
)
def test_exception_mapping(exc, exit_code, key):
    ctx = CliContext(Options(), out=Output.for_stream(io.StringIO()), ui=NonInteractive())
    r = errors.from_exception(ctx, registry.get("read"), exc)
    assert r.ok is False and r.code == exit_code and r.error.key == key


def test_operator_decline_is_not_a_fault():
    ctx = CliContext(Options(), out=Output.for_stream(io.StringIO()), ui=Scripted([]))
    r = errors.from_exception(ctx, None, ConfirmationDeclined("write cancelled by the operator"))
    assert r.code == 4 and r.error.key == "tool:not-confirmed" and r.show_hint is False


def test_hint_uses_context():
    h = errors.hint_for(codes.data_access_error(3), {"service": "write", "fc": "ST"})
    assert h is not None and "read-only" in h.render()


# ---------------------------------------------------------------------------------- shell without a device
def make_shell(lines, ui=None):
    buf = io.StringIO()
    ctx = CliContext(Options(no_log=True), out=Output.for_stream(buf, width=200), stdout=buf, ui=ui or Scripted([]), in_shell=True)
    return Shell(ctx, lines=lines), buf


def test_shell_without_device_settings_and_errors():
    sh, buf = make_shell(["help", "set mode expert", "set orcat 7", "bogus", "read X", "explain ctlModel", "set terse on", "exit", "help"])
    assert sh.run() == 0
    out = buf.getvalue()
    assert "Commands (shell)" in out and "GOOSE" in out
    assert "[EXPERT] (no device)" in out  # the prompt shows the mode (MOD-1)
    assert "unknown command 'bogus'" in out
    assert "tool:not-connected" in out  # read without a device
    assert "ctlModel" in out
    assert sh.results[-1].exit_shell  # the last `help` never ran
    assert sh.ctx.options.terse and sh.ctx.options.orcat == "7"


def test_ctrl_c_cancels_the_command_not_the_shell():
    def boom(ctx, args):
        raise KeyboardInterrupt

    registry.load_commands()
    registry.register(registry.Command("boom", "test", boom, area="Help", needs=registry.Needs.NOTHING))
    try:
        sh, buf = make_shell(["boom", "set", "exit"])
        sh.run()
    finally:
        registry.unregister("boom")
    out = buf.getvalue()
    assert "Cancelled." in out
    assert sh.results[0].interrupted and sh.results[1].ok  # the shell went on


def test_json_line_in_shell():
    sh, buf = make_shell(["--json set", "exit"])
    sh.run()
    text = buf.getvalue()
    start = text.index("{")
    env = json.JSONDecoder().raw_decode(text[start:])[0]
    assert env["command"] == "set" and env["ok"] is True


def test_command_result_code():
    assert CommandResult().code == 0
    assert CommandResult(ok=False).code == 1
    assert CommandResult(ok=False, exit_code=4).code == 4
