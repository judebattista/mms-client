"""RSK-5 memory soak: 10k reads/writes (plus reports, controls and directory calls) must not leak.

Run with ``uv run pytest -m soak tests/integration/test_soak.py -s``.
"""

from __future__ import annotations

import gc
import os
import time

import pytest

from mms_protocol.adapter import IedClient

pytestmark = [pytest.mark.integration, pytest.mark.soak]
LD = "SIMCTRL"


def rss_kb() -> int:
    with open(f"/proc/{os.getpid()}/status") as fh:
        for line in fh:
            if line.startswith("VmRSS:"):
                return int(line.split()[1])
    return 0


def test_soak_reads_writes(sim):
    c = IedClient(request_timeout_ms=5000)
    c.connect("127.0.0.1", sim.port)
    try:
        spec = c.get_variable_spec(LD, "GGIO1")
        sp = spec.find(["SP", "Setp1", "setVal"])
        mx_spec = spec.child("MX")
        ctl = c.control(f"{LD}/GGIO1.SPCSO3")
        ctl.configure(or_ident="soak", or_cat=2)
        ctl_spec = spec.find(["CO", "SPCSO3", "Oper", "ctlVal"])
        reports = []
        ref = f"{LD}/LLN0.BR.brcbMeas01"
        c.install_report_handler(ref, c.get_rcb(ref).rpt_id, reports.append)
        c.set_rcb(ref, {"intg_pd": 200, "trg_ops": 1 | 8 | 16, "rpt_ena": True}, single_request=False)

        def cycle(n: int) -> None:
            for i in range(n):
                c.read(LD, "GGIO1$SP$Setp1$setVal", sp)
                c.write(LD, "GGIO1$SP$Setp1$setVal", sp, i % 1000)
                if i % 50 == 0:
                    c.read(LD, "GGIO1$MX", mx_spec)
                if i % 100 == 0:
                    c.get_domain_variable_names(LD)
                    c.get_rcb(ref)
                    c.get_file_directory()
                    ctl.operate(ctl_spec, bool(i % 2))
                    ctl.wait_termination(2)
                    reports.clear()

        cycle(1000)  # warm-up: allocator pools, caches
        gc.collect()
        base = rss_kb()
        t0 = time.time()
        cycle(10000)
        gc.collect()
        grown = rss_kb() - base
        rate = 20000 / (time.time() - t0)
        print(f"\nsoak: 10k read+write cycles, RSS growth {grown} kB, {rate:.0f} ops/s")
        c.set_rcb(ref, {"rpt_ena": False})
        c.set_rcb(ref, {"intg_pd": 1000, "trg_ops": 27}, single_request=False)
        assert grown < 4096, f"RSS grew by {grown} kB over 10k cycles"
    finally:
        c.close()
