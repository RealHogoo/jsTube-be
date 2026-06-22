import re
from typing import Any


def normalize_search_text(value: Any) -> str:
    text = str(value or "").casefold()
    return re.sub(r"[\W_]+", "", text, flags=re.UNICODE)


def media_search_text(*values: Any) -> str:
    parts: list[str] = []
    for value in values:
        if isinstance(value, (list, tuple, set)):
            parts.extend(str(item or "") for item in value)
        else:
            parts.append(str(value or ""))
    raw_text = " ".join(part.strip() for part in parts if part and part.strip())
    normalized = normalize_search_text(raw_text)
    return f"{raw_text} {normalized}".strip()
