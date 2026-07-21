from datetime import datetime
from typing import Any

def time_to_minutes(value: Any) -> int | None:
    if value is None:
        return None

    text = str(value).strip()
    if not text:
        return None

    # Supabaseのtime型が HH:MM:SS で返ってきても扱えるようにする。
    text = text[:5]

    try:
        parsed = datetime.strptime(text, "%H:%M")
    except ValueError:
        return None

    return parsed.hour * 60 + parsed.minute

def minutes_to_time(value: float | int | None) -> str | None:
    if value is None:
        return None

    minutes = int(round(float(value)))
    minutes = max(0, min(minutes, 23 * 60 + 59))
    return f"{minutes // 60:02d}:{minutes % 60:02d}"

def weighted_average(values: list[tuple[float, float]]) -> float | None:
    valid = [(value, weight) for value, weight in values if weight > 0]
    if not valid:
        return None

    total_weight = sum(weight for _, weight in valid)
    if total_weight <= 0:
        return None

    return sum(value * weight for value, weight in valid) / total_weight

def weighted_quantile(
    values: list[tuple[float, float]],
    quantile: float,
) -> float | None:
    valid = sorted(
        (value, weight)
        for value, weight in values
        if weight > 0
    )

    if not valid:
        return None

    total_weight = sum(weight for _, weight in valid)
    threshold = total_weight * min(max(quantile, 0), 1)
    cumulative = 0.0

    for value, weight in valid:
        cumulative += weight
        if cumulative >= threshold:
            return value

    return valid[-1][0]
