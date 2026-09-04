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


def test_last_bot_interaction_group_only_at_mention():
    # bot 回应了别人(无 @),不算 u1 的互动
    msgs = [_msg("u2", "2026-08-10T10:00:00"), _msg("3545773341", "2026-08-12T10:00:00")]
    assert last_bot_interaction_time(msgs, "u1", "3545773341", stream_is_group=True) is None
    # 仅有原始 reply_to(纯消息 id,未注入 resolved_quote_user_id = 解析失败/未解析)不命中;
    # quote 命中需 plugin.py _daily_decay 预解析注入字段(最终审查 I2 恢复)
    msgs2 = [_msg("u2", "2026-08-10T10:00:00"), _msg("3545773341", "2026-08-12T10:00:00", quote="<msg u1>")]
    assert last_bot_interaction_time(msgs2, "u1", "3545773341", stream_is_group=True) is None
    # bot @ 了 u1(消息含 at 段)
    at_msg = {"timestamp": "2026-08-12T10:00:00", "message_info": {"user_info": {"user_id": "3545773341"}},
              "raw_message": [{"type": "at", "data": {"target_user_id": "u1"}}]}
    assert last_bot_interaction_time([at_msg], "u1", "3545773341", stream_is_group=True) == "2026-08-12T10:00:00"


def test_last_bot_interaction_group_quote_hit_after_resolve():
    """quote 命中:bot 消息 reply 段经 message.get_by_id 解析原发送者,
    由 plugin.py 预解析注入 resolved_quote_user_id —— == 目标则互动命中。"""

    # 解析出的原发送者 == 目标 u1 → 命中
    q_hit = {"timestamp": "2026-08-12T10:00:00",
             "message_info": {"user_info": {"user_id": "3545773341"}},
             "reply_to": "msg-1", "resolved_quote_user_id": "u1"}
    assert last_bot_interaction_time([q_hit], "u1", "3545773341", stream_is_group=True) == "2026-08-12T10:00:00"
    # 解析出的原发送者 != 目标(bot 回别人不得重置本用户计时)→ 不命中
    q_other = {"timestamp": "2026-08-12T10:00:00",
               "message_info": {"user_info": {"user_id": "3545773341"}},
               "reply_to": "msg-2", "resolved_quote_user_id": "u2"}
    assert last_bot_interaction_time([q_other], "u1", "3545773341", stream_is_group=True) is None
    # 解析失败未注入字段(仅有原始 reply_to)→ 不命中(与 at 用例区分)
    q_raw = _msg("3545773341", "2026-08-12T10:00:00", quote="msg-3")
    assert last_bot_interaction_time([q_raw], "u1", "3545773341", stream_is_group=True) is None
    # at + quote 同时存在:任一命中即互动
    q_at = {"timestamp": "2026-08-12T10:00:00",
            "message_info": {"user_info": {"user_id": "3545773341"}},
            "reply_to": "msg-4", "resolved_quote_user_id": "u2",
            "raw_message": [{"type": "at", "data": {"target_user_id": "u1"}}]}
    assert last_bot_interaction_time([q_at], "u1", "3545773341", stream_is_group=True) == "2026-08-12T10:00:00"


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
    result = asyncio.run(ex.scan_and_apply([("u1", "2026-08-12T10:00:00")], now=lambda: NOW))
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
    result = asyncio.run(ex.scan_and_apply([("u1", "2026-08-01T10:00:00")], now=lambda: NOW))
    assert result and result[0]["delta"] == -3  # 钳制到 -decay_max
    assert engine.get_level("u1")["score"] == 39


def test_scan_and_apply_judge_id_unique_per_user(tmp_path):
    """M-5:同秒多用户衰减 judge_id 含 user,INSERT OR IGNORE 不静默丢日志(重置计时 guard 有效)。"""
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
        [("u1", "2026-08-01T10:00:00"), ("u2", "2026-08-01T10:00:00")],
        now=lambda: NOW,
    ))
    assert len(result) == 2  # 两条都判定成功(judge_id 无冲突)
    rows = store.query("SELECT judge_id FROM favorability_log WHERE judge_id LIKE 'decay-%'")
    assert len(rows) == 2 and len({r[0] for r in rows}) == 2  # 判定日志互不覆盖


def test_scan_and_apply_exclusive_clamped_passthrough(tmp_path):
    """钳制状态透传:他人占据「特别」时,B 衰减(delta=0)不触发钳制,
    仅透传 apply_delta 状态即可,断言调用不抛错且结果含 exclusive_clamped 标记。"""
    import asyncio
    store = SQLiteStore(tmp_path / "d.db")
    cfg = FavorabilitySection(decay_after_days=7, decay_max=3)
    engine = BatchEngine(store, cfg)
    engine.ensure_schema()
    # A 占「特别」(score>=100,独占位被占据);B 是待衰减老用户
    engine.apply_delta("A", 100, "特别之选", judged_at="2026-08-01T12:00:00")
    engine.apply_delta("B", 42, "很好", judged_at="2026-08-01T12:00:00")

    async def fake_llm(messages, model=""):
        return {"success": True, "response": '{"delta": 0, "note": "无变化"}', "model": model}

    ex = DecayExecutor(store, cfg, fake_llm)
    result = asyncio.run(ex.scan_and_apply([("B", "2026-08-01T10:00:00")], now=lambda: NOW))
    assert result and result[0]["exclusive_clamped"] is False
    assert engine.get_level("B")["score"] == 42  # delta=0 不落分
