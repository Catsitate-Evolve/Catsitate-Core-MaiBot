"""贴表情引擎测试:冷却护栏/白名单选择 prompt/结果解析。"""

from datetime import datetime, timedelta

from catsitate_core.config import MsgReactSection
from catsitate_core.msg_react import MsgReactEngine, parse_choice_resp
from catsitate_core.storage import JsonSnapshot

NOW = datetime(2026, 8, 14, 12, 0, 0)
WHITELIST = ["em_ok", "em_laugh", "em_hug"]


def make_engine(tmp_path):
    snapshot = JsonSnapshot(tmp_path / "react_cooldown.json")
    engine = MsgReactEngine(snapshot, MsgReactSection())
    return engine


def test_cooldown_blocks_within_window(tmp_path):
    engine = make_engine(tmp_path)
    assert engine.check_cooldown("s1", now=lambda: NOW)[0] is True
    engine.mark_used("s1", now=lambda: NOW)
    assert engine.check_cooldown("s1", now=lambda: NOW)[0] is False
    later = NOW + timedelta(seconds=31)
    assert engine.check_cooldown("s1", now=lambda: later)[0] is True


def test_cooldown_is_per_stream(tmp_path):
    engine = make_engine(tmp_path)
    engine.mark_used("s1", now=lambda: NOW)
    assert engine.check_cooldown("s2", now=lambda: NOW)[0] is True


def test_build_choose_prompt_stable_prefix_first(tmp_path):
    engine = make_engine(tmp_path)
    messages, cache_key = engine.build_choose_prompt(WHITELIST, "今天好累", "想安慰对方")
    # 稳定段(指令 system + 白名单)在前、变量(消息+意图)在后
    assert messages[0]["role"] == "system"
    assert "em_ok" in messages[1]["content"]  # 白名单在稳定段
    assert "今天好累" in messages[-2]["content"]
    assert "想安慰对方" in messages[-1]["content"]
    assert cache_key


def test_parse_choice_valid():
    assert parse_choice_resp('{"emoji_id": "em_laugh"}', WHITELIST) == ("em_laugh", "")


def test_parse_choice_out_of_whitelist_rejected(tmp_path):
    result = parse_choice_resp('{"emoji_id": "em_evil"}', WHITELIST)
    assert result[0] is None and result[1]


def test_parse_choice_invalid_json(tmp_path):
    result = parse_choice_resp("随便回一句", WHITELIST)
    assert result[0] is None and result[1]
