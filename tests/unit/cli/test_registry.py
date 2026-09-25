"""The command registry is shared by the shell and one-shot mode (CLI-6)."""

from __future__ import annotations

import pytest

from ied_client.cli import registry
from ied_client.cli.registry import Needs, UsageError

# SPEC §6.2, plus the shell's own commands and ORG-4's `origin`.
SPEC_COMMANDS = [
    "connect", "disconnect", "info",
    "diagnose", "discover",
    "ls", "cd", "tree", "describe", "dataset",
    "files ls", "files get",
    "read", "write", "watch", "setgroup",
    "operate", "select", "cancel", "authority-probe",
    "rcb", "subscribe", "gi",
    "check", "snapshot", "diff",
    "explain",
    "set", "log", "restore", "export",
    "inventory from-scd", "inventory validate",
]
SHELL_COMMANDS = ["help", "exit", "quit", "pwd", "origin"]
SHELL_ONLY = {"exit", "pwd"}


@pytest.mark.parametrize("name", SPEC_COMMANDS + SHELL_COMMANDS)
def test_every_command_is_registered(name):
    cmd = registry.get(name)
    assert cmd is not None, name
    assert cmd.summary and cmd.area in registry.AREAS


def test_only_exit_quit_pwd_are_shell_only():
    assert {c.name for c in registry.all_commands() if c.shell_only} == SHELL_ONLY
    assert registry.get("quit") is registry.get("exit")


@pytest.mark.parametrize("cmd", registry.all_commands(), ids=lambda c: c.name)
def test_every_shell_command_is_available_one_shot(cmd):
    """CLI-6: the same parser works in both modes; one-shot adds <device> where one is needed."""
    shell_parser = cmd.parser(oneshot=False)
    assert shell_parser.prog == cmd.name
    if cmd.shell_only:
        return
    oneshot = cmd.parser(oneshot=True)
    dests = [a.dest for a in oneshot._actions]
    if cmd.needs is Needs.NOTHING:
        assert "device" not in dests  # no automatic <device>
    else:
        assert dests[1] == "device", dests  # right after -h


def test_two_word_commands_resolve():
    cmd, n = registry.resolve(["files", "ls", "/COMTRADE"])
    assert cmd.name == "files ls" and n == 2
    cmd, n = registry.resolve(["inventory", "from-scd", "x.scd"])
    assert cmd.name == "inventory from-scd" and n == 2
    cmd, n = registry.resolve(["read", "X"])
    assert cmd.name == "read" and n == 1
    assert registry.resolve(["files"]) == (None, 0)
    assert registry.group_words()["files"] == ["get", "ls"]


def test_needs():
    assert registry.get("read").needs is Needs.SESSION
    assert registry.get("diagnose").needs is Needs.TARGET  # probes before associating
    assert registry.get("explain").needs is Needs.NOTHING
    assert registry.get("files ls").needs_model is False


def test_parsers_never_exit():
    p = registry.get("read").parser(oneshot=True)
    with pytest.raises(UsageError) as ei:
        p.parse_args([])
    assert "required" in str(ei.value) and ei.value.usage.startswith("usage: mms-client read")


def test_register_rejects_duplicates():
    registry.load_commands()
    with pytest.raises(ValueError):
        registry.register(registry.Command("read", "x", lambda c, a: None, area="Data"))
