"""pytest 全局配置:保证从任意 cwd 运行都能导入 catsitate_core。

另(v1.0.5):旁路模板读取的 _PROJECT_ROOT 硬编码为 /MaiMBot,在容器内跑测试会
吸收主程序已部署/自定义旁路模板(data/custom_prompts 的 WebUI 编辑产物),破坏
「全量离线、无需主程序」的口径——autouse 夹具把 _PROJECT_ROOT 指向不存在目录
并清空模板缓存,全量测试恒用内置 SIDE_TEMPLATES,与运行环境解耦。
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@pytest.fixture(autouse=True)
def _hermetic_side_templates(tmp_path, monkeypatch):
    """隔离主程序旁路模板:单测恒用内置 SIDE_TEMPLATES(不吸收 WebUI 自定义)。"""

    import catsitate_core.llm_provider as llm_provider

    monkeypatch.setattr(llm_provider, "_PROJECT_ROOT", tmp_path / "no-such-root")
    monkeypatch.setattr(llm_provider, "_template_cache", {})
    monkeypatch.setattr(llm_provider, "_missing_warned", {})
    yield
