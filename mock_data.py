from datetime import datetime
from typing import Any
from config import APP_VERSION, APP_BUILD

def mock_forecast(target_date: str, entry_time: str) -> dict[str, Any]:
    dt = datetime.strptime(target_date, "%Y-%m-%d")
    weekend = dt.weekday() >= 5
    base = 58 if weekend else 36

    return {
        "version": APP_VERSION,
        "build": APP_BUILD,
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
