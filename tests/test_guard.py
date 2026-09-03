"""内容护栏纯匹配器测试(v1.0.0 Task 1):compile_guard / match_guard + 配置节 + on_load 装配。"""

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
    """on_load 装配断言:类属性 _guard_compiled 声明 + on_load 按 enabled 编译
    (编译失败整组置空;纯函数消费方为后续任务的三个拦截点)。"""

    import inspect

    import plugin as plugin_mod

    full_src = inspect.getsource(plugin_mod)
    assert "_guard_compiled: list = []" in full_src  # 类属性声明(共享可变态,实例级重置)
    on_load_src = inspect.getsource(plugin_mod.CatsitatePlugin.on_load)
    assert "compile_guard" in on_load_src  # on_load 编译装配
    assert "_guard_compiled" in on_load_src
    assert "self.config.guard.enabled" in on_load_src  # 未 enabled 零编译
