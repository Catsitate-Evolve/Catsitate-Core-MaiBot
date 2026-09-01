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


def test_qzone_scene_template_declared():
    """可读性优化(2026-09-01):空间场景文案入 SIDE_TEMPLATES(WebUI 可覆盖),
    version=2;说明〔〕参数行与工具参数名(feed_id/comment_id/at_user_id)映射。"""
    t = SIDE_TEMPLATES["qzone_scene"]
    assert t["version"] == 2
    assert "刷QQ空间" in t["system"]
    assert "〔〕括号里的是工具参数" in t["system"]
    assert "feed_id" in t["system"] and "comment_id" in t["system"] and "at_user_id" in t["system"]
    assert "qzone_comment" in t["system"] and "qzone_reply" in t["system"] and "qzone_like" in t["system"]


def test_qzone_diary_template_declared():
    """M3 表达:日记生成模板入 SIDE_TEMPLATES(version=1)——睡前以第一人称写
    当日日记发布为说说;自包含指令(80~200 字/不编造/基于素材/直接输出正文)。"""
    t = SIDE_TEMPLATES["qzone_diary"]
    assert t["version"] == 1
    s = t["system"]
    assert "睡前" in s and "日记" in s and "说说" in s
    assert "80~200字" in s and "第一人称" in s
    assert "不要编造" in s  # 内容必须基于当日素材
    assert "直接输出日记正文" in s  # 无 JSON 包裹,纯文本产出
    assert "简体中文" in s
    messages, key = build_side_prompt("qzone_diary", ["今天的日程:发呆"], [])
    assert messages[0]["role"] == "system"
    assert messages[1]["content"] == "今天的日程:发呆"
    assert key.startswith("qzone_diary:v1+")
