"""Load a local fixture. Reads stay inside fixtures/ or demo-state/."""

from paper_trader.errors import DataError
from paper_trader.jsonutil import parse
from paper_trader.paths import confine_existing, read_text


def load_fixture(path, root="fixtures"):
    if not isinstance(path, str) or "://" in path:
        raise DataError("a fixture is a local file path")
    leaf = confine_existing(path, root)
    text = read_text(leaf)
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
        raise DataError("label must be synthetic or historical")
    bars = doc.get("bars")
    if not isinstance(bars, list):
        raise DataError("fixture bars must be a list")
    return {"bars": bars, "label": label, "note": note}
