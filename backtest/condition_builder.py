"""condition_builder.py ? Condition string parser for strategy screening."""

import re
from typing import List, Dict, Any


def parse_condition_str(condition_str: str) -> List[Dict[str, Any]]:
    """Parse a condition string into a list of filter dictionaries.

    Supports simple AND-joined conditions like:
        "drop_3d > 0.10 AND volume_ratio > 2.0"

    Returns a list of dicts with keys: field, op, value.
    """
    if not condition_str or not condition_str.strip():
        return []

    parts = condition_str.split("AND")
    result = []

    for part in parts:
        part = part.strip()
        if not part:
            continue

        # Numeric comparisons: field >= value, field > value, etc.
        m = re.match(r"(\w+)\s*(>=|<=|!=|==|>|<|=)\s*([\d.]+)", part)
        if m:
            field, op, value_str = m.groups()
            value = float(value_str) if "." in value_str else int(value_str)
            result.append({"field": field, "op": op, "value": value})
        else:
            # String equality: field == "value"
            m = re.match(r"(\w+)\s*(==|=)\s*\"([^\"]+)\"", part)
            if m:
                result.append({"field": m.group(1), "op": "==", "value": m.group(3)})
            else:
                result.append({"field": part, "op": "raw", "value": part})

    return result
