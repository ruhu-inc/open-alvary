"""Opaque identifiers: a type prefix and 26 Crockford base-32 characters of randomness."""

import re
import secrets

ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
PREFIXES = ("acc", "act", "idn", "org", "mem", "inv", "ent", "wrq", "grt", "evt", "wat", "inb", "dig", "whe", "whd")
PATTERN = re.compile(r"^(?:" + "|".join(PREFIXES) + r")_[0-9A-HJKMNP-TV-Z]{26}$")


def new_id(prefix: str) -> str:
    if prefix not in PREFIXES:
        raise ValueError(f"Unknown identifier prefix: {prefix}")
    value = secrets.randbits(130)
    return prefix + "_" + "".join(ALPHABET[(value >> (5 * i)) & 31] for i in range(26))


def valid(value: str, prefix: str) -> bool:
    return isinstance(value, str) and value.startswith(prefix + "_") and bool(PATTERN.match(value))
