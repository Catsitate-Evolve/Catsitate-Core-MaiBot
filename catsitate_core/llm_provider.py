"""LLM Provider 声明与旁路请求组装辅助。

旁路 LLM 请求缓存规范(规格 §4.10):稳定段在前、变量素材在后,模板版本化。
结构 = [任务指令+输出格式(system,模板固定)][稳定上下文(5 级规则/白名单/人设背景,配置数据)][变量素材]。
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# 模板:system = 任务指令+输出格式(内置默认;可被主程序 prompt 管理覆盖);
# 稳定上下文由调用方经 stable_ctx 传入
SIDE_TEMPLATES: dict[str, dict] = {
    "favorability": {
        "version": 1,
        "system": (
            "你是一个关系评估助手。根据对话素材评估「用户与 bot」的关系变化。\n"
            '严格输出 JSON,格式:{"delta": 整数(-5 到 5 之间), "note": "一句话关系注记(不超过40字)"}。'
            "delta 为正表示关系变好,为负表示变差,0 表示无明显变化。不要输出其它内容。"
        ),
    },
    "msg_react": {
        "version": 2,
        "system": (
            "你是表情包选择助手。从可选表情表中选择一个最贴合目标消息与意图的表情,"
            '严格输出 JSON:{"emoji_id": "表情表中的 id"}。不要输出其它内容。'
        ),
    },
    "sentinel": {
        "version": 1,
        "system": (
            "你是回复质检助手。判断「待判定回复」是否与聊天上下文明显不符或本不该回复。"
            '严格输出 JSON:{"should_send": true/false, "reason": "一句话理由"}。不要输出其它内容。'
        ),
    },
    "image_relook": {
        "version": 1,
        "system": (
            "你是图像观察助手。仔细观察图片,回答用户的具体问题。用简体中文,简洁准确。"
        ),
    },
}


_PROJECT_ROOT = Path("/MaiMBot")
_TEMPLATE_LOCALE = "zh-CN"
_template_cache: dict[str, tuple[float, str]] = {}  # template_id -> (mtime, 文本)


def load_side_system(template_id: str) -> tuple[str, str]:
    """读取旁路模板的 system 段文本(主程序 prompt 管理可覆盖)。

    读取顺序:data/custom_prompts/<locale>/<name>.prompt(WebUI 编辑产物)
    → prompts/<locale>/<name>.prompt(内置层)→ 插件内置默认。
    文件缺失回退内置(正常路径);存在但读取异常时显式告警后回退(不静默)。

    Returns:
        (system_text, version_tag): version_tag 参与缓存键,模板变更即缓存失效。
    """

    name = f"catsitate_{template_id}"
    candidates = [
        _PROJECT_ROOT / "data" / "custom_prompts" / _TEMPLATE_LOCALE / f"{name}.prompt",
        _PROJECT_ROOT / "prompts" / _TEMPLATE_LOCALE / f"{name}.prompt",
    ]
    for path in candidates:
        try:
            stat = path.stat()
        except OSError:
            continue  # 文件不存在 → 尝试下一层
        cached = _template_cache.get(template_id)
        if cached and cached[0] == stat.st_mtime:
            return cached[1], _version_tag(template_id, cached[1])
        try:
            text = path.read_text(encoding="utf-8").strip()
        except OSError:
            logger.exception("旁路模板 %s 读取失败,回退内置模板", path)
            break
        if not text:
            logger.warning("旁路模板 %s 为空,回退内置模板", path)
            break
        _template_cache[template_id] = (stat.st_mtime, text)
        return text, _version_tag(template_id, text)
    builtin = SIDE_TEMPLATES[template_id]["system"]
    return builtin, _version_tag(template_id, builtin)


def _replacements_tag(replacements: dict[str, str]) -> str:
    """占位符替换值的稳定标签(参与缓存键,值变即失效)。"""

    return hashlib.md5("|".join(f"{k}={v}" for k, v in sorted(replacements.items())).encode("utf-8")).hexdigest()[:8]


def _version_tag(template_id: str, system_text: str) -> str:
    """模板版本标签:内置版本号 + 文本哈希(模板变更即缓存失效,§4.10)。"""

    digest = hashlib.md5(system_text.encode("utf-8")).hexdigest()[:8]
    return f"{template_id}:v{SIDE_TEMPLATES[template_id]['version']}+{digest}"


def build_side_prompt(
    template_id: str, stable_ctx: list[str], variable_tail: list[str],
    replacements: dict[str, str] | None = None,
) -> tuple[list[dict], str]:
    """按稳定段前置纪律组装旁路 prompt(规格 §4.9 签名)。

    Args:
        template_id: 模板 id(SIDE_TEMPLATES 键)。
        stable_ctx: 稳定上下文段列表(5 级规则/白名单/人设背景;内容稳定,配置变更才变)。
        variable_tail: 变量素材段列表(按序追加为 user 消息)。
        replacements: system 模板中 {{key}} 占位符的替换映射(模板可含占位符,渲染后进缓存键)。

    Returns:
        (messages, cache_key): messages 为 OpenAI 兼容消息列表;cache_key 标识模板版本。
    """

    if template_id not in SIDE_TEMPLATES:
        raise ValueError(f"未知旁路模板 id: {template_id}")
    system_text, cache_key = load_side_system(template_id)
    if replacements:
        for key, value in replacements.items():
            system_text = system_text.replace("{{" + key + "}}", value)
        cache_key = f"{cache_key}&{_replacements_tag(replacements)}"
    messages: list[dict] = [{"role": "system", "content": system_text}]
    messages += [{"role": "user", "content": part} for part in stable_ctx]
    messages += [{"role": "user", "content": part} for part in variable_tail]
    return messages, cache_key
