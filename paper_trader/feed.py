"""Load a local fixture file. This module does not open a socket or a URL."""

import os

from paper_trader.errors import DataError
from paper_trader.jsonutil import parse


def load_fixture(path):
    if not isinstance(path, str) or "://" in path:
        raise DataError("a fixture is a local file path")
    if not os.path.isfile(path):
        raise DataError("fixture not found")
    with open(path, "r", encoding="utf-8") as handle:
        text = handle.read()
    if not text.strip():
        raise DataError("fixture is empty")
    doc = parse(text, DataError)
    if not isinstance(doc, dict):
        raise DataError("fixture must be a JSON object")
    note = doc.get("note")
    label = doc.get("label")
    if not isinstance(note, str):
        raise DataError("fixture note must be a string")
    if label == "synthetic":
        if "SYNTHETIC" not in note.upper():
            raise DataError("a synthetic fixture note must say SYNTHETIC")
    elif label == "historical":
        if "NOT VERIFIED" not in note.upper():
            raise DataError(
                "a historical label is caller-supplied; the note must say NOT VERIFIED"
            )
    else:
        raise DataError("label must be synthetic or historical; forward is disabled")
    bars = doc.get("bars")
    if not isinstance(bars, list):
        raise DataError("fixture bars must be a list")
    # Unknown keys, including summary, are not bars and are not returned.
    return {"bars": bars, "label": label, "note": note}
