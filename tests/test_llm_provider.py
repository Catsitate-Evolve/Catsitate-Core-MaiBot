"""旁路 prompt 组装辅助测试(稳定段前置纪律)。"""

import pytest

from catsitate_core.llm_provider import SIDE_TEMPLATES, build_side_prompt


def test_stable_prefix_first():
    messages, cache_key = build_side_prompt("favorability", ["五级规则稳定段"], ["素材1", "素材2"])
    assert messages[0] == {"role": "system", "content": SIDE_TEMPLATES["favorability"]["system"]}
    assert messages[1]["role"] == "user"
    assert "五级规则稳定段" in messages[1]["content"]
    assert messages[-2]["content"] == "素材1"
    assert messages[-1]["content"] == "素材2"
    assert cache_key.startswith("favorability:v1+")  # 版本+内容哈希


def test_tail_changes_do_not_change_prefix():
    m1, k1 = build_side_prompt("favorability", ["稳定"], ["甲"])
    m2, k2 = build_side_prompt("favorability", ["稳定"], ["乙"])
    assert k1 == k2
    assert m1[:-1] == m2[:-1]
    assert m1[-1] != m2[-1]


def test_stable_ctx_changes_shift_tail_only():
    m1, k1 = build_side_prompt("msg_react", ["白名单A"], ["目标消息"])
    m2, k2 = build_side_prompt("msg_react", ["白名单B"], ["目标消息"])
    assert k1 == k2
    assert m1[0] == m2[0]
    assert m1[1] != m2[1]
    assert m1[2] == m2[2]  # 变量尾不受影响


def test_all_templates_share_contract():
    for tid in ("favorability", "msg_react", "sentinel", "image_relook"):
        messages, key = build_side_prompt(tid, ["稳定"], ["变量"])
        assert messages[0]["role"] == "system"
        assert key.startswith(f"{tid}:v")


def test_unknown_template_raises():
    with pytest.raises(ValueError, match="未知"):
        build_side_prompt("nope", [], [])
