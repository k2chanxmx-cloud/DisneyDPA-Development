import html
import os
import re
import time
from datetime import date, datetime, timedelta
from typing import Any

import requests
from flask import Flask, jsonify, render_template, request

app = Flask(__name__)

SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_ANON_KEY = os.getenv("SUPABASE_ANON_KEY", "")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
REQUEST_TIMEOUT = 15
YOSOCAL_URL = "https://yosocal.com/"
YOSOCAL_CACHE_SECONDS = 60 * 60 * 6
OFFICIAL_CACHE_SECONDS = 60 * 60 * 3
_yosocal_cache: dict[str, tuple[float, dict[str, Any] | None]] = {}
_yosocal_calendar_cache: dict[str, tuple[float, dict[str, Any] | None]] = {}
_official_calendar_cache: dict[str, tuple[float, dict[str, Any] | None]] = {}


def supabase_enabled() -> bool:
    return bool(SUPABASE_URL and SUPABASE_ANON_KEY)


def supabase_get(
    table: str,
    params: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    if not supabase_enabled():
        return []

    headers = {
        "apikey": SUPABASE_ANON_KEY,
        "Authorization": f"Bearer {SUPABASE_ANON_KEY}",
    }

    url = f"{SUPABASE_URL}/rest/v1/{table}"

    response = requests.get(
        url,
        headers=headers,
        params=params or {},
        timeout=REQUEST_TIMEOUT,
    )

    response.raise_for_status()
    return response.json()


def _supabase_write_key() -> str:
    return SUPABASE_SERVICE_ROLE_KEY or SUPABASE_ANON_KEY


def supabase_write_enabled() -> bool:
    return bool(SUPABASE_URL and _supabase_write_key())


def supabase_upsert(table: str, rows: list[dict[str, Any]], on_conflict: str) -> None:
    if not rows or not supabase_write_enabled():
        return
    key = _supabase_write_key()
    response = requests.post(
        f"{SUPABASE_URL}/rest/v1/{table}",
        headers={
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Prefer": "resolution=merge-duplicates,return=minimal",
        },
        params={"on_conflict": on_conflict},
        json=rows,
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()


def supabase_patch(table: str, filters: dict[str, str], values: dict[str, Any]) -> None:
    if not values or not supabase_write_enabled():
        return
    key = _supabase_write_key()
    response = requests.patch(
        f"{SUPABASE_URL}/rest/v1/{table}",
        headers={
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Prefer": "return=minimal",
        },
        params=filters,
        json=values,
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()


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


def _decode_yosocal_response(response: requests.Response) -> str:
    """YosocalのShift_JIS系レスポンスを文字化けせずに読み込む。"""
    for encoding in ("cp932", "shift_jis", response.apparent_encoding, response.encoding, "utf-8"):
        if not encoding:
            continue
        try:
            return response.content.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    return response.content.decode("cp932", errors="replace")


def _to_float(value: Any) -> float | None:
    text = str(value or "").strip()
    if not text or text in {"-", "--", "null", "None"}:
        return None
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    if not match:
        return None
    try:
        return float(match.group(0))
    except ValueError:
        return None


def _weather_from_forecast_code(code: Any) -> str:
    """Yosocalが表示に使う wXXX.gif のコードを大分類へ変換する。"""
    text = str(code or "").strip().lower()
    text = re.sub(r"^w|\.gif$", "", text)
    match = re.search(r"\d{3}", text)
    if not match:
        return ""
    group = int(match.group(0)) // 100
    return {1: "晴", 2: "曇", 3: "雨", 4: "雪", 5: "雨", 6: "曇"}.get(group, "")


def _weather_from_actual_row(columns: list[str]) -> tuple[str, str]:
    """Yosocal本体と同じ条件分岐で過去天気を判定する。"""
    value4 = _to_float(columns[4] if len(columns) > 4 else None)
    value7 = _to_float(columns[7] if len(columns) > 7 else None)
    if value4 is None:
        return "", ""
    if value4 < 3:
        return ("晴", "100") if value7 is not None and value7 >= 3 else ("曇", "200")
    if value4 < 5:
        return "曇", "600"
    return "雨", "300"


def _parse_yosocal_records(payload: str) -> list[list[str]]:
    """logwh*.xmlの独自形式（バックスラッシュ区切り）を配列へ変換する。"""
    payload = payload.replace("\r", "").replace("\n", "")
    records: list[list[str]] = []
    for raw_record in payload.split("\\"):
        raw_record = raw_record.strip().strip("\ufeff")
        if not raw_record:
            continue
        columns = [column.strip().strip('"') for column in raw_record.split(",")]
        if columns and re.fullmatch(r"\d{8}", columns[0]):
            records.append(columns)
    return records


def _build_yosocal_weather(columns: list[str], requested_date: date, source_date: date, source_file: str) -> dict[str, Any] | None:
    if len(columns) < 5:
        return None
    row_type = columns[1].strip()
    high = _to_float(columns[2] if len(columns) > 2 else None)
    low = _to_float(columns[3] if len(columns) > 3 else None)
    precipitation = _to_float(columns[5] if len(columns) > 5 else None)
    if row_type == "0":
        weather, weather_code = _weather_from_actual_row(columns)
    else:
        weather_code = str(columns[4]).strip()
        weather = _weather_from_forecast_code(weather_code)
    if not weather:
        return None
    if source_date != requested_date:
        reference_type, reference_label = "previous_year", "前年同日の参考天気"
    elif row_type == "0" or requested_date < date.today():
        reference_type, reference_label = "actual", "当日の実績天気"
    else:
        reference_type, reference_label = "forecast", "天気予報"
    return {
        "weather": weather,
        "temperature_high": high,
        "temperature_low": low,
        "precipitation_probability": precipitation if row_type != "0" else None,
        "weather_code": weather_code,
        "source": "Yosocal",
        "source_url": f"{YOSOCAL_URL}{source_file}",
        "source_file": source_file,
        "source_date": source_date.isoformat(),
        "reference_type": reference_type,
        "reference_label": reference_label,
    }


def _yosocal_file_candidates(target_dt: datetime) -> list[str]:
    current_year = date.today().year
    candidates = ["logwh.xml"]
    if target_dt.year < current_year:
        candidates.append(f"logwh{target_dt.year}.xml")
    if target_dt.year - 1 < current_year:
        candidates.append(f"logwh{target_dt.year - 1}.xml")
    return list(dict.fromkeys(candidates))


def fetch_yosocal_weather(target_dt: datetime) -> dict[str, Any] | None:
    """Yosocalのlogwh*.xmlを読み、なければ前年同日のデータを利用する。"""
    cache_key = target_dt.strftime("%Y-%m-%d")
    cached = _yosocal_cache.get(cache_key)
    now = time.time()
    if cached and now - cached[0] < YOSOCAL_CACHE_SECONDS:
        return cached[1]

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/150 Safari/537.36",
        "Accept": "text/plain,application/xml,text/xml,*/*",
        "Accept-Language": "ja-JP,ja;q=0.9",
        "Referer": YOSOCAL_URL,
        "Cache-Control": "no-cache",
    }
    target_date = target_dt.date()
    previous_year_date = date(target_date.year - 1, target_date.month, target_date.day)
    target_key = target_date.strftime("%Y%m%d")
    previous_key = previous_year_date.strftime("%Y%m%d")
    records_by_date: dict[str, tuple[list[str], str]] = {}
    last_error: requests.RequestException | None = None

    for source_file in _yosocal_file_candidates(target_dt):
        try:
            response = requests.get(
                f"{YOSOCAL_URL}{source_file}",
                headers=headers,
                params={"time": int(now * 1000)},
                timeout=REQUEST_TIMEOUT,
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            last_error = exc
            continue
        for columns in _parse_yosocal_records(_decode_yosocal_response(response)):
            if columns[0] in {target_key, previous_key} and columns[0] not in records_by_date:
                records_by_date[columns[0]] = (columns, source_file)
        if target_key in records_by_date:
            break

    selected = records_by_date.get(target_key)
    source_date = target_date
    if selected is None:
        selected = records_by_date.get(previous_key)
        source_date = previous_year_date
    if selected is None:
        _yosocal_cache[cache_key] = (now, None)
        if last_error and not records_by_date:
            raise last_error
        return None

    columns, source_file = selected
    result = _build_yosocal_weather(columns, target_date, source_date, source_file)
    _yosocal_cache[cache_key] = (now, result)
    return result


def _plain_text_from_html(source: str) -> str:
    source = re.sub(r"<script\b[^>]*>.*?</script>", " ", source, flags=re.I | re.S)
    source = re.sub(r"<style\b[^>]*>.*?</style>", " ", source, flags=re.I | re.S)
    source = re.sub(r"<[^>]+>", " ", source)
    return re.sub(r"\s+", " ", html.unescape(source)).strip()


def fetch_official_park_info(target_dt: datetime, park: str = "tdl") -> dict[str, Any] | None:
    """公式の日次カレンダーから開閉園時刻と1デーパスポート大人料金を取得。"""
    park = "tds" if park == "tds" else "tdl"
    key = f"{park}:{target_dt:%Y-%m-%d}"
    now = time.time()
    cached = _official_calendar_cache.get(key)
    if cached and now - cached[0] < OFFICIAL_CACHE_SECONDS:
        return cached[1]

    url = f"https://www.tokyodisneyresort.jp/{park}/daily/calendar/{target_dt:%Y%m%d}/"
    response = requests.get(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/150 Safari/537.36",
            "Accept-Language": "ja-JP,ja;q=0.9",
        },
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()
    response.encoding = response.apparent_encoding or "utf-8"
    text = _plain_text_from_html(response.text)

    open_time = close_time = None
    match = re.search(r"開園時間\s*(\d{1,2}:\d{2})\s*[-–－〜～]\s*(\d{1,2}:\d{2})", text)
    if match:
        open_time, close_time = match.group(1), match.group(2)

    ticket_price = None
    price_match = re.search(
        r"チケット情報.{0,80}?(?:販売中|残りわずか|売り切れ)?\s*[￥¥]?\s*([0-9]{1,2}(?:,[0-9]{3})+|[0-9]{4,5})",
        text,
    )
    if price_match:
        ticket_price = int(price_match.group(1).replace(",", ""))

    result = None
    if open_time or ticket_price:
        result = {
            "official_open_time": open_time,
            "official_close_time": close_time,
            "ticket_price": ticket_price,
            "source": "東京ディズニーリゾート公式",
            "source_url": url,
        }
    _official_calendar_cache[key] = (now, result)
    return result


def _parse_yosocal_calendar_records(payload: str) -> list[list[str]]:
    return _parse_yosocal_records(payload)


def _yosocal_tdl_base_people(row: list[str]) -> int | None:
    if len(row) < 17 or row[4] in {"", "-"} or row[5] in {"", "-"}:
        return None
    opening = row[4]
    closing = row[5]
    people = {"2:00": 15000, "8:00": 20000, "8:30": 17000, "9:00": 20000,
              "9:30": 16000, "10:00": 15000}.get(opening, 20000)
    people += {"18:00": 3000, "18:30": 3000, "19:00": 5000, "20:00": 8000,
               "21:00": 10000, "22:00": 10000}.get(closing, 10000)
    if len(row) > 9 and row[8] and row[8] > row[4]:
        people = int(people * 1.05)
    if len(row) > 9 and row[9] and row[9][:5] < row[5]:
        people = int(people * 1.05)
    people = int(people * 0.8)
    for index, fixed, per_day in ((12, 3000, 100), (14, 5000, 200)):
        if len(row) <= index or not row[index]:
            continue
        if row[index] == "*":
            people += fixed
        elif re.fullmatch(r"\d{8}", row[index]):
            sold = datetime.strptime(row[index], "%Y%m%d").date()
            visit = datetime.strptime(row[0], "%Y%m%d").date()
            people += max((visit - sold).days, 0) * per_day
    if row[1] == "*":
        people += 3000
    if row[2] == "*":
        people += 3000
    if row[16] == "*":
        people -= 3000
    return max(people, 0)


def _yosocal_rank(people: int | None) -> tuple[str | None, str | None]:
    if people is None:
        return None, None
    bounds = [(20000, "A", "ガラガラ"), (25000, "B", "かなり空いている"),
              (30000, "C", "空いている"), (40000, "D", "まぁ混雑"),
              (50000, "E", "やや混雑"), (60000, "F", "混雑"),
              (70000, "G", "非常に混雑")]
    for bound, rank, label in bounds:
        if people < bound:
            return rank, label
    return "H", "激しく混雑"


def fetch_yosocal_calendar(target_dt: datetime) -> dict[str, Any] | None:
    """Yosocal cal.xmlからランドの開閉園時刻と基本混雑ランクを取得。"""
    cache_key = target_dt.strftime("%Y-%m-%d")
    now = time.time()
    cached = _yosocal_calendar_cache.get(cache_key)
    if cached and now - cached[0] < YOSOCAL_CACHE_SECONDS:
        return cached[1]

    current_year = date.today().year
    files = ["cal.xml"]
    if target_dt.year < current_year:
        files.append(f"cal{target_dt.year}.xml")
    target_key = target_dt.strftime("%Y%m%d")
    selected = None
    source_file = None
    for filename in dict.fromkeys(files):
        response = requests.get(
            f"{YOSOCAL_URL}{filename}",
            headers={"User-Agent": "Mozilla/5.0 Chrome/150", "Referer": YOSOCAL_URL},
            params={"time": int(now * 1000)},
            timeout=REQUEST_TIMEOUT,
        )
        if response.status_code == 404:
            continue
        response.raise_for_status()
        for row in _parse_yosocal_calendar_records(_decode_yosocal_response(response)):
            if row[0] == target_key:
                selected, source_file = row, filename
                break
        if selected:
            break
    result = None
    if selected:
        people = _yosocal_tdl_base_people(selected)
        rank, label = _yosocal_rank(people)
        result = {
            "yosocal_crowd_people": people,
            "yosocal_crowd_rank": rank,
            "yosocal_crowd_label": label,
            "yosocal_open_time": selected[4] if len(selected) > 4 else None,
            "yosocal_close_time": selected[5] if len(selected) > 5 else None,
            "yosocal_passport_label": selected[3] if len(selected) > 3 else None,
            "yosocal_calendar_source": f"{YOSOCAL_URL}{source_file}",
            "yosocal_prediction_scope": "cal.xmlの基本補正値（イベント・休日の全補正前）",
        }
    _yosocal_calendar_cache[cache_key] = (now, result)
    return result


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


def mock_forecast(target_date: str, entry_time: str) -> dict[str, Any]:
    dt = datetime.strptime(target_date, "%Y-%m-%d")
    weekend = dt.weekday() >= 5
    base = 58 if weekend else 36

    return {
        "date": target_date,
        "entry_time": entry_time,
        "crowd_label": "混雑" if weekend else "普通",
        "crowd_score": base,
        "weather": "データ未更新",
        "temperature_high": None,
        "temperature_low": None,
        "ticket_price": None,
        "recommended_level": 2 if weekend else 4,
        "data_status": "demo",
        "attractions": [
            {
                "attraction_code": "beauty",
                "name": "美女と野獣",
                "acquisition_probability": 54 if weekend else 78,
                "predicted_sellout_time": "12:40" if weekend else "15:20",
                "confidence_low": "11:30" if weekend else "13:50",
                "confidence_high": "14:10" if weekend else "17:10",
            },
            {
                "attraction_code": "baymax",
                "name": "ベイマックス",
                "acquisition_probability": 69 if weekend else 88,
                "predicted_sellout_time": "15:10" if weekend else "17:30",
                "confidence_low": "13:40" if weekend else "16:00",
                "confidence_high": "17:20" if weekend else "19:00",
            },
            {
                "attraction_code": "splash",
                "name": "スプラッシュ・マウンテン",
                "acquisition_probability": 87 if weekend else 96,
                "predicted_sellout_time": "18:20" if weekend else None,
                "confidence_low": "16:40" if weekend else None,
                "confidence_high": "記録上限",
            },
        ],
        "reasons": [
            "Supabase未接続のためデモ値を表示しています。",
            "接続後はPC側で作成した予測結果を読み込みます。",
        ],
    }


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/api/status")
def api_status():
    return jsonify(
        {
            "supabase_connected": supabase_enabled(),
            "today": date.today().isoformat(),
        }
    )


@app.get("/api/forecast")
def api_forecast():
    target_date = request.args.get("date", "").strip()
    entry_time = request.args.get("entry_time", "10:00").strip()

    try:
        target_dt = datetime.strptime(target_date, "%Y-%m-%d")
        entry_dt = datetime.strptime(entry_time, "%H:%M")
    except ValueError:
        return jsonify(
            {
                "error": "日付または時刻の形式が正しくありません。",
            }
        ), 400

    if not supabase_enabled():
        return jsonify(mock_forecast(target_date, entry_time))

    try:
        # 選択日の天気・価格・開園時間などが登録済みなら、
        # 類似日の判定条件として利用する。
        day_rows = supabase_get(
            "daily_forecasts",
            {
                "select": "*",
                "target_date": f"eq.{target_date}",
                "limit": "1",
            },
        )
        day = day_rows[0] if day_rows else {}

        official_info = None
        official_error = None
        try:
            official_info = fetch_official_park_info(target_dt, "tdl")
        except requests.RequestException as exc:
            official_error = str(exc)
        if official_info:
            day = {
                **day,
                "ticket_price": official_info.get("ticket_price") if official_info.get("ticket_price") is not None else day.get("ticket_price"),
                "official_open_time": official_info.get("official_open_time") or day.get("official_open_time"),
                "official_close_time": official_info.get("official_close_time") or day.get("official_close_time"),
            }

        yosocal_calendar = None
        yosocal_calendar_error = None
        try:
            yosocal_calendar = fetch_yosocal_calendar(target_dt)
        except requests.RequestException as exc:
            yosocal_calendar_error = str(exc)
        if yosocal_calendar and not day.get("official_open_time"):
            day["official_open_time"] = yosocal_calendar.get("yosocal_open_time")
            day["official_close_time"] = yosocal_calendar.get("yosocal_close_time")

        # Yosocalから対象日の天気を取得。取得失敗時はSupabase登録値、
        # それもなければ天気なしのまま予測を続行する。
        yosocal_weather = None
        yosocal_error = None
        try:
            yosocal_weather = fetch_yosocal_weather(target_dt)
        except requests.RequestException as exc:
            yosocal_error = str(exc)

        if yosocal_weather:
            day = {
                **day,
                "weather": yosocal_weather.get("weather") or day.get("weather"),
                "temperature_high": (
                    yosocal_weather.get("temperature_high")
                    if yosocal_weather.get("temperature_high") is not None
                    else day.get("temperature_high")
                ),
                "temperature_low": (
                    yosocal_weather.get("temperature_low")
                    if yosocal_weather.get("temperature_low") is not None
                    else day.get("temperature_low")
                ),
            }

        history_rows = supabase_get(
            "dpa_history_view",
            {
                "select": "*",
                "order": "visit_date.desc",
                "limit": "1000",
            },
        )

        if not history_rows:
            return jsonify(
                {
                    "error": "予測に使える実績データがありません。",
                    "data_status": "not_found",
                }
            ), 404

        weighted_rows = [
            (row, similarity_weight(row, target_dt, day))
            for row in history_rows
        ]
        weighted_rows = [
            item for item in weighted_rows
            if item[1] > 0
        ]
        weighted_rows.sort(key=lambda item: item[1], reverse=True)

        # 条件が近い上位120件を予測に利用する。
        selected_rows = weighted_rows[:120]

        entry_minutes = entry_dt.hour * 60 + entry_dt.minute

        attraction_specs = [
            (
                "beauty",
                "美女と野獣",
                "beauty_sellout_time",
                "beauty_is_limit",
            ),
            (
                "baymax",
                "ベイマックス",
                "baymax_sellout_time",
                "baymax_is_limit",
            ),
            (
                "splash",
                "スプラッシュ・マウンテン",
                "splash_sellout_time",
                "splash_is_limit",
            ),
        ]

        attractions = [
            build_attraction_prediction(
                history_rows,
                selected_rows,
                entry_minutes,
                code,
                name,
                sellout_field,
                limit_field,
            )
            for code, name, sellout_field, limit_field in attraction_specs
        ]

        evaluated_synced = 0
        learning_logs: list[dict[str, Any]] = []
        try:
            evaluated_synced = sync_prediction_results(history_rows)
            learning_logs = supabase_get("prediction_logs", {
                "select": "*", "evaluated_at": "not.is.null", "order": "target_date.desc", "limit": "200"
            })
        except requests.RequestException:
            learning_logs = []
        learning = apply_learning_calibration(attractions, learning_logs)
        learning["newly_evaluated_count"] = evaluated_synced

        crowd_values: list[tuple[float, float]] = []
        for row, weight in selected_rows:
            score = crowd_score_from_label(row.get("crowd_label"))
            if score is not None:
                crowd_values.append((float(score), weight))

        crowd_score_value = weighted_average(crowd_values)
        crowd_score = (
            int(round(crowd_score_value))
            if crowd_score_value is not None
            else None
        )
        crowd_label = crowd_label_from_score(crowd_score_value)

        probabilities = [
            int(item["acquisition_probability"])
            for item in attractions
            if item.get("sample_count", 0) > 0
        ]
        average_probability = (
            sum(probabilities) / len(probabilities)
            if probabilities
            else 0
        )

        if average_probability >= 85:
            recommended_level = 5
        elif average_probability >= 70:
            recommended_level = 4
        elif average_probability >= 55:
            recommended_level = 3
        elif average_probability >= 40:
            recommended_level = 2
        else:
            recommended_level = 1

        weekday_names = ["月", "火", "水", "木", "金", "土", "日"]
        weekday_name = weekday_names[target_dt.weekday()]

        reasons = [
            f"登録済み実績{len(history_rows)}件から、条件が近い上位{len(selected_rows)}件を使って予測しました。",
            f"選択日は{weekday_name}曜日のため、同じ曜日の実績を強く評価しています。",
            f"入園予定時刻{entry_time}にDPAが残っていた実績の割合を取得予測率として表示しています。",
        ]

        used_conditions = []
        if day.get("weather"):
            used_conditions.append("天気")
        if day.get("ticket_price") is not None:
            used_conditions.append("チケット価格")
        if day.get("official_open_time"):
            used_conditions.append("開園時刻")

        if used_conditions:
            reasons.append(
                "類似日の判定には、" + "・".join(used_conditions) + "も利用しています。"
            )
        else:
            reasons.append(
                "選択日の天気・価格・開園時刻が未登録のため、主に曜日と過去実績から予測しています。"
            )

        if official_info:
            reasons.append("チケット価格と公式開園時間は東京ディズニーリゾート公式の日次カレンダーから自動取得しました。")
        elif official_error:
            reasons.append("公式カレンダーを取得できなかったため、登録済み値またはYosocalの時刻を利用しました。")
        if yosocal_calendar and yosocal_calendar.get("yosocal_crowd_rank"):
            reasons.append(f"比較用のYosocal基本混雑予想は{yosocal_calendar.get('yosocal_crowd_rank')}（{yosocal_calendar.get('yosocal_crowd_label')}）です。")
        if learning.get("applied"):
            reasons.append(f"過去の予測と実績{learning.get('evaluated_count')}件から、売切れ時刻と取得率を自動補正しました。")

        if yosocal_weather:
            reasons.append(
                f"天気はYosocalの「{yosocal_weather.get('reference_label')}」を利用しています。"
            )
        elif yosocal_error:
            reasons.append(
                "Yosocalの取得に失敗したため、登録済み天気または天気なしで予測しました。"
            )
        else:
            reasons.append(
                "Yosocalで対象日の天気を見つけられなかったため、登録済み天気または天気なしで予測しました。"
            )

        payload = {
                "date": target_date,
                "entry_time": entry_time,
                "crowd_label": day.get("crowd_label") or crowd_label,
                "crowd_score": day.get("crowd_score") or crowd_score,
                "weather": day.get("weather") or "予報未登録",
                "temperature_high": day.get("temperature_high"),
                "temperature_low": day.get("temperature_low"),
                "precipitation_probability": (
                    yosocal_weather.get("precipitation_probability")
                    if yosocal_weather
                    else None
                ),
                "weather_source": (
                    yosocal_weather.get("source")
                    if yosocal_weather
                    else ("Supabase" if day_rows and day_rows[0].get("weather") else None)
                ),
                "weather_reference_type": (
                    yosocal_weather.get("reference_type") if yosocal_weather else None
                ),
                "weather_reference_label": (
                    yosocal_weather.get("reference_label") if yosocal_weather else None
                ),
                "weather_code": (
                    yosocal_weather.get("weather_code") if yosocal_weather else None
                ),
                "weather_source_date": (
                    yosocal_weather.get("source_date") if yosocal_weather else None
                ),
                "ticket_price": day.get("ticket_price"),
                "recommended_level": recommended_level,
                "attractions": attractions,
                "reasons": reasons,
                "data_status": "live",
                "prediction_method": "similar_history",
                "history_count": len(history_rows),
                "sample_count": len(selected_rows),
                "official_open_time": day.get("official_open_time"),
                "official_close_time": day.get("official_close_time"),
                "official_calendar_source": official_info.get("source_url") if official_info else None,
                "ticket_price_source": official_info.get("source") if official_info and official_info.get("ticket_price") is not None else ("Supabase" if day.get("ticket_price") is not None else None),
                "yosocal_crowd_people": yosocal_calendar.get("yosocal_crowd_people") if yosocal_calendar else None,
                "yosocal_crowd_rank": yosocal_calendar.get("yosocal_crowd_rank") if yosocal_calendar else None,
                "yosocal_crowd_label": yosocal_calendar.get("yosocal_crowd_label") if yosocal_calendar else None,
                "yosocal_prediction_scope": yosocal_calendar.get("yosocal_prediction_scope") if yosocal_calendar else None,
                "learning": learning,
            }
        try:
            save_prediction_log(payload)
            payload["prediction_saved"] = True
        except requests.RequestException:
            payload["prediction_saved"] = False
        return jsonify(payload)

    except requests.RequestException as exc:
        return jsonify(
            {
                "error": f"Supabaseの取得に失敗しました: {exc}",
            }
        ), 502


@app.get("/api/analytics")
def api_analytics():
    if not supabase_enabled():
        return jsonify(
            {
                "data_status": "demo",
                "summary": {
                    "record_count": 0,
                    "latest_record_date": None,
                    "model_updated_at": None,
                },
                "weekday_stats": [],
                "remaining_rate_stats": [],
                "message": "Supabase接続後に分析結果が表示されます。",
            }
        )

    try:
        park_days = supabase_get(
            "park_days",
            {
                "select": (
                    "visit_date,"
                    "crowd_label,"
                    "weather,"
                    "ticket_price,"
                    "official_open_time"
                ),
                "order": "visit_date.desc",
            },
        )

        record_count = len(park_days)

        latest_record_date = None
        if park_days:
            latest_record_date = park_days[0].get("visit_date")

        weekday_counts: dict[str, int] = {
            "月": 0,
            "火": 0,
            "水": 0,
            "木": 0,
            "金": 0,
            "土": 0,
            "日": 0,
        }

        weekday_names = [
            "月",
            "火",
            "水",
            "木",
            "金",
            "土",
            "日",
        ]

        for row in park_days:
            visit_date = row.get("visit_date")

            if not visit_date:
                continue

            try:
                visit_dt = datetime.strptime(
                    visit_date,
                    "%Y-%m-%d",
                )
            except ValueError:
                continue

            weekday_name = weekday_names[visit_dt.weekday()]
            weekday_counts[weekday_name] += 1

        weekday_stats = [
            {
                "weekday": weekday_name,
                "record_count": weekday_counts[weekday_name],
            }
            for weekday_name in weekday_names
        ]

        return jsonify(
            {
                "data_status": "live",
                "summary": {
                    "record_count": record_count,
                    "latest_record_date": latest_record_date,
                    "model_updated_at": None,
                },
                "weekday_stats": weekday_stats,
                "remaining_rate_stats": [],
                "message": (
                    "実績データを取得しました。"
                    if record_count > 0
                    else "実績データはまだありません。"
                ),
            }
        )

    except requests.RequestException as exc:
        return jsonify(
            {
                "error": f"分析結果の取得に失敗しました: {exc}",
            }
        ), 502


@app.get("/api/database")
def api_database():
    try:
        page = max(
            int(request.args.get("page", "1")),
            1,
        )
        page_size = min(
            max(
                int(request.args.get("page_size", "20")),
                1,
            ),
            100,
        )
    except ValueError:
        return jsonify(
            {
                "error": "ページ番号または表示件数が正しくありません。",
            }
        ), 400

    offset = (page - 1) * page_size

    date_from = request.args.get("date_from", "").strip()
    date_to = request.args.get("date_to", "").strip()

    if not supabase_enabled():
        return jsonify(
            {
                "data_status": "demo",
                "records": [],
                "page": page,
                "page_size": page_size,
                "message": "Supabase接続後に実績データが表示されます。",
            }
        )

    params: dict[str, Any] = {
        "select": "*",
        "order": "visit_date.desc",
        "limit": str(page_size),
        "offset": str(offset),
    }

    conditions = []

    if date_from:
        conditions.append(f"visit_date.gte.{date_from}")

    if date_to:
        conditions.append(f"visit_date.lte.{date_to}")

    if conditions:
        params["and"] = f"({','.join(conditions)})"

    try:
        records = supabase_get(
            "dpa_history_view",
            params,
        )

        return jsonify(
            {
                "data_status": "live",
                "records": records,
                "page": page,
                "page_size": page_size,
            }
        )

    except requests.RequestException as exc:
        return jsonify(
            {
                "error": f"実績データの取得に失敗しました: {exc}",
            }
        ), 502


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))

    app.run(
        host="0.0.0.0",
        port=port,
        debug=os.getenv("FLASK_DEBUG") == "1",
    )