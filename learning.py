from datetime import date, datetime
import re
from typing import Any
from db import supabase_get, supabase_patch, supabase_upsert, supabase_write_enabled
from utils import time_to_minutes, minutes_to_time

def _time_text_to_minutes(value: Any) -> int | None:
    return time_to_minutes(value) if value and re.fullmatch(r"\d{1,2}:\d{2}", str(value)) else None

def apply_learning_calibration(attractions: list[dict[str, Any]], logs: list[dict[str, Any]]) -> dict[str, Any]:
    summary: dict[str, Any] = {"evaluated_count": len(logs), "applied": False}
    if len(logs) < 5:
        return summary
    for item in attractions:
        code = item["attraction_code"]
        time_errors = []
        probability_errors = []
        for log in logs:
            predicted = _time_text_to_minutes(log.get(f"{code}_predicted_sellout_time"))
            actual = _time_text_to_minutes(log.get(f"{code}_actual_sellout_time"))
            if predicted is not None and actual is not None:
                time_errors.append(actual - predicted)
            pred_prob = log.get(f"{code}_predicted_probability")
            actual_available = log.get(f"{code}_actual_available")
            if pred_prob is not None and actual_available is not None:
                probability_errors.append(float(actual_available) * 100 - float(pred_prob))
        if time_errors and re.fullmatch(r"\d{2}:\d{2}", str(item.get("predicted_sellout_time") or "")):
            bias = max(-90, min(90, round(sum(time_errors) / len(time_errors))))
            current = time_to_minutes(item["predicted_sellout_time"])
            item["predicted_sellout_time_raw"] = item["predicted_sellout_time"]
            item["predicted_sellout_time"] = minutes_to_time((current or 0) + bias)
            item["learning_time_adjustment_minutes"] = bias
            summary["applied"] = True
        if probability_errors:
            bias = max(-15, min(15, round(sum(probability_errors) / len(probability_errors))))
            item["acquisition_probability_raw"] = item["acquisition_probability"]
            item["acquisition_probability"] = max(1, min(99, item["acquisition_probability"] + bias))
            item["learning_probability_adjustment"] = bias
            summary["applied"] = True
    return summary

def sync_prediction_results(history_rows: list[dict[str, Any]]) -> int:
    """過去予測に実績を紐付け、次回以降の補正データにする。"""
    if not supabase_write_enabled():
        return 0
    logs = supabase_get("prediction_logs", {
        "select": "*", "target_date": f"lt.{date.today().isoformat()}",
        "evaluated_at": "is.null", "limit": "200"
    })
    history = {str(row.get("visit_date")): row for row in history_rows if row.get("visit_date")}
    count = 0
    for log in logs:
        row = history.get(str(log.get("target_date")))
        if not row:
            continue
        entry = time_to_minutes(log.get("entry_time")) or 600
        values: dict[str, Any] = {"evaluated_at": datetime.utcnow().isoformat() + "Z"}
        for code in ("beauty", "baymax", "splash"):
            actual_text = row.get(f"{code}_sellout_time")
            actual_minutes = time_to_minutes(actual_text)
            is_limit = bool(row.get(f"{code}_is_limit"))
            values[f"{code}_actual_sellout_time"] = actual_text
            values[f"{code}_actual_available"] = is_limit or (actual_minutes is not None and actual_minutes >= entry)
        supabase_patch("prediction_logs", {"id": f"eq.{log['id']}"}, values)
        count += 1
    return count

def save_prediction_log(payload: dict[str, Any]) -> None:
    if not supabase_write_enabled():
        return
    attractions = {item["attraction_code"]: item for item in payload.get("attractions", [])}
    row: dict[str, Any] = {
        "target_date": payload["date"], "entry_time": payload["entry_time"],
        "crowd_score": payload.get("crowd_score"), "ticket_price": payload.get("ticket_price"),
        "official_open_time": payload.get("official_open_time"), "weather": payload.get("weather"),
        "model_version": "similar-history-v2-auto-learning",
        "prediction_payload": payload,
        "predicted_at": datetime.utcnow().isoformat() + "Z",
    }
    for code in ("beauty", "baymax", "splash"):
        item = attractions.get(code, {})
        row[f"{code}_predicted_probability"] = item.get("acquisition_probability")
        value = item.get("predicted_sellout_time")
        row[f"{code}_predicted_sellout_time"] = value if isinstance(value, str) and re.fullmatch(r"\d{2}:\d{2}", value) else None
    supabase_upsert("prediction_logs", [row], "target_date,entry_time,model_version")
