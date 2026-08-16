"""自然衰减测试:互动时间判定/判定解析/扫描执行。"""
from datetime import datetime
from catsitate_core.config import FavorabilitySection
from catsitate_core.decay import DecayExecutor, parse_decay_response, last_bot_interaction_time
from catsitate_core.favorability import BatchEngine
from catsitate_core.storage import SQLiteStore

NOW = datetime(2026, 8, 15, 12, 0, 0)


def _msg(user_id: str, ts: str, quote: str = "", mentioned: str = "") -> dict:
    msg = {"timestamp": ts, "message_info": {"user_info": {"user_id": user_id}}, "reply_to": quote}
    if mentioned:
        msg["raw_message"] = [{"type": "at", "data": {"target_user_id": mentioned}}]
    return msg


def test_parse_decay_response_ok():
    assert parse_decay_response('{"delta": -2, "note": "有点生疏了"}') == (-2, "有点生疏了")


def test_parse_decay_response_rejects_positive():
    assert parse_decay_response('{"delta": 2, "note": "x"}')[0] is None


def test_parse_decay_response_bad_json():
    assert parse_decay_response("随便")[0] is None


def test_last_bot_interaction_private_any_bot_msg():
    msgs = [_msg("u1", "2026-08-10T10:00:00"), _msg("3545773341", "2026-08-12T10:00:00")]
    assert last_bot_interaction_time(msgs, "u1", "3545773341", stream_is_group=False) == "2026-08-12T10:00:00"


def test_last_bot_interaction_group_only_quote_or_mention():
    # bot 回应了别人(无 quote),不算 u1 的互动
    msgs = [_msg("u2", "2026-08-10T10:00:00"), _msg("3545773341", "2026-08-12T10:00:00")]
    assert last_bot_interaction_time(msgs, "u1", "3545773341", stream_is_group=True) is None
    # bot quote 了 u1 的消息
    msgs2 = [_msg("u2", "2026-08-10T10:00:00"), _msg("3545773341", "2026-08-12T10:00:00", quote="<msg u1>")]
    assert last_bot_interaction_time(msgs2, "u1", "3545773341", stream_is_group=True) == "2026-08-12T10:00:00"
    # bot @ 了 u1(消息含 at 段)
    at_msg = {"timestamp": "2026-08-12T10:00:00", "message_info": {"user_info": {"user_id": "3545773341"}},
              "raw_message": [{"type": "at", "data": {"target_user_id": "u1"}}]}
    assert last_bot_interaction_time([at_msg], "u1", "3545773341", stream_is_group=True) == "2026-08-12T10:00:00"


def test_scan_and_apply_skips_recent_interaction(tmp_path):
    store = SQLiteStore(tmp_path / "d.db")
    cfg = FavorabilitySection(decay_after_days=7, decay_max=3)
    engine = BatchEngine(store, cfg)
    engine.ensure_schema()
    engine.apply_delta("u1", 42, "很好", judged_at="2026-08-14T12:00:00")
    calls: list = []

    async def fake_llm(messages, model=""):
        calls.append(messages)
        return {"success": True, "response": '{"delta": -2, "note": "生疏了"}', "model": model}

    ex = DecayExecutor(store, cfg, fake_llm)
    import asyncio
    # 有互动(8-12 日 bot 消息),不衰减
    result = asyncio.run(ex.scan_and_apply([("u1", "s1", "2026-08-12T10:00:00", "0")], now=lambda: NOW))
    assert result == [] and not calls


def test_scan_and_apply_applies_and_clamps(tmp_path):
    store = SQLiteStore(tmp_path / "d.db")
    cfg = FavorabilitySection(decay_after_days=7, decay_max=3)
    engine = BatchEngine(store, cfg)
    engine.ensure_schema()
    engine.apply_delta("u1", 42, "很好", judged_at="2026-08-14T12:00:00")
    calls: list = []

    async def fake_llm(messages, model=""):
        calls.append(messages)
        return {"success": True, "response": '{"delta": -9, "note": "生疏了"}', "model": model}

    import asyncio
    ex = DecayExecutor(store, cfg, fake_llm)
    result = asyncio.run(ex.scan_and_apply([("u1", "s1", "2026-08-01T10:00:00", "0")], now=lambda: NOW))
    assert result and result[0]["delta"] == -3  # 钳制到 -decay_max
    assert engine.get_level("u1")["score"] == 39


def test_scan_and_apply_judge_id_unique_per_user(tmp_path):
    """M-5:同秒多用户衰减 judge_id 含 user/stream,INSERT OR IGNORE 不静默丢日志(重置计时 guard 有效)。"""
    import asyncio
    store = SQLiteStore(tmp_path / "d.db")
    cfg = FavorabilitySection(decay_after_days=7, decay_max=3)
    engine = BatchEngine(store, cfg)
    engine.ensure_schema()
    engine.apply_delta("u1", 42, "很好", judged_at="2026-08-01T12:00:00")
    engine.apply_delta("u2", 42, "很好", judged_at="2026-08-01T12:00:00")

    async def fake_llm(messages, model=""):
        return {"success": True, "response": '{"delta": -2, "note": "生疏了"}', "model": model}

    ex = DecayExecutor(store, cfg, fake_llm)
    result = asyncio.run(ex.scan_and_apply(
        [("u1", "s1", "2026-08-01T10:00:00", "0"), ("u2", "s2", "2026-08-01T10:00:00", "0")],
        now=lambda: NOW,
    ))
    assert len(result) == 2  # 两条都判定成功(judge_id 无冲突)
    rows = store.query("SELECT judge_id FROM favorability_log WHERE judge_id LIKE 'decay-%'")
    assert len(rows) == 2 and len({r[0] for r in rows}) == 2  # 判定日志互不覆盖
