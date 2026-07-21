"""Application-wide immutable labels and model constants."""

YOSOCAL_EVENT_TYPE_LABELS: dict[str, str] = {
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

WEEKDAY_NAMES = ("月", "火", "水", "木", "金", "土", "日")
ATTRACTION_SPECS = (
    ("beauty", "美女と野獣", "beauty_sellout_time", "beauty_is_limit"),
    ("baymax", "ベイマックス", "baymax_sellout_time", "baymax_is_limit"),
    ("splash", "スプラッシュ・マウンテン", "splash_sellout_time", "splash_is_limit"),
)
