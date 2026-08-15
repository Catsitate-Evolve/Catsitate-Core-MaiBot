"""戳一戳引擎测试:通知解析/主动戳前置校验/冷却。"""

from datetime import datetime, timedelta

from catsitate_core.config import PokeSection
from catsitate_core.poke import PokeEngine
from catsitate_core.storage import JsonSnapshot

NOW = datetime(2026, 8, 14, 12, 0, 0)


def make_engine(tmp_path):
    snapshot = JsonSnapshot(tmp_path / "poke_cooldown.json")
    engine = PokeEngine(snapshot, PokeSection())
    return engine


def test_parse_notice_ok(tmp_path):
    engine = make_engine(tmp_path)
    payload = {
        "user_id": "123",
        "target_id": "456",
        "raw_info": [{"user_id": "123", "nickname": "小猫", "action": "拍了拍", "target": "你"}],
    }
    parsed = engine.parse_notice(payload)
    assert parsed is not None
    assert "小猫" in parsed["text"]
    assert parsed["user_id"] == "123"


def test_parse_notice_malformed_returns_none(tmp_path):
    engine = make_engine(tmp_path)
    assert engine.parse_notice({"foo": 1}) is None


def test_enhance_notice_text_renders(tmp_path):
    engine = make_engine(tmp_path)
    payload = {
        "raw_info": [{"nickname": "小猫", "action": "拍了拍", "target": "你", "remark": "该睡了"}],
    }
    text = engine.enhance_notice_text(payload)
    assert text is not None
    assert "小猫" in text and "拍了拍" in text
    assert "该睡了" in text  # 附言并入拟人文本


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
