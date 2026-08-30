"""注入框架测试:固定顺序、空块跳过、版本化复用。"""

import pytest

from catsitate_core.inject import InjectAssembler, InjectionBlock


def test_order_is_fixed_regardless_of_input_order():
    assembler = InjectAssembler()
    blocks = [
        InjectionBlock(module="favorability", content_key="u1", text="[好感度] 甲"),
        InjectionBlock(module="environment", content_key="day", text="[环境] 晴"),
        InjectionBlock(module="level_rule", content_key="cfg", text="[规则] 五级"),
    ]
    rendered = assembler.render(blocks)
    texts = [m["content"] for m in rendered]
    assert texts[0].startswith("[规则]")
    assert texts[1].startswith("[环境]")
    assert texts[-1].startswith("[好感度]")


def test_unknown_module_skipped():
    assembler = InjectAssembler()
    rendered = assembler.render([InjectionBlock(module="nope", content_key="x", text="y")])
    assert rendered == []


def test_duplicate_module_raises():
    assembler = InjectAssembler()
    blocks = [
        InjectionBlock(module="memo", content_key="a", text="[备忘] 甲"),
        InjectionBlock(module="memo", content_key="b", text="[备忘] 乙"),
    ]
    with pytest.raises(ValueError, match="重复"):
        assembler.render(blocks)


def test_same_content_reuses_rendered_object():
    assembler = InjectAssembler()
    blocks = [InjectionBlock(module="memo", content_key="m1", text="[备忘] 交作业")]
    first = assembler.render(blocks)
    second = assembler.render(blocks)
    assert first == second
    assert first[0] is second[0]  # 字节级复用同一对象


def test_changed_content_refreshes_only_that_position():
    assembler = InjectAssembler()
    a = assembler.render(
        [
            InjectionBlock(module="level_rule", content_key="cfg", text="[规则] v1"),
            InjectionBlock(module="memo", content_key="m1", text="[备忘] A"),
        ]
    )
    b = assembler.render(
        [
            InjectionBlock(module="level_rule", content_key="cfg", text="[规则] v1"),
            InjectionBlock(module="memo", content_key="m2", text="[备忘] B"),
        ]
    )
    assert a[0] is b[0]
    assert a[1] is not b[1]


def test_reset_clears_cache():
    assembler = InjectAssembler()
    blocks = [InjectionBlock(module="memo", content_key="m1", text="[备忘] A")]
    first = assembler.render(blocks)
    assembler.reset()
    second = assembler.render(blocks)
    assert first == second
    assert first[0] is not second[0]


def test_block_order_includes_qzone_between_schedule_and_memo():
    from catsitate_core.inject import BLOCK_ORDER

    assert BLOCK_ORDER == ("level_rule", "environment", "schedule", "qzone", "memo", "favorability")
