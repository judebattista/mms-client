"""Incident files (LOG-3): a self-contained record of a failure investigated on the rack.

They build the rack's failure history and feed the explanation catalogue and the quirks file
with patterns seen in practice.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from .results import SCHEMA_VERSION, envelope, jsonable
from .session import Session


def build_incident(session: Session, *, note: str | None = None, excerpt: int = 300) -> dict[str, Any]:
    inv = session.inventory
    body = {
        "kind": "mms-client-incident",
        "incident_schema_version": SCHEMA_VERSION,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "note": note,
        "device": {
            "name": session.device_name,
            "host": session.target.host,
            "port": session.target.port,
            "as_client": session.target.as_client.name if session.target.as_client else None,
            "local_ip": session.target.local_ip,
        },
        "experiment": inv.experiment if inv else None,
        "inventory": inv.to_yaml() if inv else None,
        "inventory_path": str(inv.path) if inv and inv.path else None,
        "identity": session.identity.to_json() if session.identity is not None and hasattr(session.identity, "to_json") else None,
        "mode": session.mode.value,
        "last_error": {
            "error": session.last_error.error.to_json(),
            "context": session.last_error.context,
            "message": session.last_error.message,
        }
        if session.last_error
        else None,
        "results": jsonable(session.last_results),
        "log_file": str(session.log.path) if session.log.path else None,
        "log_excerpt": session.log.excerpt(excerpt),
    }
    return envelope("export --incident", body, device=session.device_name)


def write_incident(session: Session, path: str | Path, *, note: str | None = None) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(build_incident(session, note=note), indent=1, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    session.log.write("note", action="export-incident", path=str(p))
    return p
