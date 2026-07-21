from datetime import date, datetime
from typing import Any
from flask import Blueprint, jsonify, render_template, request
import requests
from config import APP_VERSION, APP_BUILD, APP_ENV
from constants import ATTRACTION_SPECS, WEEKDAY_NAMES
from db import supabase_enabled, supabase_get
from yosocal import fetch_official_park_info, fetch_yosocal_weather, fetch_yosocal_calendar, fetch_yosocal_full_context
from learning import apply_learning_calibration, sync_prediction_results, save_prediction_log
from prediction import normalize_weather, crowd_score_from_label, crowd_label_from_score, similarity_weight, build_attraction_prediction, calculate_prediction_confidence
from mock_data import mock_forecast
from utils import time_to_minutes, minutes_to_time, weighted_average

bp = Blueprint("api", __name__)

@bp.get("/")
def index():
    return render_template("index.html")

@bp.get("/api/status")
def api_status():
    return jsonify(
        {
            "version": APP_VERSION,
            "build": APP_BUILD,
            "environment": APP_ENV,
            "supabase_connected": supabase_enabled(),
            "today": date.today().isoformat(),
        }
    )

@bp.get("/api/forecast")
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
        except Exception as exc:
            official_error = f"{type(exc).__name__}: {exc}"
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
            yosocal_calendar = fetch_yosocal_full_context(target_dt)
        except Exception as exc:
            yosocal_calendar_error = f"{type(exc).__name__}: {exc}"
        if yosocal_calendar and not day.get("official_open_time"):
            day["official_open_time"] = yosocal_calendar.get("yosocal_open_time")
            day["official_close_time"] = yosocal_calendar.get("yosocal_close_time")
        if yosocal_calendar and day.get("ticket_price") is None and yosocal_calendar.get("yosocal_ticket_price_estimate") is not None:
            day["ticket_price"] = yosocal_calendar.get("yosocal_ticket_price_estimate")

        # Yosocalから対象日の天気を取得。取得失敗時はSupabase登録値、
        # それもなければ天気なしのまま予測を続行する。
        yosocal_weather = None
        yosocal_error = None
        try:
            yosocal_weather = fetch_yosocal_weather(target_dt)
        except Exception as exc:
            yosocal_error = f"{type(exc).__name__}: {exc}"

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

        attraction_specs = ATTRACTION_SPECS

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
        except Exception:
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

        weekday_name = WEEKDAY_NAMES[target_dt.weekday()]

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

        prediction_confidence = calculate_prediction_confidence(
            attractions=attractions,
            selected_count=len(selected_rows),
            history_count=len(history_rows),
            used_condition_count=len(used_conditions),
            learning_applied=bool(learning.get("applied")),
        )
        reasons.append(
            f"予測信頼度は{prediction_confidence['score']}%（{prediction_confidence['label']}）です。"
        )

        if official_info:
            reasons.append("チケット価格と公式開園時間は東京ディズニーリゾート公式の日次カレンダーから自動取得しました。")
        elif official_error:
            reasons.append("公式カレンダーを取得できなかったため、登録済み値またはYosocalの時刻を利用しました。")
        if yosocal_calendar and yosocal_calendar.get("yosocal_full_crowd_rank"):
            reasons.append(f"Yosocalの全取得要素を反映した参考混雑予想は{yosocal_calendar.get('yosocal_full_crowd_rank')}（{yosocal_calendar.get('yosocal_full_crowd_label')}）です。")
            reasons.append(f"対象日のイベント・休暇・連休など{yosocal_calendar.get('yosocal_factor_count', 0)}件、休止施設{yosocal_calendar.get('yosocal_closure_count', 0)}件を取得しました。")
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
                "version": APP_VERSION,
                "build": APP_BUILD,
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
                "ticket_price_source": official_info.get("source") if official_info and official_info.get("ticket_price") is not None else ("Yosocal passport label" if yosocal_calendar and yosocal_calendar.get("yosocal_ticket_price_estimate") is not None else ("Supabase" if day.get("ticket_price") is not None else None)),
                "yosocal_crowd_people": yosocal_calendar.get("yosocal_crowd_people") if yosocal_calendar else None,
                "yosocal_crowd_rank": yosocal_calendar.get("yosocal_crowd_rank") if yosocal_calendar else None,
                "yosocal_crowd_label": yosocal_calendar.get("yosocal_crowd_label") if yosocal_calendar else None,
                "yosocal_prediction_scope": yosocal_calendar.get("yosocal_prediction_scope") if yosocal_calendar else None,
                "yosocal_full_crowd_people": yosocal_calendar.get("yosocal_full_crowd_people") if yosocal_calendar else None,
                "yosocal_full_crowd_rank": yosocal_calendar.get("yosocal_full_crowd_rank") if yosocal_calendar else None,
                "yosocal_full_crowd_label": yosocal_calendar.get("yosocal_full_crowd_label") if yosocal_calendar else None,
                "yosocal_event_adjustment_people": yosocal_calendar.get("yosocal_event_adjustment_people") if yosocal_calendar else None,
                "yosocal_factors": yosocal_calendar.get("yosocal_factors", []) if yosocal_calendar else [],
                "yosocal_factor_count": yosocal_calendar.get("yosocal_factor_count", 0) if yosocal_calendar else 0,
                "yosocal_closures": yosocal_calendar.get("yosocal_closures", []) if yosocal_calendar else [],
                "yosocal_closure_count": yosocal_calendar.get("yosocal_closure_count", 0) if yosocal_calendar else 0,
                "yosocal_date_source": yosocal_calendar.get("yosocal_date_source") if yosocal_calendar else None,
                "yosocal_rest_source": yosocal_calendar.get("yosocal_rest_source") if yosocal_calendar else None,
                "yosocal_passport_label": yosocal_calendar.get("yosocal_passport_label") if yosocal_calendar else None,
                "yosocal_ticket_price_estimate": yosocal_calendar.get("yosocal_ticket_price_estimate") if yosocal_calendar else None,
                "learning": learning,
                "prediction_confidence": prediction_confidence,
                "source_diagnostics": {
                    "official_calendar": {
                        "success": bool(official_info),
                        "error": official_error,
                    },
                    "yosocal_calendar": {
                        "success": bool(yosocal_calendar),
                        "error": yosocal_calendar_error,
                    },
                    "yosocal_weather": {
                        "success": bool(yosocal_weather),
                        "error": yosocal_error,
                    },
                    "supabase": {
                        "success": True,
                        "history_count": len(history_rows),
                    },
                },
            }
        try:
            save_prediction_log(payload)
            payload["prediction_saved"] = True
        except Exception as exc:
            payload["prediction_saved"] = False
            payload["prediction_save_error"] = f"{type(exc).__name__}: {exc}"
        return jsonify(payload)

    except requests.RequestException as exc:
        return jsonify({
            "version": APP_VERSION,
            "build": APP_BUILD,
            "error": f"Supabaseの取得に失敗しました: {exc}",
            "error_type": type(exc).__name__,
        }), 502
    except Exception as exc:
        return jsonify({
            "version": APP_VERSION,
            "build": APP_BUILD,
            "error": "予測処理で予期しないエラーが発生しました。",
            "error_type": type(exc).__name__,
            "error_detail": str(exc),
        }), 500

@bp.get("/api/analytics")
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

@bp.get("/api/database")
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
