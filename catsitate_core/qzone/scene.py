"""虚拟流场景替换与工具白名单纯函数(spec §2.11/§2.12)。

场景替换:读主程序 chat.reply_style.group_chat_prompt 当前值,在 system 文本中
原位精确替换为空间场景文案(按配置值匹配,用户改过配置也能命中)。
状态语义:replaced=命中替换;empty_config=群聊场景配置为空(须告警);
miss=配置非空但未命中(主程序模板改版风险,告警)。注入块去重(2026-09-01)
后回退形态=本轮无场景说明(工具链仍可用),不再由注入块承载语义。

场景文案来源(可读性优化 2026-09-01):运行时经 llm_provider.load_side_system
("qzone_scene") 三层链读取(WebUI custom_prompts → 主程序 prompts → 插件内置
SIDE_TEMPLATES),WebUI 可覆盖;下方硬编码常量降级为链路异常的兜底,内容与
内置模板逐字一致(防两处漂移,测试锁定)。
"""

from __future__ import annotations

import logging

from catsitate_core.qzone import QZONE_PLATFORM

logger = logging.getLogger(__name__)

QZONE_SCENE_TEXT = (
    "你正在用手机刷QQ空间。消息流里有好友的说说动态和互动通知(有人评论或回复了你,引用的那条就是被评论的说说)。\n"
    "每条消息底部〔〕括号里的是工具参数:说说ID填 qzone_comment/qzone_reply 的 feed_id,评论ID填 qzone_reply 的 comment_id,评论者QQ填 qzone_comment 的 at_user_id。\n"
    "想互动就调工具:评论调 qzone_comment;回复评论调 qzone_reply(填说说ID和评论ID);点赞调 qzone_like。\n"
    "不感兴趣就保持沉默,什么都不用做。在这里直接打字是发不出去的,动作只能通过工具完成。"
)

SCENE_EMPTY_CONFIG_WARNING = "群聊场景提示词配置为空(chat.reply_style.group_chat_prompt),本轮无空间场景说明(注入块已去重只留动态状态,工具仍可用)"
SCENE_MISS_WARNING = "群聊场景提示词替换未命中(主程序模板可能已改版),本轮无空间场景说明(注入块已去重只留动态状态,工具仍可用)"


def current_scene_text() -> str:
    """当前空间场景文案:运行时经 load_side_system("qzone_scene") 读取(WebUI
    custom_prompts → 主程序 prompts → 插件内置三层链,mtime 缓存由 provider
    管理);链路异常显式告警后回退内置常量 QZONE_SCENE_TEXT(不静默)。
    """

    try:
        from catsitate_core.llm_provider import load_side_system
        text, _tag = load_side_system("qzone_scene")
    except Exception:
        logger.exception("空间场景文案读取失败(load_side_system),回退内置常量")
        return QZONE_SCENE_TEXT
    return text or QZONE_SCENE_TEXT


def replace_scene(
    system_text: str, group_prompt_value: str, *, scene_text: str | None = None
) -> tuple[str, str]:
    value = str(group_prompt_value or "").strip()
    if not value:
        return system_text, "empty_config"
    if scene_text is None:
        scene_text = current_scene_text()  # 默认走运行时三层链(WebUI 可覆盖)
    if value in system_text:
        return system_text.replace(value, scene_text, 1), "replaced"
    return system_text, "miss"


def _tool_name(d: dict) -> str:
    """取工具定义名:function.name(OpenAI 形态)优先,回退顶层 name(扁平形态)。"""

    fn = d.get("function")
    if isinstance(fn, dict):
        return str(fn.get("name") or "")
    return str(d.get("name") or "")


def filter_tool_definitions(defs: list[dict], whitelist: list[str]) -> list[dict]:
    """按 function.name(OpenAI 形态)或 name(扁平形态)过滤;通过项原样保留。"""

    allowed = {str(w) for w in whitelist}
    out: list[dict] = []
    for d in defs:
        if not isinstance(d, dict):
            continue
        name = _tool_name(d)
        if name and name in allowed:
            out.append(d)
    return out


def filter_qzone_tools_for_stream(defs: list[dict], *, is_qzone: bool, whitelist: list[str]) -> list[dict]:
    """双向工具隔离:qzone 流走白名单;非 qzone 流剥离 qzone_* 工具。

    qzone 流正向隔离(硬门控不随白名单配置放松);非 qzone 流反向隔离——
    qzone_like 等工具在真实聊天流不可见,防模型误调(T11/终审 I4)。
    """

    if is_qzone:
        return filter_tool_definitions(defs, whitelist)
    return [d for d in defs if isinstance(d, dict)
            and not _tool_name(d).startswith("qzone_")]


def _item_text(item: dict) -> str:
    parts = item.get("parts") or []
    for p in parts:
        if isinstance(p, dict) and p.get("type") == "text":
            return str(p.get("text") or "")
    return ""


def strip_deferred_reminder(items: list[dict]) -> list[dict]:
    """剥除独立的 deferred 工具提醒 user 项(文本以 <system-reminder> 开头)。"""

    return [
        it for it in items
        if not (isinstance(it, dict)
                and str(it.get("item_type") or "") == "UserMessageItem"
                and _item_text(it).lstrip().startswith("<system-reminder>"))
    ]


def apply_scene_surgery(items: list[dict], group_prompt_value: str) -> tuple[list[dict], str]:
    """对 items 做「场景替换 + deferred 剥除」手术;不原地修改,返回 (新列表, 场景状态)。

    仅当命中替换时复制 system 首项(保留 meta/item_type 结构);miss/empty_config 时
    items 原样返回(告警由 plugin 层负责——注入块去重后只留动态状态,回退形态下
    本轮无场景说明,工具链仍可用)。
    """

    status = "miss"
    out: list[dict] = list(items)
    if items:
        first = items[0]
        if isinstance(first, dict) and str(first.get("item_type") or "") == "SystemMessageItem":
            parts = first.get("parts") or []
            if parts and isinstance(parts[0], dict):
                text = str(parts[0].get("text") or "")
                new_text, status = replace_scene(text, group_prompt_value)
                if status == "replaced":
                    new_first = {
                        **first,
                        "parts": [{**parts[0], "text": new_text}, *parts[1:]],
                    }
                    out = [new_first, *items[1:]]
    return strip_deferred_reminder(out), status


def is_qzone_message(msg: dict) -> bool:
    """判定消息是否来自虚拟流(顶层或 message_info 内层 platform)。"""

    if not isinstance(msg, dict):
        return False
    if str(msg.get("platform") or "") == QZONE_PLATFORM:
        return True
    info = msg.get("message_info")
    return isinstance(info, dict) and str(info.get("platform") or "") == QZONE_PLATFORM
