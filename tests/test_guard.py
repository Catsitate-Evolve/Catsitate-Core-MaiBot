"""内容护栏纯匹配器测试(v1.0.0):compile_guard / match_guard + 配置节 + on_load 装配。"""

from catsitate_core.config import CatsitateConfig, GuardSection, QzoneSection
from catsitate_core.guard import compile_guard, match_guard


def test_compile_success():
    """编译成功:返回 (编译列表, 空错误串),长度与输入一致。"""

    compiled, err = compile_guard(["hello", "w\\d+"])
    assert err == ""
    assert len(compiled) == 2


def test_match_first_hit_among_many():
    """多条规则:返回首个命中的 1 基编号(后面的命中不再看)。"""

    compiled, err = compile_guard(["aaa", "wor", "zzz"])
    assert err == ""
    assert match_guard(compiled, "say wor now") == 2


def test_match_case_sensitive():
    """大小写敏感:规则「hello」不匹配「Hello」(re.search 无 flags)。"""

    compiled, _ = compile_guard(["hello"])
    assert match_guard(compiled, "Hello world") == 0
    assert match_guard(compiled, "oh hello there") == 1


def test_match_partial_hit_counts():
    """部分命中即中(re.search 语义:不要求整段匹配)。"""

    compiled, _ = compile_guard(["ell"])
    assert match_guard(compiled, "hello") == 1


def test_compile_invalid_regex_returns_error_and_empty():
    """非法正则:整组拒绝——返回空列表+错误串(含 1 基序号/原文/异常类型)。"""

    compiled, err = compile_guard(["ok", "[unclosed", "fine"])
    assert compiled == []
    assert err != ""
    assert "2" in err  # 首个坏规则的 1 基序号(第 2 条)
    assert "[unclosed" in err  # 坏规则原文
    assert "re.error" in err  # 异常类型


def test_empty_patterns_and_empty_text_zero_hit():
    """空列表+空文本:编译成功为空列表,匹配零命中。"""

    compiled, err = compile_guard([])
    assert err == ""
    assert compiled == []
    assert match_guard([], "") == 0
    assert match_guard([], "任意文本") == 0


def test_guard_section_defaults():
    """GuardSection 默认值与 UI 元数据(label/顺序在 QzoneSection 之后)。"""

    cfg = CatsitateConfig()
    assert cfg.guard.enabled is False  # 默认关(v1.0.0 显式开启才拦截)
    assert cfg.guard.patterns == []
    assert GuardSection.__ui_label__ == "内容护栏"
    assert QzoneSection.__ui_order__ == 11
    assert GuardSection.__ui_order__ == 12  # 排在 QQ空间(11) 之后


def test_plugin_on_load_assembles_guard_compiled():
    """装配断言:类属性 _guard_compiled 声明 + on_load 经 _assemble_guard 按
    enabled 编译(装配块抽为独立方法;编译失败整组置空)。装配路径的实例级
    行为测试见 test_qzone_wiring.py::test_guard_assembly_compiles_on_load_path。"""

    import inspect

    import plugin as plugin_mod

    full_src = inspect.getsource(plugin_mod)
    assert "_guard_compiled: list = []" in full_src  # 类属性声明(共享可变态,实例级重置)
    guard_src = inspect.getsource(plugin_mod.CatsitatePlugin._assemble_guard)
    assert "compile_guard" in guard_src  # 装配方法内按配置编译
    assert "_guard_compiled" in guard_src
    assert "self.config.guard.enabled" in guard_src  # 未 enabled 零编译
    on_load_src = inspect.getsource(plugin_mod.CatsitatePlugin.on_load)
    assert "self._assemble_guard()" in on_load_src  # on_load 接线装配方法


def test_content_guard_send_intercepts_final_text():
    """send_service.before_send 最终防线:对后处理产出的最终文本(processed_plain_text)
    匹配护栏,命中 abort;不命中/空文本/护栏关原样 continue。

    实证背景(2026-09-10):replyer 输出「[表情包: ...]」被核心后处理剥空后走硬编码
    兜底「呃呃」,该文案只有本钩子点可见。config 为 SDK 基类只读 property,
    经子类覆盖注入。"""

    import asyncio
    import inspect

    import plugin as plugin_mod
    from catsitate_core.config import CatsitateConfig

    def _run(cfg_enabled: bool, text: str) -> str:
        cfg = CatsitateConfig()
        cfg.plugin.enabled = True
        cfg.guard.enabled = cfg_enabled
        cfg.guard.patterns = ["^呃呃$"]

        class _P(plugin_mod.CatsitatePlugin):
            @property
            def config(self):  # noqa: N802 - 覆盖基类只读 property
                return cfg

            @property
            def ctx(self):  # noqa: N802 - 裸实例无 SDK 上下文,拦截日志走标准 logging
                import logging
                import types

                return types.SimpleNamespace(logger=logging.getLogger("test"))

        p = _P.__new__(_P)
        p._assemble_guard()
        r = asyncio.run(p.content_guard_send(message={"processed_plain_text": text}))
        return str(r["action"])

    assert _run(True, "呃呃") == "abort"  # 兜底文案命中即中止发送
    assert _run(False, "呃呃") == "continue"  # 护栏关不拦截
    assert _run(True, "今天天气不错") == "continue"  # 正常文本放行
    assert _run(True, "") == "continue"  # 空文本(如纯表情包消息)放行

    # 装配断言:钩子挂在 send_service.before_send,读取 processed_plain_text 并 match_guard
    src = inspect.getsource(plugin_mod.CatsitatePlugin.content_guard_send)
    assert "processed_plain_text" in src
    assert "match_guard" in src
