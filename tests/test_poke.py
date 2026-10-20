"""戳一戳引擎测试:主动戳前置校验/冷却。"""

from datetime import datetime, timedelta

from catsitate_core.config import PokeSection
from catsitate_core.poke import PokeEngine
from catsitate_core.storage import JsonSnapshot

NOW = datetime(2026, 8, 14, 12, 0, 0)


def make_engine(tmp_path):
    snapshot = JsonSnapshot(tmp_path / "poke_cooldown.json")
    engine = PokeEngine(snapshot, PokeSection())
    return engine


def test_can_poke_no_cooldown_record(tmp_path):
    engine = make_engine(tmp_path)
    ok, _ = engine.can_poke("u1", now=lambda: NOW)
    assert ok is True
    engine.mark_poked("u1", now=lambda: NOW)
    ok2, reason2 = engine.can_poke("u1", now=lambda: NOW)
    assert ok2 is False
    assert "冷却" in reason2


def test_can_poke_cooldown_expires(tmp_path):
    engine = make_engine(tmp_path)
    engine.mark_poked("u1", now=lambda: NOW)
    later = NOW + timedelta(seconds=601)
    assert engine.can_poke("u1", now=lambda: later)[0] is True


def _bare_plugin(message=None, streams=None):
    """构造仅带解析所需依赖的插件实例(不经 on_load)。"""
    import logging
    from types import SimpleNamespace

    from plugin import CatsitatePlugin

    p = object.__new__(CatsitatePlugin)
    p._stream_cache = {}
    p._stream_cache_at = 0.0

    async def call_capability(name, **kwargs):
        assert name == "message.get_by_id"
        return message

    class _Chat:
        async def get_all_streams(self, platform="qq"):
            return streams or []

    p._ctx = SimpleNamespace(
        call_capability=call_capability,
        chat=_Chat(),
        logger=logging.getLogger("test.poke"),
    )
    return p


def test_resolve_poke_target_by_msg_id():
    """msg_id 模式:目标与群号均取自消息,模型无需传群号。"""
    import asyncio

    msg = {"message_info": {"user_info": {"user_id": "3341299096"},
                            "group_info": {"group_id": "172496324"}}}
    p = _bare_plugin(message=msg)
    assert asyncio.run(p._resolve_poke_target("m1", "", "s1")) == ("3341299096", "172496324", "")


def test_resolve_poke_target_conflicting_user_id_rejected():
    import asyncio

    msg = {"message_info": {"user_info": {"user_id": "3341299096"}}}
    p = _bare_plugin(message=msg)
    target, group, err = asyncio.run(p._resolve_poke_target("m1", "999", "s1"))
    assert not target and "不一致" in err


def test_resolve_poke_target_by_user_id_uses_stream_group():
    """user_id 模式:群上下文取自当前流;私聊流群号为空。"""
    import asyncio

    group_stream = [{"session_id": "g1", "group_id": "172496324", "is_group_session": True}]
    p = _bare_plugin(streams=group_stream)
    assert asyncio.run(p._resolve_poke_target("", "3341299096", "g1")) == ("3341299096", "172496324", "")
    private_stream = [{"session_id": "p1", "group_id": "", "is_group_session": False}]
    p2 = _bare_plugin(streams=private_stream)
    assert asyncio.run(p2._resolve_poke_target("", "3341299096", "p1")) == ("3341299096", "", "")


def test_resolve_poke_target_requires_one_input():
    import asyncio

    p = _bare_plugin()
    target, group, err = asyncio.run(p._resolve_poke_target("", "", "s1"))
    assert not target and "msg_id" in err and "user_id" in err


def test_poke_tool_no_longer_accepts_group_id():
    """群号不由模型传:工具参数中不得再有 group_id。"""
    import inspect

    from plugin import CatsitatePlugin

    src = inspect.getsource(CatsitatePlugin.poke_user.__wrapped__ if hasattr(CatsitatePlugin.poke_user, "__wrapped__") else CatsitatePlugin.poke_user)
    assert "group_id: str" not in src


def test_drift_log_carries_structured_fields(caplog):
    """偏移取证日志:统一前缀 + 预期/实际/细节三字段(供 grep 收集下一版适配证据)。"""
    import logging

    p = _bare_plugin()
    with caplog.at_level(logging.WARNING):
        p._log_drift("api:x", expected="dict(status/success)", actual="list", detail="len=3")
    msg = caplog.records[-1].getMessage()
    assert "[适配偏移]" in msg and "api:x" in msg
    assert "expected=dict(status/success)" in msg and "actual=list" in msg and "detail=len=3" in msg


def test_api_ok_known_shapes_pass_without_drift(caplog):
    import logging

    p = _bare_plugin()
    with caplog.at_level(logging.WARNING):
        assert p._api_ok({"status": "ok"}) is True
        assert p._api_ok({"success": True}) is True
        assert p._api_ok({"status": "failed", "retcode": 100}) is False  # 已知失败形态:不记偏移
        drift = [r for r in caplog.records if "[适配偏移]" in r.getMessage()]
        assert not drift, "已知形态不得产生偏移日志"


def test_api_ok_unknown_shape_logs_drift(caplog):
    """适配器回执改成未知结构时:返回 False 且留下可 grep 的偏移证据。"""
    import logging

    p = _bare_plugin()
    with caplog.at_level(logging.WARNING):
        assert p._api_ok({"retcode": 0, "data": {}}, capability="adapter.test") is False
        assert p._api_ok("boom", capability="adapter.test") is False
    msgs = [r.getMessage() for r in caplog.records if "[适配偏移]" in r.getMessage()]
    assert len(msgs) == 2
    assert "keys=[data,retcode]" in msgs[0]
    assert "actual=str" in msgs[1]
