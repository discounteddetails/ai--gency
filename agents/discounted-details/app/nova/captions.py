"""Caption rules enforced in code (NOVA_SPEC §3-4)."""

from __future__ import annotations

import re

BLOCKED = re.compile(r"\b(?:perfect(?:ly|ion)?|flawless(?:ly)?|guarantee[ds]?)\b|\b100\s?%", re.IGNORECASE)
PRICE = re.compile(r"\$\s?\d")
STREET = re.compile(r"\b\d{2,6}\s+(?:[NSEW]\.?\s+)?[A-Z][a-z]+(?:\s[A-Z][a-z]+)?\s+"
                    r"(?:St|Street|Rd|Road|Dr|Drive|Ln|Lane|Ave|Avenue|Blvd|Ct|Court|Way|Trl|Trail|Pkwy|Cir)\b")
HASHTAG = re.compile(r"#\w+")
MAX_HASHTAGS = 5
MAX_LEN = 2200


class CaptionRejected(Exception):
    pass


def check(caption: str) -> str:
    """Returns the cleaned caption, or raises CaptionRejected with a reason."""
    c = caption.strip()
    if m := BLOCKED.search(c):
        raise CaptionRejected(f"blocked word '{m.group(0)}'")
    if PRICE.search(c):
        raise CaptionRejected("prices aren't allowed in captions")
    if STREET.search(c):
        raise CaptionRejected("looks like a street address")
    tags = HASHTAG.findall(c)
    if len(tags) > MAX_HASHTAGS:  # keep the first five, drop the rest
        keep = set(tags[:MAX_HASHTAGS])
        seen: set[str] = set()

        def drop(m):
            t = m.group(0)
            if t in keep and t not in seen:
                seen.add(t)
                return t
            return ""

        c = re.sub(r"[ \t]{2,}", " ", HASHTAG.sub(drop, c)).strip()
    if len(c) > MAX_LEN:
        raise CaptionRejected("too long")
    return c
