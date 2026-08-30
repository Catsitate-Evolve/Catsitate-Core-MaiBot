"""场景替换/工具白名单过滤/deferred reminder 剥除测试(spec §2.11/§2.12)。"""
from catsitate_core.qzone import QZONE_PLATFORM
from catsitate_core.qzone.scene import (
    QZONE_SCENE_TEXT, filter_tool_definitions, is_qzone_message, replace_scene, strip_deferred_reminder,
)

GROUP_PROMPT = "你正在qq群里聊天,下面是群里正在聊的内容……请注意群聊礼仪。"
SYSTEM = f"你是猫娘。\n在该聊天中的注意事项:\n通用注意事项:\n{GROUP_PROMPT}\n# 工具使用说明"


def _sys_item(text):
    return {"item_type": "SystemMessageItem", "meta": {"item_id": "i0"},
            "parts": [{"type": "text", "text": text}]}


def _user_item(text, item_id="u1"):
    return {"item_type": "UserMessageItem", "meta": {"item_id": item_id},
            "parts": [{"type": "text", "text": text}]}


def test_replace_scene_hit():
    new_text, status = replace_scene(SYSTEM, GROUP_PROMPT)
    assert status == "replaced"
    assert GROUP_PROMPT not in new_text and QZONE_SCENE_TEXT in new_text
    assert "你是猫娘" in new_text and "# 工具使用说明" in new_text  # 其余段落不动


def test_replace_scene_empty_config_and_miss():
    _, status = replace_scene(SYSTEM, "")
    assert status == "empty_config"
    _, status2 = replace_scene("完全不同的 system", GROUP_PROMPT)
    assert status2 == "miss"


def test_filter_tool_definitions_openai_and_flat_forms():
    defs = [
        {"type": "function", "function": {"name": "reply", "description": "d", "parameters": {}}},
        {"type": "function", "function": {"name": "msg_react", "description": "d", "parameters": {}}},
        {"type": "function", "function": {"name": "tool_search", "description": "d", "parameters": {}}},
        {"name": "wait", "description": "d", "parameters_schema": {}},  # 扁平形态容忍
    ]
    out = filter_tool_definitions(defs, ["wait", "reply"])
    assert [d.get("function", {}).get("name") or d.get("name") for d in out] == ["reply", "wait"]
    assert out[0] is defs[0]  # 原样保留(重建缺键会炸整轮请求)


def test_strip_deferred_reminder_only_removes_standalone_reminder():
    items = [
        _sys_item("system"),
        _user_item("<system-reminder>以下工具暂不可用: view_forward_message…</system-reminder>", "r1"),
        _user_item("普通历史消息", "h1"),
        _user_item("开头不是标记的 <system-reminder> 内嵌文本", "h2"),
    ]
    out = strip_deferred_reminder(items)
    assert [i["meta"]["item_id"] for i in out] == ["i0", "h1", "h2"]


def test_is_qzone_message():
    assert is_qzone_message({"platform": QZONE_PLATFORM, "message_info": {}}) is True
    assert is_qzone_message({"platform": "qq", "message_info": {}}) is False
    assert is_qzone_message({"message_info": {"platform": QZONE_PLATFORM}}) is True  # 兼容内层
    assert is_qzone_message({}) is False


def test_qzone_exempt_matrix():
    """豁免矩阵:虚拟流消息不进好感度计数;晚安判定按 session 豁免(引擎侧纯判定)。"""
    from catsitate_core.qzone.scene import is_qzone_message

    assert is_qzone_message({"platform": "qzone-qq", "session_id": "s1"}) is True
    # 好感度豁免 = is_qzone_message;晚安豁免 = session_id ∈ qzone 集合(集合由 plugin 运行时维护)
    # 这里锁定判定函数的行为契约,plugin 接线在审查清单核验(见 Step 3 注)
    assert is_qzone_message({"platform": "qq", "session_id": "s2"}) is False
