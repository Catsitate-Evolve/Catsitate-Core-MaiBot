"""QQ 表情表(数据源:https://koishi.js.org/QFace/assets/qq_emoji/_index.json,内置随插件发布)。

键 = QQ 表情 id(字符串,对应 napcat set_msg_emoji_like 的 emoji_id);值 = 中文描述。
"""

from __future__ import annotations

import json
from pathlib import Path

_DATA_PATH = Path(__file__).resolve().parent / "data" / "qq_emoji.json"

QQ_EMOJI: dict[str, str] = {}


def load_emoji_table() -> dict[str, str]:
    """加载表情表(id -> 描述,缓存到模块级)。"""

    global QQ_EMOJI
    if not QQ_EMOJI:
        with open(_DATA_PATH, encoding="utf-8") as fp:
            raw = json.load(fp)
        for entry in raw:
            if isinstance(entry, dict) and not entry.get("isHide"):
                QQ_EMOJI[str(entry.get("emojiId"))] = str(entry.get("describe") or "").lstrip("/")
    return QQ_EMOJI


def compact_emoji_table() -> str:
    """紧凑表情列表(稳定段用):「id 描述, id 描述, ...」数字 id 升序在前,非数字 id 按原序在后。"""

    table = load_emoji_table()

    def sort_key(item: tuple[str, str]) -> tuple[int, int]:
        try:
            return (0, int(item[0]))
        except ValueError:
            return (1, 0)

    return ", ".join(f"{emoji_id} {desc}" for emoji_id, desc in sorted(table.items(), key=sort_key))
