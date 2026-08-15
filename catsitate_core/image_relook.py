"""图片重看引擎(规格 §4.8):图片段定位 + VLM prompt 组装(文本前缀稳定)。"""

from __future__ import annotations

from .llm_provider import build_side_prompt


def describe_segment(seg: dict) -> str:
    """段描述辅助:文件名/类型/hash 摘要,用于报错文本。"""

    return (
        f"type={seg.get('type')} file_name={seg.get('file_name') or '-'} "
        f"hash={(str(seg.get('hash'))[:12] + '…') if seg.get('hash') else '-'}"
    )


def find_image_segment(
    messages: list[dict], target_message_id: str | None, image_index: int
) -> tuple[dict | None, str]:
    """定位图片段:指定 message_id 按 id 找,否则取倒数第 image_index 条含图消息。

    spike ④:段在 raw_message 键。
    """

    if target_message_id is not None:
        for msg in messages:
            if msg.get("message_id") == target_message_id:
                for seg in msg.get("raw_message") or []:
                    if isinstance(seg, dict) and seg.get("type") == "image":
                        return seg, ""
                return None, f"消息 {target_message_id} 无图片段"
        return None, f"未找到消息 {target_message_id}(可能太旧已被丢弃)"
    image_msgs = [
        msg for msg in messages
        if any(isinstance(s, dict) and s.get("type") == "image" for s in (msg.get("raw_message") or []))
    ]
    if not image_msgs:
        return None, "近期消息中没有图片(目标太旧时 get_recent 取不到,属预期错误)"
    if image_index < 1 or image_index > len(image_msgs):
        return None, f"image_index={image_index} 超出范围(共 {len(image_msgs)} 条含图消息)"
    target = image_msgs[-image_index]
    for seg in target.get("raw_message") or []:
        if isinstance(seg, dict) and seg.get("type") == "image":
            return seg, ""
    return None, f"消息 {target.get('message_id')} 无图片段"


def build_relook_prompt(question: str, image_segment: dict) -> tuple[list[dict], str]:
    """VLM prompt:任务指令(稳定)在前,问题(变量)在尾部;图片 dict 追加为最后一条内容。"""

    data = image_segment.get("data") or ""
    tail: list[str]
    if data:
        tail = [f"问题:{question}"]
    else:
        tail = [f"问题:{question}", f"图片引用:{describe_segment(image_segment)}(无二进制,由调用方补图后重试)"]
    messages, cache_key = build_side_prompt("image_relook", [], tail)
    if data:
        # 图片块追加到 user 消息内容之后(图片 token 无前缀缓存意义,§4.10)
        messages[-1]["content"] = [
            {"type": "text", "text": tail[0]},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{data}"}},
        ]
    return messages, cache_key
