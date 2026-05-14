"""Slugify utility: turn a free-text title into a URL-safe slug.

Used by the doc-publishing pipeline. The current implementation has a known
bug: it leaves leading and trailing hyphens behind when the input begins or
ends with non-alphanumeric characters. Example:

    >>> slugify("  Hello, World!  ")
    '-hello-world-'

The expected behavior is `'hello-world'` (no surrounding hyphens).
"""

from __future__ import annotations

import re


def slugify(text: str) -> str:
    """Turn a string into a URL-safe slug.

    - Lowercase.
    - Non-alphanumeric runs collapse to a single hyphen.
    - Leading/trailing hyphens are removed.

    Returns an empty string when the input has no alphanumeric characters.
    """
    text = text.lower()
    # BUG: this doesn't strip leading/trailing hyphens after collapsing.
    return re.sub(r"[^a-z0-9]+", "-", text)


if __name__ == "__main__":
    samples = ["Hello, World!", "  spaces around  ", "---weird-input---"]
    for s in samples:
        print(f"{s!r} -> {slugify(s)!r}")
