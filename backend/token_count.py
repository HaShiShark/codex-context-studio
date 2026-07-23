from __future__ import annotations

import re
from functools import lru_cache
from typing import Any

from backend.text_safety import sanitize_text

try:
    import tiktoken
except ImportError:
    tiktoken = None


_TOKEN_ENCODING: Any | None = None
_TOKEN_ENCODING_LOAD_FAILED = False


def _token_encoding() -> Any | None:
    global _TOKEN_ENCODING, _TOKEN_ENCODING_LOAD_FAILED
    if _TOKEN_ENCODING is not None:
        return _TOKEN_ENCODING
    if _TOKEN_ENCODING_LOAD_FAILED or tiktoken is None:
        return None
    try:
        _TOKEN_ENCODING = tiktoken.get_encoding("cl100k_base")
    except Exception:
        _TOKEN_ENCODING_LOAD_FAILED = True
        return None
    return _TOKEN_ENCODING


def _fallback_count(text: str) -> int:
    compact = text.strip()
    ascii_tokens = re.findall(r"[A-Za-z0-9_]+", compact)
    non_ascii_chars = [char for char in compact if not char.isspace() and not char.isascii()]
    return max(1, len(ascii_tokens) + len(non_ascii_chars))


@lru_cache(maxsize=4096)
def _cached_count(text: str) -> int:
    encoding = _token_encoding()
    if encoding is not None:
        try:
            return len(encoding.encode(text))
        except Exception:
            pass
    return _fallback_count(text)


def estimate_token_count(text: Any) -> int:
    safe_text = sanitize_text(text)
    return _cached_count(safe_text) if safe_text.strip() else 0
