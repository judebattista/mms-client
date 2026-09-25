"""Tab completion for the shell (CLI-3): command names, subcommands, options, and object
references from the model browsed on connect (path components after ``/`` and ``.``, and
``[FC]``)."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator

from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.document import Document

from ied_client.core.model import DeviceModel
from ied_client.core.refs import ObjectRef, RefError, parse_path_components

from . import registry

SUBCOMMANDS: dict[str, list[str]] = {
    "set": ["mode", "orcat", "terse", "oridentity"],
    "setgroup": ["show", "activate", "edit"],
}
SET_VALUES: dict[str, list[str]] = {
    "mode": ["standard", "expert"],
    "orcat": ["bay", "station", "remote", "1", "2", "3"],
    "terse": ["on", "off"],
}
# Commands whose arguments are not object references.
NO_REFERENCES = {"help", "explain", "set", "log", "export", "files ls", "files get", "inventory from-scd",
                 "inventory validate", "connect", "diff", "snapshot", "restore", "discover"}


def _absolute(model: DeviceModel, cwd: tuple[str, ...], body: str) -> tuple[str, ...]:
    if body in ("", "/"):
        return () if body == "/" else cwd
    return tuple(parse_path_components(body, cwd, model.ld_names))


def complete_reference(model: DeviceModel, cwd: tuple[str, ...], word: str) -> list[tuple[str, str, str]]:
    """Candidates for ``word`` as (replacement text, display, kind).

    ``word`` may be absolute (``/LD/LN/DO``), IEC style (``LD/LN.DO.DA``) or relative to ``cwd``
    (``DO.DA``, ``../LN``), and may end in ``[`` or ``[S`` to complete the functional constraint.
    """
    if "[" in word:
        body, _, partial = word.rpartition("[")
        partial = partial.rstrip("]").upper()
        try:
            path = _absolute(model, cwd, body.rstrip())
            if len(path) < 3:
                return []
            r = model.resolve(ObjectRef(path[0], path[1], tuple(path[2:])))
        except RefError:
            return []
        fcs = r.node.fcs if r.node is not None else []
        return [(f"{body}[{fc}]", fc, "FC") for fc in fcs if fc.startswith(partial)]
    idx = max(word.rfind("/"), word.rfind("."))
    head, partial = word[: idx + 1], word[idx + 1 :]
    try:
        if not head:
            parent = cwd
        elif head == "/":
            parent = ()
        else:
            parent = _absolute(model, cwd, head[:-1])
        children = model.children(parent)
    except RefError:
        return []
    out = []
    for name, kind in children:
        if name.startswith(partial):
            out.append((head + name, name, kind))
    return out


class ShellCompleter(Completer):
    """prompt_toolkit completer; ``get_model``/``get_cwd`` read the live session state."""

    def __init__(
        self,
        get_model: Callable[[], DeviceModel | None],
        get_cwd: Callable[[], tuple[str, ...]],
        vocabulary: Callable[[], Iterable[str]] | None = None,
    ) -> None:
        self.get_model = get_model
        self.get_cwd = get_cwd
        self.vocabulary = vocabulary

    def get_completions(self, document: Document, complete_event) -> Iterator[Completion]:
        text = document.text_before_cursor
        words = text.split()
        if text.endswith((" ", "\t")) or not text:
            cur, done = "", words
        else:
            cur, done = words[-1], words[:-1]
        for value, display, meta in self.candidates(done, cur):
            yield Completion(value, start_position=-len(cur), display=display, display_meta=meta)

    def candidates(self, done: list[str], cur: str) -> list[tuple[str, str, str]]:
        """Pure part of the completer (unit-tested without prompt_toolkit)."""
        if not done:
            return [(w, w, "command") for w in registry.top_level_words() if w.startswith(cur)]
        first = done[0]
        groups = registry.group_words()
        if len(done) == 1 and first in groups:
            return [(w, w, "subcommand") for w in groups[first] if w.startswith(cur)]
        if len(done) == 1 and first in SUBCOMMANDS:
            return [(w, w, "subcommand") for w in SUBCOMMANDS[first] if w.startswith(cur)]
        if first == "set" and len(done) == 2 and done[1] in SET_VALUES:
            return [(w, w, "value") for w in SET_VALUES[done[1]] if w.startswith(cur)]
        if first == "help":
            return [(w, w, "command") for w in registry.top_level_words() if w.startswith(cur)]
        cmd, _ = registry.resolve(done)
        if cur.startswith("-") and cmd is not None:
            opts = sorted(o for o in cmd.parser(oneshot=False)._option_string_actions if o.startswith("--"))
            return [(o, o, "option") for o in opts if o.startswith(cur)]
        if first == "explain":
            if not cur or self.vocabulary is None:
                return []
            words = [w for w in self.vocabulary() if w.lower().startswith(cur.lower()) and " " not in w]
            return [(w, w, "term") for w in words[:200]]
        if cmd is not None and cmd.name in NO_REFERENCES:
            return []
        model = self.get_model()
        if model is None:
            return []
        return complete_reference(model, self.get_cwd(), cur)
