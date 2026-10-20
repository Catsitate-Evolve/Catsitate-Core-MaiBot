"""贴表情引擎测试:冷却护栏/白名单选择 prompt/结果解析。"""

from datetime import datetime, timedelta

from catsitate_core.config import MsgReactSection
from catsitate_core.msg_react import MsgReactEngine, parse_choice_resp
from catsitate_core.storage import JsonSnapshot

NOW = datetime(2026, 8, 14, 12, 0, 0)
VALID_IDS = ["9", "13", "182"]  # 表内 id(9 大哭 / 13 呲牙 / 182 笑哭)


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
    assert "【安慰/关心】" in messages[1]["content"]  # 分组标题在稳定段
    assert "106 委屈" in messages[1]["content"]  # 表情条目在稳定段
    assert "今天好累" in messages[-2]["content"]
    assert "想安慰对方" in messages[-1]["content"]
    assert cache_key


def test_parse_choice_valid():
    assert parse_choice_resp('{"emoji_id": "5"}') == ("5", "")


def test_parse_choice_out_of_table_rejected(tmp_path):
    result = parse_choice_resp('{"emoji_id": "9999"}')  # 9999 不在表内
    assert result[0] is None and result[1]


def test_parse_choice_invalid_json(tmp_path):
    result = parse_choice_resp("随便回一句")
    assert result[0] is None and result[1]
    result2 = parse_choice_resp("[]")  # 合法 JSON 非对象同样拒绝
    assert result2[0] is None and result2[1]


def test_parse_choice_bare_braces(tmp_path):
    # 无围栏时前后杂文本中提取第一段裸花括号(与 parse_judge_response 兜底一致)
    result = parse_choice_resp('好的,我选:\n{"emoji_id": "13"}\n就这样')
    assert result == ("13", "")


def test_emoji_table_grouped_and_consistent():
    """分组表与扁平表一致:每项属于至少一组,组内 id 均在扁平表内。"""
    from catsitate_core.qq_emoji import AVAILABLE_REACT_EMOJIS, REACT_EMOJI_GROUPS

    assert len(AVAILABLE_REACT_EMOJIS) >= 100, "表情表应已扩容"
    grouped: set[int] = set()
    for label, hint, ids in REACT_EMOJI_GROUPS:
        assert label and hint
        for i in ids:
            assert i in AVAILABLE_REACT_EMOJIS, f"{label} 引用了表外 id {i}"
            grouped.add(i)
    assert grouped == set(AVAILABLE_REACT_EMOJIS), "存在未归组的表情"


def test_alias_hints_and_single_intent_contract():
    """直连参数已移除:工具只有 message_id+intent;俗称提示供选择器 prompt 使用。"""
    import inspect

    from catsitate_core.qq_emoji import EMOJI_ALIAS_HINTS
    from plugin import CatsitatePlugin

    info = getattr(CatsitatePlugin.msg_react, "__maibot_component_info__")
    assert [p.name for p in info.parameters] == ["message_id", "intent"]
    assert "选择器" in info.description and "意图" in info.description
    assert "狗头=doge" in EMOJI_ALIAS_HINTS and "点赞=" in EMOJI_ALIAS_HINTS
    # 选择器 prompt 的稳定段同时含分组表与俗称对照
    p = object.__new__(CatsitatePlugin)
    from catsitate_core.config import MsgReactSection
    from catsitate_core.msg_react import MsgReactEngine
    from catsitate_core.storage import JsonSnapshot
    import tempfile, pathlib
    tmp = pathlib.Path(tempfile.mkdtemp())
    engine = MsgReactEngine(JsonSnapshot(tmp / "r.json"), MsgReactSection())
    messages, _ = engine.build_choose_prompt("今天好累", "想安慰对方")
    stable = messages[1]["content"]
    assert "【安慰/关心】" in stable and "俗称对照" in stable
    assert "今天好累" in messages[-2]["content"]
