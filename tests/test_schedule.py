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


from catsitate_core.schedule import ScheduleGenerator, build_schedule_generate_prompt


def test_build_generate_prompt_stable_first():
    messages, key = build_schedule_generate_prompt(
        persona="猫耳少女", today_review="睡了8小时", weather_text="多云",
        fav_summary="无", due_memos=["周四交作业(19:00)"], min_sleep=240, max_sleep=660,
        target_date="2026-08-16",
    )
    assert messages[0]["role"] == "system"
    assert key
    assert any("周四交作业" in m["content"] for m in messages)


def test_generator_valid_output(tmp_path):
    import asyncio
    from catsitate_core.config import ScheduleSection, SleepSection
    from catsitate_core.schedule import validate_schedule
    good = {"date": "2026-08-16", "windows": [
        {"kind": "sleep", "start": "2026-08-16T23:00", "end": "2026-08-17T07:00"},
        {"kind": "daily", "start": "2026-08-16T09:00", "end": "2026-08-16T11:00",
         "activity": "写代码", "plan_speak": False, "topic": ""},
    ]}
    import json as _json
    async def fake_llm(messages, model=""):
        return {"success": True, "response": _json.dumps(good, ensure_ascii=False), "model": model}
    gen = ScheduleGenerator(fake_llm, ScheduleSection(), SleepSection())
    data, err = asyncio.run(gen.generate(persona="猫耳少女", today_review="", weather_text="", fav_summary="", due_memos=[]))
    assert err == "" and data == good


def test_generator_retry_then_fix(tmp_path):
    import asyncio, json as _json
    from catsitate_core.config import ScheduleSection, SleepSection
    bad = {"date": "2026-08-16", "windows": [
        {"kind": "sleep", "start": "2026-08-16T23:00", "end": "2026-08-17T02:00"},  # 3h < 240min? 3h=180 < 240 非法
        {"kind": "daily", "start": "2026-08-16T09:00", "end": "2026-08-16T11:00",
         "activity": "写代码", "plan_speak": False, "topic": ""},
    ]}
    count = {"n": 0}
    async def fake_llm(messages, model=""):
        count["n"] += 1
        return {"success": True, "response": _json.dumps(bad, ensure_ascii=False), "model": model}
    gen = ScheduleGenerator(fake_llm, ScheduleSection(max_regenerate=1), SleepSection())
    data, err = asyncio.run(gen.generate(persona="猫耳少女", today_review="", weather_text="", fav_summary="", due_memos=[]))
    assert count["n"] == 2  # 首次 + 重生成 1 次
    # 仍失败 → 钳制修复(睡眠钳到最短 240)
    assert err == ""
    fixed, verr = validate_schedule(data, min_sleep=240, max_sleep=660)
    assert fixed is not None and verr == ""


def test_generator_llm_failure_uses_template(tmp_path):
    import asyncio
    from catsitate_core.config import ScheduleSection, SleepSection
    async def fake_llm(messages, model=""):
        raise RuntimeError("boom")
    gen = ScheduleGenerator(fake_llm, ScheduleSection(), SleepSection())
    data, err = asyncio.run(gen.generate(persona="", today_review="", weather_text="", fav_summary="", due_memos=[]))
    assert err != ""  # 显式失败信息(调用方记录日志)
    assert data.get("windows")  # 兜底默认模板


from catsitate_core.schedule import build_speak_prompt, parse_speak_response, threshold_met


def test_threshold_met():
    assert threshold_met("熟悉", "熟悉") is True
    assert threshold_met("亲近", "熟悉") is True  # 等级高于门槛
    assert threshold_met("陌生", "熟悉") is False


def test_parse_speak_response():
    data, err = parse_speak_response('{"speak": true, "stream_index": 0, "text": "今天天气不错"}', candidate_count=2)
    assert err == "" and data == {"speak": True, "stream_index": 0, "text": "今天天气不错"}
    data2, err2 = parse_speak_response('{"speak": false, "text": ""}', candidate_count=0)
    assert err2 == "" and data2["speak"] is False
    assert parse_speak_response("没想好", candidate_count=0)[0] is None
    # stream_index 越界拒绝
    assert parse_speak_response('{"speak": true, "stream_index": 5, "text": "x"}', candidate_count=2)[0] is None


def test_build_speak_prompt_window_content_first():
    messages, key = build_speak_prompt(
        persona="猫耳少女",
        window={"kind": "daily", "activity": "发呆看雨", "plan_speak": False, "topic": ""},
        day_overview="今天:散步→发呆→早睡,整体懒散",
        weather_text="雷暴,30°C",
        candidates=[{"stream_id": "s1", "user_id": "u1", "level_name": "熟悉", "note": "无"}],
    )
    assert messages[0]["role"] == "system"
    assert any("发呆看雨" in m["content"] for m in messages)
    assert any("散步→发呆→早睡" in m["content"] for m in messages)
    assert any("候选流列表" in m["content"] for m in messages)
    assert key


from catsitate_core.schedule import ACTIVITY_WINDOW_LIMIT, apply_schedule_edit

BASE = {"date": "2026-08-16", "windows": [
    {"kind": "sleep", "start": "2026-08-16T23:00", "end": "2026-08-17T07:00"},
    {"kind": "daily", "start": "2026-08-16T09:00", "end": "2026-08-16T11:00",
     "activity": "写代码", "plan_speak": False, "topic": "", "speak_kind": "daily"},
]}


def test_edit_add_activity():
    data, err, hist = apply_schedule_edit(
        dict(BASE), "add", None,
        {"kind": "daily", "start": "2026-08-16T14:00", "end": "2026-08-16T16:00",
         "activity": "买菜", "plan_speak": False, "topic": ""},
        [], min_sleep=240, max_sleep=660,
    )
    assert err == "" and len(data["windows"]) == 3 and hist


def test_edit_add_over_limit_rejected_with_reason():
    data = _copy.deepcopy(BASE)  # 深拷贝:dict(BASE) 浅拷贝会污染共享 BASE["windows"] 列表
    for i in range(7):
        data["windows"].append({"kind": "daily", "start": f"2026-08-16T1{i}:00", "end": f"2026-08-16T1{i}:30",
                                "activity": f"活动{i}", "plan_speak": False, "topic": ""})
    out, err, _ = apply_schedule_edit(
        data, "add", None,
        {"kind": "daily", "start": "2026-08-16T18:00", "end": "2026-08-16T19:00",
         "activity": "x", "plan_speak": False, "topic": ""},
        [], min_sleep=240, max_sleep=660,
    )
    assert err == "今天的日程已经排得满满当当了,再排下去会累坏的,明天再安排吧。"


def test_edit_cannot_delete_sleep():
    out, err, _ = apply_schedule_edit(dict(BASE), "delete", 0, None, [], min_sleep=240, max_sleep=660)
    assert "睡眠窗口不可删除" in err and out == BASE


def test_edit_sleep_time_respects_min_max():
    out, err, _ = apply_schedule_edit(
        dict(BASE), "update", 0,
        {"kind": "sleep", "start": "2026-08-16T23:00", "end": "2026-08-17T01:00"},
        [], min_sleep=240, max_sleep=660,
    )
    assert "最短" in err  # 2h < 240min 拒绝
