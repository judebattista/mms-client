"""A simulated IED: a real libiec61850 server with scriptable behaviour, driven through ctypes.

Used by the integration tests and handy for demos:

    uv run python -m tests.sim --scl tests/fixtures/scl/bcu_ed2.cid --port 10102

Behaviour options (JSON via ``--options`` or keyword arguments of :class:`SimServer`):

``edition``            1, 2 or 2.1 (server edition: affects Owner/ResvTms and SGCB attributes)
``max_connections``    association slots (default 5)
``owner``              expose RCB Owner (default true)
``identity``           [vendor, model, revision] for MMS Identify
``files``              directory served by the MMS file services
``authority``          enforce Loc/LocSta switching hierarchy on controls (default true)
``interlocked``        control object references that fail an interlock check
``fail_execution``     {control ref: AddCause number} -> CommandTermination- / operate failure
``execution_delay_ms`` delay before an enhanced-security control completes
``deny_write``         attribute references whose writes are refused (object-access-denied)
``ignore_write``       attribute references that accept writes but keep their value (RW-5)
``measurements``       float attribute references updated every ``measurement_period_ms``
"""

from __future__ import annotations

import ctypes as C
import json
import math
import os
import signal
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import native as N

EDITIONS = {1: 0, 2: 1, 2.1: 2, "1": 0, "2": 1, "2.1": 2}


def _ref(node: int) -> str:
    s = N.srv()
    buf = C.create_string_buffer(256)
    s.ModelNode_getObjectReference(node, buf)
    return buf.value.decode()


def _name(node: int) -> str:
    raw = N.srv().ModelNode_getName(node)
    return raw.decode() if raw else ""


def _children(node: int) -> list[int]:
    return N.children(node)


@dataclass
class SimOptions:
    edition: Any = 2
    max_connections: int = 5
    owner: bool = True
    resv_tms: bool = True
    identity: tuple[str, str, str] = ("SimVendor", "SimIED", "1.0")
    files: str | None = None
    authority: bool = True
    interlocked: list[str] = field(default_factory=list)
    fail_execution: dict[str, int] = field(default_factory=dict)
    execution_delay_ms: int = 0
    deny_write: list[str] = field(default_factory=list)
    ignore_write: list[str] = field(default_factory=list)
    measurements: list[str] = field(default_factory=list)
    measurement_period_ms: int = 200
    local_ip: str | None = None

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> SimOptions:
        o = cls()
        for k, v in d.items():
            if not hasattr(o, k):
                raise ValueError(f"unknown simulator option {k!r}")
            setattr(o, k, tuple(v) if k == "identity" else v)
        return o


class _EventLog(list):
    """Events seen by the simulator; echoed to stdout as JSON lines (read by the test fixture)."""

    echo = False

    def append(self, ev: dict) -> None:  # type: ignore[override]
        ev = {"t": round(time.time(), 3), **ev}
        super().append(ev)
        if self.echo:
            print(json.dumps(ev), flush=True)


class SimServer:
    """In-process simulated IED. Prefer running it as a subprocess in tests (see fixture.py)."""

    def __init__(self, config_path: str | os.PathLike, options: SimOptions | None = None) -> None:
        self.opts = options or SimOptions()
        s = N.srv()
        self.model = s.ConfigFileParser_createModelFromConfigFileEx(str(config_path).encode())
        if not self.model:
            raise RuntimeError(f"libiec61850 could not parse model config {config_path}")
        cfg = s.IedServerConfig_create()
        s.IedServerConfig_setEdition(cfg, EDITIONS[self.opts.edition])
        s.IedServerConfig_setMaxMmsConnections(cfg, self.opts.max_connections)
        s.IedServerConfig_enableOwnerForRCB(cfg, bool(self.opts.owner))
        s.IedServerConfig_enableResvTmsForBRCB(cfg, bool(self.opts.resv_tms))
        s.IedServerConfig_enableEditSG(cfg, True)
        s.IedServerConfig_enableResvTmsForSGCB(cfg, EDITIONS[self.opts.edition] >= 1)
        self._filedir = self.opts.files or tempfile.mkdtemp(prefix="sim-files-")
        s.IedServerConfig_setFileServiceBasePath(cfg, (str(Path(self._filedir).resolve()) + "/").encode())
        s.IedServerConfig_enableFileService(cfg, True)
        self.server = s.IedServer_createWithConfig(self.model, None, cfg)
        s.IedServerConfig_destroy(cfg)
        if not self.server:
            raise RuntimeError("IedServer_createWithConfig failed")
        s.IedServer_setServerIdentity(self.server, *(x.encode() for x in self.opts.identity))
        if self.opts.local_ip:
            s.IedServer_setLocalIpAddress(self.server, self.opts.local_ip.encode())
        for fc in ("CF", "DC", "SP", "SV", "SE"):
            s.IedServer_setWriteAccessPolicy(self.server, N.FC[fc], N.ACCESS_POLICY_ALLOW)
        self._keep: list[Any] = []  # ctypes callbacks must outlive the server
        self._pending: dict[str, float] = {}
        self.events = _EventLog()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._walk_model()
        self._install_controls()
        self._install_setting_groups()
        self._install_write_handlers()
        cb = N.ConnectionIndicationHandler(self._on_connection)
        self._keep.append(cb)
        s.IedServer_setConnectionIndicationHandler(self.server, cb, None)

    # ------------------------------------------------------------------ model
    def _walk_model(self) -> None:
        s = N.srv()
        self.lds: list[int] = [s.IedModel_getDeviceByIndex(self.model, i) for i in range(s.IedModel_getLogicalDeviceCount(self.model))]
        self.controls: dict[str, int] = {}
        self.das_by_fc: dict[str, list[tuple[str, int]]] = {}

        def visit(node: int, depth: int) -> None:
            t = s.ModelNode_getType(node)
            if t == N.NODE_DO and s.ModelNode_getChild(node, b"Oper"):
                self.controls[_ref(node)] = node
            if t == N.NODE_DA:
                fc = s.DataAttribute_getFC(node)
                self.das_by_fc.setdefault(fc, []).append((_ref(node), node))
            if depth < 12:
                for c in _children(node):
                    visit(c, depth + 1)

        for ld in self.lds:
            visit(ld, 0)

    def node(self, ref: str) -> int:
        n = N.srv().IedModel_getModelNodeByObjectReference(self.model, ref.encode())
        if not n:
            raise KeyError(ref)
        return n

    def _da(self, do: int, name: str, fc: str | None = None) -> int | None:
        s = N.srv()
        if fc is not None:
            return s.ModelNode_getChildWithFc(do, name.encode(), N.FC[fc]) or None
        return s.ModelNode_getChild(do, name.encode()) or None

    def _bool_at(self, do: int | None, name: str = "stVal") -> bool | None:
        if not do:
            return None
        da = self._da(do, name)
        if not da:
            return None
        v = N.srv().IedServer_getAttributeValue(self.server, da)
        return bool(N.srv().MmsValue_getBoolean(v)) if v else None

    def _int_at(self, do: int | None, name: str = "stVal") -> int | None:
        if not do:
            return None
        da = self._da(do, name)
        if not da:
            return None
        v = N.srv().IedServer_getAttributeValue(self.server, da)
        return int(N.srv().MmsValue_toInt32(v)) if v else None

    # ------------------------------------------------------------------ controls
    def _install_controls(self) -> None:
        s = N.srv()
        for ref, do in self.controls.items():
            chk = N.CheckHandler(lambda a, p, v, t, il, ref=ref, do=do: self._check(ref, do, a, v, t, il))
            ctl = N.ControlHandler(lambda a, p, v, t, ref=ref, do=do: self._operate(ref, do, a, v, t))
            wait = N.WaitForExecutionHandler(lambda a, p, v, t, sc, ref=ref: self._wait(ref, a, sc))
            self._keep += [chk, ctl, wait]
            s.IedServer_setPerformCheckHandler(self.server, do, chk, None)
            s.IedServer_setControlHandler(self.server, do, ctl, None)
            s.IedServer_setWaitForExecutionHandler(self.server, do, wait, None)

    def _ln_of(self, do: int) -> int:
        return N.srv().ModelNode_getParent(do)

    def _lln0_of(self, do: int) -> int | None:
        ln = self._ln_of(do)
        ld = N.srv().ModelNode_getParent(ln)
        return N.srv().ModelNode_getChild(ld, b"LLN0") or None

    def _check(self, ref: str, do: int, action: int, ctl_val: int, test: bool, interlock: bool) -> int:
        s = N.srv()
        try:
            or_cat = s.ControlAction_getOrCat(action)
            self.events.append({"event": "check", "ref": ref, "orCat": or_cat, "select": bool(s.ControlAction_isSelect(action))})
            ln = self._ln_of(do)
            beh = self._int_at(s.ModelNode_getChild(ln, b"Beh"))
            if beh == 5 or (beh in (3, 4) and not test):
                s.ControlAction_setAddCause(action, 8)  # blocked-by-mode
                return N.CONTROL_OBJECT_ACCESS_DENIED
            sbo_normal_select = bool(s.ControlAction_isSelect(action)) and self._int_at(do, "ctlModel") == 2
            if self.opts.authority and not sbo_normal_select:  # an SBO-normal select carries no origin
                if or_cat == 0:
                    s.ControlAction_setAddCause(action, 20)  # no-access-authority
                    return N.CONTROL_OBJECT_ACCESS_DENIED
                loc = self._bool_at(s.ModelNode_getChild(ln, b"Loc"))
                lln0 = self._lln0_of(do)
                loc_sta = self._bool_at(s.ModelNode_getChild(lln0, b"LocSta")) if lln0 else None
                if loc and or_cat in (2, 3, 5, 6):
                    s.ControlAction_setAddCause(action, 2)  # blocked-by-switching-hierarchy
                    return N.CONTROL_OBJECT_ACCESS_DENIED
                if loc_sta and or_cat in (3, 6):
                    s.ControlAction_setAddCause(action, 2)
                    return N.CONTROL_OBJECT_ACCESS_DENIED
            if interlock and ref in self.opts.interlocked:
                s.ControlAction_setAddCause(action, 10)  # blocked-by-interlocking
                return N.CONTROL_OBJECT_ACCESS_DENIED
            return N.CONTROL_ACCEPTED
        except Exception as exc:  # pragma: no cover
            self.events.append({"event": "error", "where": "check", "error": repr(exc)})
            return N.CONTROL_ACCEPTED

    def _wait(self, ref: str, action: int, synchro: bool) -> int:
        return N.CONTROL_RESULT_OK

    def _operate(self, ref: str, do: int, action: int, ctl_val: int, test: bool) -> int:
        s = N.srv()
        try:
            if self.opts.execution_delay_ms:
                started = self._pending.setdefault(ref, time.monotonic())
                if time.monotonic() - started < self.opts.execution_delay_ms / 1000:
                    return N.CONTROL_RESULT_WAITING
                self._pending.pop(ref, None)
            if ref in self.opts.fail_execution:
                s.ControlAction_setAddCause(action, int(self.opts.fail_execution[ref]))
                return N.CONTROL_RESULT_FAILED
            st = self._da(do, "stVal", "ST")
            if st:
                t = s.DataAttribute_getType(st)
                vtype = s.MmsValue_getType(ctl_val)
                if t == N.TYPE_CODEDENUM:
                    s.IedServer_updateDbposValue(self.server, st, 2 if s.MmsValue_getBoolean(ctl_val) else 1)
                elif t == N.TYPE_BOOLEAN:
                    s.IedServer_updateBooleanAttributeValue(self.server, st, bool(s.MmsValue_getBoolean(ctl_val)))
                elif vtype in (4, 5):
                    s.IedServer_updateInt32AttributeValue(self.server, st, int(s.MmsValue_toInt32(ctl_val)))
            tda = self._da(do, "t", "ST")
            if tda:
                s.IedServer_updateUTCTimeAttributeValue(self.server, tda, int(time.time() * 1000))
            origin = self._da(do, "origin", "ST")
            if origin:
                oc = self._da(origin, "orCat")
                if oc:
                    s.IedServer_updateInt32AttributeValue(self.server, oc, s.ControlAction_getOrCat(action))
                oi = self._da(origin, "orIdent")
                if oi:
                    n = C.c_int(0)
                    buf = s.ControlAction_getOrIdent(action, C.byref(n))
                    val = s.MmsValue_newOctetString(n.value, 64)
                    if n.value and buf:
                        s.MmsValue_setOctetString(val, buf, n.value)
                    s.IedServer_updateAttributeValue(self.server, oi, val)
                    s.MmsValue_delete(val)
            cn = self._da(do, "ctlNum", "ST")
            if cn:
                s.IedServer_updateUnsignedAttributeValue(self.server, cn, s.ControlAction_getCtlNum(action) & 0xFF)
            self.events.append({"event": "operate", "ref": ref, "orCat": s.ControlAction_getOrCat(action), "test": test})
            return N.CONTROL_RESULT_OK
        except Exception as exc:  # pragma: no cover
            self.events.append({"event": "error", "where": "operate", "error": repr(exc)})
            return N.CONTROL_RESULT_FAILED

    # ------------------------------------------------------------------ setting groups
    def _install_setting_groups(self) -> None:
        s = N.srv()
        self._sg_store: dict[int, dict[int, dict[str, int]]] = {}  # sgcb -> group -> ref -> MmsValue*
        for ld in self.lds:
            sgcb = s.LogicalDevice_getSettingGroupControlBlock(ld)
            if not sgcb:
                continue
            # ModelNode_getObjectReference crashes on LDs; derive the domain from the first LN.
            ld_prefix = _ref(N.children(ld)[0]).split("/")[0] + "/"
            sg_das = {r: n for r, n in self.das_by_fc.get(N.FC["SG"], []) if r.startswith(ld_prefix)}
            se_das = {r: n for r, n in self.das_by_fc.get(N.FC["SE"], []) if r.startswith(ld_prefix)}
            # Number of groups is not exposed through the API we bind; keep up to 8 lazily created groups.
            store: dict[int, dict[str, int]] = {}
            self._sg_store[sgcb] = store

            def group(g: int, sg_das=sg_das, store=store) -> dict[str, int]:
                if g not in store:
                    store[g] = {r: s.MmsValue_clone(s.IedServer_getAttributeValue(self.server, n)) for r, n in sg_das.items()}
                return store[g]

            act = s.IedServer_getActiveSettingGroup(self.server, sgcb)
            group(act)

            def on_active(_p, _sgcb, new_act, _conn, group=group, sg_das=sg_das):
                vals = group(new_act)
                for r, n in sg_das.items():
                    s.IedServer_updateAttributeValue(self.server, n, vals[r])
                self.events.append({"event": "active-sg", "group": new_act})
                return True

            def on_edit(_p, _sgcb, new_edit, _conn, group=group, se_das=se_das):
                if new_edit == 0:
                    return True
                vals = group(new_edit)
                for r, n in se_das.items():
                    if r in vals:
                        s.IedServer_updateAttributeValue(self.server, n, vals[r])
                self.events.append({"event": "edit-sg", "group": new_edit})
                return True

            def on_confirm(_p, sgcb_p, edit, group=group, se_das=se_das, sg_das=sg_das):
                vals = group(edit)
                for r, n in se_das.items():
                    if r in vals:
                        s.MmsValue_delete(vals[r])
                        vals[r] = s.MmsValue_clone(s.IedServer_getAttributeValue(self.server, n))
                if s.IedServer_getActiveSettingGroup(self.server, sgcb_p) == edit:
                    for r, n in sg_das.items():
                        s.IedServer_updateAttributeValue(self.server, n, vals[r])
                self.events.append({"event": "confirm-sg", "group": edit})

            cbs = (N.ActiveSgChangedHandler(on_active), N.EditSgChangedHandler(on_edit), N.EditSgConfirmationHandler(on_confirm))
            self._keep += list(cbs)
            s.IedServer_setActiveSettingGroupChangedHandler(self.server, sgcb, cbs[0], None)
            s.IedServer_setEditSettingGroupChangedHandler(self.server, sgcb, cbs[1], None)
            s.IedServer_setEditSettingGroupConfirmationHandler(self.server, sgcb, cbs[2], None)

    # ------------------------------------------------------------------ writes
    def _install_write_handlers(self) -> None:
        s = N.srv()
        for ref, result in [(r, N.DATA_ACCESS_ERROR_OBJECT_ACCESS_DENIED) for r in self.opts.deny_write] + [
            (r, N.DATA_ACCESS_ERROR_SUCCESS_NO_UPDATE) for r in self.opts.ignore_write
        ]:
            node = self.node(ref)
            h = N.WriteAccessHandler(lambda da, v, conn, p, result=result, ref=ref: self._on_write(ref, result))
            self._keep.append(h)
            s.IedServer_handleWriteAccess(self.server, node, h, None)

    def _on_write(self, ref: str, result: int) -> int:
        self.events.append({"event": "write", "ref": ref, "result": result})
        return result

    # ------------------------------------------------------------------ connections
    def _on_connection(self, _srv: int, conn: int, connected: bool, _p: int) -> None:
        peer = N.srv().ClientConnection_getPeerAddress(conn)
        self.events.append({"event": "connect" if connected else "disconnect", "peer": peer.decode() if peer else None})

    # ------------------------------------------------------------------ lifecycle
    def start(self, port: int) -> None:
        s = N.srv()
        s.IedServer_start(self.server, port)
        if not s.IedServer_isRunning(self.server):
            raise RuntimeError(f"server failed to start on port {port}")
        if self.opts.measurements:
            t = threading.Thread(target=self._measure_loop, daemon=True)
            t.start()
            self._threads.append(t)

    def _measure_loop(self) -> None:
        s = N.srv()
        nodes = [self.node(r) for r in self.opts.measurements]
        k = 0
        while not self._stop.wait(self.opts.measurement_period_ms / 1000):
            k += 1
            s.IedServer_lockDataModel(self.server)
            for i, n in enumerate(nodes):
                s.IedServer_updateFloatAttributeValue(self.server, n, 100.0 + 10 * math.sin(k / 5 + i))
            s.IedServer_unlockDataModel(self.server)

    @property
    def open_connections(self) -> int:
        return int(N.srv().IedServer_getNumberOfOpenConnections(self.server))

    def stop(self) -> None:
        self._stop.set()
        for t in self._threads:
            t.join(timeout=2)
        s = N.srv()
        s.IedServer_stop(self.server)
        s.IedServer_destroy(self.server)
        self.server = None


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(prog="python -m tests.sim", description="Simulated IED for mms-client tests")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--config", help="libiec61850 model config file")
    src.add_argument("--scl", help="SCL file (CID/ICD/SCD)")
    ap.add_argument("--ied", help="IED name inside the SCL file")
    ap.add_argument("--port", type=int, default=10102)
    ap.add_argument("--options", default="{}", help="JSON object with behaviour options")
    args = ap.parse_args(argv)
    opts = SimOptions.from_json(json.loads(args.options))
    cfg_path = args.config
    if args.scl:
        from mms_client.scl import expand_server, load_scl, to_libiec61850_config

        doc = load_scl(args.scl)
        ied = args.ied or doc.ied_names[0]
        text = to_libiec61850_config(expand_server(doc, ied))
        fd, cfg_path = tempfile.mkstemp(suffix=".cfg", prefix="sim-")
        with os.fdopen(fd, "w") as f:
            f.write(text)
    _EventLog.echo = True
    sim = SimServer(cfg_path, opts)
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *a: stop.set())
    signal.signal(signal.SIGINT, lambda *a: stop.set())
    sim.start(args.port)
    print(json.dumps({"event": "ready", "port": args.port, "controls": sorted(sim.controls)}), flush=True)
    while not stop.wait(0.2):
        pass
    sim.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
