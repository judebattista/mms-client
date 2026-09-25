"""Plain-text rendering of explanations and hints (the CLI may later dress these up with rich)."""

from __future__ import annotations

import textwrap
from collections.abc import Sequence

from ied_client import codes

from .model import Explanation, Hint


def _code_label(key: str) -> str:
    """``data-access:object-access-denied (3)``; keys without a number are shown as they are."""
    try:
        return str(codes.parse_key(key))
    except (KeyError, ValueError):
        return key


def _wrap(text: str, width: int, indent: str) -> list[str]:
    """Wrap catalogue text: paragraphs are separated by blank lines, ``- `` lines are list items."""
    out: list[str] = []
    for paragraph in text.strip().split("\n\n"):
        for line in paragraph.splitlines():
            line = line.strip()
            bullet = line.startswith("- ")
            out.extend(
                textwrap.wrap(
                    line,
                    width=width,
                    initial_indent=indent,
                    subsequent_indent=indent + ("  " if bullet else ""),
                    break_long_words=False,
                    break_on_hyphens=False,
                )
            )
        out.append("")
    while out and not out[-1]:
        out.pop()
    return out


def render_explanation(exp: Explanation, width: int = 88) -> str:
    """Format an explanation as plain text: title, codes, hint, summary, details, next checks, related."""
    lines: list[str] = [f"{exp.title}  [{exp.id}]"]
    if exp.keys:
        lines.append("Codes: " + ", ".join(_code_label(k) for k in exp.keys))
    if exp.hint is not None:
        lines.append(f"Hint: {exp.hint.render()}")
    lines.append("")
    lines.append("Summary")
    lines.extend(_wrap(exp.summary, width, "  "))
    if exp.details:
        lines.append("")
        lines.append("Details")
        lines.extend(_wrap(exp.details, width, "  "))
    if exp.next_checks:
        lines.append("")
        lines.append("Next checks")
        for i, check in enumerate(exp.next_checks, 1):
            lines.extend(
                textwrap.wrap(
                    check,
                    width=width,
                    initial_indent=f"  {i}. ",
                    subsequent_indent=" " * (len(str(i)) + 4),
                    break_long_words=False,
                    break_on_hyphens=False,
                )
            )
    if exp.related:
        lines.append("")
        lines.extend(
            textwrap.wrap(
                ", ".join(exp.related),
                width=width,
                initial_indent="Related: ",
                subsequent_indent="         ",
                break_long_words=False,
                break_on_hyphens=False,
            )
        )
    return "\n".join(lines)


def render_hint(hint: Hint) -> str:
    """One line, with the certainty prefix (EXP-4)."""
    return hint.render()


def render_search(results: Sequence[Explanation], query: str) -> str:
    """A "no exact match" line followed by the closest entries, one per line."""
    if not results:
        return f"Nothing in the catalogue matches {query!r}. Try `explain` with an error code or a term."
    lines = [f"No exact match for {query!r}. Closest entries:"]
    for exp in results:
        lines.append(f"  {exp.id:<44} {exp.title}")
    return "\n".join(lines)


def render(obj: Explanation | Hint | Sequence[Explanation], width: int = 88) -> str:
    """Render an explanation, a hint, or a list of search results."""
    if isinstance(obj, Explanation):
        return render_explanation(obj, width)
    if isinstance(obj, Hint):
        return render_hint(obj)
    return "\n".join(f"{e.id}: {e.title}" for e in obj)
