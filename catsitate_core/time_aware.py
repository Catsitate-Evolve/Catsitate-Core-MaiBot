"""时间/节日/天气感知:公历回退链 + lunar-python 农历节日/节气。"""

from __future__ import annotations

import re
from datetime import date, timedelta

try:
    from lunar_python import Solar
except ImportError:  # 依赖未安装:农历节日/节气缺失(环境块跳过该片段),显式告警不阻断(审查 Minor#6)
    Solar = None

# Open-Meteo WMO 天气码 → 中文(常见码)
WEATHER_CODE_MAP: dict[int, str] = {
    0: "晴", 1: "基本晴朗", 2: "多云", 3: "阴",
    45: "雾", 48: "雾凇",
    51: "小毛毛雨", 53: "毛毛雨", 55: "浓毛毛雨",
    61: "小雨", 63: "中雨", 65: "大雨",
    71: "小雪", 73: "中雪", 75: "大雪",
    80: "阵雨", 81: "中等阵雨", 82: "强阵雨",
    85: "阵雪", 86: "强阵雪",
    95: "雷暴", 96: "雷暴伴冰雹", 99: "强雷暴伴冰雹",
}

# 内置公历静态表(回退链末层;农历节日/节气由 lunar-python 实时计算,不预生成)
FESTIVAL_TABLE: dict[str, str] = {
    "01-01": "元旦",
    "02-14": "情人节",
    "03-08": "妇女节",
    "05-01": "劳动节",
    "06-01": "儿童节",
    "10-01": "国庆节",
    "12-24": "平安夜",
    "12-25": "圣诞节",
}


def solar_term_on(day: date) -> str:
    """某日的节气名(仅交节日当天非空);依赖缺失返回空(同 solar_terms_near 容错)。"""

    if Solar is None:
        return ""

    return Solar.fromYmd(day.year, day.month, day.day).getLunar().getJieQi() or ""


def solar_terms_near(now: date, days: int = 3) -> list[str]:
    """当天+临近 days 天的节气名列表(lunar-python 实算,按日期升序);依赖缺失返回空。"""

    out: list[str] = []
    for offset in range(days + 1):
        name = solar_term_on(now + timedelta(days=offset))
        if name:
            out.append(name)
    return out


def lunar_festivals_near(now: date, days: int = 3) -> list[str]:
    """当天+临近 days 天的农历节日名列表(lunar-python 实算,按日期升序);依赖缺失返回空。

    注意:仅「今天」的节日;临近节日经 lunar_festivals_upcoming 单独取(联调发现:
    3 天窗口混入当天文本会让人误以为今天就是那个节日,如七夕)。
    """

    return lunar_festivals_upcoming(now, days=0)


def lunar_festivals_upcoming(now: date, days: int = 3) -> list[str]:
    """今天起未来 days 天内的农历节日,带日期前缀(如「8月19日 七夕节」),升序。"""

    if Solar is None:
        return []

    out: list[str] = []
    for offset in range(days + 1):
        day = now + timedelta(days=offset)
        for name in Solar.fromYmd(day.year, day.month, day.day).getLunar().getFestivals():
            label = name if offset == 0 else f"{day.month}月{day.day}日 {name}"
            out.append(label)
    return out


def dedup_festival_names(names: list[str]) -> list[str]:
    """保序去重:去掉末尾「节」字后同名视为同一节日(双源重名,如「七夕」vs「七夕节」)。"""

    out: list[str] = []
    for name in names:
        norm = name[:-1] if name.endswith("节") else name
        if any((kept[:-1] if kept.endswith("节") else kept) == norm for kept in out):
            continue
        out.append(name)
    return out


def parse_holiday_cn(data: dict) -> dict[str, list[str]]:
    """解析 holiday-cn 在线数据为 {"MM-DD": [节日名, ...]}。"""

    result: dict[str, list[str]] = {}
    for day in data.get("days", []):
        raw = day.get("date", "")
        name = day.get("name", "")
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw) and name:
            result.setdefault(raw[5:], []).append(name)
    return result


def holiday_chain(now: date, online: dict[str, list[str]] | None, builtin_ok: bool) -> dict[str, list[str]]:
    """按回退链合并公历节日数据:在线(holiday-cn) → holiday-calendar 库 → 内置公历表。

    库层数据由调用方(task 接线)传入并合并进 online 参数前先单独处理:
    库层格式 {"MM-DD": ["节日", ...]} 直接作第二层。农历节日/节气不在此链
    (经 lunar_festivals_near/solar_terms_near 实时计算)。
    """

    del now  # 回退链与日期无关,保留参数兼容
    merged: dict[str, list[str]] = {}
    if builtin_ok:
        for key, name in FESTIVAL_TABLE.items():
            merged.setdefault(key, []).append(name)
    if online:
        for key, names in online.items():
            existing = merged.get(key)
            if existing:
                merged[key] = [n for n in names if n not in existing] + existing
            else:
                merged[key] = list(names)
    return merged


def build_environment_text(
    now: date,
    city: str,
    weather: dict | None,
    holidays: list[str],
    solar_terms: list[str],
    upcoming: list[str] | None = None,
) -> str:
    """组装环境块文本(单行,自解释)。

    holidays/solar_terms = 仅今天的节日/节气;upcoming = 未来几天的
    (带日期前缀,如「8月19日 七夕节」),单独拼「临近:」段,不与今日混淆。
    """

    weekday = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")[now.weekday()]
    parts = [f"今天 {now.month}月{now.day}日 {weekday}"]
    if weather is not None:
        code = int(weather.get("weather_code", 0))
        temp = weather.get("temperature_2m")
        desc = WEATHER_CODE_MAP.get(code, "天气不明")
        if temp is not None:
            desc = f"{desc},{round(float(temp))}°C"
        parts.append(f"{city}:{desc}")
    else:
        parts.append(f"{city}")
    extras = list(solar_terms) + list(holidays)
    if extras:
        parts.append("节日:" + "、".join(extras))
    if upcoming:
        parts.append("临近:" + "、".join(upcoming))
    return "[环境] " + ";".join(parts) + "。"
