"""The one diacritic fold every title gate and search spelling shares.

Release names are ASCII: "Duong.Ve.Nha", "Skam.Ost", "Slepnac.od.swiatel".
Unicode NFKD strips combining accents (é → e, ệ → e) but leaves a handful of
letters that are letters in their own right, not accented forms. Vietnamese
Đ/đ is the common one — every Vietnamese title starting with Đ was invisible
to the title gates — and Polish ł, Nordic ø/æ, Icelandic þ/ð, Turkish ı and
German ß are the others. Folding them to their conventional ASCII spelling is
what makes a correctly named release match its title at all.
"""

from __future__ import annotations

import unicodedata

_UNDECOMPOSED = str.maketrans({
    "Đ": "D", "đ": "d", "Ð": "D", "ð": "d",
    "Ł": "L", "ł": "l",
    "Ø": "O", "ø": "o",
    "Þ": "Th", "þ": "th",
    "Æ": "Ae", "æ": "ae", "Œ": "Oe", "œ": "oe",
    "ẞ": "SS", "ß": "ss",
    "ı": "i", "Ħ": "H", "ħ": "h", "Ŧ": "T", "ŧ": "t",
})


def strip_marks(value: str) -> str:
    """Remove diacritics and spell out undecomposed letters; case is kept."""
    value = unicodedata.normalize("NFKD", value or "")
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    return value.translate(_UNDECOMPOSED)


def fold(value: str) -> str:
    """Case- and diacritic-insensitive form for comparing titles."""
    return strip_marks(value).casefold()
