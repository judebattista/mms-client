"""The CLI against the simulated IED: one-shot commands with --json (ARC-3, exit codes) and a
scripted shell session (CLI-1 … CLI-4)."""

from __future__ import annotations

import io
import json
import time
from pathlib import Path

import pytest

from ied_client.cli.context import CliContext
from ied_client.cli.main import main
from ied_client.cli.options import Options
from ied_client.cli.render import Output
from ied_client.cli.shell import Shell
from ied_client.core.safety import Scripted

pytestmark = pytest.mark.integration
LD = "SIMCTRL"


@pytest.fixture
def dev(sim) -> str:
    return f"127.0.0.1:{sim.port}"


@pytest.fixture
def run(capsys, tmp_path):
    """Run a one-shot command with --json; return (exit code, envelope)."""

    def _run(*argv: str) -> tuple[int, dict]:
        code = main([*argv, "--json", "--log-dir", str(tmp_path / "logs")])
        out = capsys.readouterr().out
        return code, json.loads(out)

    return _run


# ---------------------------------------------------------------------------------- data
def test_read(run, dev):
    code, env = run("read", dev, f"{LD}/GGIO1.SPCSO1.stVal")
    assert code == 0 and env["ok"] is True and env["command"] == "read" and env["device"] == dev
    r = env["result"]
    assert r["reference"] == f"{LD}/GGIO1.SPCSO1.stVal" and r["fc"] == "ST" and r["type"] == "boolean"
    assert r["mms"] == f"{LD}/GGIO1$ST$SPCSO1$stVal"
    assert r["quality"]["validity"] == "good" and r["timestamp"] is not None


def test_read_missing_object(run, dev):
    code, env = run("read", dev, f"{LD}/GGIO1.Nope.stVal")
    assert code == 1 and env["ok"] is False
    assert env["error"]["key"] == "tool:object-not-in-model"


def test_ambiguous_fc_is_a_usage_error(run, dev):
    code, env = run("write", dev, f"{LD}/GGIO1.StrVal.setMag.f", "1", "--yes")  # exists under SG and SE
    assert code == 2 and env["error"]["key"] == "tool:invalid-reference" and "[FC]" in env["error"]["message"]
    assert env["result"]["fcs"] == ["SG", "SE"]


def test_write_with_yes_reads_back(run, dev):
    code, env = run("write", dev, f"{LD}/GGIO1.Setp1.setVal", "21", "--yes")
    assert code == 0, env
    r = env["result"]
    assert r["fc"] == "SP" and r["before"] == 10 and r["requested"] == 21 and r["after"] == 21 and r["applied"] is True
    assert any("confirmed by --yes" in m for m in env["messages"])
    code, env = run("write", dev, f"{LD}/GGIO1.Setp1.setVal", "10", "--yes")
    assert code == 0


def test_write_needs_confirmation_non_interactively(run, dev):
    code, env = run("write", dev, f"{LD}/GGIO1.Setp1.setVal", "99")
    assert code == 4 and env["error"]["key"] == "tool:non-interactive-no-prompt"
    code, env = run("read", dev, f"{LD}/GGIO1.Setp1.setVal")
    assert env["result"]["value"] == 10  # nothing was sent


def test_write_refused_by_device_shows_code_and_hint(run, dev):
    code, env = run("write", dev, f"{LD}/GGIO1.Cfg1.dcText", "x", "--yes")
    assert code == 1
    assert env["error"]["key"] == "data-access:object-access-denied" and env["error"]["code"] == 3
    assert env["hint"] is not None and env["hint"]["rendered"]  # EXP-1


def test_write_not_applied_is_a_failure(run, dev):
    code, env = run("write", dev, f"{LD}/GGIO1.Setp1.setMag", "2.5", "--yes")
    assert code == 1 and env["error"]["key"] == "tool:write-readback-mismatch"
    assert env["result"]["applied"] is False


def test_write_status_needs_expert(run, dev):
    code, env = run("write", dev, f"{LD}/GGIO1.SPCSO1.stVal", "true", "--yes")
    assert code == 4 and env["error"]["key"] == "tool:refused-standard-mode"


def test_text_output_shows_code_and_hint(capsys, dev, tmp_path):
    code = main(["write", dev, f"{LD}/GGIO1.Cfg1.dcText", "x", "--yes", "--log-dir", str(tmp_path)])
    out = capsys.readouterr().out
    assert code == 1
    assert f"{LD}/GGIO1.Cfg1.dcText [DC]" in out  # CLI-7: exact reference and FC
    assert "code: data-access:object-access-denied (3)" in out
    assert "Hint:" in out
    code = main(["write", dev, f"{LD}/GGIO1.Cfg1.dcText", "x", "--yes", "--terse", "--log-dir", str(tmp_path)])
    out = capsys.readouterr().out
    assert "code: data-access:object-access-denied (3)" in out and "Hint:" not in out  # CLI-8


# ---------------------------------------------------------------------------------- connection / model / files
def test_info(run, dev):
    code, env = run("info", dev)
    assert code == 0
    ident = env["result"]["identity"]
    assert ident["vendor"] == {"value": "SimVendor", "source": "MMS Identify", "confidence": "confirmed"}
    assert ident["edition"]["confidence"] == "inferred"  # IDN-3: never shown as confirmed
    assert {s["source"] for s in ident["sources"]} >= {"mms-identify", "LPHD.PhyNam", "LLN0.NamPlt"}
    assert env["result"]["association"]["connection_params"]["max_pdu_size"] > 0


def test_connection_refused_exit_3(run):
    from tests.sim.fixture import free_port

    code, env = run("read", f"127.0.0.1:{free_port()}", "X/Y.z", "--timeout", "1000")
    assert code == 3 and env["ok"] is False and env["error"]["domain"] == "ied"


def test_ls(run, dev):
    code, env = run("ls", dev)
    assert code == 0 and [c["name"] for c in env["result"]["children"]] == [LD]
    code, env = run("ls", dev, f"/{LD}/LLN0")
    r = env["result"]
    assert r["kind"] == "ln" and {c["name"] for c in r["control_blocks"]} >= {"urcbEvents01", "brcbMeas01", "SGCB"}
    assert set(r["datasets"]) == {"Events", "Measurements"}
    code, env = run("ls", dev, f"{LD}/GGIO1.SPCSO2")
    kinds = {c["name"]: c for c in env["result"]["children"]}
    assert kinds["stVal"]["fcs"] == {"ST": "boolean"} and kinds["Oper"]["kind"] == "data"


def test_describe_tree_dataset(run, dev):
    code, env = run("describe", dev, f"{LD}/GGIO1.SPCSO1")
    r = env["result"]
    assert code == 0 and r["kind"] == "data object" and r["cdc"]["cdc"] == "SPC"
    assert r["fcs"]["CO"]["how"] == "operate" and r["fcs"]["ST"]["mode"] == "expert"
    code, env = run("describe", dev, f"{LD}/GGIO1.Setp1.setVal")
    assert env["result"]["fcs"]["SP"]["writable"] is True
    code, env = run("tree", dev, f"{LD}/LLN0", "--depth", "1")
    assert code == 0 and {n["name"] for n in env["result"]["nodes"]} >= {"Mod", "NamPlt"}
    code, env = run("dataset", dev, "Events")
    assert code == 0 and len(env["result"]["members"]) == 6 and env["result"]["unresolved"] == 0


def test_files(run, dev, tmp_path):
    code, env = run("files", "ls", dev)
    assert code == 0 and "COMTRADE_0001.dat" in {f["name"] for f in env["result"]["files"]}
    code, env = run("files", "get", dev, "COMTRADE_0001.dat", "--out", str(tmp_path))
    assert code == 0 and env["result"]["size"] == 150000
    assert (tmp_path / "COMTRADE_0001.dat").stat().st_size == 150000
    code, env = run("files", "get", dev, "COMTRADE_0001.dat", "--out", str(tmp_path))
    assert code == 2 and env["error"]["key"] == "tool:local-file-error"


def test_setgroup_show(run, dev):
    code, env = run("setgroup", dev, "show")
    assert code == 0 and env["result"]["sgcbs"][0]["values"]["NumOfSG"] == 3


# ---------------------------------------------------------------------------------- controls
def test_operate_refused_without_typed_confirmation(run, dev):
    code, env = run("operate", dev, f"{LD}/GGIO1.SPCSO1", "true", "--orcat", "2")
    assert code == 4 and env["ok"] is False
    assert env["result"]["sent"] is False and env["result"]["plan"]["ctl_model"]["value"] == 1
    assert env["result"]["plan"]["origin"]["orCat"] == 2
    # --yes never confirms a control (SAF-3)
    code, env = run("operate", dev, f"{LD}/GGIO1.SPCSO1", "true", "--orcat", "2", "--yes")
    assert code == 4 and env["result"]["sent"] is False


def test_operate_needs_an_orcat(run, dev):
    code, env = run("operate", dev, f"{LD}/GGIO1.SPCSO1", "true")
    assert code == 4 and env["error"]["key"] == "tool:orcat-required"  # ORG-1: no silent default
    code, env = run("operate", dev, f"{LD}/GGIO1.SPCSO1", "true", "--orcat", "7")
    assert code == 4 and env["error"]["key"] == "tool:refused-standard-mode"


def test_authority_probe_refused_non_interactively(run, dev):
    code, env = run("authority-probe", dev)
    assert code == 4 and env["error"]["key"] == "tool:non-interactive-no-prompt"


def test_origin(run, dev):
    code, env = run("origin", dev, f"{LD}/GGIO1.SPCSO1")
    assert code == 0 and env["result"]["reference"] == f"{LD}/GGIO1.SPCSO1" and "origin" in env["result"]


# ---------------------------------------------------------------------------------- reports
def test_rcb(run, dev):
    code, env = run("rcb", dev)
    assert code == 0
    rcbs = {r["reference"]: r for r in env["result"]["rcbs"]}
    assert rcbs[f"{LD}/LLN0.RP.urcbEvents01"]["state"] == "free"
    assert rcbs[f"{LD}/LLN0.BR.brcbMeas01"]["type"] == "BR"


def test_subscribe_and_cleanup(run, dev):
    code, env = run("subscribe", dev, "urcbEvents", "--duration", "1.5")
    assert code == 0, env
    r = env["result"]
    assert r["received"] >= 1 and r["reports"][0]["entries"][0]["member"] == f"{LD}/GGIO1.SPCSO1.stVal"
    assert any("undone" in n for n in r["cleanup"])  # RPT-7
    code, env = run("rcb", dev)
    assert all(x["state"] == "free" for x in env["result"]["rcbs"])


def test_gi_one_shot(run, dev):
    code, env = run("gi", dev, "urcbEvents", "--wait", "1")
    assert code == 0 and env["result"]["temporary_subscription"] is True
    assert any("general-interrogation" in e["reasons"] for rep in env["result"]["reports"] for e in rep["entries"])


# ---------------------------------------------------------------------------------- verification, logs, explain
def test_check_snapshot_diff(run, dev, tmp_path):
    code, env = run("check", dev)
    assert code == 0
    assert set(env["result"]["summary"]) >= {"Configuration", "Communication", "device_passes"}  # VER-7
    snap = tmp_path / "s.json"
    code, env = run("snapshot", dev, "--out", str(snap))
    assert code == 0 and snap.exists() and env["result"]["metadata"]["unreadable_count"] == 0
    code, env = run("diff", dev, str(snap))
    assert code == 0 and env["result"]["status"] == "pass"
    code, env = run("diff", str(snap), str(snap))
    assert code == 0 and env["result"]["entries"] == []


def test_session_log_and_explain_last(run, dev, tmp_path):
    run("write", dev, f"{LD}/GGIO1.Cfg1.dcText", "x", "--yes")
    logs = sorted((tmp_path / "logs").glob("*.jsonl"))
    assert logs
    entries = [json.loads(line) for line in logs[-1].read_text().splitlines()]
    kinds = [e["kind"] for e in entries]
    assert kinds[0] == "session-start" and "command" in kinds and "write" in kinds and kinds[-1] == "session-end"
    code, env = run("explain", "last")
    assert code == 0 and env["result"]["last_error"]["key"] == "data-access:object-access-denied"
    assert env["result"]["explanation"] is not None


def test_diagnose(run, dev):
    code, env = run("diagnose", dev, "--no-ping")
    assert code == 0, env["result"]["verdict"]
    assert env["result"]["stopped_at"] is None and env["result"]["verdict"]["text"]
    assert any("GOOSE" in n for n in env["result"]["notes"])


def test_export_incident(run, dev, tmp_path):
    run("read", dev, f"{LD}/GGIO1.Nope.stVal")
    out = tmp_path / "incident.json"
    code, env = run("export", dev, "--incident", str(out), "--note", "test")
    assert code == 0 and out.exists()
    doc = json.loads(out.read_text())
    assert doc["result"]["note"] == "test" and doc["result"]["log_excerpt"]
    assert doc["result"]["last_error"]["error"]["name"] == "object-not-in-model"


# ---------------------------------------------------------------------------------- the shell
def shell_ctx(tmp_path: Path, answers: list, *, width: int = 200) -> tuple[CliContext, io.StringIO]:
    buf = io.StringIO()
    ctx = CliContext(Options(log_dir=str(tmp_path / "logs")), out=Output.for_stream(buf, width=width), stdout=buf,
                     ui=Scripted(answers), in_shell=True)
    return ctx, buf


def test_scripted_shell_session(dev, tmp_path):
    # answers in order: write confirm, orCat choice (ORG-1), typed control confirmation, restore confirm
    ctx, buf = shell_ctx(tmp_path, [True, "2", "GGIO1.SPCSO3", True])
    lines = [
        "ls",
        f"cd {LD}/GGIO1",
        "pwd",
        "read SPCSO1.stVal",
        "write Setp1.setVal 33",
        "operate SPCSO3 true",
        "origin SPCSO3",
        "cd ..",
        "subscribe urcbEvents --duration 1",
        "subscribe urcbEvents --keep --duration 0.3",
        "gi --wait 0.5",
        "restore",
        "read GGIO1.Setp1.setVal",
        "set mode expert",
        "log -n 5",
        "exit",
        "read SPCSO1.stVal",  # never runs
    ]
    sh = Shell(ctx, lines=lines)
    assert sh.run(dev) == 0
    out = buf.getvalue()
    codes_ = [r.code for r in sh.results]
    assert codes_ == [0] * len(codes_), (codes_, out)
    assert len(sh.results) == 1 + 16  # connect + every line up to `exit`
    host = "127.0.0.1"
    # prompts (CLI-4, MOD-1): device, path, orCat once set, mode
    assert f"[std] {host}:/> ls" in out
    assert f"[std] {host}:/{LD}/GGIO1> pwd" in out
    assert f"[std] {host}:/{LD}/GGIO1 [orCat=station]> origin SPCSO3" in out
    assert f"[EXPERT] {host}:/{LD} [orCat=station]> log -n 5" in out
    assert f"/{LD}/GGIO1   ({LD}/GGIO1)" in out
    assert "Pre-flight check" in out and "Operated" in out
    assert ctx.ui.options[0][0] == ("1", "bay-control")  # the orCat question offers 1-3
    by_line = dict(zip(["connect", *lines], sh.results, strict=False))
    assert by_line["origin SPCSO3"].data["origin"]["orCat"] == 2  # ORG-4
    assert by_line["read GGIO1.Setp1.setVal"].data["value"] == 10  # restore put it back (LOG-2)
    assert by_line["subscribe urcbEvents --duration 1"].data["received"] >= 1
    # kept subscription undone on exit (RPT-7), log path shown
    assert "Cleanup: undone: RptEna of SIMCTRL/LLN0.RP.urcbEvents01" in out
    assert "Session log:" in out
    log = next((tmp_path / "logs").glob("*.jsonl"))
    entries = [json.loads(x) for x in log.read_text().splitlines()]
    assert [e["line"] for e in entries if e["kind"] == "command"][:3] == [f"connect {dev}", "ls", f"cd {LD}/GGIO1"]
    assert entries[0]["kind"] == "session-start" and entries[-1]["kind"] == "session-end"
    assert any(e["kind"] == "control" and e["action"] == "operate" for e in entries)  # CTL-10


def test_shell_refusals_keep_the_shell(dev, tmp_path):
    ctx, buf = shell_ctx(tmp_path, ["wrong"])
    sh = Shell(ctx, lines=[
        f"operate {LD}/GGIO1.SPCSO1 true --orcat 1",  # typed confirmation does not match
        "subscribe urcbEvents --takeover --duration 0.2",  # takeover needs expert mode
        f"write {LD}/GGIO1.SPCSO1.Oper.ctlVal true",  # RW-8
        "read --json " + f"{LD}/GGIO1.SPCSO1.stVal",
        "exit",
    ])
    sh.run(dev)
    out = buf.getvalue()
    assert [r.code for r in sh.results[1:5]] == [4, 4, 4, 0]
    assert "nothing was sent" in out
    assert "tool:refused-standard-mode" in out
    assert "operate" in out and "tool:write-co-needs-operate" in out
    assert '"schema_version": "1.0"' in out  # --json on one shell line


def test_shell_reports_lost_connection(tmp_path):
    from tests.conftest import FIXTURES
    from tests.sim.fixture import SimProcess

    with SimProcess(config=FIXTURES / "sim" / "basic.cfg") as p:
        ctx, buf = shell_ctx(tmp_path, [])

        def lines():
            yield "subscribe brcbMeas01 --keep --duration 0.3"
            p.kill()
            assert ctx.session is not None and ctx.session.connection_lost.wait(5)
            time.sleep(0.1)
            yield f"read {LD}/GGIO1.SPCSO1.stVal"
            yield "connect"
            yield "exit"

        sh = Shell(ctx, lines=lines())
        sh.run(f"127.0.0.1:{p.port}")
    out = buf.getvalue()
    assert [r.code for r in sh.results] == [0, 0, 3, 3, 0]
    assert "was lost" in out and "could not undo" in out  # RPT-7: what lingers is reported


def test_info_saves_identity_to_the_inventory(run, sim, tmp_path):
    inv = tmp_path / "rack.yaml"
    inv.write_text(f"schema_version: 1\nsafety: lab\ndevices:\n  - name: sim\n    ip: 127.0.0.1\n    port: {sim.port}\n")
    code, env = run("-i", str(inv), "info", "sim", "--save-identity")
    assert code == 4  # a file write needs confirmation too (--yes)
    code, env = run("-i", str(inv), "info", "sim", "--save-identity", "--yes")
    assert code == 0 and env["device"] == "sim" and env["result"]["saved_to"] == str(inv.resolve())
    text = inv.read_text()
    assert "vendor: SimVendor" in text and "model: SimIED" in text
    assert "edition" not in text  # an inferred edition is never stored as if operator-supplied
