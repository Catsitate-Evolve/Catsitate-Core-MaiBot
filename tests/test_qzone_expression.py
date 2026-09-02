"""表达润色层测试:planner 草稿 → 按人设+表达方式顺一遍;失败回退草稿直发。"""

import pytest

from catsitate_core.qzone.expression import polish_action_text


def _llm(responses):
    calls = []

    async def call(messages):
        calls.append(messages)
        if isinstance(responses, list):
            item = responses.pop(0) if responses else {"success": False}
        else:
            item = responses
        return item

    return call, calls


@pytest.mark.asyncio
async def test_polish_success():
    call, calls = _llm({"success": True, "response": " ……好看。"})
    text = await polish_action_text(
        call, persona="猫耳少女", voice="短句,语气平淡", draft="这个真好看！",
        scene="QQ空间里,想给好友的说说写一条评论",
    )
    assert text == "……好看。"
    # 稳定上下文:人设首段+表达方式次段+场景语;草稿在【待发内容】素材段
    assert "猫耳少女" in calls[0][1]["content"]
    assert "短句" in calls[0][2]["content"]
    assert calls[0][3]["content"] == "你正在QQ空间里,想给好友的说说写一条评论。"
    assert calls[0][4]["content"].startswith("【待发内容】\n这个真好看！")
    # 改写指令在 system(仿主程序改写器:完全重组许可+输出卫生)
    assert "完全重组" in calls[0][0]["content"] and "不要输出多余内容" in calls[0][0]["content"]


@pytest.mark.asyncio
async def test_polish_failure_falls_back_to_empty():
    """LLM 失败/空文本返回空串——由调用方告警后以草稿直发,润色层不阻断。"""
    call, _ = _llm({"success": False})
    assert await polish_action_text(call, persona="p", voice="v", draft="草稿") == ""
    call_empty, _ = _llm({"success": True, "response": "  "})
    assert await polish_action_text(call_empty, persona="p", voice="v", draft="草稿") == ""
    assert await polish_action_text(_llm({})[0], persona="p", voice="v", draft="  ") == ""


@pytest.mark.asyncio
async def test_polish_overlong_regenerates_then_truncates():
    call, calls = _llm([
        {"success": True, "response": "长" * 300},
        {"success": True, "response": "短"},
    ])
    text = await polish_action_text(call, persona="p", voice="v", draft="d", limit=200)
    assert text == "短"
    assert "改短一些" in calls[1][-1]["content"]  # 重试附字数要求
    call2, _ = _llm([
        {"success": True, "response": "长" * 300},
        {"success": True, "response": "还是长" * 100},
    ])
    text2 = await polish_action_text(call2, persona="p", voice="v", draft="d", limit=200)
    # 2026-09-02 用户裁定:不再设硬截断——重润仍超长按模型原样返回(300 字全量)
    assert text2 == "还是长" * 100
    assert "..." not in text2


@pytest.mark.asyncio
async def test_polish_rpc_exception_falls_back_to_draft():
    """润色调用异常兜底(2026-09-02):RPC 超时等异常原先上抛会炸掉整个动作
    工具——契约是失败以草稿直发;E_TIMEOUT 以「RPC 超时」明显警告浮出。"""

    class _FakeCode:
        def __init__(self, value):
            self.value = value

    class _RPCExp(Exception):
        def __init__(self):
            self.code = _FakeCode("E_TIMEOUT")
            self.message = "请求 cap.llm.generate 超时 (30000ms)"
            super().__init__("timeout")

    warned: list = []

    class _Logger:
        def warning(self, msg, *args):
            warned.append(msg % args if args else msg)

    async def call(_messages):
        raise _RPCExp()

    text = await polish_action_text(call, persona="p", voice="v", draft="草稿内容", logger=_Logger())
    assert text == ""  # 空=调用方以草稿直发
    assert any(w.startswith("QQ空间表达润色调用异常(RPC 超时") for w in warned)
