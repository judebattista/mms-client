"""Edition of an IED according to an SCL file (IDN-2 source 2: "SCL").

Mapping of SCL schema versions to editions:

* ``2003`` (no ``version`` attribute on ``<SCL>``)  → Ed1
* ``2007A``, ``2007B`` (release < 4 or absent)       → Ed2
* ``2007B4`` and later (release ≥ 4, or revision ≥ C) → Ed2.1

The IED's own ``originalSclVersion`` / ``originalSclRevision`` / ``originalSclRelease``
(present in Ed2.1 SCDs that integrate IEDs engineered with an older schema) take precedence
over the document version.
"""

from __future__ import annotations

from dataclasses import dataclass

from mms_client.scl.errors import SclError
from mms_client.scl.model import SclDocument


@dataclass(frozen=True, slots=True)
class EditionInfo:
    """Edition of one IED as stated by an SCL file.

    ``edition`` is ``"Ed1"``, ``"Ed2"`` or ``"Ed2.1"``; ``scl_version`` the schema version it
    was derived from (e.g. ``"2007B4"``); ``source`` is ``"ied-original-scl"`` or
    ``"document"``; ``reason`` a short human-readable explanation.
    """

    edition: str
    reason: str
    scl_version: str
    source: str

    def __str__(self) -> str:
        return self.edition


def edition_of_version(version: str | None, revision: str | None = None, release: str | int | None = None) -> str:
    """Edition for an SCL (version, revision, release) triple; ``version=None`` means 2003."""
    if version is None or version.strip() in ("", "2003"):
        return "Ed1"
    rev = (revision or "A").strip().upper() or "A"
    try:
        rel = int(release) if release not in (None, "") else 0
    except (TypeError, ValueError):
        rel = 0
    if version.strip() != "2007":
        # Unknown future schema: newer than anything we know, so at least Ed2.1.
        return "Ed2.1" if version.strip() > "2007" else "Ed1"
    if rev >= "C" or (rev == "B" and rel >= 4):
        return "Ed2.1"
    return "Ed2"


def edition_of(doc: SclDocument, ied_name: str) -> EditionInfo:
    """Edition of ``ied_name`` according to ``doc`` (see module docstring for the rules).

    Raises :class:`SclError` if the IED is not in the document.
    """
    ied = doc.ied(ied_name)
    if ied is None:
        raise SclError(f"IED {ied_name!r} not found (IEDs: {', '.join(doc.ied_names) or 'none'})",
                       source=doc.source)
    if ied.original_scl_version:
        ver = ied.original_scl or ied.original_scl_version
        ed = edition_of_version(ied.original_scl_version, ied.original_scl_revision, ied.original_scl_release)
        return EditionInfo(ed, f"IED {ied_name} declares originalSclVersion {ver} in a {doc.scl_version} file",
                           ver, "ied-original-scl")
    ed = edition_of_version(doc.version, doc.revision, doc.release)
    if doc.version is None:
        reason = "SCL file has no schema version attributes (2003 schema)"
    else:
        reason = f"SCL file schema version {doc.scl_version}"
    return EditionInfo(ed, reason, doc.scl_version, "document")
