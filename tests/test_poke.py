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
    # 实机载荷格式:raw_info.nm 常为空串、顶层 user_id 为发起者、target_id==self_id 即戳 bot
    payload = {
        "user_id": "3341299096",
        "target_id": "3545773341",
        "self_id": "3545773341",
        "raw_info": [{"nm": "", "uid": "3341299096", "remark": "该睡了"}],
    }
    text = engine.enhance_notice_text(payload, fallback_nickname="小猫")
    assert text is not None
    # 用户建议格式:昵称(qq号) 戳了 目标;目标是 bot 自身渲染为「你」
    assert "小猫(3341299096)" in text
    assert "戳了 你" in text
    assert "该睡了" in text  # 附言并入拟人文本


def test_enhance_notice_text_other_target(tmp_path):
    engine = make_engine(tmp_path)
    payload = {
        "user_id": "3341299096",
        "target_id": "9999",
        "self_id": "3545773341",
        "raw_info": [{"nm": "小明", "uid": "3341299096"}],
    }
    text = engine.enhance_notice_text(payload)
    assert "小明(3341299096)" in text
    assert "戳了 (9999)" in text  # 目标非 bot 且无昵称来源,退化为 qq 号


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
