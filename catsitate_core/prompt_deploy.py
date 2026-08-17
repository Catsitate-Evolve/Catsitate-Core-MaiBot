"""旁路模板自动部署:插件 prompt_templates/ → 主程序 prompts/zh-CN/。

主程序「提示词管理」只扫描主程序 `prompts/` 与 `data/custom_prompts/`(不扫描插件
`prompt_templates/`),故插件加载时(on_load)将 `catsitate_*.prompt` 同步到主程序
`prompts/zh-CN/`——主程序 `load_prompts()` 在插件启动后调用,同次启动即生效,无需重启。

模板内容以插件为权威源:目标缺失或内容不同则覆盖写入;内容一致跳过。
WebUI 编辑产物写 `data/custom_prompts/zh-CN/`(插件优先读取),不受覆盖影响。
写入失败显式告警,不阻断插件加载(审查 M3,禁止静默 fallback)。
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

TEMPLATE_GLOB = "catsitate_*.prompt"
PROMPTS_LOCALE = "zh-CN"


def _default_project_root() -> Path:
    """主程序项目根:插件位于 <root>/plugins/<name>/,本模块上溯四级。

    本地开发与生产容器(/MaiMBot)均为同一布局,推导路径天然成立。
    """

    return Path(__file__).resolve().parent.parent.parent.parent


def sync_prompt_templates(
    project_root: Path | None = None, plugin_root: Path | None = None
) -> tuple[int, int]:
    """把插件 prompt_templates/ 下的 catsitate_*.prompt 同步到主程序 prompts/zh-CN/。

    Args:
        project_root: 主程序项目根(测试注入;默认从插件路径推导)。
        plugin_root: 插件根目录(测试注入;默认本模块上溯两级)。

    Returns:
        (written, skipped): 写入数与跳过数(内容一致跳过)。

    目标目录必须已存在(主程序 `prompts/zh-CN/` 为结构特征);不存在说明插件不在
    `plugins/` 下或主程序布局异常,显式告警后跳过,不创建目录、不乱写。
    """

    project_root = project_root or _default_project_root()
    plugin_root = plugin_root or Path(__file__).resolve().parent.parent

    source_dir = plugin_root / "prompt_templates"
    target_dir = project_root / "prompts" / PROMPTS_LOCALE

    if not target_dir.is_dir():
        logger.warning(
            "旁路模板自动部署跳过:未识别主程序提示词目录 %s(插件应位于 <主程序根>/plugins/ 下,"
            "按目录存在的形态识别);未部署时插件回退内置默认,功能不受影响",
            target_dir,
        )
        return 0, 0

    written = 0
    skipped = 0
    for source in sorted(source_dir.glob(TEMPLATE_GLOB)):
        target = target_dir / source.name
        try:
            src_text = source.read_text(encoding="utf-8")
            if target.is_file() and target.read_text(encoding="utf-8") == src_text:
                skipped += 1
                continue
            target.write_text(src_text, encoding="utf-8")
            written += 1
        except OSError:
            logger.exception("旁路模板 %s 部署失败(目标 %s),跳过该模板", source, target)
    if written:
        logger.info(
            "旁路模板自动部署:写入 %d 个到 %s(主程序「提示词管理」可查看/编辑这 %d 个模板)",
            written,
            target_dir,
            written,
        )
    return written, skipped
