from datetime import date, datetime, timedelta
from typing import Any
from utils import time_to_minutes, minutes_to_time, weighted_average, weighted_quantile

def normalize_weather(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""

    if "雨" in text:
        return "雨"
    if "雪" in text:
        return "雪"
    if "曇" in text or "くも" in text:
        return "曇"
    if "晴" in text:
        return "晴"

    return text

def crowd_score_from_label(value: Any) -> int | None:
    text = str(value or "").strip()
    if not text:
        return None

    mappings = [
        ("閑散", 20),
        ("空", 25),
        ("やや空", 35),
        ("普通", 50),
        ("やや混", 65),
        ("混雑", 80),
        ("非常に混", 95),
    ]

    for keyword, score in mappings:
        if keyword in text:
            return score

    try:
        return int(float(text))
    except ValueError:
        return None

def crowd_label_from_score(score: float | None) -> str:
    if score is None:
        return "データ不足"
    if score < 30:
        return "空いている"
    if score < 45:
        return "やや空いている"
    if score < 60:
        return "普通"
    if score < 75:
        return "やや混雑"
    if score < 90:
        return "混雑"
    return "非常に混雑"

def similarity_weight(
    row: dict[str, Any],
    target_dt: datetime,
    day_info: dict[str, Any],
) -> float:
    visit_date = row.get("visit_date")
    if not visit_date:
        return 0.0

    try:
        history_dt = datetime.strptime(str(visit_date), "%Y-%m-%d")
    except ValueError:
        return 0.0

    weight = 1.0

    # 曜日一致を最も強く評価する。
    if history_dt.weekday() == target_dt.weekday():
        weight *= 4.0
    elif (history_dt.weekday() >= 5) == (target_dt.weekday() >= 5):
        weight *= 1.8
    else:
        weight *= 0.75

    target_weather = normalize_weather(day_info.get("weather"))
    history_weather = normalize_weather(row.get("weather"))
    if target_weather and history_weather:
        weight *= 1.8 if target_weather == history_weather else 0.8

    target_price = day_info.get("ticket_price")
    history_price = row.get("ticket_price")
    try:
        if target_price is not None and history_price is not None:
            difference = abs(float(target_price) - float(history_price))
            weight *= max(0.55, 1.7 - difference / 2500)
    except (TypeError, ValueError):
        pass

    target_open = time_to_minutes(day_info.get("official_open_time"))
    history_open = time_to_minutes(row.get("official_open_time"))
    if target_open is not None and history_open is not None:
        difference = abs(target_open - history_open)
        weight *= max(0.6, 1.5 - difference / 180)

    # 遠すぎる過去データを少しだけ弱める。
    age_days = abs((target_dt.date() - history_dt.date()).days)
    weight *= max(0.65, 1.0 - age_days / 2500)

    return max(weight, 0.01)

def build_attraction_prediction(
    history_rows: list[dict[str, Any]],
    weighted_rows: list[tuple[dict[str, Any], float]],
    entry_minutes: int,
    code: str,
    name: str,
    sellout_field: str,
    limit_field: str,
) -> dict[str, Any]:
    availability_values: list[tuple[float, float]] = []
    sellout_values: list[tuple[float, float]] = []
    limit_weight = 0.0
    known_weight = 0.0

    for row, weight in weighted_rows:
        sellout_minutes = time_to_minutes(row.get(sellout_field))
        is_limit = bool(row.get(limit_field))

        if is_limit:
            availability_values.append((1.0, weight))
            limit_weight += weight
            known_weight += weight
            continue

        if sellout_minutes is None:
            # 売り切れ時刻が欠損している行は確率計算から除外する。
            continue

        availability_values.append(
            (1.0 if sellout_minutes >= entry_minutes else 0.0, weight)
        )
        sellout_values.append((float(sellout_minutes), weight))
        known_weight += weight

    probability_average = weighted_average(availability_values)
    probability = (
        int(round(probability_average * 100))
        if probability_average is not None
        else 0
    )

    predicted_minutes = weighted_quantile(sellout_values, 0.50)
    confidence_low = weighted_quantile(sellout_values, 0.20)
    confidence_high = weighted_quantile(sellout_values, 0.80)

    limit_ratio = (
        limit_weight / known_weight
        if known_weight > 0
        else 0.0
    )

    if limit_ratio >= 0.55:
        predicted_sellout_time = "記録上限まで残る予測"
        high_text = "記録上限"
    else:
        predicted_sellout_time = minutes_to_time(predicted_minutes)
        high_text = minutes_to_time(confidence_high)

    return {
        "attraction_code": code,
        "name": name,
        "acquisition_probability": probability,
        "predicted_sellout_time": predicted_sellout_time,
        "confidence_low": minutes_to_time(confidence_low),
        "confidence_high": high_text,
        "sample_count": len(availability_values),
    }

def calculate_prediction_confidence(
    attractions: list[dict[str, Any]],
    selected_count: int,
    history_count: int,
    used_condition_count: int,
    learning_applied: bool,
) -> dict[str, Any]:
    """予測材料の量と売切れ時刻レンジから、説明用の信頼度を算出する。"""
    sample_scores = []
    range_scores = []

    for item in attractions:
        sample_count = int(item.get("sample_count") or 0)
        sample_scores.append(min(100.0, sample_count / 80.0 * 100.0))

        low = time_to_minutes(item.get("confidence_low"))
        high = time_to_minutes(item.get("confidence_high"))
        if low is not None and high is not None and high >= low:
            width = high - low
            # 60分以内は高評価、6時間以上は低評価。
            range_scores.append(max(0.0, min(100.0, 115.0 - width / 3.0)))

    sample_score = sum(sample_scores) / len(sample_scores) if sample_scores else 0.0
    range_score = sum(range_scores) / len(range_scores) if range_scores else 45.0
    selected_score = min(100.0, selected_count / 100.0 * 100.0)
    history_score = min(100.0, history_count / 300.0 * 100.0)
    condition_score = min(100.0, used_condition_count / 3.0 * 100.0)
    learning_bonus = 5.0 if learning_applied else 0.0

    score = round(
        sample_score * 0.30
        + range_score * 0.25
        + selected_score * 0.20
        + history_score * 0.15
        + condition_score * 0.10
        + learning_bonus
    )
    score = max(1, min(99, score))

    if score >= 85:
        label, stars = "高い", 5
    elif score >= 70:
        label, stars = "やや高い", 4
    elif score >= 55:
        label, stars = "標準", 3
    elif score >= 40:
        label, stars = "やや低い", 2
    else:
        label, stars = "低い", 1

    return {
        "score": score,
        "label": label,
        "stars": stars,
        "stars_text": "★" * stars + "☆" * (5 - stars),
        "components": {
            "sample_score": round(sample_score),
            "sellout_range_score": round(range_score),
            "selected_history_score": round(selected_score),
            "total_history_score": round(history_score),
            "condition_score": round(condition_score),
            "learning_bonus": int(learning_bonus),
        },
    }
