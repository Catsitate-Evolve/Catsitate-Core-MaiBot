"""日程模型测试:校验/修复/窗口定位/默认模板。"""
import copy as _copy

from catsitate_core.schedule import (
    DEFAULT_TEMPLATE_SCHEDULE, current_window, fix_schedule, next_window, schedule_from_json, validate_schedule,
)

GOOD = {
    "date": "2026-08-16",
    "windows": [
        {"kind": "sleep", "start": "2026-08-16T23:00", "end": "2026-08-17T07:00"},
        {"kind": "daily", "start": "2026-08-16T09:00", "end": "2026-08-16T11:00",
         "activity": "写代码", "plan_speak": False, "topic": ""},
        {"kind": "greeting", "start": "2026-08-16T07:00", "end": "2026-08-16T08:00",
         "activity": "起床", "plan_speak": True, "topic": "早安"},
    ],
}


def test_validate_good():
    data, err = validate_schedule(GOOD, min_sleep=240, max_sleep=660)
    assert err == "" and data is not None


def test_validate_two_sleep_windows():
    bad = {**GOOD, "windows": GOOD["windows"] + [dict(GOOD["windows"][0])]}
    data, err = validate_schedule(bad, min_sleep=240, max_sleep=660)
    assert data is None and "睡眠窗口" in err


def test_validate_sleep_too_short():
    bad = _copy.deepcopy(GOOD)
    bad["windows"][0]["end"] = "2026-08-16T23:30"  # 30min < 240
    data, err = validate_schedule(bad, min_sleep=240, max_sleep=660)
    assert data is None and "最短" in err


def test_validate_overlap():
    bad = _copy.deepcopy(GOOD)
    bad["windows"][1]["start"] = "2026-08-16T07:30"  # 与 greeting 重叠
    data, err = validate_schedule(bad, min_sleep=240, max_sleep=660)
    assert data is None and "重叠" in err


def test_validate_too_many_activities():
    bad = _copy.deepcopy(GOOD)
    for i in range(9):
        bad["windows"].append({"kind": "daily", "start": f"2026-08-16T0{i}:00", "end": f"2026-08-16T0{i}:30",
                               "activity": f"活动{i}", "plan_speak": False, "topic": ""})
    data, err = validate_schedule(bad, min_sleep=240, max_sleep=660)
    assert data is None and "活动窗口" in err


def test_parse_from_llm_with_fence():
    import json as _json
    text = "```json\n" + _json.dumps(GOOD, ensure_ascii=False) + "\n```"
    data, err = schedule_from_json(text)
    assert err == "" and data == GOOD


def test_current_and_next_window():
    data = GOOD
    cur = current_window(data, "2026-08-16T09:30")
    assert cur and cur["activity"] == "写代码"
    nxt = next_window(data, "2026-08-16T09:30")
    assert nxt and nxt["kind"] == "sleep"
    assert current_window(data, "2026-08-16T06:00") is None  # 空白时间=自由时间
