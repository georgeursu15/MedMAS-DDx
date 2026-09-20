

from __future__ import annotations

import json
from typing import Any



def extract_first_json_object(s: str) -> str | None:
 
    start = s.find("{")
    if start == -1:
        return None

    depth = 0
    in_string = False
    escape = False

    for i in range(start, len(s)):
        ch = s[i]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
        else:
            if ch == '"':
                in_string = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return s[start : i + 1]

    return None


def parse_json_safe(raw: str) -> dict[str, Any]:

    if not raw:
        raise ValueError("Empty JSON input")
    raw = raw.strip()

    if raw.startswith("```"):
        first_nl = raw.find("\n")
        if first_nl != -1:
            raw = raw[first_nl + 1:]
        if raw.endswith("```"):
            raw = raw[:-3]
        raw = raw.strip()

    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            return parsed
        raise ValueError("Top-level JSON is not an object")
    except json.JSONDecodeError:
        pass

    first_block = extract_first_json_object(raw)
    if first_block is not None:
        try:
            parsed = json.loads(first_block)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass

    raise ValueError(
        f"Cannot parse JSON from input (first 200 chars): {raw[:200]!r}"
    )
