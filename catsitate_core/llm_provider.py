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
            "你是一个关系评估助手。结合「bot 人设」背景,根据对话素材评估「用户与 bot」的关系变化。\n"
            '严格输出 JSON,格式:{"delta": 整数(-{{delta_max}} 到 {{delta_max}} 之间), "note": "一句话关系注记(不超过40字)"}。'
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
    "decay": {
        "version": 1,
        "system": (
            "你是好感度衰减评估助手。根据「上次等级/分数/注记」与「未互动天数」评估用户是否因久未互动而关系降温。\n"
            '严格输出 JSON:{"delta": 整数(-{{decay_max}} 到 0 之间), "note": "一句话拟人化新注记(不超过40字)"}。'
            "delta 必须 ≤ 0,0 表示无需衰减,为负表示关系降温幅度。不要输出其它内容。"
        ),
    },
    "schedule_generate": {
        "version": 1,
        "system": (
            "你是日程规划助手。为 bot 规划目标日的生活日程,输出 JSON:\n"
            "通常作息:晚上 23:00 左右入睡、早上 7:30 左右自然醒;当天活动可适度挤占睡眠(推迟入睡),但醒来时间尽量保持稳定;睡眠时长不得短于给定约束。\n"
            '{"date": "YYYY-MM-DD", "windows": [窗口列表]}。\n'
            "窗口结构:\n"
            '- 睡眠窗口:{"kind": "sleep", "start": "YYYY-MM-DDTHH:MM", "end": "YYYY-MM-DDTHH:MM"}\n'
            '- 活动窗口(1~8 个):{"kind": "greeting" 或 "daily", "start": "...", "end": "...", "activity": "活动描述", "plan_speak": true/false, "topic": "发言主题(计划发言时)"}\n'
            "要求:恰好 1 个睡眠窗口;活动窗口 1~8 个;窗口不重叠;时间可留空隙;睡眠时长符合给定约束;\n"
            "睡前语境活动(洗漱/准备睡等)标 kind=greeting;活动描述贴合猫耳少女人设(发呆/看雨/散步/听音乐等)。\n"
            "不要输出其它内容。"
        ),
    },
    "sleep_confirm": {
        "version": 1,
        "system": (
            "你是入睡判定助手。判断「待判定晚安短句」是否是 bot 自己发出的入睡表达。\n"
            '严格输出 JSON:{"result": "SLEEP" 或 "NOT_SLEEP" 或 "UNSURE"}。\n'
            "SLEEP=明确自我入睡表达(如「我睡了」「晚安」);NOT_SLEEP=明显不是入睡;UNSURE=拿不准。不要输出其它内容。"
        ),
    },
    "sleep_review": {
        "version": 1,
        "system": (
            "你是睡眠回顾摘要助手。根据睡眠期间某聊天流被拦截的消息列表,输出不超过 100 字的简要总结"
            "(谁说了什么、是否重要、bot 醒来是否需要知道)。不要输出其它内容。"
        ),
    },
}


_PROJECT_ROOT = Path("/MaiMBot")
_TEMPLATE_LOCALE = "zh-CN"
_template_cache: dict[str, tuple[float, str]] = {}  # template_id -> (mtime, 文本)
_missing_warned: dict[str, bool] = {}  # 模板缺失告警去重(每模板每进程一次)


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
            # 文件不存在 → 尝试下一层;全部缺失时告警一次(审查 M3,禁止静默回退)
            if path == candidates[-1] and not _missing_warned.get(template_id):
                _missing_warned[template_id] = True
                logger.warning("旁路模板 %s 未部署(已尝试 %s),使用内置默认模板;部署后自动恢复", name, candidates)
            continue
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
