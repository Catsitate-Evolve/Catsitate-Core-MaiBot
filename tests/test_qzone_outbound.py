"""出站消息组件提取测试(text 段拼接/reply·at 忽略/二进制检测)。"""
from catsitate_core.qzone.outbound import M1_OUTBOUND_ERROR, extract_outbound_text


def _msg(raw):
    return {"raw_message": raw, "message_info": {}, "message_id": "m1"}


def test_extract_plain_text():
    text, has_binary = extract_outbound_text(_msg([
        {"type": "text", "data": "好看!"},
        {"type": "text", "data": "这只猫也太胖了"},
    ]))
    assert text == "好看!这只猫也太胖了" and has_binary is False


def test_extract_ignores_reply_and_at():
    text, has_binary = extract_outbound_text(_msg([
        {"type": "reply", "data": {"target_message_id": "qzone_t1_1"}},
        {"type": "at", "data": {"target_user_id": "10001"}},
        {"type": "text", "data": "同感~"},
    ]))
    assert text == "同感~" and has_binary is False


def test_extract_detects_binary():
    text, has_binary = extract_outbound_text(_msg([
        {"type": "text", "data": "看这个"},
        {"type": "image", "data": "图", "binary_data_base64": "AAAA"},
    ]))
    assert text == "看这个" and has_binary is True


def test_m1_error_message_is_explicit():
    assert "M1" in M1_OUTBOUND_ERROR and "M2" in M1_OUTBOUND_ERROR  # 显式说明阶段与去向
