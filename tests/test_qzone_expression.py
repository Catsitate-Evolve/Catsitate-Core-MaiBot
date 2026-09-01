"""表达生成层测试(M3-r2 Task 6):空间动作正文由带完整人设的旁路 LLM 产出——
planner 传 reply_reference(表达方向)/reply_style(篇幅),正文按人设生成;
成功/超长重生成/失败/空素材/非法 style/未知 mode 的全路径行为。"""

import pytest

from catsitate_core.qzone.expression import generate_action_text


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
async def test_generate_success():
    call, calls = _llm({"success": True, "response": " 哈哈这个好可爱 "})
    text, err = await generate_action_text(call, mode="comment", persona="猫耳少女",
                                           reference="夸一下她的猫", limit=200)
    assert err == "" and text == "哈哈这个好可爱"  # 首尾空白已剥(输出卫生)
    assert calls[0][0]["role"] == "system"
    assert "猫耳少女" in calls[0][1]["content"]  # 人设前置为稳定上下文首段


@pytest.mark.asyncio
async def test_overlong_regenerates_then_truncates():
    call, _ = _llm([
        {"success": True, "response": "长" * 300},
        {"success": True, "response": "短正文"},
    ])
    text, err = await generate_action_text(call, mode="post", persona="p", reference="r", limit=200)
    assert err == "" and text == "短正文"
    call2, _ = _llm([
        {"success": True, "response": "长" * 300},
        {"success": True, "response": "还是长" * 100},
    ])
    text2, err2 = await generate_action_text(call2, mode="post", persona="p", reference="r", limit=200)
    assert err2 == "" and len(text2) == 200  # 重生成仍超长→截断


@pytest.mark.asyncio
async def test_failure_and_empty_reference_and_bad_style():
    call, _ = _llm({"success": False})
    text, err = await generate_action_text(call, mode="comment", persona="p", reference="x")
    assert text == "" and "失败" in err
    # 空 reference 显式报错,不猜
    call_blank, _ = _llm({"success": True, "response": "ok"})
    _, err2 = await generate_action_text(call_blank, mode="comment", persona="p", reference="  ")
    assert err2 != ""
    # 空响应报错
    call_empty, _ = _llm({"success": True, "response": ""})
    _, err3 = await generate_action_text(call_empty, mode="reply", persona="p", reference="x")
    assert "空" in err3
    # 非法 style 归一为「正常回复」(prompt 素材段可断言),未知 mode 报错
    call_style, calls = _llm({"success": True, "response": "ok"})
    _, err4 = await generate_action_text(call_style, mode="comment", persona="p",
                                         reference="r", style="瞎写")
    assert err4 == "" and "正常回复" in calls[0][2]["content"]  # 篇幅行=稳定上下文第二段
    _, err5 = await generate_action_text(call_style, mode="unknown", persona="p", reference="r")
    assert "未知动作类型" in err5
