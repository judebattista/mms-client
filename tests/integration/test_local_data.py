"""`local add-quirk` against the simulated IED, and `diagnose` using the local quirk (FLD-1, FLD-2)."""

from __future__ import annotations

import json

import pytest
import yaml

from ied_client.cli.main import main
from ied_client.explain import reset_default_catalogue

pytestmark = pytest.mark.integration


@pytest.fixture
def local(tmp_path, monkeypatch):
    user = tmp_path / "home"
    monkeypatch.setenv("IED_CLIENT_LOCAL_DIRS", str(user))
    reset_default_catalogue()
    yield user
    reset_default_catalogue()


def run(capsys, tmp_path, *argv):
    code = main([*argv, "--json", "--log-dir", str(tmp_path / "logs")])
    return code, json.loads(capsys.readouterr().out)


def test_add_quirk_reads_the_identity_and_diagnose_uses_it(sim, local, tmp_path, capsys):
    dev = f"127.0.0.1:{sim.port}"
    run(capsys, tmp_path, "read", dev, "SIMCTRL/GGIO1.Nope.stVal")  # something for the incident's log excerpt
    code, env = run(capsys, tmp_path, "local", "add-quirk", dev, "--set", "max_associations=2",
                    "--set", "refusal_behaviour=TCP accepted, then closed", "--save-incident", "--note", "third client refused")
    assert code == 0, env
    quirk = env["result"]["quirk"]
    assert quirk["match"] == {"vendor": "SimVendor", "model": "SimIED", "firmware": "1.0"}  # exact, from Identify
    assert quirk["source"].startswith("incidents/")
    incident = local / quirk["source"]
    assert json.loads(incident.read_text())["result"]["note"] == "third client refused"
    saved = yaml.safe_load((local / "quirks" / "mms" / "local.yaml").read_text())["quirks"]
    assert saved[0]["max_associations"] == 2

    code, env = run(capsys, tmp_path, "diagnose", dev)
    text = json.dumps(env["result"])
    assert "local quirks file" in text and "at most 2 simultaneous associations" in text


def test_add_quirk_without_the_device_needs_the_identity_and_evidence(local, tmp_path, capsys):
    code, env = run(capsys, tmp_path, "local", "add-quirk", "127.0.0.1:1", "--no-connect", "--set", "notes=x")
    assert code == 2 and "vendor" in env["error"]["message"]
    code, env = run(capsys, tmp_path, "local", "add-quirk", "127.0.0.1:1", "--no-connect", "--vendor", "V", "--model", "M",
                    "--any-firmware", "--set", "notes=x")
    assert code == 2 and "evidence" in env["error"]["message"]
    code, env = run(capsys, tmp_path, "local", "add-quirk", "127.0.0.1:1", "--no-connect", "--vendor", "V", "--model", "M",
                    "--any-firmware", "--set", "notes=x", "--source", "FAT 2026-10-02")
    assert code == 0 and env["result"]["quirk"]["match"] == {"vendor": "V", "model": "M"}
