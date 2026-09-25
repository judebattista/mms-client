"""Text output with rich: plain lines, tables and the small formatting helpers the commands share.

Markup is switched off console-wide: object references such as ``LD/LN.DO [ST]`` would otherwise
be read as rich markup. Styling is applied explicitly with :class:`rich.text.Text`.
"""

from __future__ import annotations

import datetime as _dt
import sys
from collections.abc import Iterable, Sequence
from typing import IO, Any

from rich.console import Console
from rich.table import Table
from rich.text import Text

from ied_client import codes
from ied_client.protocol.types import BitString, UtcTime, format_value, value_from_json

STYLE_ERROR = "bold red"
STYLE_WARN = "bold yellow"
STYLE_OK = "green"
STYLE_NOTE = "dim"
STYLE_REF = "cyan"
STYLE_HEAD = "bold"
STYLE_EXPERT = "bold white on red"

# Enumerations worth showing by name next to the raw value (CLI-7: raw value always shown too).
BEH = {1: "on", 2: "on-blocked", 3: "test", 4: "test/blocked", 5: "off"}
HEALTH = {1: "ok", 2: "warning", 3: "alarm"}
DBPOS = {0: "intermediate-state", 1: "off (open)", 2: "on (closed)", 3: "bad-state"}


class Output:
    """Where text goes. Thin wrapper over a rich Console with a few house styles."""

    def __init__(self, console: Console) -> None:
        self.console = console

    @classmethod
    def for_stream(cls, file: IO[str] | None = None, *, width: int | None = None) -> Output:
        f = file or sys.stdout
        try:
            tty = f.isatty()
        except (AttributeError, ValueError):
            tty = False
        console = Console(
            file=f,
            markup=False,
            highlight=False,
            emoji=False,
            width=width or (None if tty else 200),
            force_terminal=None if tty else False,
        )
        return cls(console)

    @property
    def is_terminal(self) -> bool:
        return self.console.is_terminal

    # ------------------------------------------------------------------ lines
    def print(self, *objs: Any, style: str | None = None) -> None:
        self.console.print(*objs, style=style)

    def line(self, text: str = "", style: str | None = None) -> None:
        self.console.print(Text(text, style=style or ""))

    def parts(self, *parts: str | tuple[str, str]) -> None:
        self.console.print(Text.assemble(*parts))

    def heading(self, text: str) -> None:
        self.console.print(Text(text, style=STYLE_HEAD))

    def ok(self, text: str) -> None:
        self.console.print(Text(text, style=STYLE_OK))

    def warn(self, text: str) -> None:
        self.console.print(Text.assemble(("Warning: ", STYLE_WARN), text))

    def error(self, text: str) -> None:
        self.console.print(Text.assemble(("Error: ", STYLE_ERROR), text))

    def note(self, text: str) -> None:
        self.console.print(Text(text, style=STYLE_NOTE))

    def block(self, text: str) -> None:
        """Multi-line text from the core; lines with WARNING are highlighted (CTL-7)."""
        for ln in text.splitlines():
            if "WARNING" in ln:
                self.console.print(Text(ln, style=STYLE_WARN))
            else:
                self.console.print(Text(ln))

    # ------------------------------------------------------------------ tables
    def table(
        self,
        columns: Sequence[str],
        rows: Iterable[Sequence[Any]],
        *,
        title: str | None = None,
        styles: Sequence[str | None] | None = None,
    ) -> None:
        t = Table(title=title, title_justify="left", show_lines=False, header_style=STYLE_HEAD, pad_edge=False)
        for i, c in enumerate(columns):
            t.add_column(c, style=(styles[i] if styles and i < len(styles) else None) or "", overflow="fold")
        n = 0
        for r in rows:
            t.add_row(*[cell if isinstance(cell, Text) else Text("" if cell is None else str(cell)) for cell in r])
            n += 1
        if n == 0:
            self.note(f"({title + ': ' if title else ''}nothing to show)")
            return
        self.console.print(t)

    def kv(self, pairs: Iterable[tuple[str, Any]], *, title: str | None = None) -> None:
        t = Table(title=title, title_justify="left", show_header=False, box=None, pad_edge=False, padding=(0, 2, 0, 0))
        t.add_column(style=STYLE_HEAD, no_wrap=True)
        t.add_column(overflow="fold")
        for k, v in pairs:
            t.add_row(Text(k), v if isinstance(v, Text) else Text("" if v is None else str(v)))
        self.console.print(t)


# ---------------------------------------------------------------------------------- formatting
def ref_label(ref: str, fc: str | None = None) -> str:
    """``LD/LN.DO.DA [FC]`` — the exact reference and FC (CLI-7)."""
    return f"{ref} [{fc}]" if fc else ref


def code_label(error: Any) -> str:
    """``data-access:object-access-denied (3)``."""
    if error is None:
        return ""
    if isinstance(error, dict):
        key = f"{error.get('domain')}:{error.get('name')}"
        return f"{key} ({error['code']})" if error.get("code") is not None else key
    return str(error)


def fmt(v: Any, max_len: int = 200) -> str:
    return format_value(v, max_len)


def fmt_json_value(j: Any, max_len: int = 200) -> str:
    """Format a value that is in its JSON form (``value_to_json``)."""
    return format_value(value_from_json(j), max_len)


def meaning(path: Sequence[str], value: Any) -> str | None:
    """Plain name of well-known enumerations (ctlModel, Mod/Beh, Health, orCat, Dbpos)."""
    if not path:
        return None
    last = path[-1]
    parent = path[-2] if len(path) >= 2 else ""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        if last == "ctlModel":
            return codes.CTL_MODELS.get(value)
        if last == "orCat":
            return codes.OR_CATS.get(value)
        if last == "stVal" and parent in ("Mod", "Beh"):
            return BEH.get(value)
        if last == "stVal" and parent in ("Health", "PhyHealth"):
            return HEALTH.get(value)
    if isinstance(value, BitString) and value.size == 2 and last == "stVal":
        return DBPOS.get(value.as_int_msb0())
    return None


def value_text(path: Sequence[str], value: Any, max_len: int = 200) -> str:
    s = fmt(value, max_len)
    m = meaning(path, value)
    return f"{s} ({m})" if m else s


def quality_text(q: dict[str, Any] | None) -> str:
    """One line from ``readwrite.describe_quality`` output."""
    if not q:
        return ""
    parts = [q.get("validity", "?")]
    if q.get("detail"):
        parts.append("(" + ", ".join(q["detail"]) + ")")
    if q.get("source") == "substituted":
        parts.append("substituted")
    if q.get("test"):
        parts.append("TEST")
    if q.get("operatorBlocked"):
        parts.append("operator-blocked")
    return " ".join(parts) + f"  [bits {q.get('bits')}]"


def time_text(t: UtcTime | None) -> str:
    return str(t) if t is not None else ""


def clock(ts: float) -> str:
    """Local wall-clock time with milliseconds."""
    d = _dt.datetime.fromtimestamp(ts)
    return d.strftime("%H:%M:%S.") + f"{d.microsecond // 1000:03d}"


def ms(seconds: float | None) -> str:
    return "" if seconds is None else f"{seconds * 1000:.1f} ms"


def yes_no(v: Any) -> str:
    if v is None:
        return "-"
    return "yes" if v else "no"


def flatten(value: Any, prefix: tuple[str, ...] = ()) -> list[tuple[tuple[str, ...], Any]]:
    """Leaves of a decoded structure as (path, value)."""
    if isinstance(value, dict):
        out: list[tuple[tuple[str, ...], Any]] = []
        for k, v in value.items():
            out.extend(flatten(v, (*prefix, k)))
        return out
    return [(prefix, value)]
