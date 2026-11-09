"""JSON helpers. Local text only; this module does not open sockets."""

import json


def parse(text, error_cls):
    """Parse one JSON value. Duplicate keys and NaN/Infinity fail closed."""

    def pairs(items):
        out = {}
        for key, value in items:
            if key in out:
                raise error_cls("duplicate key: %s" % key)
            out[key] = value
        return out

    def const(name):
        raise error_cls("unsupported JSON constant: %s" % name)

    try:
        return json.loads(text, object_pairs_hook=pairs, parse_constant=const)
    except error_cls:
        raise
    except json.JSONDecodeError as exc:
        raise error_cls("malformed JSON: %s" % exc.msg)


def canonical(obj):
    """Stable bytes for fingerprints. Key order is not part of the meaning."""
    return json.dumps(
        obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def pretty(obj):
    return json.dumps(
        obj, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False
    ) + "\n"
