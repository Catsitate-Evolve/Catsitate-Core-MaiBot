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


def test_assembler_cache_lru_eviction():
    """M-1:缓存上限 LRU——逐块 render(各自独立 cache_key),超限逐最旧。"""

    from catsitate_core.inject import CACHE_MAX

    inj = InjectAssembler()
    inj.reset()
    assert CACHE_MAX == 512
    # 逐块 render,每块独立 cache_key(同轮多块同模块会触发重复报错,故逐个)
    for i in range(CACHE_MAX + 88):
        inj.render([InjectionBlock("memo", f"k{i}", f"t{i}")])
    assert len(inj._cache) <= CACHE_MAX  # 600 > 512:最旧的应被逐出
    # 最新仍在;命中路径 move_to_end 后再插入仍不超限
    inj.render([InjectionBlock("memo", "k-latest", "t-latest")])
    assert len(inj._cache) <= CACHE_MAX
    assert "memo|k-latest|t-latest" in inj._cache


def test_assembler_cache_hit_refreshes_recency():
    """M-1 补全:命中路径 move_to_end——同键重复渲染不增长,且新近度刷新后逐最旧。"""

    from catsitate_core.inject import CACHE_MAX

    inj = InjectAssembler()
    inj.render([InjectionBlock("memo", "k-hit", "t-hit")])
    inj.render([InjectionBlock("memo", "k-hit", "t-hit")])  # 命中路径:len 不变
    assert len(inj._cache) == 1
    # 灌满至 CACHE_MAX(k-hit 为最旧但未超限,不逐出)
    for i in range(CACHE_MAX - 1):
        inj.render([InjectionBlock("memo", f"k{i}", f"t{i}")])
    assert len(inj._cache) == CACHE_MAX
    # 命中刷新新近度(k-hit 变最新,k0 变最旧)后再插 1 条 → 逐出 k0 而非 k-hit
    inj.render([InjectionBlock("memo", "k-hit", "t-hit")])
    inj.render([InjectionBlock("memo", "k-new", "t-new")])
    assert len(inj._cache) == CACHE_MAX
    assert "memo|k-hit|t-hit" in inj._cache  # 若无 move_to_end,这里被逐出的会是 k-hit
    assert "memo|k0|t0" not in inj._cache
    assert "memo|k-new|t-new" in inj._cache
