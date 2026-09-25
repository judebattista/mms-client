"""Explanation system (SPEC §13): one-line hints for failures and ``explain`` texts.

Typical use::

    from ied_client.explain import HintContext, default_catalogue

    cat = default_catalogue()
    hint = cat.hint(err, HintContext(service="write", fc="ST"))   # err: codes.ErrorInfo
    if hint and not terse:
        print(hint.render())            # e.g. "Likely cause: …" / "Check: …" / a proved fact
    exp = cat.explain("XCBR")           # or "add-cause:10", "object-access-denied", "ctlModel", …
    print(render_explanation(exp))

The catalogue data lives in ``ied_client/data/hints.yaml`` (format documented at its top),
``hints/*.yaml``, ``glossary.yaml`` and ``glossary/*.yaml``, plus each protocol module's own files
(its error codes). Experiment-specific files can be layered on top with
``Catalogue.load(extra_paths=[...])``.
"""

from .catalogue import (
    CONTEXT_FIELDS,
    EDITIONS,
    KINDS,
    MODES,
    REQUIRED_CHECK_IDS,
    REQUIRED_TOOL_CODES,
    SERVICES,
    Catalogue,
    CatalogueError,
    Entry,
    bare_name_preference,
    default_catalogue,
    required_names,
)
from .model import Certainty, Explanation, Hint, HintContext
from .render import render_explanation, render_hint, render_search

__all__ = [
    "CONTEXT_FIELDS",
    "EDITIONS",
    "KINDS",
    "MODES",
    "REQUIRED_CHECK_IDS",
    "REQUIRED_TOOL_CODES",
    "SERVICES",
    "Catalogue",
    "CatalogueError",
    "Certainty",
    "Entry",
    "Explanation",
    "Hint",
    "HintContext",
    "bare_name_preference",
    "default_catalogue",
    "render_explanation",
    "render_hint",
    "render_search",
    "required_names",
]
