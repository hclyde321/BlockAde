"""Text normalization helpers for transcript output."""

from __future__ import annotations

from opencc import OpenCC


_TRADITIONAL_TAIWAN = OpenCC("s2twp")
_TRADITIONAL_CHARACTERS = OpenCC("s2t")


def to_traditional_verbatim(text: str) -> str:
    """Convert script without the Taiwan vocabulary substitutions in s2twp."""
    return _TRADITIONAL_CHARACTERS.convert(text)


def to_traditional_chinese(text: str) -> str:
    """Convert Simplified Chinese to Taiwan Traditional Chinese.

    OpenCC leaves Latin text intact while applying Taiwan-specific wording
    conversions such as 软件 -> 軟體.
    """
    return _TRADITIONAL_TAIWAN.convert(text)
