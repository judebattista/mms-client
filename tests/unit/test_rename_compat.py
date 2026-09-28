"""The tool was renamed from mms-client to ied-client: what the old name left behind still works.

Delete this file together with ``LEGACY_TOOL_NAME`` / ``LEGACY_ENV_PREFIX`` when the transition ends.
"""

from __future__ import annotations

import json
import os

from ied_client.cli.commands._common import latest_log_for
from ied_client.cli.context import CliContext
from ied_client.cli.options import Options, global_parser, split_options
from ied_client.cli.shell import history_path
from ied_client.verify.snapshot import SNAPSHOT_KIND, SNAPSHOT_VERSION, Snapshot


def test_old_environment_variables_still_work(monkeypatch):
    monkeypatch.delenv("IED_CLIENT_INVENTORY", raising=False)
    monkeypatch.setenv("MMS_CLIENT_INVENTORY", "/tmp/rack.yaml")
    ns, _ = split_options(global_parser(), ["read", "x", "y"])
    assert Options.from_namespace(ns).inventory == "/tmp/rack.yaml"


def test_snapshots_of_the_old_kind_still_load():
    doc = {"kind": "mms-client-snapshot", "schema_version": SNAPSHOT_VERSION, "metadata": {"device": "f12"}}
    assert Snapshot.from_json(doc).metadata == {"device": "f12"}
    assert SNAPSHOT_KIND == "ied-client-snapshot"


def _write_log(path, device, mtime):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"seq": 1, "kind": "session-start", "device": device}) + "\n")
    os.utime(path, (mtime, mtime))


def test_restore_last_finds_logs_written_under_the_old_name(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.delenv("IED_CLIENT_LOG_DIR", raising=False)
    monkeypatch.delenv("MMS_CLIENT_LOG_DIR", raising=False)
    old = tmp_path / "mms-client" / "sessions" / "old-f12.jsonl"
    _write_log(old, "f12", 1_000)
    ctx = CliContext(Options())
    assert ctx.log_dir == tmp_path / "ied-client" / "sessions"
    assert latest_log_for(ctx, "f12") == old
    new = ctx.log_dir / "new-f12.jsonl"
    _write_log(new, "f12", 2_000)
    assert latest_log_for(ctx, "f12") == new  # newest first, whichever directory


def test_an_explicit_log_dir_is_the_only_place_searched(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    _write_log(tmp_path / "mms-client" / "sessions" / "old-f12.jsonl", "f12", 1_000)
    ctx = CliContext(Options(log_dir=str(tmp_path / "elsewhere")))
    assert ctx.log_dirs == [tmp_path / "elsewhere"] and latest_log_for(ctx, "f12") is None


def test_shell_history_is_carried_over(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    (tmp_path / "mms-client").mkdir()
    (tmp_path / "mms-client" / "history").write_text("read f12 X\n")
    path = history_path()
    assert path == tmp_path / "ied-client" / "history" and path.read_text() == "read f12 X\n"
