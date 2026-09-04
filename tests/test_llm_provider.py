"""旁路 prompt 组装辅助测试(稳定段前置纪律)。"""

import pytest

from catsitate_core.llm_provider import (
    SIDE_TEMPLATES,
    build_side_prompt,
    load_side_system,
    rpc_error_brief,
)


def test_stable_prefix_first():
    messages, cache_key = build_side_prompt("favorability", ["五级规则稳定段"], ["素材1", "素材2"])
    assert messages[0] == {"role": "system", "content": SIDE_TEMPLATES["favorability"]["system"]}
    assert messages[1]["role"] == "user"
    assert "五级规则稳定段" in messages[1]["content"]
    assert messages[-2]["content"] == "素材1"
    assert messages[-1]["content"] == "素材2"
    assert cache_key.startswith("favorability:v2+")  # 版本+内容哈希(清理时随模板升 v2)


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
    """空间场景文案入 SIDE_TEMPLATES(WebUI 可覆盖),version=5——说明〔〕参数行、
    工具参数名(feed_id/comment_id/at_user_id/content)映射;润色架构(content
    由 planner 直写,发出前自动按口吻顺一遍);互动通知含点赞(「赞了你」,
    feed_id 归属含 qzone_like)。"""
    t = SIDE_TEMPLATES["qzone_scene"]
    assert t["version"] == 5
    assert "刷QQ空间" in t["system"]
    assert "〔〕括号里的是工具参数" in t["system"]
    assert "feed_id" in t["system"] and "comment_id" in t["system"] and "at_user_id" in t["system"]
    assert "qzone_comment" in t["system"] and "qzone_reply" in t["system"] and "qzone_like" in t["system"]
    assert "qzone_post" in t["system"]  # M3 表达:分享心情发自己的说说
    assert "content 直接写" in t["system"]  # 润色架构:planner 直写,自动顺口吻


def test_qzone_expression_template_preserves_facts():
    """润色模板 v6(2026-09-02 用户裁定):改写许可之外加「不修改关键事实部分」
    ——完全重组许可曾把人名/数字/明确说过的话改掉,事实保持约束进模板。"""
    t = SIDE_TEMPLATES["qzone_expression"]
    assert t["version"] == 6
    s = t["system"]
    assert "你可以完全重组内容" in s  # 重组许可保留(怎么说仍自由)
    assert "不要修改关键事实部分" in s  # 事实保持约束
    assert "人名、数字、时间、地点" in s and "明确说过的话、做过的事" in s
    assert "改写后的内容:" in s


def test_qzone_diary_template_declared():
    """M3 表达:日记生成模板入 SIDE_TEMPLATES(v6,照搬 diary_plugin prompts.py
    原文,仅占位符适配两段式布局)——指令块=蓝本编号要求+书写风格+输出卫生;
    素材侧承载 我的名字是/今天是/回顾聊天记录/目标字数/日记内容: 引导。"""
    t = SIDE_TEMPLATES["qzone_diary"]
    assert t["version"] == 6
    s = t["system"]
    # 蓝本核心逐句锁定(照搬验证:句式漂移会被抓)
    assert "现在我要写一篇日记,记录到现在为止的感受" in s
    assert "1. 开头必须是日期和天气" in s
    assert "2. 像睡前随手写的感觉,轻松自然" in s
    assert "3. 回忆到现在为止的对话,加入我的真实感受" in s
    assert "4. 如果有有趣的事就重点写,平淡的一天就简单记录" in s
    assert "5. 偶尔加一两句小总结或感想" in s
    assert "6. 不要写成流水账,要有重点和感情色彩" in s
    assert '7. 用第一人称"我"来写' in s
    assert "书写风格" in s and "日常且口语化的文段,平淡一些" in s
    assert "不要书写的太有条理,可以有个性" in s  # 个性许可(反机械化核心)
    assert "日记风格(私人记录,带反思感想)。" in s  # {style_desc} 内联蓝本默认值
    assert "只输出一段日记内容就好" in s  # 输出卫生
    messages, key = build_side_prompt("qzone_diary", ["我的名字是测试"], [])
    assert messages[0]["role"] == "system"
    assert messages[1]["content"] == "我的名字是测试"
    assert key.startswith("qzone_diary:v6+")


def test_qzone_diary_template_natural_tone():
    """日记模板口吻自然化:不用「你就是这位用户本人(人设见素材首段)」式
    机械指涉;v6 照搬蓝本原文——以第一人称内心独白式任务语开写。"""
    system, _ = load_side_system("qzone_diary")  # 无部署文件时取内置默认
    assert "你就是这位用户本人" not in system
    assert "人设见素材" not in system
    assert "现在我要写一篇日记" in system and "随手写" in system


def test_visible_output_templates_natural_tone():
    """可见输出 prompt 去机械化(2026-09-02 用户裁定):直接产出可见文本的
    模板(空间见闻/睡眠回顾——产出注入上下文并被 bot 引用)不用「你是XX助手」
    式开头;工具向生成(image_relook 等给 planner 消费)与 JSON 判定类
    (favorability 等)不受此约束。"""
    for tid in ("sleep_review", "qzone_digest"):
        system, _ = load_side_system(tid)
        assert not system.startswith("你是"), f"{tid} 仍以助手人设开头"
    assert "你睡了一觉" in SIDE_TEMPLATES["sleep_review"]["system"]
    assert "回想一下最近在QQ空间的事" in SIDE_TEMPLATES["qzone_digest"]["system"]
    # 工具向模板保持原样(image_relook 是给 bot 用的工具,输出不直接可见)
    assert SIDE_TEMPLATES["image_relook"]["version"] == 1


class _FakeRPCCode:
    """RPCError.code 形态(enum 带 value)。"""

    def __init__(self, value: str):
        self.value = value


class _FakeRPCError(Exception):
    """RPCError 形态(code+message,鸭子类型)。"""

    def __init__(self, value: str, message: str = ""):
        self.code = _FakeRPCCode(value)
        self.message = message
        super().__init__(f"[{value}] {message}")


def test_rpc_error_brief_marks_timeout_clearly():
    """E_TIMEOUT 明显超时警告(2026-09-02 用户裁定):RPC 超时以「RPC 超时」
    开头+框架 message(方法名/毫秒数,无请求体可安全输出);其它 RPC 错误带
    code;非 RPC 异常只回类型名(安全复审纪律维持)。"""
    err = _FakeRPCError("E_TIMEOUT", "请求 cap.llm.generate 超时 (30000ms)")
    brief = rpc_error_brief(err)
    assert brief.startswith("RPC 超时")
    assert "E_TIMEOUT" in brief and "30000ms" in brief
    assert rpc_error_brief(_FakeRPCError("E_UNKNOWN", "连接关闭")).startswith("RPC 错误")
    assert rpc_error_brief(RuntimeError("boom")) == "RuntimeError"  # 非 RPC:仅类型名
