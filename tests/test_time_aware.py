"""时间感知测试:节日回退链、农历节日/节气(lunar-python)、天气码、环境块渲染。"""

from datetime import date

from catsitate_core.time_aware import (
    FESTIVAL_TABLE,
    WEATHER_CODE_MAP,
    build_environment_text,
    dedup_festival_names,
    holiday_chain,
    lunar_festivals_near,
    parse_holiday_cn,
    solar_term_on,
    solar_terms_near,
)


def test_parse_holiday_cn_normalizes():
    data = {
        "year": 2026,
        "days": [
            {"date": "2026-08-19", "name": "七夕", "isOffDay": False},
            {"date": "2026-10-01", "name": "国庆节", "isOffDay": True},
        ],
    }
    parsed = parse_holiday_cn(data)
    assert parsed["08-19"] == ["七夕"]
    assert parsed["10-01"] == ["国庆节"]


def test_holiday_chain_online_first():
    online = {"08-14": ["七夕"]}
    merged = holiday_chain(date(2026, 8, 14), online, True)
    assert merged["08-14"] == ["七夕"]
    # 在线缺失日期由内置表补齐
    assert "01-01" in merged


def test_holiday_chain_falls_back_to_builtin():
    merged = holiday_chain(date(2026, 8, 14), None, True)
    assert merged == {k: [v] for k, v in FESTIVAL_TABLE.items()}  # 返回值为 list[str](接口声明)
    merged2 = holiday_chain(date(2026, 8, 14), None, False)
    assert merged2 == {}


def test_builtin_table_covers_major_festivals():
    assert "01-01" in FESTIVAL_TABLE  # 元旦
    assert "05-01" in FESTIVAL_TABLE  # 劳动节
    assert "10-01" in FESTIVAL_TABLE  # 国庆
    assert "12-25" in FESTIVAL_TABLE  # 圣诞
    assert not any("(" in name for name in FESTIVAL_TABLE.values())  # 纯公历,无年份占位


def test_weather_code_map_common():
    assert WEATHER_CODE_MAP[0] == "晴"
    assert WEATHER_CODE_MAP[3] == "阴"
    assert 95 in WEATHER_CODE_MAP


def test_build_environment_text_separates_today_and_upcoming():
    from datetime import date
    text = build_environment_text(
        date(2026, 8, 16), "珠海", None, [], [], upcoming=["8月19日 七夕节"],
    )
    assert "节日:" not in text  # 今天无节日,不出现节日段
    assert "临近:8月19日 七夕节" in text


def test_build_environment_text_with_weather_and_festival():
    text = build_environment_text(
        now=date(2026, 8, 14),
        city="北京",
        weather={"temperature_2m": 29.3, "weather_code": 0},
        holidays=["七夕"],
        solar_terms=[],
    )
    assert text.startswith("[环境] 今天 8月14日")
    assert "北京" in text
    assert "晴" in text
    assert "29°C" in text
    assert "七夕" in text


def test_build_environment_text_without_weather():
    text = build_environment_text(
        now=date(2026, 8, 14), city="北京", weather=None, holidays=[], solar_terms=[]
    )
    assert "[环境]" in text
    assert "晴" not in text


def test_solar_terms_near():
    assert solar_terms_near(date(2026, 6, 20)) == ["夏至"]  # 次日夏至在 3 天窗口内(lunar-python 实算)
    assert solar_terms_near(date(2026, 9, 23)) == ["秋分"]
    assert solar_terms_near(date(2026, 5, 1)) == []


def test_solar_term_on():
    # 按日取节气:临近段构造用(仅交节日当天非空,非节气日返回空串)
    assert solar_term_on(date(2026, 6, 21)) == "夏至"
    assert solar_term_on(date(2026, 12, 22)) == "冬至"
    assert solar_term_on(date(2026, 5, 1)) == ""  # 非节气日
    # 与窗口版一致(窗口版逐日复用本函数)
    assert [solar_term_on(date(2026, 6, 21)), solar_term_on(date(2026, 6, 22))] == ["夏至", ""]


def test_lunar_festivals_near():
    assert "七夕节" in lunar_festivals_near(date(2026, 8, 19))  # 农历七月初七
    assert "除夕" in lunar_festivals_near(date(2026, 2, 16))  # 腊月廿九(2026 除夕)
    assert "中秋节" in lunar_festivals_near(date(2026, 9, 25))  # 农历八月十五
    assert lunar_festivals_near(date(2026, 5, 1)) == []  # 五一无农历节日


def test_dedup_festival_names():
    assert dedup_festival_names(["七夕", "七夕节", "国庆节"]) == ["七夕", "国庆节"]  # 去「节」字后同名合并
    assert dedup_festival_names(["中秋", "中秋", "除夕"]) == ["中秋", "除夕"]
    assert dedup_festival_names([]) == []
