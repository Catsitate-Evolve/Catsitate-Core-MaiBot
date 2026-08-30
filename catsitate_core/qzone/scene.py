"""虚拟流场景替换与工具白名单纯函数(spec §2.11/§2.12)。

场景替换:读主程序 chat.reply_style.group_chat_prompt 当前值,在 system 文本中
原位精确替换为空间场景文案(按配置值匹配,用户改过配置也能命中)。
状态语义:replaced=命中替换;empty_config=群聊场景配置为空(追加覆盖兜底,仍须告警);
miss=配置非空但未命中(主程序模板改版风险,告警+回退追加)。
"""

from __future__ import annotations

from catsitate_core.qzone import QZONE_PLATFORM

QZONE_SCENE_TEXT = (
    "你正在用手机刷QQ空间的好友动态流。下面的消息流不是群聊发言:每条消息是一位好友发的一条说说"
    "(可能带图,「评 XX:」开头的是该好友在别人说说下的评论)。你对某条说说的回复会作为QQ空间评论发出"
)

SCENE_EMPTY_CONFIG_WARNING = "群聊场景提示词配置为空(chat.reply_style.group_chat_prompt),虚拟流以注入块语义说明工作"
SCENE_MISS_WARNING = "群聊场景提示词替换未命中(主程序模板可能已改版),虚拟流回退为注入块语义说明"


def replace_scene(system_text: str, group_prompt_value: str, *, scene_text: str = QZONE_SCENE_TEXT) -> tuple[str, str]:
    value = str(group_prompt_value or "").strip()
    if not value:
        return system_text, "empty_config"
    if value in system_text:
        return system_text.replace(value, scene_text, 1), "replaced"
    return system_text, "miss"


def filter_tool_definitions(defs: list[dict], whitelist: list[str]) -> list[dict]:
    """按 function.name(OpenAI 形态)或 name(扁平形态)过滤;通过项原样保留。"""

    allowed = {str(w) for w in whitelist}
    out: list[dict] = []
    for d in defs:
        if not isinstance(d, dict):
            continue
        fn = d.get("function")
        name = str(fn.get("name") or "") if isinstance(fn, dict) else str(d.get("name") or "")
        if name and name in allowed:
            out.append(d)
    return out


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


def is_qzone_message(msg: dict) -> bool:
    """判定消息是否来自虚拟流(顶层或 message_info 内层 platform)。"""

    if not isinstance(msg, dict):
        return False
    if str(msg.get("platform") or "") == QZONE_PLATFORM:
        return True
    info = msg.get("message_info")
    return isinstance(info, dict) and str(info.get("platform") or "") == QZONE_PLATFORM
