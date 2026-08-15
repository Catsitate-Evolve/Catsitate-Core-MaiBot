"""好感度结算执行器测试:LLM 判定/顺延/失败不落库/块渲染。"""

from datetime import datetime

from catsitate_core.config import FavorabilitySection
from catsitate_core.favorability import BatchEngine, SettleExecutor, build_favorability_block, parse_judge_response
from catsitate_core.storage import SQLiteStore

NOW = datetime(2026, 8, 14, 12, 0, 0)


def make_executor(tmp_path, llm_result=None, daily_min=None):
    store = SQLiteStore(tmp_path / "s.db")
    section = FavorabilitySection()
    if daily_min is not None:
        section.daily_settle_min = daily_min
    engine = BatchEngine(store, section)
    engine.ensure_schema()
    calls: list = []

    async def fake_llm(messages, model=""):
        calls.append(messages)
        if isinstance(llm_result, Exception):
            raise llm_result
        return llm_result or {"success": True, "response": '{"delta": 2, "note": "聊得不错"}', "model": model}

    return SettleExecutor(engine, fake_llm), engine, calls


def test_settle_empty_material_fails(tmp_path):
    import asyncio
    executor, engine, calls = make_executor(tmp_path)
    # history 无目标用户消息 → build_material 返回空 → 不调 LLM 直接 failed
    history = [
        {"role": "user", "user_id": "other", "stream_id": "s1", "text": "别人的消息", "seq": 0, "ts": "2026-08-14T11:00:00"}
    ]
    result = asyncio.run(executor.settle("u1", "s1", history, kind="early"))
    assert result["status"] == "failed" and "素材为空" in result["error"]
    assert not calls  # 未调 LLM


def test_settle_includes_persona(tmp_path):
    import asyncio
    executor, engine, calls = make_executor(tmp_path)
    history = [
        {"role": "user", "user_id": "u1", "stream_id": "s1", "text": f"消息{i}", "seq": i, "ts": f"2026-08-14T11:{i:02d}:00"}
        for i in range(10)
    ]
    result = asyncio.run(executor.settle("u1", "s1", history, kind="early", persona="猫耳少女,话少"))
    assert result["status"] == "ok"
    all_text = " ".join(m["content"] for m in calls[-1] if m.get("content"))
    assert "bot 人设:猫耳少女,话少" in all_text


def test_settle_without_persona_no_persona_line(tmp_path):
    import asyncio
    executor, engine, calls = make_executor(tmp_path)
    history = [
        {"role": "user", "user_id": "u1", "stream_id": "s1", "text": f"消息{i}", "seq": i, "ts": f"2026-08-14T11:{i:02d}:00"}
        for i in range(10)
    ]
    result = asyncio.run(executor.settle("u1", "s1", history, kind="early"))
    assert result["status"] == "ok"
    all_text = " ".join(m["content"] for m in calls[-1] if m.get("content"))
    assert "bot 人设:" not in all_text  # 无 persona 时不含人设背景行(system 模板自带「bot 人设」字样)


def test_parse_judge_response_basic():
    assert parse_judge_response('{"delta": 2, "note": "不错"}') == {"delta": 2, "note": "不错"}


def test_parse_judge_response_markdown_fence():
    text = '```json\n{"delta": -1, "note": "敷衍"}\n```'
    assert parse_judge_response(text) == {"delta": -1, "note": "敷衍"}


def test_parse_judge_response_invalid():
    assert parse_judge_response("delta=2") is None
    assert parse_judge_response('{"delta": "x", "note": "y"}') is None


def test_parse_judge_response_non_object_json():
    assert parse_judge_response('"str"') is None
    assert parse_judge_response("[]") is None
    assert parse_judge_response("42") is None


def test_daily_carry_over_when_below_min(tmp_path):
    import asyncio
    executor, engine, calls = make_executor(tmp_path, daily_min=3)
    history = [{"role": "user", "user_id": "u1", "stream_id": "s1", "text": "早", "seq": 1}]
    result = asyncio.run(executor.settle("u1", "s1", history, kind="daily"))
    assert result["status"] == "carried_over"
    assert calls == []  # 未调用 LLM
    rows = engine.store.query("SELECT count FROM batch_counter WHERE user_id = 'u1' AND stream_id = 's1'")
    assert not rows or rows[0][0] == 0  # 顺延未写计数、未清零


def test_settle_ok_applies_delta_and_resets(tmp_path):
    import asyncio
    executor, engine, calls = make_executor(tmp_path)
    for _ in range(20):
        engine.count_message("u1", "s1", now=lambda: NOW)
    history = [
        {"role": "user", "user_id": "u1", "stream_id": "s1", "text": "早", "seq": i}
        for i in range(20)
    ]
    result = asyncio.run(executor.settle("u1", "s1", history, kind="early"))
    assert result["status"] == "ok"
    assert result["delta"] == 2
    assert engine.get_level("u1", "s1")["score"] == 2
    rows = engine.store.query("SELECT count FROM batch_counter WHERE user_id = 'u1' AND stream_id = 's1'")
    assert rows[0][0] == 0  # 结算后批次计数清零
    assert calls and calls[0][0]["role"] == "system"  # 稳定段前置


def test_settle_llm_failure_keeps_state(tmp_path):
    import asyncio
    executor, engine, calls = make_executor(tmp_path, llm_result=Exception("boom"))
    for _ in range(20):
        engine.count_message("u1", "s1", now=lambda: NOW)
    history = [
        {"role": "user", "user_id": "u1", "stream_id": "s1", "text": "早", "seq": i}
        for i in range(20)
    ]
    result = asyncio.run(executor.settle("u1", "s1", history, kind="early"))
    assert result["status"] == "failed"
    assert engine.get_level("u1", "s1") is None
    rows = engine.store.query("SELECT count FROM batch_counter WHERE user_id = 'u1' AND stream_id = 's1'")
    assert rows[0][0] == 20  # 失败不重置


def test_settle_parse_failure_keeps_state(tmp_path):
    import asyncio
    executor, engine, calls = make_executor(tmp_path, llm_result={"success": True, "response": "不是JSON", "model": ""})
    for _ in range(20):
        engine.count_message("u1", "s1", now=lambda: NOW)
    history = [
        {"role": "user", "user_id": "u1", "stream_id": "s1", "text": "早", "seq": i}
        for i in range(20)
    ]
    result = asyncio.run(executor.settle("u1", "s1", history, kind="early"))
    assert result["status"] == "failed"
    assert engine.get_level("u1", "s1") is None  # 不落库
    rows = engine.store.query("SELECT count FROM batch_counter WHERE user_id = 'u1' AND stream_id = 's1'")
    assert rows[0][0] == 20  # 不重置


def test_settle_delta_clamped(tmp_path):
    import asyncio
    executor, engine, calls = make_executor(tmp_path, llm_result={"success": True, "response": '{"delta": 99, "note": "超出范围"}', "model": ""})
    for _ in range(20):
        engine.count_message("u1", "s1", now=lambda: NOW)
    history = [
        {"role": "user", "user_id": "u1", "stream_id": "s1", "text": "早", "seq": i}
        for i in range(20)
    ]
    result = asyncio.run(executor.settle("u1", "s1", history, kind="early"))
    assert result["status"] == "ok"
    assert result["delta"] == 5  # 钳制到 +5
    assert engine.get_level("u1", "s1")["score"] == 5


def test_favorability_block_render(tmp_path):
    from datetime import datetime as dt
    executor, engine, _ = make_executor(tmp_path)
    engine.apply_delta("u1", "s1", 42, "最近主动关心过你", judged_at=dt.now().strftime("%Y-%m-%dT%H:%M:%S"))
    text = build_favorability_block(engine, "u1", "s1")
    assert "[好感度] u1:等级「亲近」(累计 42)" in text
    assert "最近主动关心过你" in text


def test_favorability_block_default_stranger(tmp_path):
    executor, engine, _ = make_executor(tmp_path)
    text = build_favorability_block(engine, "newbie", "s1")
    assert "等级「陌生」" in text
    assert "注记" not in text


def test_delta_clamped_by_config(tmp_path):
    import asyncio
    from catsitate_core.config import FavorabilitySection
    executor, engine, _ = make_executor(tmp_path)
    engine.config = FavorabilitySection(delta_max=2)
    executor.engine = engine
    history = [
        {"role": "user", "user_id": "u1", "stream_id": "s1", "text": f"消息{i}", "seq": i, "ts": f"2026-08-14T11:{i:02d}:00"}
        for i in range(10)
    ]
    result = asyncio.run(executor.settle("u1", "s1", history, kind="early"))
    assert result["status"] == "ok"
    assert result["delta"] == 2  # fake LLM 返回 5,被 delta_max=2 钳制


def test_level_rules_list_five_levels():
    from catsitate_core.config import FavorabilitySection
    cfg = FavorabilitySection()
    rules = cfg.level_rules_list()
    assert len(rules) == 5
    assert any("熟悉" in r and "可自然闲聊" in r for r in rules)
    assert cfg.level_rule_by_name("亲近") == "关系较好,可主动关心"


def test_favorability_block_include_rule_single_level(tmp_path):
    executor, engine, _ = make_executor(tmp_path)
    from datetime import datetime as dt
    engine.apply_delta("u1", "s1", 42, "最近主动关心过你", judged_at=dt.now().strftime("%Y-%m-%dT%H:%M:%S"))
    text = build_favorability_block(engine, "u1", "s1", include_rule=True)
    lines = text.split("\n")
    # 规则行在最前,且只含当前等级(亲近)一条;其余等级不出现
    assert "规则「亲近」" in lines[0]
    assert "可主动关心" in lines[0]
    assert "仅按普通网友对待" not in text and "可分享心事" not in text
    assert "[好感度] u1:等级「亲近」(累计 42)" in lines[1]


def test_favorability_block_include_rule_stranger(tmp_path):
    executor, engine, _ = make_executor(tmp_path)
    text = build_favorability_block(engine, "newbie", "s1", include_rule=True)
    assert "规则「陌生」" in text and "仅按普通网友对待" in text


def test_iter_today_active_and_daily_settle_check(tmp_path):
    executor, engine, _ = make_executor(tmp_path)
    engine.count_message("u1", "s1", now=lambda: NOW)
    engine.count_message("u2", "s1", now=lambda: NOW)
    active = engine.iter_today_active(now=lambda: NOW)
    assert ("u1", "s1") in active and ("u2", "s1") in active
    assert engine.has_daily_settle_today("u1", "s1", now=lambda: NOW) is False
    engine.apply_delta(
        "u1", "s1", 1, "日终",
        judged_at=NOW.strftime("%Y-%m-%dT%H:%M:%S"),
        judge_id=f"daily-{NOW.strftime('%Y-%m-%dT%H:%M:%S')}",
    )
    assert engine.has_daily_settle_today("u1", "s1", now=lambda: NOW) is True
