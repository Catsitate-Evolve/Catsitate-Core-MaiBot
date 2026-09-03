"""内容护栏纯匹配器(v1.0.0):拦截正则编译与命中判定,纯函数无 IO。

消费方是 plugin.py 的三个拦截点(经 self._guard_compiled + match_guard);
编译失败整组拒绝加载(错误显式暴露,不做部分可用兜底)。
"""

from __future__ import annotations

import re


def compile_guard(patterns: list[str]) -> tuple[list, str]:
    """编译护栏正则列表。

    成功返回 (编译后的 Pattern 列表, "");任一条失败返回 ([], 错误串)——
    错误串含首个坏规则的 1 基序号/原文/异常类型,整组拒绝加载。
    """

    compiled: list = []
    for idx, pattern in enumerate(patterns, start=1):
        try:
            compiled.append(re.compile(pattern))
        except re.error as exc:
            # 异常类型固定写 re.error:except 子句只捕这一种,且 3.13+ 该类本名是
            # PatternError(re.error 为别名),动态取名会随解释器版本漂移
            return [], f"第 {idx} 条「{pattern}」非法正则(re.error: {exc})"
    return compiled, ""


def match_guard(compiled: list, text: str) -> int:
    """对文本做护栏命中判定:返回首个命中规则的 1 基编号,无命中返回 0。

    re.search 语义(部分命中即中),无 flags——大小写敏感。
    """

    for idx, pattern in enumerate(compiled, start=1):
        if pattern.search(text):
            return idx
    return 0
