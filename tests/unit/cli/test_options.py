"""Global options before or after the command; per-line options in the shell."""

from __future__ import annotations

import pytest

from mms_client.cli import runner
from mms_client.cli.options import LineOptions, Options, global_parser, line_parser, split_options
from mms_client.cli.registry import UsageError


def test_global_options_anywhere():
    argv = ["--expert", "read", "relay-F12", "PROT/PTOC1.Str.general", "--json", "--orcat", "2", "-i", "inv.yaml"]
    ns, rest = split_options(global_parser(), argv)
    o = Options.from_namespace(ns)
    assert o.expert and o.json and o.orcat == "2" and o.inventory == "inv.yaml"
    assert rest == ["read", "relay-F12", "PROT/PTOC1.Str.general"]


def test_command_options_and_values_are_kept_in_order():
    argv = ["write", "dev", "X.setVal", "-5", "--fc", "SP", "--yes", "--as", "gw"]
    ns, rest = split_options(global_parser(), argv)
    assert rest == ["write", "dev", "X.setVal", "-5", "--fc", "SP"]
    assert ns.yes is True and ns.as_client == "gw"


def test_double_dash_protects_values():
    ns, rest = split_options(global_parser(), ["write", "dev", "Cfg.d", "--", "--json"])
    assert ns.json is None and rest == ["write", "dev", "Cfg.d", "--", "--json"]


def test_inventory_default_from_environment(monkeypatch):
    monkeypatch.setenv("MMS_CLIENT_INVENTORY", "/tmp/rack.yaml")
    ns, _ = split_options(global_parser(), ["read", "x", "y"])
    assert Options.from_namespace(ns).inventory == "/tmp/rack.yaml"


def test_unset_flags_are_none_so_lines_fall_back():
    ns, _ = split_options(line_parser(), ["read", "X"])
    lo = LineOptions.from_namespace(ns)
    assert lo.json is None and lo.terse is None and lo.yes is None


def test_line_parser_rejects_session_options_with_a_pointer():
    ns, rest = split_options(line_parser(), ["read", "X", "--expert"])
    cmd, rest = runner.lookup(rest, in_shell=True)
    with pytest.raises(UsageError) as ei:
        runner.parse(cmd, rest, oneshot=False)
    assert "set mode expert" in str(ei.value)


def test_lookup_errors():
    with pytest.raises(UsageError, match="did you mean `read`"):
        runner.lookup(["raed"], in_shell=False)
    with pytest.raises(UsageError, match="files ls"):
        runner.lookup(["files"], in_shell=True)
    with pytest.raises(UsageError, match="only makes sense inside the shell"):
        runner.lookup(["pwd"], in_shell=False)


def test_setgroup_subcommands_parse():
    cmd, rest = runner.lookup(["setgroup", "edit", "2", "A.b", "1", "C.d", "2.5"], in_shell=True)
    args = runner.parse(cmd, rest, oneshot=False)
    assert args.action == "edit" and args.group == 2 and args.pairs == ["A.b", "1", "C.d", "2.5"]
    cmd, rest = runner.lookup(["setgroup", "relay", "activate", "3"], in_shell=False)
    args = runner.parse(cmd, rest, oneshot=True)
    assert args.device == "relay" and args.action == "activate" and args.group == 3
