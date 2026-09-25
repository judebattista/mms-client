"""MMS names of model objects (IEC 61850-8-1): ``LD/LN$FC$DO$DA``, datasets ``LD/LN$DS``, control blocks
``LD/LN$FC$name``. Implements the client's :class:`~ied_client.protocol.api.Naming`."""

from __future__ import annotations

from ied_client.codes import FCS
from ied_client.core.refs import ObjectRef, RefError
from ied_client.protocol.types import DatasetRef


def mms_item(ref: ObjectRef, fc: str | None = None) -> str:
    """MMS item name within the domain (``LN$FC$DO$DA``); needs an FC for data below the LN."""
    if ref.ln is None:
        raise RefError("a logical device has no MMS item name")
    fc = fc or ref.fc
    if not ref.path:
        return ref.ln if fc is None else f"{ref.ln}${fc}"
    if fc is None:
        raise RefError(f"{ref.iec()}: a functional constraint is needed")
    return "$".join((ref.ln, fc, *ref.path))


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
    if fc not in FCS:
        raise RefError(f"{text!r}: {parts[1]!r} is not a functional constraint")
    return ObjectRef(ld, ln, tuple(parts[2:]), fc)


def dataset_item(ds: DatasetRef) -> str:
    """The dataset's named-variable-list name within its domain (``LLN0$Events``)."""
    return f"{ds.ln}${ds.name}" if ds.ln else ds.name


def dataset_from_item(ld: str, item: str) -> DatasetRef:
    ln, sep, name = item.partition("$")
    return DatasetRef(ld, ln, name) if sep else DatasetRef(ld, "", item)


class MmsNaming:
    key = "mms"
    label = "MMS name"

    def looks_native(self, text: str) -> bool:
        return "$" in text and not text.startswith("/")

    def data(self, ref: ObjectRef) -> str:
        if ref.ln is None:
            return ref.ld
        return f"{ref.ld}/{mms_item(ref)}"

    def parse_data(self, text: str, default_ld: str | None = None) -> ObjectRef:
        if "/" not in text:
            if default_ld is None:
                raise RefError(f"{text!r}: MMS names need the logical device (LD/LN$FC$...)")
            text = f"{default_ld}/{text}"
        return parse_mms(text)

    def dataset(self, ds: DatasetRef) -> str:
        return f"{ds.ld}/{dataset_item(ds)}"

    def parse_dataset(self, text: str, default_ld: str | None = None) -> DatasetRef:
        """``LD/LLN0$Events``, ``LD/LLN0.Events``, or ``LLN0$Events`` with ``default_ld``."""
        ld, sep, rest = text.partition("/")
        if not sep:
            if default_ld is None:
                raise RefError(f"{text!r}: expected LD/LN.dataset")
            ld, rest = default_ld, text
        return dataset_from_item(ld, rest.replace(".", "$"))

    def control_block(self, ld: str, ln: str, fc: str, name: str) -> str:
        return f"{ld}/{ln}${fc}${name}"

    def control_block_to_iec(self, text: str) -> str:
        return text.replace("$", ".")
