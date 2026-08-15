"""贴表情引擎测试:冷却护栏/白名单选择 prompt/结果解析。"""

from datetime import datetime, timedelta

from catsitate_core.config import MsgReactSection
from catsitate_core.msg_react import MsgReactEngine, parse_choice_resp
from catsitate_core.storage import JsonSnapshot

NOW = datetime(2026, 8, 14, 12, 0, 0)
VALID_IDS = ["0", "1", "2"]  # 内置 QQ 表情表(0 惊讶 / 1 撇嘴 / 2 色)


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
    messages, cache_key = engine.build_choose_prompt("今天好累", "想安慰对方")
    # 稳定段(指令 system + 表情表)在前、变量(消息+意图)在后
    assert messages[0]["role"] == "system"
    assert "可选表情" in messages[1]["content"]
    assert "0 惊讶" in messages[1]["content"]  # 表情表在稳定段
    assert "今天好累" in messages[-2]["content"]
    assert "想安慰对方" in messages[-1]["content"]
    assert cache_key


def test_parse_choice_valid():
    assert parse_choice_resp('{"emoji_id": "1"}') == ("1", "")


def test_parse_choice_out_of_table_rejected(tmp_path):
    result = parse_choice_resp('{"emoji_id": "999999"}')
    assert result[0] is None and result[1]


def test_parse_choice_invalid_json(tmp_path):
    result = parse_choice_resp("随便回一句")
    assert result[0] is None and result[1]
    result2 = parse_choice_resp("[]")  # 合法 JSON 非对象同样拒绝
    assert result2[0] is None and result2[1]


def test_parse_choice_bare_braces(tmp_path):
    # 无围栏时前后杂文本中提取第一段裸花括号(与 parse_judge_response 兜底一致)
    result = parse_choice_resp('好的,我选:\n{"emoji_id": "1"}\n就这样')
    assert result == ("1", "")
