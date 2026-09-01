"""日程模型测试:校验/修复/窗口定位/默认模板。"""
import copy as _copy

from catsitate_core.schedule import (
    DEFAULT_TEMPLATE_SCHEDULE, current_window, fix_schedule, next_window, schedule_from_json,
    schedule_overview_text, validate_schedule,
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


def test_schedule_overview_text():
    from catsitate_core.schedule import schedule_overview_text
    data = {"date": "2026-08-16", "windows": [
        {"kind": "sleep", "start": "2026-08-16T23:00", "end": "2026-08-17T07:30", "activity": ""},
        {"kind": "daily", "start": "2026-08-16T09:00", "end": "2026-08-16T11:00", "activity": "发呆"},
    ]}
    text = schedule_overview_text(data)
    assert "0. 睡眠 23:00-07:30" in text
    assert "1. 活动 09:00-11:00 发呆" in text
    assert schedule_overview_text({}) == "(空)"


def test_parse_hm():
    from catsitate_core.schedule import parse_hm
    assert parse_hm("11:45", "2026-08-16") == "2026-08-16T11:45"
    assert parse_hm(" 16:00 ", "2026-08-16") == "2026-08-16T16:00"
    assert parse_hm("bad", "2026-08-16") is None
    assert parse_hm("25:00", "2026-08-16") is None


def test_move_window_hhmm_and_shift(tmp_path):
    from catsitate_core.schedule import apply_schedule_move
    data = {"date": "2026-08-16", "windows": [
        {"kind": "sleep", "start": "2026-08-16T23:00", "end": "2026-08-17T07:30", "activity": ""},
        {"kind": "daily", "start": "2026-08-16T15:00", "end": "2026-08-16T18:00", "activity": "听歌"},
    ]}
    out, err, hist, adj = apply_schedule_move(data, 0, "11:45", "16:00", "2026-08-16", min_sleep=240, max_sleep=660, history=[])
    assert err == ""
    sleep = next(w for w in out["windows"] if w["kind"] == "sleep")
    assert sleep["start"] == "2026-08-16T11:45" and sleep["end"] == "2026-08-16T16:00"
    # 听歌 15:00-18:00 与睡眠(锚点)重叠 → 头部压缩到 16:00-18:00(不整体顺延)
    song = next(w for w in out["windows"] if w["kind"] != "sleep")
    assert song["start"] == "2026-08-16T16:00" and song["end"] == "2026-08-16T18:00"
    assert hist and adj  # 压缩明细非空


def test_move_sleep_keeps_kind(tmp_path):
    from catsitate_core.schedule import apply_schedule_move
    data = {"date": "2026-08-16", "windows": [
        {"kind": "sleep", "start": "2026-08-16T23:00", "end": "2026-08-17T07:30", "activity": ""},
        {"kind": "daily", "start": "2026-08-16T09:00", "end": "2026-08-16T11:00", "activity": "发呆"},
    ]}
    out, err, _, _ = apply_schedule_move(data, 0, "11:45", "16:00", "2026-08-16", min_sleep=240, max_sleep=660, history=[])
    assert err == "" and any(w["kind"] == "sleep" for w in out["windows"])  # move 保持 sleep(排序后首位未必是睡眠)


def test_add_window_hhmm(tmp_path):
    from catsitate_core.schedule import apply_schedule_add
    data = {"date": "2026-08-16", "windows": [
        {"kind": "sleep", "start": "2026-08-16T23:00", "end": "2026-08-17T07:30", "activity": ""},
    ]}
    out, err, hist, _ = apply_schedule_add(data, "16:00", "18:00", "和Hesitate_P一起听歌", "2026-08-16", min_sleep=240, max_sleep=660, history=[])
    assert err == ""
    new_w = next(w for w in out["windows"] if w["kind"] == "daily")
    assert new_w["start"] == "2026-08-16T16:00" and "听歌" in new_w["activity"]
    assert hist


def test_add_anchor_squeezes_earlier_window_tail(tmp_path):
    from catsitate_core.schedule import apply_schedule_add
    data = {"date": "2026-08-16", "windows": [
        {"kind": "sleep", "start": "2026-08-16T23:00", "end": "2026-08-17T07:30", "activity": ""},
        {"kind": "daily", "start": "2026-08-16T09:00", "end": "2026-08-16T11:00", "activity": "发呆"},
    ]}
    # 新窗口(锚点)10:30-12:00 在旧窗之后 → 旧窗尾部压缩 09:00-10:30
    out, err, _, adj = apply_schedule_add(data, "10:30", "12:00", "买菜", "2026-08-16", min_sleep=240, max_sleep=660, history=[])
    assert err == ""
    old_w = next(w for w in out["windows"] if w.get("activity") == "发呆")
    assert old_w["end"] == "2026-08-16T10:30"
    assert any("发呆" in a for a in adj)


def test_add_anchor_fully_covers_window_rejected(tmp_path):
    from catsitate_core.schedule import apply_schedule_add
    data = {"date": "2026-08-16", "windows": [
        {"kind": "sleep", "start": "2026-08-16T23:00", "end": "2026-08-17T07:30", "activity": ""},
        {"kind": "daily", "start": "2026-08-16T10:30", "end": "2026-08-16T11:00", "activity": "短暂活动"},
    ]}
    # 新窗口 10:00-12:00 完全覆盖旧窗 10:30-11:00 → 挤没拒绝(Q1=A)
    out, err, _, _ = apply_schedule_add(data, "10:00", "12:00", "大块活动", "2026-08-16", min_sleep=240, max_sleep=660, history=[])
    assert "挤没" in err
    assert out == data


def test_add_anchor_squeezes_two_windows_both_sides(tmp_path):
    from catsitate_core.schedule import apply_schedule_add
    data = {"date": "2026-08-16", "windows": [
        {"kind": "sleep", "start": "2026-08-16T23:00", "end": "2026-08-17T07:30", "activity": ""},
        {"kind": "daily", "start": "2026-08-16T09:00", "end": "2026-08-16T11:00", "activity": "前窗"},
        {"kind": "daily", "start": "2026-08-16T15:00", "end": "2026-08-16T18:00", "activity": "后窗"},
    ]}
    # 锚点 10:30-16:30:前窗尾部压缩到 09:00-10:30,后窗头部压缩到 16:30-18:00
    out, err, _, adj = apply_schedule_add(data, "10:30", "16:30", "大块活动", "2026-08-16", min_sleep=240, max_sleep=660, history=[])
    assert err == ""
    front = next(w for w in out["windows"] if w.get("activity") == "前窗")
    back = next(w for w in out["windows"] if w.get("activity") == "后窗")
    assert front["end"] == "2026-08-16T10:30"
    assert back["start"] == "2026-08-16T16:30" and back["end"] == "2026-08-16T18:00"
    assert len(adj) == 2  # 两个窗口都被压缩且都有明细


def test_add_anchor_chain_squeezes_two_following(tmp_path):
    from catsitate_core.schedule import apply_schedule_add
    data = {"date": "2026-08-16", "windows": [
        {"kind": "sleep", "start": "2026-08-16T23:00", "end": "2026-08-17T07:30", "activity": ""},
        {"kind": "daily", "start": "2026-08-16T14:00", "end": "2026-08-16T15:00", "activity": "A"},
        {"kind": "daily", "start": "2026-08-16T15:30", "end": "2026-08-16T17:00", "activity": "B"},
    ]}
    # 锚点 14:30-16:00:A 尾部压至 14:00-14:30;B 头部压至 16:00-17:00
    out, err, _, adj = apply_schedule_add(data, "14:30", "16:00", "插入活动", "2026-08-16", min_sleep=240, max_sleep=660, history=[])
    assert err == ""
    a = next(w for w in out["windows"] if w.get("activity") == "A")
    b = next(w for w in out["windows"] if w.get("activity") == "B")
    assert a["end"] == "2026-08-16T14:30"
    assert b["start"] == "2026-08-16T16:00" and b["end"] == "2026-08-16T17:00"
    assert len(adj) == 2


def test_add_cross_midnight_anchor_pushes_sleep_bedtime(tmp_path):
    from catsitate_core.schedule import apply_schedule_add, validate_schedule
    data = {"date": "2026-08-16", "windows": [
        {"kind": "sleep", "start": "2026-08-16T23:00", "end": "2026-08-17T07:30", "activity": ""},
        {"kind": "daily", "start": "2026-08-16T09:00", "end": "2026-08-16T11:00", "activity": "发呆"},
    ]}
    # 跨午夜锚点 23:30-01:00 压睡眠尾侧:入睡推迟到 01:00,醒来 07:30 不变
    out, err, _, adj = apply_schedule_add(data, "23:30", "01:00", "深夜活动", "2026-08-16", min_sleep=240, max_sleep=660, history=[])
    assert err == ""
    sleep = next(w for w in out["windows"] if w.get("kind") == "sleep")
    assert sleep["start"] == "2026-08-17T01:00"
    assert sleep["end"] == "2026-08-17T07:30"
    assert any("睡觉" in a for a in adj)
    assert validate_schedule(out, min_sleep=240, max_sleep=660)[1] == ""


def test_add_anchor_squeezes_sleep_below_min_rejected(tmp_path):
    from catsitate_core.schedule import apply_schedule_add
    data = {"date": "2026-08-16", "windows": [
        {"kind": "sleep", "start": "2026-08-16T23:00", "end": "2026-08-17T07:30", "activity": ""},
        {"kind": "daily", "start": "2026-08-16T09:00", "end": "2026-08-16T11:00", "activity": "发呆"},
    ]}
    # 入睡被推到 06:30 只剩 60 分钟 < 最短 240 → 拒绝且原数据不变
    out, err, _, _ = apply_schedule_add(data, "23:30", "06:30", "深夜活动", "2026-08-16", min_sleep=240, max_sleep=660, history=[])
    assert "睡眠不足" in err
    assert out == data


def test_add_anchor_fully_covers_sleep_rejected(tmp_path):
    from catsitate_core.schedule import apply_schedule_add
    data = {"date": "2026-08-16", "windows": [
        {"kind": "sleep", "start": "2026-08-16T23:00", "end": "2026-08-17T07:30", "activity": ""},
        {"kind": "daily", "start": "2026-08-16T09:00", "end": "2026-08-16T11:00", "activity": "发呆"},
    ]}
    # 锚点 22:00-08:00 完全盖住睡眠窗 → 挤没拒绝(不是「睡眠不足」)
    out, err, _, _ = apply_schedule_add(data, "22:00", "08:00", "大块活动", "2026-08-16", min_sleep=240, max_sleep=660, history=[])
    assert "挤没" in err
    assert out == data


def test_move_anchor_compresses_overlapping_window(tmp_path):
    from catsitate_core.schedule import apply_schedule_move, validate_schedule
    data = {"date": "2026-08-16", "windows": [
        {"kind": "sleep", "start": "2026-08-16T23:00", "end": "2026-08-17T07:30", "activity": ""},
        {"kind": "daily", "start": "2026-08-16T09:00", "end": "2026-08-16T11:00", "activity": "发呆"},
        {"kind": "daily", "start": "2026-08-16T15:00", "end": "2026-08-16T18:00", "activity": "听歌"},
    ]}
    # move 窗口 1(发呆)到 15:00-16:00:锚点挤「听歌」头部到 16:00
    out, err, _, adj = apply_schedule_move(data, 1, "15:00", "16:00", "2026-08-16", min_sleep=240, max_sleep=660, history=[])
    assert err == ""
    song = next(w for w in out["windows"] if w.get("activity") == "听歌")
    assert song["start"] == "2026-08-16T16:00" and song["end"] == "2026-08-16T18:00"
    assert any("听歌" in a for a in adj)
    assert validate_schedule(out, min_sleep=240, max_sleep=660)[1] == ""


def test_add_no_overlap_no_adjustments(tmp_path):
    from catsitate_core.schedule import apply_schedule_add
    data = {"date": "2026-08-16", "windows": [
        {"kind": "sleep", "start": "2026-08-16T23:00", "end": "2026-08-17T07:30", "activity": ""},
        {"kind": "daily", "start": "2026-08-16T09:00", "end": "2026-08-16T11:00", "activity": "发呆"},
    ]}
    out, err, _, adj = apply_schedule_add(data, "13:00", "14:00", "买菜", "2026-08-16", min_sleep=240, max_sleep=660, history=[])
    assert err == ""
    assert adj == []  # 无重叠:任何窗口都不得被记入压缩明细


def test_compress_chain_with_overlapping_followers(tmp_path):
    from catsitate_core.schedule import compress_with_anchor
    # 直接喂「锚点后窗口彼此重叠」的输入:链式循环触发,B/C 依次头部后推
    windows = [
        {"kind": "daily", "start": "2026-08-16T10:00", "end": "2026-08-16T12:00", "activity": "锚点"},
        {"kind": "daily", "start": "2026-08-16T12:30", "end": "2026-08-16T14:00", "activity": "A"},
        {"kind": "daily", "start": "2026-08-16T13:00", "end": "2026-08-16T15:00", "activity": "B"},
        {"kind": "daily", "start": "2026-08-16T14:30", "end": "2026-08-16T16:00", "activity": "C"},
    ]
    out, err, adj = compress_with_anchor(windows, 0)
    assert err == ""
    by = {w["activity"]: w for w in out}
    assert by["A"]["start"] == "2026-08-16T12:30"  # 与锚点不重叠,不动
    assert by["B"]["start"] == "2026-08-16T14:00"  # 被 A 尾部链式后推
    assert by["C"]["start"] == "2026-08-16T15:00"  # 再被 B 尾部链式后推
    assert len(adj) == 2


def test_compress_chain_squeeze_out_rejected(tmp_path):
    from catsitate_core.schedule import compress_with_anchor
    # 链上窗口被推到挤没 → 拒绝,返回错误
    windows = [
        {"kind": "daily", "start": "2026-08-16T10:00", "end": "2026-08-16T12:00", "activity": "锚点"},
        {"kind": "daily", "start": "2026-08-16T12:30", "end": "2026-08-16T14:00", "activity": "A"},
        {"kind": "daily", "start": "2026-08-16T13:00", "end": "2026-08-16T13:30", "activity": "短窗"},
    ]
    _, err, _ = compress_with_anchor(windows, 0)
    assert "挤没" in err


def test_edit_result_windows_time_ordered(tmp_path):
    from catsitate_core.schedule import apply_schedule_add, schedule_overview_text
    data = {"date": "2026-08-16", "windows": [
        {"kind": "sleep", "start": "2026-08-16T23:00", "end": "2026-08-17T07:30", "activity": ""},
        {"kind": "daily", "start": "2026-08-16T09:00", "end": "2026-08-16T11:00", "activity": "发呆"},
        {"kind": "daily", "start": "2026-08-16T15:00", "end": "2026-08-16T18:00", "activity": "随便做点什么"},
    ]}
    # 读书追加到存储尾部,但结果必须按时间排序:读书 15:00 排在随便做点什么 16:00 之前
    out, err, _, _ = apply_schedule_add(data, "15:00", "16:00", "读书", "2026-08-16", min_sleep=240, max_sleep=660, history=[])
    assert err == ""
    starts = [w["start"] for w in out["windows"]]
    assert starts == sorted(starts)
    text = schedule_overview_text(out)
    assert text.index("读书") < text.index("随便做点什么")


def test_materialize_template_time_ordered():
    from catsitate_core.schedule import _materialize_template
    out = _materialize_template(DEFAULT_TEMPLATE_SCHEDULE, "2026-08-16")
    starts = [w["start"] for w in out["windows"]]
    assert starts == sorted(starts)


def test_seconds_format_tolerated_and_normalized():
    from catsitate_core.schedule import ScheduleGenerator
    from catsitate_core.config import ScheduleSection, SleepSection
    import asyncio, json as _json
    # LLM 偶发带秒的时间:校验通过、落库归一化到分钟(实机 WARN「unconverted data remains: :00」)
    good = {"date": "2026-08-17", "windows": [
        {"kind": "sleep", "start": "2026-08-17T23:00:00", "end": "2026-08-18T07:30:00"},
        {"kind": "daily", "start": "2026-08-17T09:00:00", "end": "2026-08-17T11:00:00",
         "activity": "写代码", "plan_speak": False, "topic": ""},
    ]}
    async def fake_llm(messages, model=""):
        return {"success": True, "response": _json.dumps(good, ensure_ascii=False), "model": model}
    gen = ScheduleGenerator(fake_llm, ScheduleSection(), SleepSection())
    data, err = asyncio.run(gen.generate(persona="", today_review="", weather_text="", fav_summary="", due_memos=[]))
    assert err == ""
    for w in data["windows"]:
        assert len(w["start"]) == 16 and len(w["end"]) == 16  # 归一化到 YYYY-MM-DDTHH:MM
    sleep = next(w for w in data["windows"] if w["kind"] == "sleep")
    assert sleep["end"] == "2026-08-18T07:30"  # 跨午夜秒格式归一化后仍是分钟精度


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
        persona="猫耳少女", behavior_style="温柔陪伴", today_review="睡了8小时", weather_text="多云",
        fav_summary="无", due_memos=["周四交作业(19:00)"], min_sleep=240, max_sleep=660,
        target_date="2026-08-16",
    )
    assert messages[0]["role"] == "system"
    assert key
    assert any("周四交作业" in m["content"] for m in messages)
    # 稳定段(系统模板之后的 user 段):人设在行为风格之前(顺序固定,前缀缓存),变量尾在后
    stable_text = "\n".join(m["content"] for m in messages[1:] if m["role"] == "user")
    assert stable_text.index("bot 人设:猫耳少女") < stable_text.index("bot 行为风格:温柔陪伴")
    assert stable_text.index("bot 行为风格:温柔陪伴") < stable_text.index("周四交作业")


def test_build_generate_prompt_style_optional():
    messages, _ = build_schedule_generate_prompt(
        persona="猫耳少女", behavior_style="", today_review="", weather_text="",
        fav_summary="无", due_memos=[], min_sleep=240, max_sleep=660,
        target_date="2026-08-16",
    )
    assert not any("行为风格" in m["content"] for m in messages)


def test_generator_valid_output(tmp_path):
    import asyncio
    from catsitate_core.config import ScheduleSection, SleepSection
    from catsitate_core.schedule import sort_windows, validate_schedule
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
    # 生成结果规范化为时间顺序(睡眠 23:00 排到活动之后)
    expected = {"date": good["date"], "windows": sort_windows(good["windows"])}
    assert err == "" and data == expected


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


def test_fix_schedule_zero_sleep_inserts_default_and_validates():
    """I-1:0 个睡眠窗口 → 插入默认睡眠段,修复结果恰好 1 睡眠且校验通过。"""
    data = {"date": "2026-08-16", "windows": [
        {"kind": "daily", "start": "2026-08-16T09:00", "end": "2026-08-16T11:00",
         "activity": "写代码", "plan_speak": False, "topic": ""},
    ]}
    fixed = fix_schedule(data, min_sleep=240, max_sleep=660)
    sleeps = [w for w in fixed["windows"] if w.get("kind") == "sleep"]
    assert len(sleeps) == 1 and sleeps[0]["start"] == "2026-08-16T23:00"
    checked, verr = validate_schedule(fixed, min_sleep=240, max_sleep=660)
    assert checked is not None and verr == ""


def test_fix_schedule_multiple_sleep_keeps_exactly_one():
    """I-1:2+ 个睡眠窗口 → 只保留第一个,修复后恰好 1 个且校验通过。"""
    data = {"date": "2026-08-16", "windows": [
        {"kind": "sleep", "start": "2026-08-16T23:00", "end": "2026-08-17T07:00"},
        {"kind": "sleep", "start": "2026-08-16T12:00", "end": "2026-08-16T14:00"},
        {"kind": "daily", "start": "2026-08-16T09:00", "end": "2026-08-16T11:00",
         "activity": "写代码", "plan_speak": False, "topic": ""},
    ]}
    fixed = fix_schedule(data, min_sleep=240, max_sleep=660)
    sleeps = [w for w in fixed["windows"] if w.get("kind") == "sleep"]
    assert len(sleeps) == 1 and sleeps[0]["start"] == "2026-08-16T23:00"
    checked, verr = validate_schedule(fixed, min_sleep=240, max_sleep=660)
    assert checked is not None and verr == ""


def test_generator_fix_still_invalid_returns_template_with_error():
    """I-1 兜底链:钳制修复后仍无效(如活动窗口为 0)→ 默认模板 + 显式错误文案。"""
    import asyncio
    from catsitate_core.config import ScheduleSection, SleepSection
    async def fake_llm(messages, model=""):
        return {"success": True, "response": '{"date": "2026-08-16", "windows": []}', "model": model}
    gen = ScheduleGenerator(fake_llm, ScheduleSection(max_regenerate=0), SleepSection())
    data, err = asyncio.run(gen.generate(persona="", today_review="", weather_text="", fav_summary="", due_memos=[]))
    assert "仍无效" in err  # 显式错误(调用方记录日志)
    checked, verr = validate_schedule(data, min_sleep=240, max_sleep=660)
    assert checked is not None and verr == ""  # 模板兜底必合法


from catsitate_core.schedule import threshold_met


def test_threshold_met():
    assert threshold_met("熟悉", "熟悉") is True
    assert threshold_met("亲近", "熟悉") is True  # 等级高于门槛
    assert threshold_met("陌生", "熟悉") is False


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


def test_fix_schedule_strips_illegal_qzone_attr():
    bad = _copy.deepcopy(GOOD)
    bad["windows"][0]["qzone"] = True  # 睡眠窗口的旧键残留(迁移清理一并剥除)
    bad["windows"][0]["read_qzone"] = True  # 睡眠窗口的非法标记
    bad["windows"][1]["read_qzone"] = True  # daily 窗口的合法标记(应保留)
    bad["windows"][2]["send_qzone"] = True  # greeting 窗口的非法标记
    fixed = fix_schedule(bad, min_sleep=240, max_sleep=660)
    w0, w1, w2 = fixed["windows"][0], fixed["windows"][1], fixed["windows"][2]
    assert not w0.get("qzone") and not w0.get("read_qzone") and not w2.get("send_qzone")
    assert w1.get("read_qzone") is True  # daily 的合法标记保留


def test_overview_marks_qzone_window():
    data = _copy.deepcopy(GOOD)
    data["windows"][1]["read_qzone"] = True
    data["windows"][1]["send_qzone"] = True
    text = schedule_overview_text(data)
    assert "(刷空间)" in text and "(发说说)" in text
    legacy = _copy.deepcopy(GOOD)
    legacy["windows"][1]["qzone"] = True  # 旧键不再消费 → 不标注
    assert "(刷空间)" not in schedule_overview_text(legacy)


def test_validate_schedule_attribute_split():
    """read_qzone/send_qzone 仅 daily 窗口合法;旧键 qzone 不再消费(放行)。"""
    base = {"date": "2026-09-02", "windows": [
        {"kind": "sleep", "start": "2026-09-01T23:00", "end": "2026-09-02T07:30"},
        {"kind": "daily", "start": "2026-09-02T09:00", "end": "2026-09-02T12:00",
         "activity": "窝着刷手机", "read_qzone": True, "send_qzone": True},
    ]}
    data, err = validate_schedule(base, min_sleep=240, max_sleep=660)
    assert err == "" and data is not None
    # 非 daily 窗口带任一新键 → 拒绝
    bad = {"date": "2026-09-02", "windows": [
        {"kind": "sleep", "start": "2026-09-01T23:00", "end": "2026-09-02T07:30", "read_qzone": True},
        {"kind": "daily", "start": "2026-09-02T09:00", "end": "2026-09-02T12:00", "activity": "x"},
    ]}
    data2, err2 = validate_schedule(bad, min_sleep=240, max_sleep=660)
    assert data2 is None and "read_qzone" in err2
    bad2 = {"date": "2026-09-02", "windows": [
        {"kind": "sleep", "start": "2026-09-01T23:00", "end": "2026-09-02T07:30"},
        {"kind": "greeting", "start": "2026-09-02T09:00", "end": "2026-09-02T10:00",
         "activity": "早安", "send_qzone": True},
    ]}
    data3, err3 = validate_schedule(bad2, min_sleep=240, max_sleep=660)
    assert data3 is None and "send_qzone" in err3
    # 旧键 qzone 不再消费:非 daily 窗口带旧键不再校验拒绝(消费点已迁移)
    legacy = {"date": "2026-09-02", "windows": [
        {"kind": "sleep", "start": "2026-09-01T23:00", "end": "2026-09-02T07:30", "qzone": True},
        {"kind": "daily", "start": "2026-09-02T09:00", "end": "2026-09-02T12:00", "activity": "x"},
    ]}
    data4, err4 = validate_schedule(legacy, min_sleep=240, max_sleep=660)
    assert err4 == "" and data4 is not None


def test_fix_schedule_strips_attributes_on_nondaily():
    day = "2026-09-02"
    data = {"date": day, "windows": [
        {"kind": "sleep", "start": "2026-09-01T23:00", "end": "2026-09-02T07:30",
         "read_qzone": True, "send_qzone": True},
        {"kind": "daily", "start": "2026-09-02T09:00", "end": "2026-09-02T12:00",
         "activity": "窝着刷手机", "read_qzone": True},
    ]}
    fixed = fix_schedule(data, min_sleep=240, max_sleep=660)
    sleep_win = fixed["windows"][0]
    assert "read_qzone" not in sleep_win and "send_qzone" not in sleep_win
    assert fixed["windows"][1].get("read_qzone") is True


def test_schedule_generate_template_v4_splits_qzone_attributes():
    from catsitate_core.llm_provider import SIDE_TEMPLATES

    assert SIDE_TEMPLATES["schedule_generate"]["version"] == 4
    assert "read_qzone" in SIDE_TEMPLATES["schedule_generate"]["system"]
    assert "send_qzone" in SIDE_TEMPLATES["schedule_generate"]["system"]
