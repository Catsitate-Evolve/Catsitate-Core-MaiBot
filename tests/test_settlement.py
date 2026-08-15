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
