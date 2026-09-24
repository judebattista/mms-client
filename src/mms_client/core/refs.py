"""Object references: IEC 61850 (``LD/LN.DO.DA``), MMS (``LD/LN$FC$DO$DA``) and shell paths
(``/LD/LN/DO/DA``), with an optional functional constraint (``[ST]``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from mms_client.codes import FCS

FC_NAMES = frozenset(FCS)
_FC_SUFFIX = re.compile(r"^(?P<body>.*?)\s*\[(?P<fc>[A-Za-z]{2})\]$")


class RefError(ValueError):
    """A reference could not be parsed or does not fit the current location."""


@dataclass(frozen=True, slots=True)
class ObjectRef:
    """A reference to a logical device, logical node, or data inside a logical node.

    ``path`` holds the components below the LN (DO, SDOs, DA, BDAs). ``fc`` is optional:
    when absent, the model decides (and ambiguity is reported, never guessed silently).
    """

    ld: str
    ln: str | None = None
    path: tuple[str, ...] = ()
    fc: str | None = None

    @property
    def depth(self) -> int:
        return 1 + (self.ln is not None) + len(self.path)

    def iec(self) -> str:
        if self.ln is None:
            return self.ld
        s = f"{self.ld}/{self.ln}"
        if self.path:
            s += "." + ".".join(self.path)
        return s

    def mms_item(self, fc: str | None = None) -> str:
        """MMS item name within the domain (``LN$FC$DO$DA``); needs an FC for data below the LN."""
        if self.ln is None:
            raise RefError("a logical device has no MMS item name")
        fc = fc or self.fc
        if not self.path:
            return self.ln if fc is None else f"{self.ln}${fc}"
        if fc is None:
            raise RefError(f"{self.iec()}: a functional constraint is needed")
        return "$".join((self.ln, fc, *self.path))

    def shell_path(self) -> str:
        parts = [self.ld] + ([self.ln] if self.ln else []) + list(self.path)
        return "/" + "/".join(parts)

    def with_fc(self, fc: str | None) -> ObjectRef:
        return ObjectRef(self.ld, self.ln, self.path, fc)

    def child(self, name: str) -> ObjectRef:
        if self.ln is None:
            return ObjectRef(self.ld, name)
        return ObjectRef(self.ld, self.ln, (*self.path, name), self.fc)

    def parent(self) -> ObjectRef | None:
        if self.path:
            return ObjectRef(self.ld, self.ln, self.path[:-1], self.fc if len(self.path) > 1 else None)
        if self.ln is not None:
            return ObjectRef(self.ld)
        return None

    @property
    def data_object(self) -> ObjectRef | None:
        """The top-level DO this reference lies in (None for LD/LN references)."""
        if not self.path or self.ln is None:
            return None
        return ObjectRef(self.ld, self.ln, self.path[:1])

    def __str__(self) -> str:
        return self.iec() + (f" [{self.fc}]" if self.fc else "")


def split_fc(text: str) -> tuple[str, str | None]:
    """Strip an ``[FC]`` suffix."""
    m = _FC_SUFFIX.match(text.strip())
    if not m:
        return text.strip(), None
    fc = m.group("fc").upper()
    if fc not in FC_NAMES:
        raise RefError(f"unknown functional constraint {fc!r}")
    return m.group("body"), fc


def parse_mms(text: str) -> ObjectRef:
    """``LD/LN$FC$DO$DA`` → ObjectRef (FC taken from the MMS name)."""
    ld, sep, item = text.partition("/")
    if not sep or not ld:
        raise RefError(f"{text!r}: expected LD/LN$FC$...")
    parts = item.split("$")
    ln = parts[0]
    if len(parts) == 1:
        return ObjectRef(ld, ln)
    fc = parts[1].upper()
    if fc not in FC_NAMES:
        raise RefError(f"{text!r}: {parts[1]!r} is not a functional constraint")
    return ObjectRef(ld, ln, tuple(parts[2:]), fc)


def parse_ref(
    text: str, cwd: tuple[str, ...] = (), fc: str | None = None, known_lds: frozenset[str] | set[str] | None = None
) -> ObjectRef:
    """Parse any accepted reference form, relative to the shell location ``cwd``.

    Accepted: ``LD/LN.DO.DA``, ``LD/LN$FC$DO$DA``, ``/LD/LN/DO/DA``, relative ``DO.DA`` /
    ``DO/DA`` / ``../X``, each optionally followed by ``[FC]``. An explicit ``fc`` argument
    must agree with any FC in the text. ``known_lds`` (the device's logical devices) resolves
    whether ``X/Y`` is absolute (X is an LD) or relative to ``cwd``.
    """
    body, fc_suffix = split_fc(text)
    if not body:
        raise RefError("empty reference")
    found_fc = None
    if "$" in body and not body.startswith("/"):
        if "/" not in body:
            if len(cwd) < 1:
                raise RefError(f"{text!r}: MMS names need the logical device (LD/LN$FC$...)")
            body = f"{cwd[0]}/{body}"
        ref = parse_mms(body)
        found_fc = ref.fc
    else:
        ref = _from_parts(parse_path_components(body, cwd, known_lds), text)
    fcs = {f for f in (fc_suffix, found_fc, fc.upper() if fc else None) if f}
    if len(fcs) > 1:
        raise RefError(f"{text!r}: conflicting functional constraints {sorted(fcs)}")
    return ObjectRef(ref.ld, ref.ln, ref.path, next(iter(fcs), None))


def parse_path_components(
    body: str, cwd: tuple[str, ...], known_lds: frozenset[str] | set[str] | None = None
) -> list[str]:
    """Absolute component list for a path/IEC reference relative to ``cwd``."""
    if body.startswith("/"):
        return _normalise(re.split(r"[/.]", body.lstrip("/")))
    first = body.split("/")[0]
    if first not in (".", ".."):
        first = first.split(".")[0]
    is_absolute = False
    if "/" in body and first not in (".", ".."):
        if known_lds is not None:
            is_absolute = first in known_lds
        else:
            is_absolute = not cwd or "." in body.split("/", 1)[1]
    elif not cwd:
        is_absolute = True
    comps = _split_keep_dots(body)
    return _normalise(comps if is_absolute else list(cwd) + comps)


def _split_keep_dots(body: str) -> list[str]:
    comps: list[str] = []
    for chunk in body.split("/"):
        if chunk in (".", ".."):
            comps.append(chunk)
        else:
            comps.extend(c for c in chunk.split(".") if c)
    return comps


def _normalise(parts: list[str]) -> list[str]:
    out: list[str] = []
    for p in parts:
        if p in ("", "."):
            continue
        if p == "..":
            if out:
                out.pop()
            continue
        out.append(p)
    return out


def _from_parts(parts: list[str], original: str) -> ObjectRef:
    if not parts:
        raise RefError(f"{original!r} refers to the server root, not to an object")
    for p in parts:
        if not re.fullmatch(r"[A-Za-z0-9_()\-]+", p):
            raise RefError(f"{original!r}: invalid name component {p!r}")
    ld = parts[0]
    ln = parts[1] if len(parts) > 1 else None
    return ObjectRef(ld, ln, tuple(parts[2:]))


def parse_shell_path(
    text: str, cwd: tuple[str, ...], known_lds: frozenset[str] | set[str] | None = None
) -> tuple[str, ...]:
    """Resolve a ``cd``/``ls`` argument to an absolute path tuple (``()`` = root)."""
    body, _ = split_fc(text)
    if body in ("", "/"):
        return ()
    return tuple(parse_path_components(body, cwd, known_lds))


def ln_class_of(ln_name: str) -> str:
    """Best-effort LN class from an LN name (``Q0XCBR1`` → ``XCBR``, ``LLN0`` → ``LLN0``)."""
    if ln_name == "LLN0":
        return "LLN0"
    m = re.match(r"^(?:.*?)([A-Z]{4})(\d*)$", ln_name)
    return m.group(1) if m else ln_name
