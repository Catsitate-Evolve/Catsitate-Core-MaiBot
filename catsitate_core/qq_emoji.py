"""贴表情可用表情表(用户精选 36 项,id 为 QQ 表情 id,对应 napcat set_msg_emoji_like 的 emoji_id)。"""

from __future__ import annotations

AVAILABLE_REACT_EMOJIS: dict[int, str] = {
    76: "点赞", 307: "喵喵", 285: "摸鱼",
    66: "爱心", 147: "棒棒糖", 424: "狂按按钮、想报警",
    49: "抱抱", 38: "木槌敲头", 277: "狗头",
    265: "辣眼睛", 390: "头秃", 63: "玫瑰",
    212: "托腮", 5: "大哭", 9: "委屈",
    350: "贴贴", 175: "卖萌", 344: "大怨种",
    187: "鬼魂", 144: "恭喜", 146: "爆筋",
    311: "打call", 181: "戳一戳", 46: "猪",
    37: "骷髅头", 13: "呲牙", 124: "OK",
    233: "笑哭", 20: "偷笑", 293: "敲脑瓜",
    387: "太好笑", 41: "发抖", 43: "跳跳",
    324: "吃糖", 326:"生气", 339:"舔屏"
}


def load_emoji_table() -> dict[str, str]:
    """表情表(id 字符串 -> 描述),兼容 msg_react 的接口形状。"""

    return {str(emoji_id): desc for emoji_id, desc in AVAILABLE_REACT_EMOJIS.items()}


def compact_emoji_table() -> str:
    """紧凑表情列表(稳定段用):「id 描述, id 描述, ...」按 id 升序。"""

    return ", ".join(
        f"{emoji_id} {desc}" for emoji_id, desc in sorted(AVAILABLE_REACT_EMOJIS.items())
    )
