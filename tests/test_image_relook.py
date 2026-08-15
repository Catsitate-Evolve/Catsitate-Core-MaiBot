"""图片重看引擎测试:图片段定位/报错暴露/prompt 组装/MIME 魔数嗅探。"""

import base64

from catsitate_core.image_relook import _mime_from_bytes, build_relook_prompt, find_image_segment

IMG = {"type": "image", "file_name": "a.png", "hash": "h1", "data": "base64x"}
TXT = {"type": "text", "text": "看图"}


def make_messages():
    return [
        {"message_id": "m1", "raw_message": [IMG, TXT]},
        {"message_id": "m2", "raw_message": [TXT]},
        {"message_id": "m3", "raw_message": [IMG]},
    ]


def test_find_by_message_id():
    seg, err = find_image_segment(make_messages(), "m3", 0)
    assert seg == IMG and err == ""


def test_find_by_image_index_from_tail():
    seg, err = find_image_segment(make_messages(), None, 1)
    assert seg == IMG  # 倒数第 1 条含图消息 m3
    seg2, _ = find_image_segment(make_messages(), None, 2)
    assert seg2 == IMG  # m1


def test_find_missing_reports_error():
    seg, err = find_image_segment(make_messages(), "m404", 0)
    assert seg is None and "m404" in err  # 错误里带目标 id,不静默


def test_find_no_image_messages():
    seg, err = find_image_segment([{"message_id": "m2", "raw_message": [TXT]}], None, 1)
    assert seg is None and err


def test_build_relook_prompt_stable_prefix_and_image_tail():
    messages, cache_key = build_relook_prompt("图片里写了什么?", IMG)
    assert messages[0]["role"] == "system"
    assert "图片里写了什么?" in messages[-1]["content"] or any(
        "image" in str(m.get("content", "")) for m in messages
    )
    assert cache_key


def test_mime_png_head():
    # PNG 魔数 \x89PNG\r\n\x1a\n → png;str(base64) 路径也走同一条嗅探
    head = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\x0dIHDRrest"
    assert _mime_from_bytes(head) == "png"
    data_b64 = base64.b64encode(head).decode()
    messages, _ = build_relook_prompt("什么图?", {**IMG, "data": data_b64})
    url = messages[-1]["content"][1]["image_url"]["url"]
    assert url.startswith("data:image/png;base64,")


def test_mime_jpeg_head():
    # JPEG 魔数 \xff\xd8\xff → jpeg(不再被硬编码成 png)
    head = b"\xff\xd8\xff\xe0" + b"\x00\x10JFIFrest"
    assert _mime_from_bytes(head) == "jpeg"
    data_b64 = base64.b64encode(head).decode()
    messages, _ = build_relook_prompt("什么图?", {**IMG, "data": data_b64})
    url = messages[-1]["content"][1]["image_url"]["url"]
    assert url.startswith("data:image/jpeg;base64,")
