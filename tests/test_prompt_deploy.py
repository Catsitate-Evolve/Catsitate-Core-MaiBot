"""旁路模板自动部署(prompt_deploy)测试。"""

from pathlib import Path

import pytest

from catsitate_core.llm_provider import SIDE_TEMPLATES
from catsitate_core.prompt_deploy import TEMPLATE_GLOB, PROMPTS_LOCALE, sync_prompt_templates

TEMPLATES = {
    "catsitate_favorability.prompt": "favorability v1 内容",
    "catsitate_schedule_generate.prompt": "schedule v2 内容",
}


def _make_plugin_root(tmp_path: Path, templates: dict[str, str] | None = None) -> Path:
    """构造模拟插件根(含 prompt_templates/ 源)。"""

    plugin_root = tmp_path / "plugin"
    tdir = plugin_root / "prompt_templates"
    tdir.mkdir(parents=True)
    for name, text in (templates or TEMPLATES).items():
        (tdir / name).write_text(text, encoding="utf-8")
    return plugin_root


def _make_project_root(tmp_path: Path) -> Path:
    """构造模拟主程序根(prompts/zh-CN/ 已含内置模板,结构特征)。"""

    project_root = tmp_path / "maibot"
    pdir = project_root / "prompts" / PROMPTS_LOCALE
    pdir.mkdir(parents=True)
    (pdir / "builtin_example.prompt").write_text("内置模板", encoding="utf-8")
    return project_root


def _target_dir(project_root: Path) -> Path:
    return project_root / "prompts" / PROMPTS_LOCALE


def test_first_deploy_writes_all(tmp_path: Path) -> None:
    plugin_root = _make_plugin_root(tmp_path)
    project_root = _make_project_root(tmp_path)

    written, skipped = sync_prompt_templates(project_root, plugin_root)

    assert (written, skipped) == (2, 0)
    for name, text in TEMPLATES.items():
        assert (_target_dir(project_root) / name).read_text(encoding="utf-8") == text


def test_idempotent_second_deploy_skips_all(tmp_path: Path) -> None:
    plugin_root = _make_plugin_root(tmp_path)
    project_root = _make_project_root(tmp_path)
    sync_prompt_templates(project_root, plugin_root)

    written, skipped = sync_prompt_templates(project_root, plugin_root)

    assert (written, skipped) == (0, 2)
    # 目标文件 mtime 未被触碰(内容一致跳过,不写盘)
    target = _target_dir(project_root) / "catsitate_favorability.prompt"
    mtime_after_first = target.stat().st_mtime
    sync_prompt_templates(project_root, plugin_root)
    assert target.stat().st_mtime == mtime_after_first


def test_changed_source_overwrites_target(tmp_path: Path) -> None:
    plugin_root = _make_plugin_root(tmp_path)
    project_root = _make_project_root(tmp_path)
    sync_prompt_templates(project_root, plugin_root)

    new_text = "favorability v3 新内容"
    (plugin_root / "prompt_templates" / "catsitate_favorability.prompt").write_text(new_text, encoding="utf-8")

    written, skipped = sync_prompt_templates(project_root, plugin_root)

    assert (written, skipped) == (1, 1)
    assert (_target_dir(project_root) / "catsitate_favorability.prompt").read_text(encoding="utf-8") == new_text


def test_missing_target_dir_warns_and_skips(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    plugin_root = _make_plugin_root(tmp_path)
    # 主程序根存在但无 prompts/zh-CN/(识别不了主程序结构 → 跳过,不创建目录)
    project_root = tmp_path / "not_maibot"
    project_root.mkdir()

    with caplog.at_level("WARNING", logger="catsitate_core.prompt_deploy"):
        written, skipped = sync_prompt_templates(project_root, plugin_root)

    assert (written, skipped) == (0, 0)
    assert not (_target_dir(project_root)).exists()
    assert any("自动部署跳过" in rec.message for rec in caplog.records)


def test_unrelated_files_untouched(tmp_path: Path) -> None:
    plugin_root = _make_plugin_root(tmp_path)
    project_root = _make_project_root(tmp_path)
    other = _target_dir(project_root) / "unrelated.prompt"
    other.write_text("其它模板", encoding="utf-8")

    sync_prompt_templates(project_root, plugin_root)

    assert other.read_text(encoding="utf-8") == "其它模板"


def test_non_catsitate_files_not_deployed(tmp_path: Path) -> None:
    # 源目录只有 catsitate_* 会同步,无关文件不动
    plugin_root = _make_plugin_root(tmp_path, {"readme.txt": "说明", **TEMPLATES})
    project_root = _make_project_root(tmp_path)

    written, skipped = sync_prompt_templates(project_root, plugin_root)

    assert (written, skipped) == (2, 0)
    assert not (_target_dir(project_root) / "readme.txt").exists()


def test_unwritable_target_warns_but_does_not_raise(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    plugin_root = _make_plugin_root(tmp_path)
    project_root = _make_project_root(tmp_path)
    # 目标路径被同名目录占据 → 写入失败,必须显式告警而不是抛出/静默
    occupied = _target_dir(project_root) / "catsitate_favorability.prompt"
    occupied.mkdir()

    with caplog.at_level("ERROR", logger="catsitate_core.prompt_deploy"):
        written, skipped = sync_prompt_templates(project_root, plugin_root)

    assert (written, skipped) == (1, 0)
    assert any("部署失败" in rec.message for rec in caplog.records)


def test_qzone_scene_prompt_in_template_dir_and_syncs(tmp_path: Path) -> None:
    """空间场景模板入列插件 prompt_templates/(M3-r2 表达生成层起仓库内置 12 个),
    内容与 llm_provider 内置一致(插件为权威源);sync 后落在主程序
    prompts/zh-CN/,经主程序「提示词管理」WebUI 可查看/编辑。"""
    real_plugin_root = Path(__file__).resolve().parents[1]
    src = real_plugin_root / "prompt_templates" / "catsitate_qzone_scene.prompt"
    assert src.is_file()  # 仓库内置 12 个模板之一

    from catsitate_core.llm_provider import SIDE_TEMPLATES

    builtin = SIDE_TEMPLATES["qzone_scene"]["system"]
    assert src.read_text(encoding="utf-8").strip() == builtin  # 与内置逐字一致(权威源不漂移)

    project_root = _make_project_root(tmp_path)
    written, skipped = sync_prompt_templates(project_root, real_plugin_root)
    assert written >= 1 and skipped == 0  # 空目标目录:全部 12 个写入
    deployed = _target_dir(project_root) / "catsitate_qzone_scene.prompt"
    assert deployed.read_text(encoding="utf-8") == src.read_text(encoding="utf-8")


def test_qzone_diary_prompt_in_template_dir_and_syncs(tmp_path: Path) -> None:
    """M3 表达:日记生成模板入列插件 prompt_templates/,内容与 llm_provider
    内置一致(插件为权威源);sync 后落在主程序 prompts/zh-CN/,WebUI 可编辑。"""
    real_plugin_root = Path(__file__).resolve().parents[1]
    src = real_plugin_root / "prompt_templates" / "catsitate_qzone_diary.prompt"
    assert src.is_file()

    from catsitate_core.llm_provider import SIDE_TEMPLATES

    builtin = SIDE_TEMPLATES["qzone_diary"]["system"]
    assert src.read_text(encoding="utf-8").strip() == builtin

    project_root = _make_project_root(tmp_path)
    written, _skipped = sync_prompt_templates(project_root, real_plugin_root)
    assert written >= 1
    deployed = _target_dir(project_root) / "catsitate_qzone_diary.prompt"
    assert deployed.read_text(encoding="utf-8") == src.read_text(encoding="utf-8")


def test_qzone_expression_prompt_in_template_dir_and_syncs(tmp_path: Path) -> None:
    """M3-r2 表达生成层:空间动作表达模板(评论/回复/发布共用)入列插件
    prompt_templates/,内容与 llm_provider 内置一致(插件为权威源);sync 后
    落在主程序 prompts/zh-CN/,WebUI 可编辑。"""
    real_plugin_root = Path(__file__).resolve().parents[1]
    src = real_plugin_root / "prompt_templates" / "catsitate_qzone_expression.prompt"
    assert src.is_file()

    from catsitate_core.llm_provider import SIDE_TEMPLATES

    builtin = SIDE_TEMPLATES["qzone_expression"]["system"]
    assert src.read_text(encoding="utf-8").strip() == builtin

    project_root = _make_project_root(tmp_path)
    written, _skipped = sync_prompt_templates(project_root, real_plugin_root)
    assert written >= 1
    deployed = _target_dir(project_root) / "catsitate_qzone_expression.prompt"
    assert deployed.read_text(encoding="utf-8") == src.read_text(encoding="utf-8")


def test_qzone_digest_prompt_in_template_dir_and_syncs(tmp_path: Path) -> None:
    """M3-r2 空间见闻:见闻摘要模板(read_qzone 窗口结束旁路生成)入列插件
    prompt_templates/,内容与 llm_provider 内置一致(插件为权威源);sync 后
    落在主程序 prompts/zh-CN/,WebUI 可编辑。"""
    real_plugin_root = Path(__file__).resolve().parents[1]
    src = real_plugin_root / "prompt_templates" / "catsitate_qzone_digest.prompt"
    assert src.is_file()

    from catsitate_core.llm_provider import SIDE_TEMPLATES

    builtin = SIDE_TEMPLATES["qzone_digest"]["system"]
    assert src.read_text(encoding="utf-8").strip() == builtin

    project_root = _make_project_root(tmp_path)
    written, _skipped = sync_prompt_templates(project_root, real_plugin_root)
    assert written >= 1
    deployed = _target_dir(project_root) / "catsitate_qzone_digest.prompt"
    assert deployed.read_text(encoding="utf-8") == src.read_text(encoding="utf-8")


@pytest.mark.parametrize("template_id", sorted(SIDE_TEMPLATES))
def test_side_templates_mirror_prompt_files(template_id: str) -> None:
    """全部旁路模板镜像一致(v1.0.0 清理):prompt_templates/*.prompt 为
    权威源,SIDE_TEMPLATES 内置兜底必须逐字一致——2026-09-03 曾有 5 个模板
    (decay/schedule_generate/sleep_confirm/sleep_review/favorability)漂移,
    此用例覆盖全部模板防复发(load_side_system 对文件 strip 后使用,比较同口径)。"""
    real_plugin_root = Path(__file__).resolve().parents[1]
    src = real_plugin_root / "prompt_templates" / f"catsitate_{template_id}.prompt"
    assert src.is_file()  # 每个内置模板都必须有对应权威源文件
    assert src.read_text(encoding="utf-8").strip() == SIDE_TEMPLATES[template_id]["system"]
