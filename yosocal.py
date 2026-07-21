import html
import re
import time
from datetime import date, datetime, timedelta
from typing import Any
import requests
from config import REQUEST_TIMEOUT, YOSOCAL_URL, YOSOCAL_CACHE_SECONDS, OFFICIAL_CACHE_SECONDS
from utils import time_to_minutes
_yosocal_cache: dict[str, tuple[float, dict[str, Any] | None]] = {}
_yosocal_calendar_cache: dict[str, tuple[float, dict[str, Any] | None]] = {}
_official_calendar_cache: dict[str, tuple[float, dict[str, Any] | None]] = {}

YOSOCAL_EVENT_TYPE_LABELS = {
    "1": "祝日",
    "2": "連休",
    "3": "学校休み",
    "4": "キャンパスデー",
    "5": "混雑注意日",
    "19": "学生休暇",
    "20": "学生休暇",
    "21": "学生休暇",
    "22": "学生休暇",
    "23": "学生休暇",
    "24": "学生休暇",
    "25": "学生休暇",
    "26": "ランドイベント",
    "27": "ランドイベント",
    "28": "ランドイベント",
    "29": "ランドイベント",
    "30": "ランドイベント",
    "31": "ランドイベント",
    "32": "ランドイベント",
    "33": "ランドイベント",
    "34": "ランドイベント",
    "35": "ランドイベント",
    "36": "シーイベント",
    "37": "シーイベント",
    "38": "シーイベント",
    "39": "シーイベント",
    "40": "シーイベント",
}

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

def _parse_yosocal_generic_records(payload: str) -> list[list[str]]:
    payload = payload.replace("\r", "").replace("\n", "")
    records = []
    for raw_record in payload.split("\\"):
        raw_record = raw_record.strip().strip("\ufeff")
        if not raw_record:
            continue
        records.append([column.strip().strip('"') for column in raw_record.split(",")])
    return records

def _yosocal_data_file_candidates(prefix: str, target_dt: datetime) -> list[str]:
    current_year = date.today().year
    names = [f"{prefix}.xml"]
    if target_dt.year < current_year:
        names.append(f"{prefix}{target_dt.year}.xml")
    return list(dict.fromkeys(names))

def _fetch_yosocal_rows(prefix: str, target_dt: datetime) -> tuple[list[list[str]], str | None]:
    now = time.time()
    for filename in _yosocal_data_file_candidates(prefix, target_dt):
        response = requests.get(
            f"{YOSOCAL_URL}{filename}",
            headers={"User-Agent": "Mozilla/5.0 Chrome/150", "Referer": YOSOCAL_URL},
            params={"time": int(now * 1000)},
            timeout=REQUEST_TIMEOUT,
        )
        if response.status_code == 404:
            continue
        response.raise_for_status()
        return _parse_yosocal_generic_records(_decode_yosocal_response(response)), filename
    return [], None

def _event_category(type_code: str, name: str) -> str:
    text = name.lower()
    if any(word in text for word in ("パレード", "ショー", "花火", "グリーティング")):
        return "entertainment"
    if any(word in text for word in ("春休み", "夏休み", "冬休み", "秋休み", "学生", "卒業旅行", "修学旅行", "テスト休み")):
        return "school_holiday"
    if any(word in text for word in ("ゴールデンウィーク", "お盆", "正月", "連休", "祝日", "祭日")):
        return "holiday"
    if any(word in text for word in ("ハロウィーン", "クリスマス", "イースター", "周年", "イベント")):
        return "seasonal_event"
    if type_code.startswith("2"):
        return "tdl_event"
    if type_code.startswith("3"):
        return "tds_event"
    return "other"

def fetch_yosocal_full_context(target_dt: datetime) -> dict[str, Any]:
    """date/cal/rest XMLから対象日の要素をまとめて取得する。"""
    target_key = target_dt.strftime("%Y%m%d")
    calendar = fetch_yosocal_calendar(target_dt) or {}

    date_rows, date_file = _fetch_yosocal_rows("date", target_dt)
    factors = []
    for row in date_rows:
        if len(row) < 5 or not re.fullmatch(r"\d{8}", row[1] or "") or not re.fullmatch(r"\d{8}", row[2] or ""):
            continue
        if row[1] <= target_key <= row[2]:
            adjustment = _to_float(row[3])
            name = row[4] or YOSOCAL_EVENT_TYPE_LABELS.get(row[0], "名称未登録")
            factors.append({
                "type_code": row[0],
                "type_label": YOSOCAL_EVENT_TYPE_LABELS.get(row[0], "その他"),
                "category": _event_category(row[0], name),
                "name": name,
                "start_date": f"{row[1][:4]}-{row[1][4:6]}-{row[1][6:8]}",
                "end_date": f"{row[2][:4]}-{row[2][4:6]}-{row[2][6:8]}",
                "base_adjustment_people": adjustment,
                "extra_fields": row[5:],
            })

    rest_rows, rest_file = _fetch_yosocal_rows("rest", target_dt)
    closures = []
    for row in rest_rows:
        if len(row) < 4 or not re.fullmatch(r"\d{8}", row[1] or "") or not re.fullmatch(r"\d{8}", row[2] or ""):
            continue
        if row[1] <= target_key <= row[2]:
            closures.append({
                "park": "tdl" if row[0] == "0" else "tds" if row[0] == "1" else "unknown",
                "name": row[3],
                "start_date": f"{row[1][:4]}-{row[1][4:6]}-{row[1][6:8]}",
                "end_date": f"{row[2][:4]}-{row[2][4:6]}-{row[2][6:8]}",
                "extra_fields": row[4:],
            })

    event_adjustment = int(round(sum(float(x["base_adjustment_people"] or 0) for x in factors)))
    base_people = calendar.get("yosocal_crowd_people")
    full_people = max(0, int(base_people + event_adjustment)) if base_people is not None else None
    full_rank, full_label = _yosocal_rank(full_people)

    passport_label = calendar.get("yosocal_passport_label")
    estimated_price = None
    if passport_label:
        price_match = re.search(r"(?:￥|¥)?\s*(\d{1,2}(?:,\d{3})|\d{4,5})", passport_label)
        if price_match:
            estimated_price = int(price_match.group(1).replace(",", ""))

    return {
        **calendar,
        "yosocal_full_crowd_people": full_people,
        "yosocal_full_crowd_rank": full_rank,
        "yosocal_full_crowd_label": full_label,
        "yosocal_event_adjustment_people": event_adjustment,
        "yosocal_factors": factors,
        "yosocal_factor_count": len(factors),
        "yosocal_closures": closures,
        "yosocal_closure_count": len(closures),
        "yosocal_date_source": f"{YOSOCAL_URL}{date_file}" if date_file else None,
        "yosocal_rest_source": f"{YOSOCAL_URL}{rest_file}" if rest_file else None,
        "yosocal_ticket_price_estimate": estimated_price,
        "yosocal_prediction_scope": "cal.xmlの基本補正＋date.xmlの対象日要素。休止施設はrest.xmlから取得",
    }
