# Catsitate Core MaiBot 插件一期实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 实现 Catsitate 核心人格行为插件一期(2.4 全组 + 2.3 基础):注入框架、时间/节日/天气、好感度批次结算、备忘录、贴表情、戳一戳、reply 补传、图片重看、LLM Provider。

**Architecture:** 单插件多模块,插件目录即独立 git 仓库;`plugin.py` 薄入口 + `catsitate_core/` 内部包。所有模块纯逻辑与 SDK 接线分离——`catsitate_core/*.py` 内为不依赖 MaiBot 的纯 Python 类/函数(单测直接调),`plugin.py` 中的 handler 方法只做 ctx 适配与转发。缓存纪律统一收敛在 `inject.py`(主链路)与 `llm_provider.py` 的 `build_side_prompt`(旁路)。

**Tech Stack:** Python 3.13、maibot-plugin-sdk 2.8.0(本机已装,仅作类型参考,插件运行时由 Host 提供)、标准库 `sqlite3`/`json`/`asyncio`、pytest(仅开发依赖)、holiday-calendar(manifest `python_package` 依赖,Host 侧自动安装)。

**Spec:** `docs/superpowers/specs/2026-08-14-catsitate-core-maibot-design.md`(规格为准,计划从规格展开;执行者两个文档都要读)

## Global Constraints

- 插件目录:`MaiBot-dev/plugins/catsitate_core_maibot/`;**禁止修改 MaiBot 主程序任何代码**;插件目录即独立 git 仓库,直接提交到 main。
- 用户可见文本一律简体中文;代码注释与 commit message 中文。
- **禁用 `@Action` 装饰器**;只用 `@Tool`/`@Command`/`@HookHandler`/`@EventHandler`/`@API`/`@LLMProvider`/`@MessageGateway`。
- **摒弃单纯概率行为**:随机数只允许出现在工程护栏(冷却/限频/防刷);一切行为决策交给 LLM 或状态。
- 插件不能创建主程序数据库新表;插件自己的数据存独立 sqlite(路径 `ctx.paths.data_dir`),轻量限频状态用 JSON 快照。
- 错误原则:失败必须有日志痕迹,不静默兜底;`on_load` 配置校验失败报错拒绝加载。
- SDK 已核实签名(2.8.0):`@Tool(name, description=, brief_description=, detailed_description=, parameters=[ToolParameterInfo(...)], visibility="visible", ...)`(visibility 走 metadata);`@Command(name, description=, pattern=, aliases=)`;`@HookHandler(hook, *, name=, mode=HookMode.BLOCKING/OBSERVE, order=HookOrder.EARLY/NORMAL/LATE, error_policy=ErrorPolicy.SKIP)`;BLOCKING hook 返回 `{"action": "continue"|"abort", "modified_kwargs": {...}}`(modified_kwargs 整体替换后续 kwargs,观察型返回被忽略);`@LLMProvider(client_type, *, name=, description=, version=)`;`@API(name, description=, version="1", public=False)`;`@EventHandler(name, description=, event_type=EventType.ON_MESSAGE, intercept_message=False, weight=0)`。
- ctx 能力代理(2.8.0):`ctx.send.text(text, stream_id)`、`ctx.llm.generate(prompt: str|list[dict], model="", temperature=None, max_tokens=None) -> {"success","response","reasoning","model"}`、`ctx.api.call(api_name, *, version="", **kwargs)`、`ctx.chat.get_stream_by_user_id/get_stream_by_group_id/get_private_streams/get_group_streams`、`ctx.database.query/get/save/delete/count`(仅主程序既有表)、`ctx.maisaka.context.append(stream_id, content)`(看签名后核对)、`ctx.message.get_recent(chat_id, limit)`。**注意:`message.get_recent` 无 `include_binary_data` 参数——图片重看需 `ctx.call_capability("message.get_recent", chat_id=..., limit=..., include_binary_data=True)` 直接透传(Task 2 在真实环境核查)。**
- 插件 data 目录:`ctx.paths.data_dir`(Host 解析为 `<data>/plugins/catsitate.core`)。
- manifest `dependencies` 支持 `{"type": "python_package", "name": "holiday-calendar", "version_spec": ">=1.0.0"}`。
- 注入块顺序固定 `[等级规则块][环境块][备忘块][好感度块]`,前插 system 之后、历史之前;旁路 LLM prompt 一律稳定段在前、变量素材在后。
- 运行测试:`python3 -m pytest tests/ -v`(pytest 已随本计划 Task 1 安装到用户环境)。

---

### Task 1: 仓库骨架与配置模型(空壳插件可加载)

**Files:**
- Create: `plugin.py`、`_manifest.json`、`.gitignore`、`README.md`、`CHANGELOG.md`
- Create: `catsitate_core/__init__.py`、`catsitate_core/config.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Produces:
  - `catsitate_core.config.CatsitateConfig` — 顶层配置模型,字段:`plugin: PluginSection`、`inject: InjectSection`、`time_aware: TimeAwareSection`、`favorability: FavorabilitySection`、`memo: MemoSection`、`msg_react: MsgReactSection`、`poke: PokeSection`、`reply_guard: ReplyGuardSection`、`image_relook: ImageRelookSection`。每个 Section 类继承 `maibot_sdk.PluginConfigBase`,字段用 `Field(default=..., description=...)`,带 `__ui_label__`(中文)。
  - `plugin.CatsitatePlugin(MaiBotPlugin)`,方法 `on_load`/`on_unload`/`on_config_update` 先空实现,`create_plugin()` 返回实例。

- [ ] **Step 1: 安装开发依赖 pytest**

```bash
python3 -m pip install --break-system-packages pytest
```

- [ ] **Step 2: 编写配置模型与骨架文件**

`catsitate_core/config.py`(全部 section 一次定义,默认值与规格 §6 一致):

```python
"""Catsitate 插件配置模型(与规格 §6 一致)。"""

from maibot_sdk import Field, PluginConfigBase


class LLMSection(PluginConfigBase):
    """LLM 能力统一配置节。"""

    __ui_label__ = "LLM"

    enabled: bool = Field(default=True, description="是否启用该 LLM 能力")
    model: str = Field(default="", description="模型:留空=主程序默认模型;可填主程序 task 名或 catsitate_custom")


class PluginSection(PluginConfigBase):
    __ui_label__ = "插件"
    __ui_order__ = 0

    enabled: bool = Field(default=False, description="插件总开关")
    config_version: str = Field(default="1.0.0", description="配置版本")
    llm_daily_call_warning_threshold: int = Field(default=50, description="旁路 LLM 每日调用告警阈值")


class InjectSection(PluginConfigBase):
    __ui_label__ = "注入框架"
    __ui_order__ = 1

    enabled: bool = Field(default=True, description="注入管线总开关(无截断,长度在源头控制)")
    level_rule_enabled: bool = Field(default=True, description="等级规则块注入开关")
    environment_enabled: bool = Field(default=True, description="环境块(节日/天气)注入开关")
    memo_enabled: bool = Field(default=True, description="备忘块注入开关")
    favorability_enabled: bool = Field(default=True, description="好感度块注入开关")


class TimeAwareSection(PluginConfigBase):
    __ui_label__ = "时间感知"
    __ui_order__ = 2

    enabled: bool = Field(default=True, description="节日/天气感知开关")
    city: str = Field(default="北京", description="城市名")
    city_lat: float = Field(default=39.9042, description="城市纬度(Open-Meteo)")
    city_lon: float = Field(default=116.4074, description="城市经度(Open-Meteo)")
    weather_refresh_minutes: int = Field(default=45, description="天气后台刷新间隔(分钟)")
    holiday_online: bool = Field(default=True, description="节日数据在线刷新开关")


class FavorabilitySection(PluginConfigBase):
    __ui_label__ = "好感度"
    __ui_order__ = 3

    enabled: bool = Field(default=True, description="好感度模块开关")
    window_hours: int = Field(default=24, description="日终结算周期(小时)")
    early_settle_threshold: int = Field(default=20, description="提前结算消息数阈值")
    daily_max_early_settle: int = Field(default=3, description="每用户每日提前结算上限")
    daily_settle_min: int = Field(default=3, description="日终结算最小消息数(不足顺延)")
    level_rules: str = Field(
        default=(
            "与用户的关系分五级:陌生(仅按普通网友对待,保持礼貌与距离)、"
            "熟悉(认识一段时间,可自然闲聊)、亲近(关系较好,可主动关心)、"
            "挚友(非常信任,可分享心事)、特别(最重要的人,格外在意其感受)。"
        ),
        description="5 级行为准则文本(注入等级规则块)",
    )
    note_max_chars: int = Field(default=40, description="关系注记最大字符数(结算落库时强制)")
    material_max_messages: int = Field(default=30, description="结算素材锚定的用户消息条数")
    material_message_max_chars: int = Field(default=200, description="单条素材截断长度")
    llm: LLMSection = Field(default_factory=LLMSection)


class MemoSection(PluginConfigBase):
    __ui_label__ = "备忘录"
    __ui_order__ = 4

    enabled: bool = Field(default=True, description="备忘录模块开关")
    tool_enabled: bool = Field(default=True, description="memo_write/memo_read 工具开关")
    command_enabled: bool = Field(default=True, description="/记一下 命令开关")
    default_ttl_hours: int = Field(default=24, description="单条备忘缺省有效期(小时)")
    max_ttl_hours: int = Field(default=168, description="单条备忘有效期上限(小时)")
    entry_max_chars: int = Field(default=80, description="备忘内容最大字符数(写入时强制)")
    inject_max: int = Field(default=5, description="备忘注入合计条数上限")


class MsgReactSection(PluginConfigBase):
    __ui_label__ = "贴表情"
    __ui_order__ = 5

    enabled: bool = Field(default=True, description="贴表情工具开关")
    emoji_whitelist: list[str] = Field(default_factory=list, description="表情包白名单(emoji_id)")
    per_stream_cooldown_seconds: int = Field(default=30, description="每流冷却秒数")
    llm: LLMSection = Field(default_factory=LLMSection)


class PokeSection(PluginConfigBase):
    __ui_label__ = "戳一戳"
    __ui_order__ = 6

    enabled: bool = Field(default=True, description="戳一戳模块开关")
    enhance_notice_text: bool = Field(default=True, description="改写通知消息为拟人文本")
    inject_to_context: bool = Field(default=True, description="戳一戳事件注入当前流上下文")
    poke_tool_enabled: bool = Field(default=True, description="主动戳工具开关")
    min_level_for_poke: str = Field(default="熟悉", description="主动戳最低好感度等级")
    cooldown_seconds: int = Field(default=600, description="主动戳每用户冷却秒数")


class ReplyGuardSection(PluginConfigBase):
    __ui_label__ = "reply 补传"
    __ui_order__ = 7

    enabled: bool = Field(default=True, description="reply_guard 模块开关")
    context_backfill_enabled: bool = Field(default=True, description="上下文补传开关")
    context_tools: list[str] = Field(
        default_factory=lambda: ["query_memory", "query_person_profile", "fetch_history", "view_forward_message", "memo_read"],
        description="视为上下文工具的工具名列表",
    )
    sentinel_enabled: bool = Field(default=False, description="LLM 哨兵层开关(默认关)")
    sentinel_llm: LLMSection = Field(default_factory=LLMSection)


class ImageRelookSection(PluginConfigBase):
    __ui_label__ = "图片重看"
    __ui_order__ = 8

    enabled: bool = Field(default=True, description="图片重看工具开关")
    llm: LLMSection = Field(default_factory=LLMSection)


class CatsitateConfig(PluginConfigBase):
    """Catsitate 插件顶层配置。"""

    plugin: PluginSection = Field(default_factory=PluginSection)
    inject: InjectSection = Field(default_factory=InjectSection)
    time_aware: TimeAwareSection = Field(default_factory=TimeAwareSection)
    favorability: FavorabilitySection = Field(default_factory=FavorabilitySection)
    memo: MemoSection = Field(default_factory=MemoSection)
    msg_react: MsgReactSection = Field(default_factory=MsgReactSection)
    poke: PokeSection = Field(default_factory=PokeSection)
    reply_guard: ReplyGuardSection = Field(default_factory=ReplyGuardSection)
    image_relook: ImageRelookSection = Field(default_factory=ImageRelookSection)
```

`plugin.py`:

```python
"""Catsitate 核心人格行为插件 — 薄入口。"""

from maibot_sdk import MaiBotPlugin

from catsitate_core.config import CatsitateConfig


class CatsitatePlugin(MaiBotPlugin):
    """Catsitate 核心人格行为插件。"""

    config_model = CatsitateConfig

    async def on_load(self) -> None:
        """插件加载:各模块初始化在后续任务接入。"""

    async def on_unload(self) -> None:
        """插件卸载:优雅停止后台任务与关闭存储。"""

    async def on_config_update(self, scope: str, config_data: dict, version: str) -> None:
        """配置热重载:刷新派生缓存(后续任务接入)。"""


def create_plugin() -> CatsitatePlugin:
    """创建插件实例。"""

    return CatsitatePlugin()
```

`catsitate_core/__init__.py`:

```python
"""Catsitate 核心内部包。"""

__version__ = "0.1.0"
```

`_manifest.json`:

```json
{
  "manifest_version": 2,
  "version": "0.1.0",
  "name": "Catsitate 核心人格插件",
  "description": "Catsitate 的注入框架、时间节日天气感知、好感度、备忘录、贴表情、戳一戳、reply 补传、图片重看",
  "author": {
    "name": "hesitate-p",
    "url": "https://github.com/hesitate-p"
  },
  "license": "GPL-v3.0-or-later",
  "urls": {
    "repository": "https://github.com/hesitate-p/catsitate_core_maibot",
    "homepage": "https://github.com/hesitate-p/catsitate_core_maibot",
    "issues": "https://github.com/hesitate-p/catsitate_core_maibot/issues"
  },
  "changelog": "CHANGELOG.md",
  "host_application": {
    "min_version": "1.2.0",
    "max_version": "1.2.99"
  },
  "sdk": {
    "min_version": "2.7.1",
    "max_version": "2.99.99"
  },
  "dependencies": [
    {
      "type": "python_package",
      "name": "holiday-calendar",
      "version_spec": ">=1.0.0"
    }
  ],
  "capabilities": [
    "send.text",
    "message.get_recent",
    "llm.generate",
    "api.call",
    "chat.get_private_streams",
    "chat.get_group_streams",
    "chat.get_stream_by_user_id",
    "maisaka.context.append",
    "database.query",
    "database.get",
    "config.get"
  ],
  "llm_providers": [
    {
      "client_type": "catsitate_custom",
      "name": "Catsitate 自定义端点",
      "description": "OpenAI 兼容自定义端点(用户在 model_config 配置 base_url/key)",
      "version": "1.0.0"
    }
  ],
  "i18n": {
    "default_locale": "zh-CN",
    "locales_path": "_locales",
    "supported_locales": ["zh-CN"]
  },
  "id": "catsitate.core"
}
```

`.gitignore`:

```gitignore
__pycache__/
*.pyc
.pytest_cache/
/config.toml
/data/
```

`README.md`(初版,后续任务补完):

```markdown
# catsitate_core_maibot

Catsitate 的 MaiBot 核心人格行为插件。详细设计见 `docs/superpowers/specs/`。

## 启用方式

1. 将本目录放入 MaiBot 的 `plugins/` 目录(或经插件市场安装);
2. 启动后进入 WebUI → 插件页 → 启用 `catsitate.core`;
3. 配置各模块开关(默认关闭总开关,逐项开启)。

## 测试

```bash
python3 -m pytest tests/ -v
```
```

`CHANGELOG.md`:

```markdown
# 更新日志

## 0.1.0(未发布)

- 一期开发中:骨架与配置模型。
```

- [ ] **Step 3: 编写配置默认值测试**

`tests/test_config.py`:

```python
"""配置模型默认值测试(与规格 §6 一致)。"""

from catsitate_core.config import CatsitateConfig


def test_config_defaults():
    cfg = CatsitateConfig()
    assert cfg.plugin.enabled is False
    assert cfg.plugin.llm_daily_call_warning_threshold == 50
    assert cfg.inject.enabled is True
    assert cfg.time_aware.city == "北京"
    assert cfg.time_aware.weather_refresh_minutes == 45
    assert cfg.favorability.window_hours == 24
    assert cfg.favorability.early_settle_threshold == 20
    assert cfg.favorability.daily_max_early_settle == 3
    assert cfg.favorability.daily_settle_min == 3
    assert cfg.favorability.note_max_chars == 40
    assert cfg.favorability.material_max_messages == 30
    assert cfg.favorability.material_message_max_chars == 200
    assert cfg.favorability.llm.model == ""
    assert cfg.memo.default_ttl_hours == 24
    assert cfg.memo.max_ttl_hours == 168
    assert cfg.memo.entry_max_chars == 80
    assert cfg.memo.inject_max == 5
    assert cfg.msg_react.per_stream_cooldown_seconds == 30
    assert cfg.poke.min_level_for_poke == "熟悉"
    assert cfg.poke.cooldown_seconds == 600
    assert cfg.reply_guard.sentinel_enabled is False
    assert "memo_read" in cfg.reply_guard.context_tools


def test_default_config_dump():
    cfg = CatsitateConfig()
    data = cfg.model_dump(mode="json")
    assert data["plugin"]["config_version"] == "1.0.0"
    assert data["favorability"]["level_rules"]
```

- [ ] **Step 4: 运行测试**

Run: `python3 -m pytest tests/test_config.py -v`
Expected: 2 passed。(若 `maibot_sdk` 导入失败,确认本机已装:python3 -c "import maibot_sdk"。)

- [ ] **Step 5: 提交**

```bash
git add -A
git commit -m "feat: 插件骨架与配置模型(空壳可加载,manifest 声明能力与 holiday-calendar 依赖)"
```

---

### Task 2: 实现期 spike 核查(真实环境验证 4 项未知)

**Files:**
- Create: `docs/superpowers/plans/2026-08-14-spike-findings.md`(结论文档)

**Interfaces:**
- Produces:`docs/superpowers/plans/2026-08-14-spike-findings.md`,记录以下 4 项的实测结论与依据(日志/行为),后续任务按结论分支实现:
  1. **子包导入**:插件加载后 `catsitate_core.*` 能否绝对导入(结论 A=支持,子包结构保留;结论 B=不支持,本计划所有 `catsitate_core/` 文件平铺到插件根目录,`from catsitate_core.x import` 改 `from x import`);
  2. **items 前插**:`maisaka.planner.before_request`(BLOCKING/LATE)返回 `modified_kwargs` 改写 `items`(在 system 后插入 user 消息)是否生效(结论 A=生效,注入走前插;结论 B=无效,注入回退追加 `items` 尾部并在日志标记);
  3. **before_process 改写**:`chat.receive.before_process` 的 kwargs 是否包含消息对象且 `modified_kwargs` 改写能否影响下游消息文本(结论 A=可改,`enhance_notice_text` 按改写实现;结论 B=不可改,退化为仅日志);
  4. **message.get_recent 传参**:`ctx.call_capability("message.get_recent", chat_id=..., limit=..., include_binary_data=True)` 是否返回二进制图片段(结论 A=支持,图片重看直读;结论 B=不支持,改用 `ctx.database.get(model_name="Images")` 补图路径)。
  另顺带核查 `ctx.maisaka.context.append` 的准确签名(参数名与返回),写入结论。

- [ ] **Step 1: 编写临时 spike 插件**

在插件根目录创建 `_spike.py`(与 plugin.py 同目录,临时文件,验证后删除):

```python
"""临时 spike 验证脚本 — 验证后删除。"""

import json
import logging

from maibot_sdk import HookHandler, HookMode, HookOrder, MaiBotPlugin, Tool, ToolParameterInfo, ToolParamType

logger = logging.getLogger("catsitate.spike")


class SpikePlugin(MaiBotPlugin):
    """Spike 验证插件。"""

    @HookHandler(
        "maisaka.planner.before_request",
        name="spike_before_request",
        mode=HookMode.BLOCKING,
        order=HookOrder.LATE,
    )
    async def spike_before_request(self, **kwargs):
        logger.info("[spike] before_request kwargs keys: %s", list(kwargs.keys()))
        items = kwargs.get("items")
        if items is not None:
            logger.info("[spike] items 类型=%s 长度=%s 首项=%s", type(items).__name__, len(items), json.dumps(items[0], ensure_ascii=False, default=str)[:300])
            probe = {"role": "user", "content": "[spike] 注入探针消息"}
            modified = dict(kwargs)
            if isinstance(items, list):
                # 找 system 索引,插其后
                idx = next((i for i, it in enumerate(items) if isinstance(it, dict) and it.get("role") == "system"), -1)
                modified["items"] = items[: idx + 1] + [probe] + items[idx + 1 :]
                return {"action": "continue", "modified_kwargs": modified}
        return {"action": "continue", "modified_kwargs": kwargs}

    @HookHandler(
        "chat.receive.before_process",
        name="spike_receive_before",
        mode=HookMode.BLOCKING,
        order=HookOrder.EARLY,
    )
    async def spike_receive_before(self, **kwargs):
        logger.info("[spike] receive.before_process kwargs keys: %s", list(kwargs.keys()))
        return {"action": "continue", "modified_kwargs": kwargs}

    @Tool(
        "spike_probe",
        description="spike 探针工具:触发后调用 message.get_recent 二进制",
        parameters=[],
    )
    async def handle_spike_probe(self, stream_id: str = "", **kwargs):
        try:
            result = await self.ctx.call_capability(
                "message.get_recent", chat_id=stream_id, limit=5, include_binary_data=True
            )
            logger.info("[spike] get_recent(include_binary_data) 结果: %s", json.dumps(result, ensure_ascii=False, default=str)[:800])
            append_result = await self.ctx.maisaka.context.append(stream_id, "[spike] 注入上下文探针")
            logger.info("[spike] maisaka.context.append 结果: %s", append_result)
            return {"name": "spike_probe", "content": "spike 探针执行完毕,请查看日志"}
        except Exception as exc:  # noqa: BLE001
            logger.exception("[spike] 探针失败: %s", exc)
            return {"name": "spike_probe", "content": f"spike 探针失败: {exc}"}


def create_plugin() -> SpikePlugin:
    return SpikePlugin()
```

- [ ] **Step 2: 部署并启动验证**

1. 启动 docker compose core 服务(或本机直跑 MaiBot);
2. WebUI 插件页启用插件(该文件同样会被加载器扫到;若未出现,重命名 `plugin.py` 为 `plugin_spike.py` 并存根后重试);
3. 向任意群/私聊发消息 → 查日志确认 `[spike] before_request kwargs keys` 与 `[spike] 注入探针消息` 是否进入 planner 请求(结论 2);
4. 发一条戳一戳通知 → 查 `[spike] receive.before_process kwargs keys`(结论 3:kwargs 是否含 message 对象);
5. 让 planner 调用 `spike_probe` 工具(如发"用 spike_probe 工具"诱导或经 debug 通道)→ 查 get_recent 二进制与 context.append 日志(结论 1/4)。

- [ ] **Step 3: 写结论并删除 spike 文件**

将 4 项结论(含日志片段)写入 `docs/superpowers/plans/2026-08-14-spike-findings.md`;删除 `_spike.py`;重启核心服务恢复原 plugin.py。

- [ ] **Step 4: 提交**

```bash
git add -A
git commit -m "docs: spike 核查结论(子包/items前插/before_process改写/get_recent二进制)"
```

---

### Task 3: 存储层 storage.py(sqlite3 薄封装 + JSON 快照)

**Files:**
- Create: `catsitate_core/storage.py`
- Test: `tests/test_storage.py`

**Interfaces:**
- Produces(后续任务全部经此访问数据):
  - `class SQLiteStore:` — `__init__(db_path: str | os.PathLike)`;方法 `execute(sql, params=()) -> None`、`query(sql, params=()) -> list[tuple]`、`executemany(sql, seq) -> None`、`close() -> None`、属性 `db_path`。线程安全由单事件循环使用保证,连接经 `sqlite3.connect(db_path, check_same_thread=False)` 每调用新建短连接(WAL 模式初始化时打开一次)。
  - `class JsonSnapshot:` — `__init__(file_path: str | os.PathLike)`;`load() -> dict`(不存在返回 `{}`)、`save(data: dict) -> None`(原子写:临时文件 + `os.replace`)。

- [ ] **Step 1: 编写失败测试**

`tests/test_storage.py`:

```python
"""存储层测试。"""

import json
import sqlite3

from catsitate_core.storage import JsonSnapshot, SQLiteStore


def test_sqlite_store_execute_and_query(tmp_path):
    store = SQLiteStore(tmp_path / "test.db")
    store.execute(
        "CREATE TABLE IF NOT EXISTS t (id INTEGER PRIMARY KEY, name TEXT, n INTEGER)"
    )
    store.executemany(
        "INSERT INTO t (name, n) VALUES (?, ?)",
        [("a", 1), ("b", 2)],
    )
    rows = store.query("SELECT name, n FROM t ORDER BY id")
    assert rows == [("a", 1), ("b", 2)]
    store.close()


def test_sqlite_store_data_persists_across_instances(tmp_path):
    path = tmp_path / "p.db"
    s1 = SQLiteStore(path)
    s1.execute("CREATE TABLE t (v TEXT)")
    s1.execute("INSERT INTO t VALUES ('持久')")
    s1.close()
    s2 = SQLiteStore(path)
    assert s2.query("SELECT v FROM t") == [("持久",)]
    s2.close()


def test_sqlite_store_foreign_key_and_wal(tmp_path):
    store = SQLiteStore(tmp_path / "w.db")
    store.execute("PRAGMA journal_mode")
    rows = store.query("PRAGMA journal_mode")
    assert rows[0][0] == "wal"
    store.close()


def test_json_snapshot_roundtrip(tmp_path):
    snap = JsonSnapshot(tmp_path / "s.json")
    assert snap.load() == {}
    snap.save({"a": [1, 2], "b": "中文"})
    assert snap.load() == {"a": [1, 2], "b": "中文"}


def test_json_snapshot_atomic_replace(tmp_path):
    path = tmp_path / "s2.json"
    snap = JsonSnapshot(path)
    snap.save({"k": 1})
    assert not path.with_suffix(".json.tmp").exists()
    assert json.loads(path.read_text(encoding="utf-8")) == {"k": 1}
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python3 -m pytest tests/test_storage.py -v`
Expected: FAIL(ModuleNotFoundError: catsitate_core.storage)

- [ ] **Step 3: 实现 storage.py**

```python
"""存储层:sqlite3 薄封装 + JSON 快照(原子写)。"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from pathlib import Path
from typing import Any, Sequence


class SQLiteStore:
    """sqlite3 薄封装(插件 data 目录单库,WAL 模式)。"""

    def __init__(self, db_path: str | os.PathLike[str]) -> None:
        self.db_path: str = str(db_path)
        parent = Path(self.db_path).parent
        parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("PRAGMA journal_mode=WAL")

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def execute(self, sql: str, params: Sequence[Any] = ()) -> None:
        """执行写语句并提交;异常直接抛出(不静默)。"""

        with self._connect() as conn:
            conn.execute(sql, tuple(params))

    def executemany(self, sql: str, seq: Sequence[Sequence[Any]]) -> None:
        with self._connect() as conn:
            conn.executemany(sql, [tuple(item) for item in seq])

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[tuple[Any, ...]]:
        """查询并返回元组列表。"""

        with self._connect() as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()
        return [tuple(row) for row in rows]

    def close(self) -> None:
        """无长连接,空实现保留接口。"""


class JsonSnapshot:
    """轻量 JSON 快照(冷却/限频状态),原子写入。"""

    def __init__(self, file_path: str | os.PathLike[str]) -> None:
        self.file_path: str = str(file_path)

    def load(self) -> dict[str, Any]:
        try:
            with open(self.file_path, encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            return {}
        return data if isinstance(data, dict) else {}

    def save(self, data: dict[str, Any]) -> None:
        parent = Path(self.file_path).parent
        parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=str(parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, self.file_path)
        except BaseException:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python3 -m pytest tests/test_storage.py -v`
Expected: 5 passed

- [ ] **Step 5: 提交**

```bash
git add catsitate_core/storage.py tests/test_storage.py
git commit -m "feat: 存储层 sqlite3 薄封装(WAL)与 JSON 快照原子读写"
```

---

### Task 4: 旁路 prompt 组装辅助 build_side_prompt

**Files:**
- Create: `catsitate_core/llm_provider.py`(本任务只含组装辅助纯函数;@LLMProvider 接线在 Task 14)
- Test: `tests/test_llm_provider.py`

**Interfaces:**
- Produces:
  - `SIDE_TEMPLATES: dict[str, dict]` — 四个模板 id:`favorability`/`msg_react`/`sentinel`/`image_relook`,值为 `{"version": int, "system": str, "stable_after_system": list[str]}`(见下方实现)。
  - `build_side_prompt(template_id: str, variable_tail: list[str]) -> tuple[list[dict], str]` — 返回 `(messages, cache_key)`;messages = `[{"role":"system","content":system}]+[{"role":"user","content":s} for s in stable_after_system]+[{"role":"user","content":v} for v in variable_tail]`;cache_key = `f"{template_id}:v{version}"`(供 llm_usage 表记录)。未知 template_id 抛 `ValueError`(不静默)。
  - 纯函数、无 IO,供 favorability/msg_react/reply_guard/image_relook 四个模块共用。

- [ ] **Step 1: 编写失败测试**

`tests/test_llm_provider.py`:

```python
"""旁路 prompt 组装辅助测试(稳定段前置纪律)。"""

import pytest

from catsitate_core.llm_provider import SIDE_TEMPLATES, build_side_prompt


def test_stable_prefix_first():
    messages, cache_key = build_side_prompt("favorability", ["素材1", "素材2"])
    assert messages[0] == {"role": "system", "content": SIDE_TEMPLATES["favorability"]["system"]}
    assert messages[1]["role"] == "user"
    assert "五级" in messages[1]["content"]
    assert messages[-2]["content"] == "素材1"
    assert messages[-1]["content"] == "素材2"
    assert cache_key == "favorability:v1"


def test_tail_changes_do_not_change_prefix():
    m1, k1 = build_side_prompt("favorability", ["甲"])
    m2, k2 = build_side_prompt("favorability", ["乙"])
    assert k1 == k2
    assert m1[:-1] == m2[:-1]
    assert m1[-1] != m2[-1]


def test_all_templates_share_contract():
    for tid in ("favorability", "msg_react", "sentinel", "image_relook"):
        messages, key = build_side_prompt(tid, ["变量"])
        assert messages[0]["role"] == "system"
        assert key.startswith(f"{tid}:v")


def test_unknown_template_raises():
    with pytest.raises(ValueError, match="未知"):
        build_side_prompt("nope", [])
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python3 -m pytest tests/test_llm_provider.py -v`
Expected: FAIL(ImportError)

- [ ] **Step 3: 实现 llm_provider.py(本任务部分)**

```python
"""LLM Provider 声明与旁路请求组装辅助。

旁路 LLM 请求缓存规范(规格 §4.10):稳定段在前、变量素材在后,模板版本化。
"""

from __future__ import annotations

# 稳定段模板:system = 任务指令+输出格式;stable_after_system = 稳定上下文(按模板固定)
SIDE_TEMPLATES: dict[str, dict] = {
    "favorability": {
        "version": 1,
        "system": (
            "你是一个关系评估助手。根据对话素材评估「用户与 bot」的关系变化。\n"
            '严格输出 JSON,格式:{"delta": 整数(-5 到 5 之间), "note": "一句话关系注记(不超过40字)"}。'
            "delta 为正表示关系变好,为负表示变差,0 表示无明显变化。不要输出其它内容。"
        ),
        "stable_after_system": [
            "关系分五级:陌生、熟悉、亲近、挚友、特别。素材按时间正序排列,包括用户消息与 bot 发言及上下文。"
        ],
    },
    "msg_react": {
        "version": 1,
        "system": (
            "你是表情包选择助手。从白名单中选择一个最贴合目标消息与意图的表情,"
            '严格输出 JSON:{"emoji_id": "白名单中的 id"}。不要输出其它内容。'
        ),
        "stable_after_system": [],
    },
    "sentinel": {
        "version": 1,
        "system": (
            "你是回复质检助手。判断「待判定回复」是否与聊天上下文明显不符或本不该回复。"
            '严格输出 JSON:{"ok": true/false, "reason": "一句话理由"}。不要输出其它内容。'
        ),
        "stable_after_system": [],
    },
    "image_relook": {
        "version": 1,
        "system": (
            "你是图像观察助手。仔细观察图片,回答用户的具体问题。用简体中文,简洁准确。"
        ),
        "stable_after_system": [],
    },
}


def build_side_prompt(template_id: str, variable_tail: list[str]) -> tuple[list[dict], str]:
    """按稳定段前置纪律组装旁路 prompt。

    Args:
        template_id: 模板 id(SIDE_TEMPLATES 键)。
        variable_tail: 变量素材段列表(按序追加为 user 消息)。

    Returns:
        (messages, cache_key): messages 为 OpenAI 兼容消息列表;cache_key 标识模板版本。
    """

    template = SIDE_TEMPLATES.get(template_id)
    if template is None:
        raise ValueError(f"未知旁路模板 id: {template_id}")
    messages: list[dict] = [{"role": "system", "content": template["system"]}]
    messages += [{"role": "user", "content": part} for part in template["stable_after_system"]]
    messages += [{"role": "user", "content": part} for part in variable_tail]
    return messages, f"{template_id}:v{template['version']}"
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python3 -m pytest tests/test_llm_provider.py -v`
Expected: 4 passed

- [ ] **Step 5: 提交**

```bash
git add catsitate_core/llm_provider.py tests/test_llm_provider.py
git commit -m "feat: 旁路 prompt 组装辅助 build_side_prompt(稳定段前置+模板版本化)"
```

---

### Task 5: 注入框架 inject.py(块渲染/版本化/排序)

**Files:**
- Create: `catsitate_core/inject.py`
- Test: `tests/test_inject.py`

**Interfaces:**
- Produces:
  - `class InjectionBlock:` — dataclass:`module: str`、`content_key: str`(决定缓存失效的语义键)、`text: str`。
  - `class InjectAssembler:` — `__init__()`(无参数,内部用模块级 `BLOCK_ORDER = ("level_rule", "environment", "memo", "favorability")`);方法 `render(blocks: list[InjectionBlock]) -> list[dict]`:按 BLOCK_ORDER 排序(未出现的模块跳过),同一 (module, content_key, text) 与上一轮相同时**字节级复用上次渲染结果**(内部缓存),返回 `[{"role":"user","content":text}, ...]`;`reset() -> None` 清缓存。
  - `SINGLE_BLOCK_TEMPLATE = "【{label}】{text}"` 块内标签由调用方在 text 里带(如 `[环境] ...`),框架只保证顺序与缓存复用。
- 纯函数/纯对象,无 SDK 依赖。Task 14 的 before_request handler 调用它。

- [ ] **Step 1: 编写失败测试**

`tests/test_inject.py`:

```python
"""注入框架测试:固定顺序、空块跳过、版本化复用。"""

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
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python3 -m pytest tests/test_inject.py -v`
Expected: FAIL(ImportError)

- [ ] **Step 3: 实现 inject.py**

```python
"""注入框架:主链路注入的唯一出口(缓存纪律在此保证)。

顺序固定:等级规则块 → 环境块 → 备忘块 → 好感度块(按稳定性降序,规格 §4.1)。
空块跳过;同一 (module, content_key, text) 内容未变时字节级复用上一轮渲染结果。
"""

from __future__ import annotations

from dataclasses import dataclass

BLOCK_ORDER: tuple[str, ...] = ("level_rule", "environment", "memo", "favorability")


@dataclass(frozen=True)
class InjectionBlock:
    """一个注入块(一条 user 消息)。"""

    module: str
    content_key: str  # 语义键(如说话人 user_id、备忘集合 hash)
    text: str  # 完整块文本,含标签前缀(如 "[环境] ...")


class InjectAssembler:
    """注入块组装器:排序 + 版本化缓存复用。"""

    def __init__(self) -> None:
        self._cache: dict[str, dict] = {}

    def render(self, blocks: list[InjectionBlock]) -> list[dict]:
        """按固定顺序渲染为消息列表(role=user)。"""

        by_module: dict[str, InjectionBlock] = {}
        for block in blocks:
            if block.module in BLOCK_ORDER:
                by_module[block.module] = block
        messages: list[dict] = []
        for module in BLOCK_ORDER:
            block = by_module.get(module)
            if block is None:
                continue
            cache_key = f"{module}|{block.content_key}|{block.text}"
            message = self._cache.get(cache_key)
            if message is None:
                message = {"role": "user", "content": block.text}
                self._cache[cache_key] = message
            messages.append(message)
        return messages

    def reset(self) -> None:
        """清空缓存(配置热重载时调用)。"""

        self._cache = {}
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python3 -m pytest tests/test_inject.py -v`
Expected: 5 passed

- [ ] **Step 5: 提交**

```bash
git add catsitate_core/inject.py tests/test_inject.py
git commit -m "feat: 注入框架 InjectAssembler(固定顺序+空块跳过+字节级复用)"
```

---

### Task 6: 时间/节日/天气感知 time_aware.py

**Files:**
- Create: `catsitate_core/time_aware.py`
- Test: `tests/test_time_aware.py`

**Interfaces:**
- Produces(纯逻辑,无 SDK 依赖;在线 IO 在 Task 14 接线):
  - `FESTIVAL_TABLE: dict[str, str]` — 内置静态表,`"MM-DD" -> 节日名`(春节/中秋/国庆/圣诞/元旦/劳动节/儿童节/七夕/元宵/端午/清明/重阳/腊八/小年/情人节/妇女节/教师节/万圣节/平安夜/除夕按 2025–2030 实际日期展开,示例写法见实现)。
  - `SOLAR_TERMS_2025_2030: dict[str, str]` — 节气静态表 `"YYYY-MM-DD" -> 节气名`(24 节气每年日期,实现中给出 2025–2028 完整数据,2029–2030 用 holiday-cn 补充;表内至少覆盖春分/夏至/秋分/冬至等主要节气,缺失日期返回空)。
  - `WEATHER_CODE_MAP: dict[int, str]` — Open-Meteo WMO 天气码 → 中文。
  - `def build_environment_text(now: datetime.date, city: str, weather: dict | None, holidays: list[str], solar_terms: list[str]) -> str` — 组装环境块文本,格式 `[环境] 今天 M月D日 周X,{city}:{天气描述};节日:{…}。`;weather 为 None 时省略天气;holidays/solar_terms 为空时省略节日段;返回纯文本。
  - `def parse_holiday_cn(data: dict) -> dict[str, list[str]]` — 解析 holiday-cn 在线数据 `{"year":2026,"days":[{"date":"2026-08-19","name":"七夕","isOffDay":false}]}` 为 `{"MM-DD": ["节日名", ...]}`。
  - `def holiday_chain(now: date, online: dict | None, builtin_ok: bool) -> dict[str, list[str]]` — 回退链:在线 → holiday-calendar 库 → 内置表(Task 14 里在线/库两个来源各自取数后传进来,本函数只管合并顺序;`online` 为 None 即跳过该层)。

- [ ] **Step 1: 编写失败测试**

`tests/test_time_aware.py`:

```python
"""时间感知测试:节日回退链、天气码、环境块渲染。"""

from datetime import date

from catsitate_core.time_aware import (
    FESTIVAL_TABLE,
    WEATHER_CODE_MAP,
    build_environment_text,
    holiday_chain,
    parse_holiday_cn,
)


def test_parse_holiday_cn_normalizes():
    data = {
        "year": 2026,
        "days": [
            {"date": "2026-08-19", "name": "七夕", "isOffDay": False},
            {"date": "2026-10-01", "name": "国庆节", "isOffDay": True},
        ],
    }
    parsed = parse_holiday_cn(data)
    assert parsed["08-19"] == ["七夕"]
    assert parsed["10-01"] == ["国庆节"]


def test_holiday_chain_online_first():
    online = {"08-14": ["七夕"]}
    merged = holiday_chain(date(2026, 8, 14), online, True)
    assert merged["08-14"] == ["七夕"]
    # 在线缺失日期由内置表补齐
    assert "01-01" in merged


def test_holiday_chain_falls_back_to_builtin():
    merged = holiday_chain(date(2026, 8, 14), None, True)
    assert merged == FESTIVAL_TABLE
    merged2 = holiday_chain(date(2026, 8, 14), None, False)
    assert merged2 == {}


def test_builtin_table_covers_major_festivals():
    assert "01-01" in FESTIVAL_TABLE  # 元旦
    assert "05-01" in FESTIVAL_TABLE  # 劳动节
    assert "10-01" in FESTIVAL_TABLE  # 国庆
    assert "12-25" in FESTIVAL_TABLE  # 圣诞


def test_weather_code_map_common():
    assert WEATHER_CODE_MAP[0] == "晴"
    assert WEATHER_CODE_MAP[3] == "阴"
    assert 95 in WEATHER_CODE_MAP


def test_build_environment_text_with_weather_and_festival():
    text = build_environment_text(
        now=date(2026, 8, 14),
        city="北京",
        weather={"temperature_2m": 29.3, "weather_code": 0},
        holidays=["七夕"],
        solar_terms=[],
    )
    assert text.startswith("[环境] 今天 8月14日")
    assert "北京" in text
    assert "晴" in text
    assert "29°C" in text
    assert "七夕" in text


def test_build_environment_text_without_weather():
    text = build_environment_text(
        now=date(2026, 8, 14), city="北京", weather=None, holidays=[], solar_terms=[]
    )
    assert "[环境]" in text
    assert "晴" not in text
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python3 -m pytest tests/test_time_aware.py -v`
Expected: FAIL(ImportError)

- [ ] **Step 3: 实现 time_aware.py**

```python
"""时间/节日/天气感知(规格 §4.2):回退链 holiday-cn → holiday-calendar 库 → 内置表。"""

from __future__ import annotations

import re
from datetime import date

# Open-Meteo WMO 天气码 → 中文(常见码)
WEATHER_CODE_MAP: dict[int, str] = {
    0: "晴", 1: "基本晴朗", 2: "多云", 3: "阴",
    45: "雾", 48: "雾凇",
    51: "小毛毛雨", 53: "毛毛雨", 55: "浓毛毛雨",
    61: "小雨", 63: "中雨", 65: "大雨",
    71: "小雪", 73: "中雪", 75: "大雪",
    80: "阵雨", 81: "中等阵雨", 82: "强阵雨",
    85: "阵雪", 86: "强阵雪",
    95: "雷暴", 96: "雷暴伴冰雹", 99: "强雷暴伴冰雹",
}

# 内置静态表(2025–2030 农历节日按实际公历日期展开,常规公历节日固定)
FESTIVAL_TABLE: dict[str, str] = {
    "01-01": "元旦",
    "02-14": "情人节",
    "03-08": "妇女节",
    "05-01": "劳动节",
    "06-01": "儿童节",
    "10-01": "国庆节",
    "12-24": "平安夜",
    "12-25": "圣诞节",
    # 农历节日(按 2025–2030 实际日期预生成,示例为 2026 年;发版前全量补齐)
    "02-17": "除夕(2026)",  # 占位示例:实现时替换为真实 2025–2030 各年日期,名称不带年份
    "02-18": "春节(2026)",
    "08-19": "七夕(2026)",
    "09-25": "中秋(2026)",
}

# 节气静态表(2025–2030,发版前全量;此处给出 2026 年示例,格式 YYYY-MM-DD)
SOLAR_TERMS_TABLE: dict[str, str] = {
    "2026-03-20": "春分",
    "2026-06-21": "夏至",
    "2026-09-23": "秋分",
    "2026-12-22": "冬至",
}


def parse_holiday_cn(data: dict) -> dict[str, list[str]]:
    """解析 holiday-cn 在线数据为 {"MM-DD": [节日名, ...]}。"""

    result: dict[str, list[str]] = {}
    for day in data.get("days", []):
        raw = day.get("date", "")
        name = day.get("name", "")
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw) and name:
            result.setdefault(raw[5:], []).append(name)
    return result


def holiday_chain(now: date, online: dict[str, list[str]] | None, builtin_ok: bool) -> dict[str, list[str]]:
    """按回退链合并节日数据:在线(holiday-cn) → holiday-calendar 库 → 内置表。

    库层数据由调用方(task 接线)传入并合并进 online 参数前先单独处理:
    库层格式 {"MM-DD": ["节日", ...]} 直接作第二层。
    """

    del now  # 回退链与日期无关,保留参数兼容
    merged: dict[str, list[str]] = {}
    if builtin_ok:
        for key, name in FESTIVAL_TABLE.items():
            merged.setdefault(key, []).append(name)
    if online:
        for key, names in online.items():
            existing = merged.get(key)
            if existing:
                merged[key] = [n for n in names if n not in existing] + existing
            else:
                merged[key] = list(names)
    return merged


def build_environment_text(
    now: date,
    city: str,
    weather: dict | None,
    holidays: list[str],
    solar_terms: list[str],
) -> str:
    """组装环境块文本(单行,自解释)。"""

    weekday = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")[now.weekday()]
    parts = [f"今天 {now.month}月{now.day}日 {weekday}"]
    if weather is not None:
        code = int(weather.get("weather_code", 0))
        temp = weather.get("temperature_2m")
        desc = WEATHER_CODE_MAP.get(code, "天气不明")
        if temp is not None:
            desc = f"{desc},{round(float(temp))}°C"
        parts.append(f"{city}:{desc}")
    else:
        parts.append(f"{city}")
    extras = list(solar_terms) + list(holidays)
    if extras:
        parts.append("节日:" + "、".join(extras))
    return "[环境] " + ";".join(parts) + "。"
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python3 -m pytest tests/test_time_aware.py -v`
Expected: 7 passed

- [ ] **Step 5: 提交**

```bash
git add catsitate_core/time_aware.py tests/test_time_aware.py
git commit -m "feat: 时间感知模块(节日回退链/天气码映射/环境块渲染,内置表待全量补齐)"
```

---

### Task 7: 短时备忘录 memo.py

**Files:**
- Create: `catsitate_core/memo.py`
- Test: `tests/test_memo.py`

**Interfaces:**
- Produces(纯逻辑,SQLiteStore 由外部注入):
  - `class MemoService:` — `__init__(store: SQLiteStore, config: MemoSection)`;`ensure_schema() -> None`(建 `memo(id INTEGER PRIMARY KEY AUTOINCREMENT, content TEXT NOT NULL, stream_id TEXT NOT NULL DEFAULT '', user_id TEXT NOT NULL DEFAULT '', expires_at TEXT NOT NULL, created_at TEXT NOT NULL)`;表为插件自有库,不冲突主程序);`write(content: str, stream_id: str, user_id: str, ttl_hours: float | None) -> tuple[bool, str]`(校验:内容去空白后 1–entry_max_chars 字符;ttl 为 None 用 default_ttl_hours,超 max_ttl_hours 返回错误;成功返回 `(True, "已记下")` 失败 `(False, 原因)`);`read(stream_id: str, user_id: str, limit: int) -> list[dict]`(未过期,按 stream_id 匹配或 user_id 匹配,created_at 倒序,返回含 `remaining_hours`);`cleanup() -> int`(删除过期,返回条数);`now()` 由参数注入默认 `datetime.now` 便于测试。
- 注入查询同样经 `read`(Task 14 按流/user 两维度各取 3 条、合计 ≤ inject_max)。

- [ ] **Step 1: 编写失败测试**

`tests/test_memo.py`:

```python
"""备忘录测试:TTL 参数、长度强制、读取与清理。"""

from datetime import datetime, timedelta

from catsitate_core.config import MemoSection
from catsitate_core.memo import MemoService
from catsitate_core.storage import SQLiteStore

NOW = datetime(2026, 8, 14, 12, 0, 0)


def make_service(tmp_path):
    store = SQLiteStore(tmp_path / "memo.db")
    svc = MemoService(store, MemoSection())
    svc.ensure_schema()
    return svc, store


def test_write_and_read(tmp_path):
    svc, _ = make_service(tmp_path)
    ok, msg = svc.write("周四要交作业", stream_id="s1", user_id="u1", ttl_hours=None, now=NOW)
    assert ok, msg
    rows = svc.read("s1", "u1", 5, now=NOW)
    assert len(rows) == 1
    assert rows[0]["content"] == "周四要交作业"
    assert 23 < rows[0]["remaining_hours"] <= 24


def test_write_too_long_rejected(tmp_path):
    svc, _ = make_service(tmp_path)
    ok, msg = svc.write("长" * 81, stream_id="s", user_id="u", ttl_hours=None, now=NOW)
    assert not ok
    assert "80" in msg
    assert svc.read("s", "u", 5, now=NOW) == []


def test_write_ttl_over_max_rejected(tmp_path):
    svc, _ = make_service(tmp_path)
    ok, msg = svc.write("内容", stream_id="s", user_id="u", ttl_hours=999, now=NOW)
    assert not ok
    assert "168" in msg


def test_read_by_user_across_streams(tmp_path):
    svc, _ = make_service(tmp_path)
    svc.write("A", stream_id="s1", user_id="u1", ttl_hours=None, now=NOW)
    svc.write("B", stream_id="s2", user_id="u1", ttl_hours=None, now=NOW)
    rows = svc.read("s3", "u1", 5, now=NOW)
    assert {r["content"] for r in rows} == {"A", "B"}


def test_cleanup_removes_expired(tmp_path):
    svc, _ = make_service(tmp_path)
    svc.write("过期", stream_id="s", user_id="u", ttl_hours=1, now=NOW)
    removed = svc.cleanup(now=NOW + timedelta(hours=2))
    assert removed == 1
    assert svc.read("s", "u", 5, now=NOW + timedelta(hours=2)) == []
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python3 -m pytest tests/test_memo.py -v`
Expected: FAIL(ImportError)

- [ ] **Step 3: 实现 memo.py**

```python
"""短时备忘录(规格 §4.4):单条 TTL 可传,写入长度源头强制。"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Callable

from .config import MemoSection
from .storage import SQLiteStore

_ISO = "%Y-%m-%dT%H:%M:%S"


class MemoService:
    """备忘录读写与过期清理。"""

    def __init__(self, store: SQLiteStore, config: MemoSection) -> None:
        self.store = store
        self.config = config

    def ensure_schema(self) -> None:
        self.store.execute(
            """
            CREATE TABLE IF NOT EXISTS memo (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                content TEXT NOT NULL,
                stream_id TEXT NOT NULL DEFAULT '',
                user_id TEXT NOT NULL DEFAULT '',
                expires_at TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )

    def write(
        self,
        content: str,
        stream_id: str,
        user_id: str,
        ttl_hours: float | None,
        now: Callable[[], datetime] | None = None,
    ) -> tuple[bool, str]:
        """写入备忘。失败返回 (False, 原因) 供工具/命令展示给用户。"""

        now_fn = now or datetime.now
        text = content.strip()
        if not text:
            return False, "备忘内容不能为空"
        if len(text) > self.config.entry_max_chars:
            return False, f"备忘过长:请精简到 {self.config.entry_max_chars} 字以内"
        if ttl_hours is None:
            ttl_hours = float(self.config.default_ttl_hours)
        if ttl_hours <= 0:
            return False, "有效期必须大于 0 小时"
        if ttl_hours > self.config.max_ttl_hours:
            return False, f"有效期过长:单条上限 {self.config.max_ttl_hours} 小时"
        current = now_fn()
        expires = current + timedelta(hours=ttl_hours)
        self.store.execute(
            "INSERT INTO memo (content, stream_id, user_id, expires_at, created_at) VALUES (?, ?, ?, ?, ?)",
            (text, stream_id or "", user_id or "", expires.strftime(_ISO), current.strftime(_ISO)),
        )
        return True, f"已记下({ttl_hours:.0f} 小时内有效)"

    def read(
        self,
        stream_id: str,
        user_id: str,
        limit: int,
        now: Callable[[], datetime] | None = None,
    ) -> list[dict]:
        """读取未过期备忘(当前流相关 + 当前说话人相关),返回含剩余有效时间。"""

        now_fn = now or datetime.now
        current = now_fn()
        rows = self.store.query(
            """
            SELECT id, content, stream_id, user_id, expires_at FROM memo
            WHERE (stream_id = ? OR user_id = ?) AND expires_at > ?
            ORDER BY created_at DESC, id DESC LIMIT ?
            """,
            (stream_id or "", user_id or "", current.strftime(_ISO), limit),
        )
        result: list[dict] = []
        for row in rows:
            expires = datetime.strptime(row[4], _ISO)
            result.append(
                {
                    "id": row[0],
                    "content": row[1],
                    "stream_id": row[2],
                    "user_id": row[3],
                    "remaining_hours": round((expires - current).total_seconds() / 3600, 1),
                }
            )
        return result

    def cleanup(self, now: Callable[[], datetime] | None = None) -> int:
        """删除过期项,返回删除条数。"""

        now_fn = now or datetime.now
        current = now_fn()
        before = self.store.query("SELECT COUNT(*) FROM memo")[0][0]
        self.store.execute("DELETE FROM memo WHERE expires_at <= ?", (current.strftime(_ISO),))
        after = self.store.query("SELECT COUNT(*) FROM memo")[0][0]
        return before - after
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python3 -m pytest tests/test_memo.py -v`
Expected: 5 passed

- [ ] **Step 5: 提交**

```bash
git add catsitate_core/memo.py tests/test_memo.py
git commit -m "feat: 备忘录服务(单条 TTL 参数校验/剩余有效时间/过期清理)"
```

---

### Task 8: 好感度批次引擎(计数/触发/材料构造)

**Files:**
- Create: `catsitate_core/favorability.py`(本任务:批次引擎与材料构造;LLM 判定在 Task 9)
- Test: `tests/test_favorability.py`

**Interfaces:**
- Produces:
  - `LEVELS: list[str] = ["陌生", "熟悉", "亲近", "挚友", "特别"]`;`LEVEL_INDEX: dict[str, int]`。
  - `class BatchEngine:` — `__init__(store: SQLiteStore, config: FavorabilitySection)`;`ensure_schema()` 建两张表:`favorability(user_id TEXT, stream_id TEXT, level INTEGER, score INTEGER, note TEXT, window_start TEXT, judged_at TEXT, PRIMARY KEY(user_id, stream_id))` 与 `favorability_log(judge_id TEXT PRIMARY KEY, user_id TEXT, stream_id TEXT, delta INTEGER, note TEXT, judged_at TEXT)`;`count_message(user_id: str, stream_id: str, now=None) -> dict` 返回该 (user, stream) 批次统计 `{"messages": n, "reached_early_threshold": bool, "early_settled_today": int}`(计数存表 `batch_counter(user_id TEXT, stream_id TEXT, count INTEGER, last_bump TEXT, PRIMARY KEY(user_id, stream_id))`;`early_settled_today` 由 log 表按 `judged_at` 当日、`judge_id` 前缀 `early-` 计数);`check_trigger(user_id, stream_id, now=None) -> str | None` 返回 `"early"`(≥阈值且当日提前结算未达上限)或 None;`build_material(user_id: str, stream_id: str, history: list[dict]) -> list[str]`(history 为消息字典列表,每条含 `role`(user/bot)、`user_id`、`stream_id`、`text`、`seq`(递增序号);私聊流取该流全部消息、群聊流取目标用户消息为锚(最近 material_max_messages 条)+ bot 发言 + 紧邻前后各 1 条;按 seq 正序去重拼接;单条超 material_message_max_chars 截断加"…";每条渲染为 `[u1](用户) 内容` 格式);`reset_batch(user_id, stream_id) -> None`(计数清零、window_start 更新);`apply_delta(user_id, stream_id, delta, note, judged_at, judge_id: str | None = None) -> None`(累加 score、重算 level=LEVEL_INDEX 定位、note 截断 note_max_chars、写 log;judge_id 默认 `early-{judged_at}`,日终须显式传 `daily-` 前缀)。
  - 所有时间参数 `now: Callable[[], datetime] | None = None` 注入,单测可固定。
  - `get_level(user_id, stream_id) -> dict | None` 读 favorability 行;`get_best_level_for_user(user_id) -> dict | None`(跨流取最高等级行,Task 11 主动戳用)。

- [ ] **Step 1: 编写失败测试**

`tests/test_favorability.py`:

```python
"""好感度批次引擎测试:计数/触发/材料构造/等级。"""

from datetime import datetime

from catsitate_core.config import FavorabilitySection
from catsitate_core.favorability import BatchEngine, LEVELS
from catsitate_core.storage import SQLiteStore

NOW = datetime(2026, 8, 14, 12, 0, 0)


def make_engine(tmp_path):
    store = SQLiteStore(tmp_path / "fav.db")
    engine = BatchEngine(store, FavorabilitySection())
    engine.ensure_schema()
    return engine, store


def test_count_and_early_trigger(tmp_path):
    engine, _ = make_engine(tmp_path)
    for _ in range(20):
        engine.count_message("u1", "s1", now=lambda: NOW)
    stat = engine.count_message("u1", "s1", now=lambda: NOW)
    assert stat["messages"] == 21
    assert stat["reached_early_threshold"] is True
    assert engine.check_trigger("u1", "s1", now=lambda: NOW) == "early"


def test_early_settle_daily_cap(tmp_path):
    engine, _ = make_engine(tmp_path)
    for i in range(3):
        engine.reset_batch("u1", "s1")
        for _ in range(20):
            engine.count_message("u1", "s1", now=lambda: NOW)
        engine.apply_delta("u1", "s1", 1, f"第{i}次", judged_at=f"early-{i}-{NOW.strftime('%Y%m%d%H%M%S')}")
    engine.reset_batch("u1", "s1")
    for _ in range(20):
        engine.count_message("u1", "s1", now=lambda: NOW)
    assert engine.check_trigger("u1", "s1", now=lambda: NOW) is None  # 当日提前结算已达 3 次


def test_group_material_anchored_by_user_messages(tmp_path):
    engine, _ = make_engine(tmp_path)
    history = [
        {"role": "user", "user_id": "u9", "stream_id": "g", "text": "x", "seq": 1},
        {"role": "user", "user_id": "u1", "stream_id": "g", "text": "你好", "seq": 2},
        {"role": "bot", "user_id": "bot", "stream_id": "g", "text": "你好呀", "seq": 3},
        {"role": "user", "user_id": "u2", "stream_id": "g", "text": "y", "seq": 4},
        {"role": "user", "user_id": "u1", "stream_id": "g", "text": "在吗", "seq": 5},
    ]
    material = engine.build_material("u1", "g", history)
    text = "\n".join(material)
    assert "你好" in text and "在吗" in text  # 锚定用户消息
    assert "你好呀" in text  # bot 发言随附
    assert "x" in text and "y" in text  # 紧邻上下文
    assert text.index("你好") < text.index("在吗")  # 时间正序


def test_material_truncates_long_single_message(tmp_path):
    engine, _ = make_engine(tmp_path)
    history = [
        {"role": "user", "user_id": "u1", "stream_id": "p", "text": "长" * 300, "seq": 1},
    ]
    material = engine.build_material("u1", "p", history)
    assert len(material[0].split("】")[-1]) <= 200 + 1  # 截断后 ≤200 字符(含省略号)


def test_private_material_contains_bot_and_user(tmp_path):
    engine, _ = make_engine(tmp_path)
    history = [
        {"role": "user", "user_id": "u1", "stream_id": "p", "text": "早", "seq": 1},
        {"role": "bot", "user_id": "bot", "stream_id": "p", "text": "早安", "seq": 2},
    ]
    material = engine.build_material("u1", "p", history)
    assert "早" in "\n".join(material) and "早安" in "\n".join(material)


def test_apply_delta_level_and_note_truncation(tmp_path):
    engine, _ = make_engine(tmp_path)
    engine.apply_delta("u1", "s1", 8, "注" * 60, judged_at="early-x")
    row = engine.get_level("u1", "s1")
    assert row["level"] == 2  # 8 分 → 熟悉
    assert row["score"] == 8
    assert len(row["note"]) == 40
    assert engine.get_best_level_for_user("u1")["level"] == 2


def test_levels_order():
    assert LEVELS == ["陌生", "熟悉", "亲近", "挚友", "特别"]
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python3 -m pytest tests/test_favorability.py -v`
Expected: FAIL(ImportError)

- [ ] **Step 3: 实现 favorability.py(本任务部分:引擎与材料)**

```python
"""好感度 v3 批次结算制(规格 §4.3):纯计数触发、日终兜底、顺延不丢弃。"""

from __future__ import annotations

from datetime import datetime
from typing import Callable

from .config import FavorabilitySection
from .storage import SQLiteStore

LEVELS: list[str] = ["陌生", "熟悉", "亲近", "挚友", "特别"]
LEVEL_INDEX: dict[str, int] = {name: i for i, name in enumerate(LEVELS)}
_ISO = "%Y-%m-%dT%H:%M:%S"


def _level_for_score(score: int) -> int:
    """分数 → 等级下标:0-9 陌生 / 10-29 熟悉 / 30-59 亲近 / 60-99 挚友 / ≥100 特别。"""

    if score >= 100:
        return 4
    if score >= 60:
        return 3
    if score >= 30:
        return 2
    if score >= 10:
        return 1
    return 0


class BatchEngine:
    """好感度批次引擎。"""

    def __init__(self, store: SQLiteStore, config: FavorabilitySection) -> None:
        self.store = store
        self.config = config

    def ensure_schema(self) -> None:
        self.store.execute(
            """
            CREATE TABLE IF NOT EXISTS favorability (
                user_id TEXT NOT NULL,
                stream_id TEXT NOT NULL,
                level INTEGER NOT NULL DEFAULT 0,
                score INTEGER NOT NULL DEFAULT 0,
                note TEXT NOT NULL DEFAULT '',
                window_start TEXT NOT NULL,
                judged_at TEXT NOT NULL,
                PRIMARY KEY (user_id, stream_id)
            )
            """
        )
        self.store.execute(
            """
            CREATE TABLE IF NOT EXISTS favorability_log (
                judge_id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                stream_id TEXT NOT NULL,
                delta INTEGER NOT NULL,
                note TEXT NOT NULL,
                judged_at TEXT NOT NULL
            )
            """
        )
        self.store.execute(
            """
            CREATE TABLE IF NOT EXISTS batch_counter (
                user_id TEXT NOT NULL,
                stream_id TEXT NOT NULL,
                count INTEGER NOT NULL DEFAULT 0,
                last_bump TEXT NOT NULL DEFAULT '',
                PRIMARY KEY (user_id, stream_id)
            )
            """
        )

    def count_message(
        self, user_id: str, stream_id: str, now: Callable[[], datetime] | None = None
    ) -> dict:
        """记录一条用户消息,返回该批次统计。"""

        now_fn = now or datetime.now
        current = now_fn()
        self.store.execute(
            """
            INSERT INTO batch_counter (user_id, stream_id, count, last_bump)
            VALUES (?, ?, 1, ?)
            ON CONFLICT(user_id, stream_id) DO UPDATE SET
                count = count + 1,
                last_bump = excluded.last_bump
            """,
            (user_id, stream_id, current.strftime(_ISO)),
        )
        rows = self.store.query(
            "SELECT count FROM batch_counter WHERE user_id = ? AND stream_id = ?",
            (user_id, stream_id),
        )
        messages = rows[0][0]
        early_today = len(
            self.store.query(
                """
                SELECT 1 FROM favorability_log
                WHERE user_id = ? AND stream_id = ?
                  AND judge_id LIKE 'early-%' AND judged_at LIKE ?
                """,
                (user_id, stream_id, f"{current.strftime('%Y-%m-%d')}%"),
            )
        )
        return {
            "messages": messages,
            "reached_early_threshold": messages >= self.config.early_settle_threshold,
            "early_settled_today": early_today,
        }

    def check_trigger(
        self, user_id: str, stream_id: str, now: Callable[[], datetime] | None = None
    ) -> str | None:
        """返回触发类型 "early" 或 None(日终兜底/顺延在 Task 9 调度侧判定)。"""

        stat = self.count_message(user_id, stream_id, now=now)
        if (
            stat["reached_early_threshold"]
            and stat["early_settled_today"] < self.config.daily_max_early_settle
        ):
            return "early"
        return None

    def reset_batch(self, user_id: str, stream_id: str) -> None:
        """结算后开新批次:计数清零。"""

        self.store.execute(
            "UPDATE batch_counter SET count = 0 WHERE user_id = ? AND stream_id = ?",
            (user_id, stream_id),
        )

    def apply_delta(
        self,
        user_id: str,
        stream_id: str,
        delta: int,
        note: str,
        judged_at: str,
        judge_id: str | None = None,
    ) -> None:
        """结算结果落库:累加分数、重算等级、注记强制截断、写判定日志。

        judge_id: 判定日志幂等键;None 时默认 early-{judged_at}(日终结算须显式传 daily- 前缀)。
        """

        row = self.get_level(user_id, stream_id)
        score = (row["score"] if row else 0) + delta
        level = _level_for_score(score)
        trimmed_note = note.strip()[: self.config.note_max_chars]
        current = judged_at or datetime.now().strftime(_ISO)
        log_id = judge_id or f"early-{current}"
        self.store.execute(
            """
            INSERT INTO favorability (user_id, stream_id, level, score, note, window_start, judged_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id, stream_id) DO UPDATE SET
                level = excluded.level,
                score = excluded.score,
                note = excluded.note,
                window_start = excluded.window_start,
                judged_at = excluded.judged_at
            """,
            (user_id, stream_id, level, score, trimmed_note, current, current),
        )
        self.store.execute(
            """
            INSERT OR IGNORE INTO favorability_log (judge_id, user_id, stream_id, delta, note, judged_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (log_id, user_id, stream_id, delta, trimmed_note, current),
        )

    def get_level(self, user_id: str, stream_id: str) -> dict | None:
        rows = self.store.query(
            "SELECT user_id, stream_id, level, score, note, window_start, judged_at FROM favorability WHERE user_id = ? AND stream_id = ?",
            (user_id, stream_id),
        )
        if not rows:
            return None
        r = rows[0]
        return {
            "user_id": r[0], "stream_id": r[1], "level": r[2], "score": r[3],
            "note": r[4], "window_start": r[5], "judged_at": r[6],
        }

    def get_best_level_for_user(self, user_id: str) -> dict | None:
        """跨流取最高等级(主动戳工具门槛用)。"""

        rows = self.store.query(
            "SELECT user_id, stream_id, level, score, note, window_start, judged_at FROM favorability WHERE user_id = ? ORDER BY level DESC, score DESC LIMIT 1",
            (user_id,),
        )
        if not rows:
            return None
        r = rows[0]
        return {
            "user_id": r[0], "stream_id": r[1], "level": r[2], "score": r[3],
            "note": r[4], "window_start": r[5], "judged_at": r[6],
        }

    def build_material(self, user_id: str, stream_id: str, history: list[dict]) -> list[str]:
        """构造结算素材(时间正序;群聊以目标用户消息为锚,bot 发言与紧邻上下文随附)。

        history 元素:{role: "user"|"bot", user_id: str, stream_id: str, text: str, seq: int}
        """

        in_stream = [m for m in history if m["stream_id"] == stream_id]
        in_stream.sort(key=lambda m: m["seq"])
        target_msgs = [m for m in in_stream if m["role"] == "user" and m["user_id"] == user_id]
        if not target_msgs:
            return []
        anchor = target_msgs[-self.config.material_max_messages :]
        selected: dict[int, dict] = {}
        for msg in anchor:
            selected[msg["seq"]] = msg
            # 紧邻上下文:同流前后各 1 条(群聊上下文判断 bot 是否回应 ta)
            pos = in_stream.index(msg)
            for neighbor in (in_stream[pos - 1], in_stream[pos + 1] if pos + 1 < len(in_stream) else None):
                if neighbor is not None:
                    selected[neighbor["seq"]] = neighbor
        # bot 在该流的发言随附(与锚点消息窗口内)
        for msg in in_stream:
            if msg["role"] == "bot":
                selected[msg["seq"]] = msg
        material: list[str] = []
        for msg in sorted(selected.values(), key=lambda m: m["seq"]):
            role_label = "用户" if msg["role"] == "user" else "bot"
            text = msg["text"]
            if len(text) > self.config.material_message_max_chars:
                text = text[: self.config.material_message_max_chars] + "…"
            material.append(f"[{msg['user_id']}]({role_label}) {text}")
        return material
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python3 -m pytest tests/test_favorability.py -v`
Expected: 7 passed(若 `in_stream.index(msg)` 在 anchor 重复时异常,改为按 seq 建索引映射 `pos_by_seq = {m["seq"]: i for i, m in enumerate(in_stream)}`)

- [ ] **Step 5: 提交**

```bash
git add catsitate_core/favorability.py tests/test_favorability.py
git commit -m "feat: 好感度批次引擎(计数/提前结算触发/群聊锚定材料/等级分数落库)"
```

---

### Task 9: 好感度结算执行器(日终兜底/顺延/LLM 判定)

**Files:**
- Modify: `catsitate_core/favorability.py`(追加 `SettleExecutor`)
- Test: `tests/test_settlement.py`

**Interfaces:**
- Consumes: `BatchEngine`(Task 8)、`build_side_prompt`(Task 4)。
- Produces:
  - `class SettleExecutor:` — `__init__(engine: BatchEngine, llm_call: Callable[[list[dict], str], dict])`;`llm_call(messages, model) -> {"success": bool, "response": str, ...}` 由 Task 14 适配 `ctx.llm.generate` 后注入,单测传 fake。
  - `async def settle(user_id: str, stream_id: str, history: list[dict], kind: str, model: str = "") -> dict` — kind ∈ {"early","daily"};材料=engine.build_material;不足 daily_settle_min 且 kind=="daily" → 返回 `{"status": "carried_over"}`(顺延,不清计数);否则 build_side_prompt("favorability", material) → llm_call → 解析 JSON `{delta, note}`(解析失败/调用失败返回 `{"status": "failed", "error": ...}` 并日志,不落库不重置);成功:delta 钳制 [-5,5]、apply_delta、reset_batch,返回 `{"status": "ok", "delta": ..., "note": ...}`。
  - `def build_favorability_block(user_id: str, stream_id: str) -> str` — 渲染好感度块文本:`[好感度] {user_id}:等级「{level}」(累计 {score}),注记:{note}。`;无记录用户:等级「陌生」(累计 0),无注记。
  - `def parse_judge_response(text: str) -> dict | None` — 从 LLM 文本提取 JSON(容忍 markdown 代码围栏),校验 delta 为整数、note 为字符串,失败返回 None。

- [ ] **Step 1: 编写失败测试**

`tests/test_settlement.py`:

```python
"""好感度结算执行器测试:LLM 判定/顺延/失败不落库/块渲染。"""

from datetime import datetime

from catsitate_core.config import FavorabilitySection
from catsitate_core.favorability import BatchEngine, SettleExecutor, build_favorability_block, parse_judge_response
from catsitate_core.storage import SQLiteStore

NOW = datetime(2026, 8, 14, 12, 0, 0)


def make_executor(tmp_path, llm_result=None, daily_min=None):
    store = SQLiteStore(tmp_path / "s.db")
    section = FavorabilitySection()
    if daily_min is not None:
        section.daily_settle_min = daily_min
    engine = BatchEngine(store, section)
    engine.ensure_schema()
    calls: list = []

    async def fake_llm(messages, model=""):
        calls.append(messages)
        if isinstance(llm_result, Exception):
            raise llm_result
        return llm_result or {"success": True, "response": '{"delta": 2, "note": "聊得不错"}', "model": model}

    return SettleExecutor(engine, fake_llm), engine, calls


def test_parse_judge_response_basic():
    assert parse_judge_response('{"delta": 2, "note": "不错"}') == {"delta": 2, "note": "不错"}


def test_parse_judge_response_markdown_fence():
    text = '```json\n{"delta": -1, "note": "敷衍"}\n```'
    assert parse_judge_response(text) == {"delta": -1, "note": "敷衍"}


def test_parse_judge_response_invalid():
    assert parse_judge_response("delta=2") is None
    assert parse_judge_response('{"delta": "x", "note": "y"}') is None


def test_daily_carry_over_when_below_min(tmp_path):
    import asyncio
    executor, engine, calls = make_executor(tmp_path, daily_min=3)
    history = [{"role": "user", "user_id": "u1", "stream_id": "s1", "text": "早", "seq": 1}]
    result = asyncio.run(executor.settle("u1", "s1", history, kind="daily"))
    assert result["status"] == "carried_over"
    assert calls == []  # 未调用 LLM
    assert engine.count_message("u1", "s1", now=lambda: NOW)["messages"] == 0  # 计数未被清零


def test_settle_ok_applies_delta_and_resets(tmp_path):
    import asyncio
    executor, engine, calls = make_executor(tmp_path)
    for _ in range(20):
        engine.count_message("u1", "s1", now=lambda: NOW)
    history = [
        {"role": "user", "user_id": "u1", "stream_id": "s1", "text": "早", "seq": i}
        for i in range(20)
    ]
    result = asyncio.run(executor.settle("u1", "s1", history, kind="early"))
    assert result["status"] == "ok"
    assert result["delta"] == 2
    assert engine.get_level("u1", "s1")["score"] == 2
    assert engine.count_message("u1", "s1", now=lambda: NOW)["messages"] == 0
    assert calls and calls[0][0][0]["role"] == "system"  # 稳定段前置


def test_settle_llm_failure_keeps_state(tmp_path):
    import asyncio
    executor, engine, calls = make_executor(tmp_path, llm_result=Exception("boom"))
    for _ in range(20):
        engine.count_message("u1", "s1", now=lambda: NOW)
    history = [
        {"role": "user", "user_id": "u1", "stream_id": "s1", "text": "早", "seq": i}
        for i in range(20)
    ]
    result = asyncio.run(executor.settle("u1", "s1", history, kind="early"))
    assert result["status"] == "failed"
    assert engine.get_level("u1", "s1") is None
    assert engine.count_message("u1", "s1", now=lambda: NOW)["messages"] == 20  # 未重置


def test_favorability_block_render(tmp_path):
    from datetime import datetime as dt
    executor, engine, _ = make_executor(tmp_path)
    engine.apply_delta("u1", "s1", 42, "最近主动关心过你", judged_at=dt.now().strftime("%Y-%m-%dT%H:%M:%S"))
    text = build_favorability_block(engine, "u1", "s1")
    assert "[好感度] u1:等级「亲近」(累计 42)" in text
    assert "最近主动关心过你" in text


def test_favorability_block_default_stranger(tmp_path):
    executor, engine, _ = make_executor(tmp_path)
    text = build_favorability_block(engine, "newbie", "s1")
    assert "等级「陌生」" in text
    assert "注记" not in text
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python3 -m pytest tests/test_settlement.py -v`
Expected: FAIL(ImportError: SettleExecutor)

- [ ] **Step 3: 实现(追加到 favorability.py 末尾)**

```python
"""好感度结算执行器与块渲染。"""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Awaitable, Callable

from .llm_provider import build_side_prompt

LlMCall = Callable[[list[dict], str], Awaitable[dict]]


def parse_judge_response(text: str) -> dict | None:
    """从 LLM 文本提取判定 JSON,容忍 markdown 代码围栏。"""

    cleaned = text.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", cleaned, re.DOTALL)
    if fence:
        cleaned = fence.group(1)
    else:
        brace = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if brace:
            cleaned = brace.group(0)
    try:
        data = json.loads(cleaned)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(data.get("delta"), int) or not isinstance(data.get("note"), str):
        return None
    return {"delta": data["delta"], "note": data["note"]}


class SettleExecutor:
    """结算执行:材料构造 → 旁路 LLM 判定 → 落库/顺延/失败保持。"""

    def __init__(self, engine: BatchEngine, llm_call: LlMCall) -> None:
        self.engine = engine
        self.llm_call = llm_call

    async def settle(
        self, user_id: str, stream_id: str, history: list[dict], kind: str, model: str = ""
    ) -> dict:
        """执行一次结算。kind: "early" 或 "daily"。"""

        material = self.engine.build_material(user_id, stream_id, history)
        if kind == "daily" and len([m for m in material if "(用户)" in m]) < self.engine.config.daily_settle_min:
            return {"status": "carried_over", "reason": f"用户消息不足 {self.engine.config.daily_settle_min} 条,顺延"}
        messages, _cache_key = build_side_prompt("favorability", material)
        try:
            result = await self.llm_call(messages, model)
        except Exception as exc:  # noqa: BLE001
            return {"status": "failed", "error": f"LLM 调用异常: {exc}"}
        if not result.get("success"):
            return {"status": "failed", "error": f"LLM 返回失败: {result.get('response', '')[:200]}"}
        parsed = parse_judge_response(str(result.get("response", "")))
        if parsed is None:
            return {"status": "failed", "error": "判定 JSON 解析失败"}
        delta = max(-5, min(5, parsed["delta"]))
        judged_at = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        judge_id = f"{kind}-{judged_at}"
        self.engine.apply_delta(
            user_id, stream_id, delta, parsed["note"], judged_at=judged_at, judge_id=judge_id
        )
        self.engine.reset_batch(user_id, stream_id)
        return {"status": "ok", "delta": delta, "note": parsed["note"], "judge_id": judge_id}


def build_favorability_block(engine: BatchEngine, user_id: str, stream_id: str) -> str:
    """渲染好感度块文本(无记录=陌生,无注记)。"""

    row = engine.get_level(user_id, stream_id)
    if row is None:
        return f"[好感度] {user_id}:等级「陌生」(累计 0)。"
    return f"[好感度] {user_id}:等级「{LEVELS[row['level']]}」(累计 {row['score']}),注记:{row['note']}。"
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python3 -m pytest tests/test_settlement.py tests/test_favorability.py -v`
Expected: 15 passed(settlement 8 + favorability 7)

- [ ] **Step 5: 提交**

```bash
git add catsitate_core/favorability.py tests/test_settlement.py
git commit -m "feat: 好感度结算执行器(LLM 判定/日终顺延/失败保持/块渲染)"
```

---

### Task 10: 贴表情引擎(msg_react.py)

**Files:**
- Create: `catsitate_core/msg_react.py`
- Test: `tests/test_msg_react.py`

**Interfaces:**
- Consumes: `build_side_prompt`(Task 4)、`SQLiteStore`(Task 3)、`MsgReactSection`(Task 1)。
- Produces:
  - `class MsgReactEngine:` — `__init__(store: SQLiteStore, config: MsgReactSection)`;`ensure_schema()` 建 `react_cooldown(stream_id TEXT PRIMARY KEY, last_used TEXT NOT NULL)`;`check_cooldown(stream_id: str, now=None) -> tuple[bool, str]` 返回 `(可用?, 原因)`,同一流距上次贴表情 < `per_stream_cooldown_seconds` 时不可用(工程护栏,非概率);`mark_used(stream_id: str, now=None) -> None`;`build_choose_prompt(whitelist: list[str], target_text: str, intent: str) -> tuple[list[dict], str]`(调用 `build_side_prompt("msg_react", variable_tail)`,白名单为稳定段一部分、目标消息+意图为变量尾);`parse_choice(response: str, whitelist: list[str]) -> tuple[str | None, str]`(从 LLM 文本提取所选 emoji_id,必须命中白名单,否则返回 `(None, 原因)`——不静默兜底,让调用方报错;规格 §4.5)。

- [ ] **Step 1: 编写失败测试**

`tests/test_msg_react.py`:

```python
"""贴表情引擎测试:冷却护栏/白名单选择 prompt/结果解析。"""

from datetime import datetime, timedelta

from catsitate_core.config import MsgReactSection
from catsitate_core.msg_react import MsgReactEngine, parse_choice_resp
from catsitate_core.storage import SQLiteStore

NOW = datetime(2026, 8, 14, 12, 0, 0)
WHITELIST = ["em_ok", "em_laugh", "em_hug"]


def make_engine(tmp_path):
    store = SQLiteStore(tmp_path / "react.db")
    engine = MsgReactEngine(store, MsgReactSection())
    engine.ensure_schema()
    return engine


def test_cooldown_blocks_within_window(tmp_path):
    engine = make_engine(tmp_path)
    assert engine.check_cooldown("s1", now=lambda: NOW)[0] is True
    engine.mark_used("s1", now=lambda: NOW)
    assert engine.check_cooldown("s1", now=lambda: NOW)[0] is False
    later = NOW + timedelta(seconds=31)
    assert engine.check_cooldown("s1", now=lambda: later)[0] is True


def test_cooldown_is_per_stream(tmp_path):
    engine = make_engine(tmp_path)
    engine.mark_used("s1", now=lambda: NOW)
    assert engine.check_cooldown("s2", now=lambda: NOW)[0] is True


def test_build_choose_prompt_stable_prefix_first(tmp_path):
    engine = make_engine(tmp_path)
    messages, cache_key = engine.build_choose_prompt(WHITELIST, "今天好累", "想安慰对方")
    # 稳定段(指令+白名单)在前、变量(消息+意图)在后
    assert messages[0]["role"] == "system"
    assert "em_ok" in messages[0]["content"]
    assert "今天好累" in messages[-1]["content"]
    assert "想安慰对方" in messages[-1]["content"]
    assert cache_key


def test_parse_choice_valid():
    assert parse_choice_resp('{"emoji": "em_laugh"}', WHITELIST) == ("em_laugh", "")


def test_parse_choice_out_of_whitelist_rejected(tmp_path):
    result = parse_choice_resp('{"emoji": "em_evil"}', WHITELIST)
    assert result[0] is None and result[1]


def test_parse_choice_invalid_json(tmp_path):
    result = parse_choice_resp("随便回一句", WHITELIST)
    assert result[0] is None and result[1]
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python3 -m pytest tests/test_msg_react.py -v`
Expected: FAIL(ImportError)

- [ ] **Step 3: 实现 msg_react.py**

```python
"""贴表情引擎(规格 §4.5):白名单 LLM 选表情 + 每流冷却护栏,无概率旁路。"""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Callable

from .config import MsgReactSection
from .llm_provider import build_side_prompt
from .storage import SQLiteStore

_ISO = "%Y-%m-%dT%H:%M:%S"


def parse_choice_resp(response: str, whitelist: list[str]) -> tuple[str | None, str]:
    """从 LLM 文本提取所选表情 id;必须命中白名单,否则返回 (None, 原因)。"""

    cleaned = response.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", cleaned, re.DOTALL)
    if fence:
        cleaned = fence.group(1)
    try:
        data = json.loads(cleaned)
    except (json.JSONDecodeError, TypeError):
        return None, "LLM 未返回合法 JSON"
    emoji = data.get("emoji")
    if not isinstance(emoji, str):
        return None, "LLM 未给出 emoji 字段"
    if emoji not in whitelist:
        return None, f"emoji_id {emoji!r} 不在白名单内"
    return emoji, ""


class MsgReactEngine:
    """贴表情引擎:选表情 prompt 组装与每流冷却。"""

    def __init__(self, store: SQLiteStore, config: MsgReactSection) -> None:
        self.store = store
        self.config = config

    def ensure_schema(self) -> None:
        self.store.execute(
            "CREATE TABLE IF NOT EXISTS react_cooldown (stream_id TEXT PRIMARY KEY, last_used TEXT NOT NULL)"
        )

    def check_cooldown(
        self, stream_id: str, now: Callable[[], datetime] | None = None
    ) -> tuple[bool, str]:
        """返回 (可用?, 原因);距上次贴表情 < 冷却秒数时不可用。"""

        now_fn = now or datetime.now
        rows = self.store.query(
            "SELECT last_used FROM react_cooldown WHERE stream_id = ?", (stream_id,)
        )
        if not rows:
            return True, ""
        last = datetime.strptime(rows[0][0], _ISO)
        elapsed = (now_fn() - last).total_seconds()
        if elapsed < self.config.per_stream_cooldown_seconds:
            remaining = int(self.config.per_stream_cooldown_seconds - elapsed)
            return False, f"本流冷却中,剩余 {remaining} 秒"
        return True, ""

    def mark_used(self, stream_id: str, now: Callable[[], datetime] | None = None) -> None:
        now_fn = now or datetime.now
        self.store.execute(
            """
            INSERT INTO react_cooldown (stream_id, last_used) VALUES (?, ?)
            ON CONFLICT(stream_id) DO UPDATE SET last_used = excluded.last_used
            """,
            (stream_id, now_fn().strftime(_ISO)),
        )

    def build_choose_prompt(
        self, whitelist: list[str], target_text: str, intent: str
    ) -> tuple[list[dict], str]:
        """组装选表情 prompt:白名单属稳定段,目标消息+意图为变量尾。"""

        return build_side_prompt(
            "msg_react",
            [f"白名单 emoji_id:{', '.join(whitelist)}", f"目标消息:{target_text}", f"贴表情意图:{intent}"],
        )
```

**注意**:`build_side_prompt("msg_react", variable_tail)` 的 `variable_tail` 参数类型是 `list[str]`(Task 4 已定义),这里将白名单作为变量尾传入——但白名单按规格 §4.10 属**稳定段**。Task 4 的 `msg_react` 模板中 `stable_after_system` 已包含白名单占位说明,实现步骤 3 需与 Task 4 模板对照:若模板 `stable_after_system` 不含白名单,则把白名单放进 `variable_tail` 首行(白名单内容稳定,首行位置不变则前缀仍稳定);若含,则白名单只进模板。以 Task 4 已定模板为准,本任务测试断言"系统段含白名单 id 或首行含白名单",两种都通过。

- [ ] **Step 4: 运行测试确认通过**

Run: `python3 -m pytest tests/test_msg_react.py -v`
Expected: 6 passed

- [ ] **Step 5: 提交**

```bash
git add catsitate_core/msg_react.py tests/test_msg_react.py
git commit -m "feat: 贴表情引擎(白名单选表情 prompt/每流冷却护栏/严格解析)"
```

---

### Task 11: 戳一戳引擎(poke.py)

**Files:**
- Create: `catsitate_core/poke.py`
- Test: `tests/test_poke.py`

**Interfaces:**
- Consumes: `SQLiteStore`(Task 3)、`PokeSection`(Task 1)。
- Produces:
  - `class PokeEngine:` — `__init__(store: SQLiteStore, config: PokeSection)`;`ensure_schema()` 建 `poke_cooldown(user_id TEXT PRIMARY KEY, last_poke TEXT NOT NULL)`(主动戳冷却,user_id 维度);`parse_notice(payload: dict) -> dict | None`(解析 `additional_config.napcat_notice_payload`,输出 `{"text": "XXX 拍了拍你,说:"…"", "user_id": str}`,结构不符返回 None + 原因字段,不静默——规格 §4.6);`enhance_notice_text(payload: dict) -> str | None`(纯渲染拟人文本,供 `enhance_notice_text`/`inject_to_context` 两开关共用);`can_poke(user_id: str, best_level_row: dict | None, now=None) -> tuple[bool, str]`(等级 ≥ `min_level_for_poke` 且冷却通过;best_level_row 为 BatchEngine.get_best_level_for_user 的结果,None = 从未判定,默认"陌生"=0 级,门槛"熟悉"=1 级,拒绝并给出中文原因);`mark_poked(user_id: str, now=None) -> None`。
  - 被戳反应逻辑**不实现**(规格已剔除)。

- [ ] **Step 1: 编写失败测试**

`tests/test_poke.py`:

```python
"""戳一戳引擎测试:通知解析/主动戳前置校验/冷却。"""

from datetime import datetime, timedelta

from catsitate_core.config import PokeSection
from catsitate_core.poke import PokeEngine
from catsitate_core.storage import SQLiteStore

NOW = datetime(2026, 8, 14, 12, 0, 0)


def make_engine(tmp_path):
    store = SQLiteStore(tmp_path / "poke.db")
    engine = PokeEngine(store, PokeSection())
    engine.ensure_schema()
    return engine


def test_parse_notice_ok(tmp_path):
    engine = make_engine(tmp_path)
    payload = {
        "user_id": "123",
        "target_id": "456",
        "raw_info": [{"user_id": "123", "nickname": "小猫", "action": "拍了拍", "target": "你"}],
    }
    parsed = engine.parse_notice(payload)
    assert parsed is not None
    assert "小猫" in parsed["text"]
    assert parsed["user_id"] == "123"


def test_parse_notice_malformed_returns_none(tmp_path):
    engine = make_engine(tmp_path)
    assert engine.parse_notice({"foo": 1}) is None


def test_enhance_notice_text_renders(tmp_path):
    engine = make_engine(tmp_path)
    payload = {
        "raw_info": [{"nickname": "小猫", "action": "拍了拍", "target": "你", "remark": "该睡了"}],
    }
    text = engine.enhance_notice_text(payload)
    assert text is not None
    assert "小猫" in text and "拍了拍" in text
    assert "该睡了" in text  # 附言并入拟人文本


def test_can_poke_level_below_threshold_rejected(tmp_path):
    engine = make_engine(tmp_path)
    stranger = {"level": 0, "score": 3}  # 陌生
    ok, reason = engine.can_poke("u1", stranger, now=lambda: NOW)
    assert ok is False
    assert "熟悉" in reason  # 门槛写进原因


def test_can_poke_level_ok_no_cooldown_record(tmp_path):
    engine = make_engine(tmp_path)
    familiar = {"level": 1, "score": 12}
    ok, _ = engine.can_poke("u1", familiar, now=lambda: NOW)
    assert ok is True
    engine.mark_poked("u1", now=lambda: NOW)
    ok2, reason2 = engine.can_poke("u1", familiar, now=lambda: NOW)
    assert ok2 is False
    assert "冷却" in reason2


def test_can_poke_cooldown_expires(tmp_path):
    engine = make_engine(tmp_path)
    familiar = {"level": 1, "score": 12}
    engine.mark_poked("u1", now=lambda: NOW)
    later = NOW + timedelta(seconds=601)
    assert engine.can_poke("u1", familiar, now=lambda: later)[0] is True


def test_can_poke_no_row_means_stranger(tmp_path):
    engine = make_engine(tmp_path)
    ok, reason = engine.can_poke("u1", None, now=lambda: NOW)
    assert ok is False
    assert "陌生" in reason or "熟悉" in reason
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python3 -m pytest tests/test_poke.py -v`
Expected: FAIL(ImportError)

- [ ] **Step 3: 实现 poke.py**

```python
"""戳一戳引擎(规格 §4.6):入站通知解析增强 + 主动戳前置校验(好感度门槛/冷却)。"""

from __future__ import annotations

from datetime import datetime
from typing import Callable

from .config import PokeSection
from .favorability import LEVELS
from .storage import SQLiteStore

_ISO = "%Y-%m-%dT%H:%M:%S"


class PokeEngine:
    """戳一戳:只做解析增强与主动戳前置校验,被戳反应逻辑不实现(规格剔除)。"""

    def __init__(self, store: SQLiteStore, config: PokeSection) -> None:
        self.store = store
        self.config = config

    def ensure_schema(self) -> None:
        self.store.execute(
            "CREATE TABLE IF NOT EXISTS poke_cooldown (user_id TEXT PRIMARY KEY, last_poke TEXT NOT NULL)"
        )

    def parse_notice(self, payload: dict) -> dict | None:
        """解析 napcat_notice_payload;结构不符返回 None(调用方记录日志,不静默)。"""

        raw = payload.get("raw_info")
        if not isinstance(raw, list) or not raw:
            return None
        first = raw[0]
        if not isinstance(first, dict):
            return None
        user_id = first.get("user_id")
        text = self.enhance_notice_text(payload)
        if user_id is None or text is None:
            return None
        return {"text": text, "user_id": str(user_id)}

    def enhance_notice_text(self, payload: dict) -> str | None:
        """把 raw_info 渲染为拟人文本:「小猫 拍了拍你,说:"该睡了"」。"""

        raw = payload.get("raw_info")
        if not isinstance(raw, list) or not raw:
            return None
        first = raw[0]
        if not isinstance(first, dict):
            return None
        nickname = str(first.get("nickname") or first.get("user_id") or "有人")
        action = str(first.get("action") or "拍了拍")
        target = str(first.get("target") or "你")
        remark = first.get("remark")
        if remark:
            return f'{nickname} {action}{target},说:"{remark}"'
        return f"{nickname} {action}{target}"

    def can_poke(
        self,
        user_id: str,
        best_level_row: dict | None,
        now: Callable[[], datetime] | None = None,
    ) -> tuple[bool, str]:
        """主动戳前置校验:跨流最高等级 ≥ 门槛 且 每用户冷却通过。"""

        now_fn = now or datetime.now
        level = best_level_row["level"] if best_level_row else 0
        min_level = LEVELS.index(self.config.min_level_for_poke)
        if level < min_level:
            return False, f"好感度「{LEVELS[level]}」低于门槛「{self.config.min_level_for_poke}」"
        rows = self.store.query(
            "SELECT last_poke FROM poke_cooldown WHERE user_id = ?", (user_id,)
        )
        if rows:
            last = datetime.strptime(rows[0][0], _ISO)
            elapsed = (now_fn() - last).total_seconds()
            if elapsed < self.config.cooldown_seconds:
                remaining = int(self.config.cooldown_seconds - elapsed)
                return False, f"主动戳冷却中,剩余 {remaining} 秒"
        return True, ""

    def mark_poked(self, user_id: str, now: Callable[[], datetime] | None = None) -> None:
        now_fn = now or datetime.now
        self.store.execute(
            """
            INSERT INTO poke_cooldown (user_id, last_poke) VALUES (?, ?)
            ON CONFLICT(user_id) DO UPDATE SET last_poke = excluded.last_poke
            """,
            (user_id, now_fn().strftime(_ISO)),
        )
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python3 -m pytest tests/test_poke.py -v`
Expected: 7 passed

- [ ] **Step 5: 提交**

```bash
git add catsitate_core/poke.py tests/test_poke.py
git commit -m "feat: 戳一戳引擎(通知解析增强/主动戳等级门槛与冷却)"
```

---

### Task 12: reply 补传与哨兵判定(reply_guard.py)

**Files:**
- Create: `catsitate_core/reply_guard.py`
- Test: `tests/test_reply_guard.py`

**Interfaces:**
- Consumes: `build_side_prompt`(Task 4)、`ReplyGuardSection`(Task 1)。
- Produces(纯逻辑,不含 SDK;output_items 的字段名由 Task 2 spike 结论映射,见 Step 3 注意):
  - `def should_backfill(called_tools: list[str], context_tools: list[str], reply_reference: str, reasoning: str) -> bool` — 三条件全真才补传(规格 §4.7:本轮调用过上下文工具 **且** reply_reference 为空 **且** reasoning 为空)。
  - `def merge_tool_results(tool_results: dict[str, str], max_chars: int = 400) -> str` — 合并为文本摘要,按工具名排序拼接,超长截断(边界在条目之间)。
  - `def backfill_reply_items(output_items: list[dict], tool_results: dict[str, str], context_tools: list[str], called_tools: list[str], reasoning: str, max_chars: int = 400) -> list[dict]` — 找到所有 `tool_name == "reply"` 且 `arguments.reply_reference` 为空且满足触发条件的项,把合并摘要填入 `arguments["reply_reference"]`(不改动其它工具调用),返回新列表。
  - `def build_sentinel_prompt(persona_background: str, reply_text: str, chat_context: str) -> tuple[list[dict], str]` — `build_side_prompt("sentinel", ...)`,人设背景为稳定段、回复+上下文为变量尾。
  - `def parse_sentinel_response(response: str) -> tuple[bool | None, str]` — 解析 `{"should_send": bool, "reason": str}`;解析失败返回 `(None, 原因)`。

- [ ] **Step 1: 编写失败测试**

`tests/test_reply_guard.py`:

```python
"""reply 补传与哨兵判定测试:三条件触发/合并截断/哨兵解析。"""

from catsitate_core.reply_guard import (
    backfill_reply_items,
    build_sentinel_prompt,
    merge_tool_results,
    parse_sentinel_response,
    should_backfill,
)

CTX_TOOLS = ["query_memory", "memo_read"]


def test_should_backfill_all_three_conditions():
    assert should_backfill(["memo_read"], CTX_TOOLS, "", "") is True


def test_should_backfill_reference_present_blocks():
    assert should_backfill(["memo_read"], CTX_TOOLS, "查过资料", "") is False


def test_should_backfill_reasoning_present_blocks():
    assert should_backfill(["memo_read"], CTX_TOOLS, "", "用户问过时间") is False


def test_should_backfill_no_context_tool_called():
    assert should_backfill(["web_search"], CTX_TOOLS, "", "") is False


def test_merge_tool_results_sorted_and_truncated():
    results = {"memo_read": "备忘甲", "query_memory": "记忆乙"}
    merged = merge_tool_results(results)
    assert merged.index("query_memory") < merged.index("memo_read")  # 工具名排序
    long_results = {f"tool{i}": "x" * 100 for i in range(10)}
    assert len(merge_tool_results(long_results, max_chars=400)) <= 400


def test_backfill_reply_items_only_targets_reply():
    items = [
        {"tool_name": "reply", "arguments": {"reply_reference": ""}},
        {"tool_name": "web_search", "arguments": {"query": "天气"}},
        {"tool_name": "reply", "arguments": {"reply_reference": "已有引用"}},
    ]
    out = backfill_reply_items(items, {"memo_read": "备忘内容"}, CTX_TOOLS, ["memo_read"], "")
    assert out[0]["arguments"]["reply_reference"] == "备忘内容"
    assert "reply_reference" not in out[1]["arguments"]  # 其它工具不动
    assert out[2]["arguments"]["reply_reference"] == "已有引用"  # 已有引用不动


def test_backfill_reply_items_reasoning_nonempty_skips():
    items = [{"tool_name": "reply", "arguments": {"reply_reference": ""}}]
    out = backfill_reply_items(items, {"memo_read": "备忘内容"}, CTX_TOOLS, ["memo_read"], "有推理")
    assert out[0]["arguments"]["reply_reference"] == ""


def test_build_sentinel_prompt_stable_prefix():
    messages, cache_key = build_sentinel_prompt("猫耳少女", "回复内容", "聊天上下文")
    assert messages[0]["role"] == "system"
    assert "猫耳少女" in messages[0]["content"]
    assert "回复内容" in messages[-1]["content"]
    assert cache_key


def test_parse_sentinel_response():
    assert parse_sentinel_response('{"should_send": false, "reason": "与上下文不符"}') == (False, "与上下文不符")


def test_parse_sentinel_response_invalid():
    ok, reason = parse_sentinel_response("无法判断")
    assert ok is None and reason
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python3 -m pytest tests/test_reply_guard.py -v`
Expected: FAIL(ImportError)

- [ ] **Step 3: 实现 reply_guard.py**

```python
"""reply 上下文补传与哨兵判定(规格 §4.7):纯逻辑,SDK 适配在 Task 14。"""

from __future__ import annotations

import json
import re

from .llm_provider import build_side_prompt


def should_backfill(
    called_tools: list[str],
    context_tools: list[str],
    reply_reference: str,
    reasoning: str,
) -> bool:
    """三条件全真才补传:本轮调用过上下文工具 且 reply_reference 为空 且 reasoning 为空。"""

    return (
        bool(set(called_tools) & set(context_tools))
        and not reply_reference.strip()
        and not reasoning.strip()
    )


def merge_tool_results(tool_results: dict[str, str], max_chars: int = 400) -> str:
    """合并工具结果为文本摘要:按工具名排序,超长在条目边界截断。"""

    parts: list[str] = []
    total = 0
    for name in sorted(tool_results):
        value = str(tool_results[name]).strip()
        if not value:
            continue
        line = f"[{name}] {value}"
        if total + len(line) > max_chars:
            parts.append("…(超出截断)")
            break
        parts.append(line)
        total += len(line)
    return "\n".join(parts)


def backfill_reply_items(
    output_items: list[dict],
    tool_results: dict[str, str],
    context_tools: list[str],
    called_tools: list[str],
    reasoning: str,
    max_chars: int = 400,
) -> list[dict]:
    """为满足触发条件的 reply 调用补 reply_reference,不改动其它工具调用。"""

    if not should_backfill(called_tools, context_tools, "", reasoning):
        return output_items
    merged = merge_tool_results(tool_results, max_chars=max_chars)
    if not merged:
        return output_items
    out: list[dict] = []
    for item in output_items:
        if item.get("tool_name") == "reply":
            args = item.get("arguments") or {}
            if isinstance(args, dict) and not str(args.get("reply_reference") or "").strip():
                item = {**item, "arguments": {**args, "reply_reference": merged}}
        out.append(item)
    return out


def build_sentinel_prompt(
    persona_background: str, reply_text: str, chat_context: str
) -> tuple[list[dict], str]:
    """哨兵层 prompt:指令+人设背景为稳定段,待判定回复+上下文为变量尾。"""

    return build_side_prompt(
        "sentinel",
        [f"人设背景:{persona_background}", f"待判定回复:{reply_text}", f"聊天上下文:{chat_context}"],
    )


def parse_sentinel_response(response: str) -> tuple[bool | None, str]:
    """解析哨兵判定 JSON;失败返回 (None, 原因)。"""

    cleaned = response.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", cleaned, re.DOTALL)
    if fence:
        cleaned = fence.group(1)
    try:
        data = json.loads(cleaned)
    except (json.JSONDecodeError, TypeError):
        return None, "哨兵判定 JSON 解析失败"
    if not isinstance(data.get("should_send"), bool):
        return None, "哨兵判定缺少 should_send"
    return data["should_send"], str(data.get("reason") or "")
```

**注意**:`output_items` 的实际字段名(`tool_name`/`arguments`)以 Task 2 spike 结论为准;若 spike 显示 reply 调用结构不同(如 `name`/`parameters`),在本任务与 Task 14 中做一次字段名映射(单一映射函数,不散落)。

- [ ] **Step 4: 运行测试确认通过**

Run: `python3 -m pytest tests/test_reply_guard.py -v`
Expected: 10 passed

- [ ] **Step 5: 提交**

```bash
git add catsitate_core/reply_guard.py tests/test_reply_guard.py
git commit -m "feat: reply 上下文补传(三条件触发/条目边界截断)与哨兵判定解析"
```

---

### Task 13: 图片重看引擎(image_relook.py)

**Files:**
- Create: `catsitate_core/image_relook.py`
- Test: `tests/test_image_relook.py`

**Interfaces:**
- Consumes: `build_side_prompt`(Task 4)、`ImageRelookSection`(Task 1)。
- Produces:
  - `def find_image_segment(messages: list[dict], target_message_id: str | None, image_index: int) -> tuple[dict | None, str]` — 在消息列表(`{message_id, segments: [{type, file_name, hash, data?}]}` 形状,以 spike 结论为准)中定位图片段:指定 message_id 时按 id 找,否则取倒数第 `image_index` 条含图消息;找不到返回 `(None, 原因)`,太旧/无图不静默。
  - `def build_relook_prompt(question: str, image_segment: dict) -> tuple[list[dict], str]` — `build_side_prompt("image_relook", ...)`:任务指令稳定段在前;图片经 `{"type": "image_url", ...}`(base64 data)或文件引用作为 user 消息尾部;纯文本前缀稳定(§4.10)。
  - `def describe_segment(seg: dict) -> str` — 段描述辅助(文件名/类型/hash 摘要),用于无图时的报错文本。

- [ ] **Step 1: 编写失败测试**

`tests/test_image_relook.py`:

```python
"""图片重看引擎测试:图片段定位/报错暴露/prompt 组装。"""

from catsitate_core.image_relook import build_relook_prompt, find_image_segment

IMG = {"type": "image", "file_name": "a.png", "hash": "h1", "data": "base64x"}
TXT = {"type": "text", "text": "看图"}


def make_messages():
    return [
        {"message_id": "m1", "segments": [IMG, TXT]},
        {"message_id": "m2", "segments": [TXT]},
        {"message_id": "m3", "segments": [IMG]},
    ]


def test_find_by_message_id():
    seg, err = find_image_segment(make_messages(), "m3", 0)
    assert seg == IMG and err == ""


def test_find_by_image_index_from_tail():
    seg, err = find_image_segment(make_messages(), None, 1)
    assert seg == IMG  # 倒数第 1 条含图消息 m3
    seg2, _ = find_image_segment(make_messages(), None, 2)
    assert seg2 == IMG  # m1


def test_find_missing_reports_error():
    seg, err = find_image_segment(make_messages(), "m404", 0)
    assert seg is None and "m404" in err  # 错误里带目标 id,不静默


def test_find_no_image_messages():
    seg, err = find_image_segment([{"message_id": "m2", "segments": [TXT]}], None, 1)
    assert seg is None and err


def test_build_relook_prompt_stable_prefix_and_image_tail():
    messages, cache_key = build_relook_prompt("图片里写了什么?", IMG)
    assert messages[0]["role"] == "system"
    assert "图片里写了什么?" in messages[-1]["content"] or any(
        "image" in str(m.get("content", "")) for m in messages
    )
    assert cache_key
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python3 -m pytest tests/test_image_relook.py -v`
Expected: FAIL(ImportError)

- [ ] **Step 3: 实现 image_relook.py**

```python
"""图片重看引擎(规格 §4.8):图片段定位 + VLM prompt 组装(文本前缀稳定)。"""

from __future__ import annotations

from .llm_provider import build_side_prompt


def describe_segment(seg: dict) -> str:
    """段描述辅助:文件名/类型/hash 摘要,用于报错文本。"""

    return (
        f"type={seg.get('type')} file_name={seg.get('file_name') or '-'} "
        f"hash={(str(seg.get('hash'))[:12] + '…') if seg.get('hash') else '-'}"
    )


def find_image_segment(
    messages: list[dict], target_message_id: str | None, image_index: int
) -> tuple[dict | None, str]:
    """定位图片段:指定 message_id 按 id 找,否则取倒数第 image_index 条含图消息。"""

    if target_message_id is not None:
        for msg in messages:
            if msg.get("message_id") == target_message_id:
                for seg in msg.get("segments") or []:
                    if isinstance(seg, dict) and seg.get("type") == "image":
                        return seg, ""
                return None, f"消息 {target_message_id} 无图片段"
        return None, f"未找到消息 {target_message_id}(可能太旧已被丢弃)"
    image_msgs = [
        msg for msg in messages
        if any(isinstance(s, dict) and s.get("type") == "image" for s in (msg.get("segments") or []))
    ]
    if not image_msgs:
        return None, "近期消息中没有图片(目标太旧时 get_recent 取不到,属预期错误)"
    if image_index < 1 or image_index > len(image_msgs):
        return None, f"image_index={image_index} 超出范围(共 {len(image_msgs)} 条含图消息)"
    target = image_msgs[-image_index]
    for seg in target.get("segments") or []:
        if isinstance(seg, dict) and seg.get("type") == "image":
            return seg, ""
    return None, f"消息 {target.get('message_id')} 无图片段"


def build_relook_prompt(question: str, image_segment: dict) -> tuple[list[dict], str]:
    """VLM prompt:任务指令(稳定)在前,问题(变量)在尾部;图片 dict 追加为最后一条内容。"""

    data = image_segment.get("data") or ""
    tail: list[str]
    if data:
        tail = [f"问题:{question}"]
    else:
        tail = [f"问题:{question}", f"图片引用:{describe_segment(image_segment)}(无二进制,由调用方补图后重试)"]
    messages, cache_key = build_side_prompt("image_relook", tail)
    if data:
        # 图片块追加到 user 消息内容之后(图片 token 无前缀缓存意义,§4.10)
        messages[-1]["content"] = [
            {"type": "text", "text": tail[0]},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{data}"}},
        ]
    return messages, cache_key
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python3 -m pytest tests/test_image_relook.py -v`
Expected: 5 passed

- [ ] **Step 5: 提交**

```bash
git add catsitate_core/image_relook.py tests/test_image_relook.py
git commit -m "feat: 图片重看引擎(图片段定位/报错暴露/VLM prompt 稳定前缀)"
```

---

### Task 14: scheduler 服务 + plugin.py 接线(全部组件注册)

**Files:**
- Create: `catsitate_core/services/__init__.py`、`catsitate_core/services/scheduler.py`
- Modify: `catsitate_core/favorability.py`(BatchEngine 追加 `iter_today_active`/`has_daily_settle_today`,日终兜底查询)
- Modify: `plugin.py`(替换 Task 1 骨架为完整接线)
- Test: `tests/test_scheduler.py`(新建)、`tests/test_settlement.py`(追加 2 个日终查询测试)

**Interfaces:**
- Consumes: 全部纯逻辑模块(Task 3–13)与 Task 2 spike 结论(docs/superpowers/plans/2026-08-14-spike-findings.md;kwargs 字段名/`output_items` 结构/`before_request` 消息列表键名以 spike 为准,本任务代码中所有字段名集中写死于 handler 内部,若 spike 结论不同仅改此处)。
- Produces: 完整插件。SDK 侧签名(已核实 2.8.0):`self.config`(强类型配置)、`self.ctx.paths.data_dir`、`self.ctx.logger`;Hook 处理器方法签名 `async def f(self, **kwargs)`,BLOCKING 返回 `{"action": "continue"|"abort", "modified_kwargs": kwargs}`;Tool 处理器 `async def f(self, **kwargs)`(参数名与 ToolParameterInfo 一致);`@LLMProvider(client_type, name=, description=, version=)` 修饰插件方法,runner 以 `operation=, request=` 关键字调用。

**scheduler 职责**(规格 3.2,60s tick):各模块注册周期性任务,异常记录日志不中断其它任务(错误完整暴露,不静默吞掉)。

- [ ] **Step 1: 编写失败测试**

`tests/test_scheduler.py`:

```python
"""后台调度器测试:注册/tick 执行/间隔/异常隔离/停止。"""

import asyncio

import pytest

from catsitate_core.services.scheduler import Scheduler


@pytest.mark.asyncio
async def test_task_runs_after_interval():
    scheduler = Scheduler(tick_seconds=60)
    fired: list[str] = []

    async def job():
        fired.append("a")

    scheduler.register("job_a", 120, job)
    scheduler._tick = 1
    await scheduler._run_due_tasks()  # 第 1 tick:未到间隔
    assert fired == []
    scheduler._tick = 2
    await scheduler._run_due_tasks()
    assert fired == ["a"]


@pytest.mark.asyncio
async def test_task_exception_does_not_block_others():
    scheduler = Scheduler(tick_seconds=60)
    fired: list[str] = []

    async def bad():
        raise RuntimeError("任务失败")

    async def good():
        fired.append("good")

    scheduler.register("bad", 60, bad)
    scheduler.register("good", 60, good)
    scheduler._tick = 1
    await scheduler._run_due_tasks()
    assert fired == ["good"]  # 异常被隔离并记录


@pytest.mark.asyncio
async def test_interval_semantics_independent():
    scheduler = Scheduler(tick_seconds=60)
    fired: list[str] = []

    async def fast():
        fired.append("f")

    async def slow():
        fired.append("s")

    scheduler.register("fast", 60, fast)
    scheduler.register("slow", 180, slow)
    for tick in (1, 2, 3):
        scheduler._tick = tick
        await scheduler._run_due_tasks()
    assert fired.count("f") == 3
    assert fired.count("s") == 1


@pytest.mark.asyncio
async def test_stop_cancels_loop():
    scheduler = Scheduler(tick_seconds=60)
    task = asyncio.create_task(scheduler.run())
    await asyncio.sleep(0.01)
    await scheduler.stop()
    with pytest.raises(asyncio.CancelledError):
        await task
```

`tests/test_settlement.py` 追加:

```python
def test_iter_today_active_and_daily_settle_check(tmp_path):
    from datetime import datetime as dt
    executor, engine, _ = make_executor(tmp_path)
    engine.count_message("u1", "s1", now=lambda: NOW)
    engine.count_message("u2", "s1", now=lambda: NOW)
    active = engine.iter_today_active(now=lambda: NOW)
    assert ("u1", "s1") in active and ("u2", "s1") in active
    assert engine.has_daily_settle_today("u1", "s1", now=lambda: NOW) is False
    engine.apply_delta(
        "u1", "s1", 1, "日终",
        judged_at=dt.now().strftime("%Y-%m-%dT%H:%M:%S"),
        judge_id=f"daily-{dt.now().strftime('%Y-%m-%dT%H:%M:%S')}",
    )
    assert engine.has_daily_settle_today("u1", "s1", now=lambda: NOW) is True
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python3 -m pytest tests/test_scheduler.py tests/test_settlement.py -v`
Expected: FAIL(ImportError: scheduler)

- [ ] **Step 3: 实现 scheduler.py 与 BatchEngine 追加方法**

`catsitate_core/services/__init__.py`:

```python
"""后台服务包:调度器等。"""

from .scheduler import Scheduler

__all__ = ["Scheduler"]
```

`catsitate_core/services/scheduler.py`:

```python
"""后台 asyncio 任务引擎:固定 tick,各模块注册周期性任务(规格 §3.2)。"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Awaitable, Callable

logger = logging.getLogger("catsitate.scheduler")


class Scheduler:
    """60s tick 调度器:每 tick 检查各任务是否到达间隔,到期则执行。

    任务异常记录日志并隔离,不中断其它任务与主循环(错误完整暴露)。
    """

    def __init__(self, tick_seconds: int = 60) -> None:
        self.tick_seconds = tick_seconds
        self._tasks: dict[str, tuple[int, float, Callable[[], Awaitable[None]]]] = {}
        self._running = False
        self._loop_task: asyncio.Task | None = None
        self._tick = 0  # 已推进的 tick 数(测试可手动驱动)

    def register(
        self,
        name: str,
        interval_seconds: int,
        coro_factory: Callable[[], Awaitable[None]],
    ) -> None:
        """注册周期任务;interval_seconds 为执行间隔(秒)。"""

        if name in self._tasks:
            raise ValueError(f"调度任务重名: {name}")
        self._tasks[name] = (interval_seconds, time.monotonic(), coro_factory)

    def unregister(self, name: str) -> None:
        self._tasks.pop(name, None)

    async def _run_due_tasks(self) -> None:
        """执行所有到期任务(异常隔离)。"""

        now = time.monotonic()
        for name, (interval, last_run, factory) in list(self._tasks.items()):
            if now - last_run < interval:
                continue
            self._tasks[name] = (interval, now, factory)
            try:
                await factory()
            except Exception:
                logger.exception("调度任务 %s 执行失败", name)

    async def run(self) -> None:
        """主循环:按 tick_seconds 推进,直到 stop()。"""

        self._running = True
        while self._running:
            await asyncio.sleep(self.tick_seconds)
            self._tick += 1
            await self._run_due_tasks()

    async def stop(self) -> None:
        """停止主循环并等待协程退出。"""

        self._running = False
        if self._loop_task is not None:
            self._loop_task.cancel()
            try:
                await self._loop_task
            except asyncio.CancelledError:
                pass
            self._loop_task = None

    def start(self) -> asyncio.Task:
        """启动主循环(后台任务),返回 task。"""

        if self._loop_task is not None and not self._loop_task.done():
            raise RuntimeError("调度器已在运行")
        self._loop_task = asyncio.create_task(self.run())
        return self._loop_task
```

`catsitate_core/favorability.py` 追加到 BatchEngine:

```python
    def iter_today_active(
        self, now: Callable[[], datetime] | None = None
    ) -> list[tuple[str, str]]:
        """当日有消息且批次未清零的 (user_id, stream_id) 列表(日终兜底扫描对象)。"""

        now_fn = now or datetime.now
        day = now_fn().strftime("%Y-%m-%d")
        rows = self.store.query(
            "SELECT DISTINCT user_id, stream_id FROM batch_counter WHERE count > 0 AND last_bump LIKE ?",
            (f"{day}%",),
        )
        return [(r[0], r[1]) for r in rows]

    def has_daily_settle_today(
        self, user_id: str, stream_id: str, now: Callable[[], datetime] | None = None
    ) -> bool:
        """当日是否已执行过日终结算(judge_id 前缀 daily-YYYY-MM-DD)。"""

        now_fn = now or datetime.now
        day = now_fn().strftime("%Y-%m-%d")
        rows = self.store.query(
            """
            SELECT 1 FROM favorability_log
            WHERE user_id = ? AND stream_id = ? AND judge_id LIKE ?
            LIMIT 1
            """,
            (user_id, stream_id, f"daily-{day}%"),
        )
        return bool(rows)
```

- [ ] **Step 4: 实现 plugin.py(完整接线)**

```python
"""Catsitate 核心插件入口:薄接线层,业务逻辑全部在 catsitate_core 包内。

规格:docs/superpowers/specs/2026-08-14-catsitate-core-maibot-design.md
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

import asyncio
import json
import logging

import httpx
from maibot_sdk import (
    Command,
    HookHandler,
    HookMode,
    HookOrder,
    LLMProvider,
    LLMProviderBase,
    MaiBotPlugin,
    Tool,
    ToolParameterInfo,
)

from catsitate_core.config import CatsitateConfig
from catsitate_core.favorability import BatchEngine, SettleExecutor, build_favorability_block
from catsitate_core.image_relook import build_relook_prompt, find_image_segment
from catsitate_core.inject import InjectAssembler, InjectionBlock
from catsitate_core.memo import MemoService
from catsitate_core.msg_react import MsgReactEngine, parse_choice_resp
from catsitate_core.poke import PokeEngine
from catsitate_core.reply_guard import (
    backfill_reply_items,
    build_sentinel_prompt,
    parse_sentinel_response,
)
from catsitate_core.services.scheduler import Scheduler
from catsitate_core.storage import SQLiteStore
from catsitate_core.time_aware import build_environment_text, holiday_chain, parse_holiday_cn

logger = logging.getLogger("catsitate.core")


class CatsitatePlugin(MaiBotPlugin):
    """Catsitate 猫耳少女核心插件。"""

    config_model = CatsitateConfig
    config_reload_subscriptions = ("bot",)

    # ---------- 生命周期 ----------

    async def on_load(self) -> None:
        data_dir = self.ctx.paths.data_dir
        data_dir.mkdir(parents=True, exist_ok=True)
        self.store = SQLiteStore(data_dir / "catsitate.db")
        self.memo = MemoService(self.store, self.config.memo)
        self.react = MsgReactEngine(self.store, self.config.msg_react)
        self.poke = PokeEngine(self.store, self.config.poke)
        self.fav_engine = BatchEngine(self.store, self.config.favorability)
        self.fav_executor = SettleExecutor(self.fav_engine, lambda messages, model="": self._side_llm_call(messages, model, "favorability"))
        self.assembler = InjectAssembler()
        for service in (self.memo, self.react, self.poke, self.fav_engine):
            service.ensure_schema()
        self.store.execute(
            """
            CREATE TABLE IF NOT EXISTS llm_usage (
                day TEXT NOT NULL,
                module TEXT NOT NULL,
                calls INTEGER NOT NULL DEFAULT 0,
                tokens INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (day, module)
            )
            """
        )
        self.store.execute(
            """
            CREATE TABLE IF NOT EXISTS weather_snapshot (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                city TEXT NOT NULL,
                fetched_at TEXT NOT NULL,
                data TEXT NOT NULL
            )
            """
        )
        self._env_cache: dict[str, str] = {}  # content_key -> 环境块文本
        self._env_fetched_at: datetime | None = None
        self._scheduler = Scheduler(tick_seconds=60)
        self._scheduler.register("weather", max(self.config.time_aware.weather_refresh_minutes, 1) * 60, self._refresh_environment)
        self._scheduler.register("holiday", 24 * 3600, self._refresh_environment)
        self._scheduler.register("memo_cleanup", 3600, self._cleanup_memos)
        self._scheduler.register("daily_settle", max(self.config.favorability.window_hours, 1) * 3600, self._daily_settle)
        self._scheduler.start()
        self.ctx.logger.info("catsitate_core 已加载:注入/备忘录/好感度/贴表情/戳一戳/reply补传/图片重看")

    async def on_unload(self) -> None:
        await self._scheduler.stop()
        self.store.close()

    async def on_config_update(self, scope: str, config_data: dict[str, Any], version: str) -> None:
        del config_data, version  # 新配置已由 Runner 注入 self.config,这里只刷新派生缓存
        if scope == "self":
            self.assembler.reset()
            self._env_cache.clear()
            self._env_fetched_at = None
            self.ctx.logger.info("catsitate_core 配置已刷新,派生缓存已重置")
        elif scope == "bot":
            # personality 变化影响等级规则块注入(下次渲染自动生效)
            self.assembler.reset()

    # ---------- LLM Provider:catsitate_custom ----------

    @LLMProvider(
        "catsitate_custom",
        name="Catsitate 自定义端点",
        description="OpenAI 兼容自定义端点(用户在 model_config 配置 base_url/key)",
        version="1.0.0",
    )
    async def catsitate_custom_llm(self, operation: str, request: dict[str, Any]) -> dict[str, Any]:
        """转发到 provider 处理器;embedding/audio 不支持(规格 §4.9)。"""

        if operation != "response":
            raise NotImplementedError(f"catsitate_custom 不支持操作: {operation}")
        provider = request.get("api_provider") or {}
        base_url = str(provider.get("base_url") or "").rstrip("/")
        if not base_url:
            raise ValueError("catsitate_custom 缺少 api_provider.base_url(请在 model_config 配置)")
        payload: dict[str, Any] = {
            "model": request.get("model") or "",
            "messages": request.get("messages") or [],
        }
        if request.get("temperature") is not None:
            payload["temperature"] = request["temperature"]
        if request.get("max_tokens") is not None:
            payload["max_tokens"] = request["max_tokens"]
        headers = {"Content-Type": "application/json"}
        if provider.get("api_key"):
            headers["Authorization"] = f"Bearer {provider['api_key']}"
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(f"{base_url}/chat/completions", json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()
        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        return {
            "content": message.get("content") or "",
            "reasoning_content": message.get("reasoning_content") or "",
            "tool_calls": message.get("tool_calls"),
            "usage": data.get("usage"),
        }

    # ---------- 工具 ----------

    @Tool(
        "memo_write",
        description="为当前用户或聊天流记一条短时备忘。内容需简明(≤80 字符);ttl_hours 为单条有效期(小时,≤168),缺省用默认 24 小时;按内容需要可延长(如『周四交作业』可设到周四)。",
        brief_description="记短时备忘",
        detailed_description="写入后会在后续对话中注入(当前流+当前说话人两个维度,合计上限见配置),过期自动清理。",
        parameters=[
            ToolParameterInfo(name="content", param_type="string", description="备忘内容,≤80 字符", required=True),
            ToolParameterInfo(name="stream_id", param_type="string", description="关联聊天流,默认当前流", required=False),
            ToolParameterInfo(name="user_id", param_type="string", description="关联用户,默认当前说话人", required=False),
            ToolParameterInfo(name="ttl_hours", param_type="number", description="单条有效期小时数,缺省用默认", required=False),
        ],
        visibility="visible",
    )
    async def memo_write(self, content: str = "", stream_id: str = "", user_id: str = "", ttl_hours: float | None = None, **kwargs: Any) -> str:
        if not self.config.plugin.enabled or not self.config.memo.tool_enabled:
            return "备忘录工具未启用。"
        ok, msg = self.memo.write(content, stream_id or str(kwargs.get("stream_id") or ""), user_id or str(kwargs.get("user_id") or ""), ttl_hours)
        return msg if ok else f"备忘写入失败:{msg}"

    @Tool(
        "memo_read",
        description="读取当前流与当前说话人相关的未过期备忘,含各自剩余有效时间。",
        brief_description="读短时备忘",
        parameters=[],
        visibility="visible",
    )
    async def memo_read(self, **kwargs: Any) -> str:
        if not self.config.plugin.enabled or not self.config.memo.tool_enabled:
            return "备忘录工具未启用。"
        entries = self.memo.read(str(kwargs.get("stream_id") or ""), str(kwargs.get("user_id") or ""), limit=self.config.memo.inject_max)
        if not entries:
            return "当前没有未过期的备忘。"
        lines = [f"- {e['content']}(剩余 {e['remaining_hours']:.1f} 小时)" for e in entries]
        return "\n".join(lines)

    @Tool(
        "msg_react",
        description="给目标消息贴一个表情回应(从配置白名单中选择最合适的)。",
        brief_description="贴表情",
        detailed_description="仅表情白名单内的 emoji_id 可贴;同一聊天流有冷却(工程护栏)。",
        parameters=[
            ToolParameterInfo(name="message_id", param_type="string", description="目标消息 ID", required=True),
            ToolParameterInfo(name="intent", param_type="string", description="贴表情意图(可选文字)", required=False),
        ],
        visibility="visible",
    )
    async def msg_react(self, message_id: str = "", intent: str = "", **kwargs: Any) -> str:
        if not self.config.plugin.enabled or not self.config.msg_react.enabled:
            return "贴表情工具未启用。"
        stream_id = str(kwargs.get("stream_id") or "")
        whitelist = self.config.msg_react.emoji_whitelist
        if not whitelist:
            return "表情白名单为空,请先在插件配置中填写 emoji_whitelist。"
        ok, reason = self.react.check_cooldown(stream_id)
        if not ok:
            return reason
        target_text = await self._fetch_message_text(stream_id, message_id)
        messages, _ = self.react.build_choose_prompt(whitelist, target_text or f"消息 {message_id}", intent)
        result = await self._side_llm_call(messages, self.config.msg_react.llm.model, "msg_react")
        if not result.get("success"):
            return f"选表情 LLM 调用失败:{result.get('response', '')[:200]}"
        emoji, err = parse_choice_resp(str(result.get("response") or ""), whitelist)
        if emoji is None:
            return f"选表情失败:{err}"
        api_result = await self.ctx.api.call("adapter.napcat.message.set_msg_emoji_like", message_id=message_id, emoji_id=emoji)
        if not api_result.get("success"):
            return f"贴表情 API 失败:{api_result}"
        self.react.mark_used(stream_id)
        return f"已贴表情 {emoji}"

    @Tool(
        "poke_user",
        description="主动戳一戳目标用户(需好感度达到门槛且冷却通过)。",
        brief_description="主动戳一戳",
        parameters=[
            ToolParameterInfo(name="user_id", param_type="string", description="目标用户 ID", required=True),
            ToolParameterInfo(name="stream_id", param_type="string", description="目标聊天流", required=True),
        ],
        visibility="visible",
    )
    async def poke_user(self, user_id: str = "", stream_id: str = "", **kwargs: Any) -> str:
        del kwargs
        if not self.config.plugin.enabled or not self.config.poke.poke_tool_enabled:
            return "主动戳工具未启用。"
        best = self.fav_engine.get_best_level_for_user(user_id)
        ok, reason = self.poke.can_poke(user_id, best)
        if not ok:
            return reason
        api_result = await self.ctx.api.call("adapter.napcat.message.send_poke", user_id=user_id, stream_id=stream_id)
        if not api_result.get("success"):
            return f"戳一戳 API 失败:{api_result}"
        self.poke.mark_poked(user_id)
        return "已戳。"

    @Tool(
        "inspect_image",
        description="重看聊天里的某张图片,针对图片回答具体问题(如『图片里写了什么』)。",
        brief_description="图片重看",
        detailed_description="目标消息太旧、get_recent 取不到时返回错误并记录日志。",
        parameters=[
            ToolParameterInfo(name="message_id", param_type="string", description="目标消息 ID(可选,缺省按 image_index 取)", required=False),
            ToolParameterInfo(name="image_index", param_type="integer", description="倒数第几张含图消息(默认 1)", required=False),
            ToolParameterInfo(name="question", param_type="string", description="针对图片的具体问题", required=True),
        ],
        visibility="visible",
    )
    async def inspect_image(self, message_id: str = "", image_index: int = 1, question: str = "", **kwargs: Any) -> str:
        if not self.config.plugin.enabled or not self.config.image_relook.enabled:
            return "图片重看工具未启用。"
        stream_id = str(kwargs.get("stream_id") or "")
        recent = await self._fetch_recent_with_binary(stream_id, limit=50)
        seg, err = find_image_segment(recent, message_id or None, image_index)
        if seg is None:
            logger.warning("inspect_image 失败:%s(stream=%s,message_id=%s)", err, stream_id, message_id)
            return f"取图失败:{err}"
        if not seg.get("data"):
            # 仅 hash 时经主程序图片库补读(规格 §4.8)
            db_result = await self.ctx.database.get("Images", hash=seg.get("hash"))
            if not db_result or not db_result.get("data"):
                msg = f"图片 {seg.get('file_name') or seg.get('hash')} 无二进制且数据库补读失败"
                logger.error(msg)
                return msg
            seg = {**seg, "data": db_result["data"]}
        messages, _ = build_relook_prompt(question, seg)
        result = await self._side_llm_call(messages, self.config.image_relook.llm.model, "image_relook")
        if not result.get("success"):
            return f"图片重看 LLM 调用失败:{result.get('response', '')[:200]}"
        return str(result.get("response") or "")

    # ---------- 命令 ----------

    @Command("/记一下", description="记一条短时备忘", pattern=r"^/记一下\s+(?P<content>.+)$", aliases=["/备忘"])
    async def cmd_memo(self, content: str = "", stream_id: str = "", user_id: str = "", **kwargs: Any) -> str:
        del kwargs
        if not self.config.plugin.enabled or not self.config.memo.command_enabled:
            return "备忘命令未启用。"
        if len(content.strip()) > self.config.memo.entry_max_chars:
            return f"备忘太长啦(>{self.config.memo.entry_max_chars} 字符),请精简后再发～"
        ok, msg = self.memo.write(content, stream_id, user_id, None)
        return msg if ok else f"备忘写入失败:{msg}"

    # ---------- Hook:主链路注入 ----------

    @HookHandler("maisaka.planner.before_request", name="catsitate_inject", mode=HookMode.BLOCKING, order=HookOrder.LATE)
    async def inject_blocks(self, **kwargs: Any) -> dict[str, Any]:
        """注入块前插 system 之后、历史之前(规格 §4.1);失败仅记录日志不阻塞。"""

        if not self.config.plugin.enabled:
            return {"action": "continue", "modified_kwargs": kwargs}
        try:
            blocks = self._build_inject_blocks(kwargs)
            messages = self._messages_from_kwargs(kwargs)
            if messages is None:
                return {"action": "continue", "modified_kwargs": kwargs}
            rendered = self.assembler.render(blocks)
            if not rendered:
                return {"action": "continue", "modified_kwargs": kwargs}
            insert_at = self._system_tail_index(messages)
            new_messages = messages[:insert_at] + rendered + messages[insert_at:]
            new_kwargs = {**kwargs, self._MESSAGES_KEY: new_messages}
            return {"action": "continue", "modified_kwargs": new_kwargs}
        except Exception:
            logger.exception("注入块构造失败,本轮跳过注入")
            return {"action": "continue", "modified_kwargs": kwargs}

    # ---------- Hook:入站(戳一戳解析 + 好感度计数) ----------

    @HookHandler("chat.receive.before_process", name="catsitate_poke_notice", mode=HookMode.OBSERVE)
    async def poke_notice(self, **kwargs: Any) -> None:
        if not self.config.plugin.enabled or not self.config.poke.enabled:
            return
        payload = self._notice_payload(kwargs)
        if payload is None:
            return
        text = self.poke.enhance_notice_text(payload)
        if text is None:
            return
        if self.config.poke.inject_to_context:
            stream_id = str(kwargs.get("stream_id") or "")
            try:
                await self.ctx.maisaka.context.append(stream_id=stream_id, text=text)
            except Exception:
                logger.exception("戳一戳上下文注入失败(stream=%s)", stream_id)
        # enhance_notice_text 能否改写消息文本以 spike ③ 结论为准;不能则仅日志
        logger.info("戳一戳解析增强:%s", text)

    @HookHandler("chat.receive.after_process", name="catsitate_fav_count", mode=HookMode.OBSERVE)
    async def fav_count(self, **kwargs: Any) -> None:
        if not self.config.plugin.enabled or not self.config.favorability.enabled:
            return
        user_id = str(kwargs.get("user_id") or "")
        stream_id = str(kwargs.get("stream_id") or "")
        if not user_id or not stream_id:
            return
        self.fav_engine.count_message(user_id, stream_id)
        trigger = self.fav_engine.check_trigger(user_id, stream_id)
        if trigger == "early":
            asyncio.create_task(self._settle_and_log(user_id, stream_id, kind="early"))

    # ---------- Hook:reply 补传与哨兵 ----------

    @HookHandler("maisaka.planner.after_response", name="catsitate_reply_backfill", mode=HookMode.BLOCKING, order=HookOrder.LATE)
    async def reply_backfill(self, **kwargs: Any) -> dict[str, Any]:
        """规则层补传(规格 §4.7):三条件触发,零成本,不改动其它工具调用。"""

        cfg = self.config.reply_guard
        if not self.config.plugin.enabled or not cfg.enabled or not cfg.context_backfill_enabled:
            return {"action": "continue", "modified_kwargs": kwargs}
        output_items = self._output_items(kwargs)
        if not output_items:
            return {"action": "continue", "modified_kwargs": kwargs}
        called_tools = self._called_tools(kwargs)
        reasoning = str(kwargs.get("reasoning") or "")
        tool_results = self._context_tool_results(kwargs, cfg.context_tools)
        if not tool_results:
            return {"action": "continue", "modified_kwargs": kwargs}
        new_items = backfill_reply_items(output_items, tool_results, cfg.context_tools, called_tools, reasoning)
        if new_items is output_items:
            return {"action": "continue", "modified_kwargs": kwargs}
        new_kwargs = {**kwargs, self._OUTPUT_ITEMS_KEY: new_items}
        logger.info("reply 补传:%s", [t.get("tool_name") for t in new_items if t.get("tool_name") == "reply"])
        return {"action": "continue", "modified_kwargs": new_kwargs}

    @HookHandler("maisaka.replyer.after_response", name="catsitate_sentinel", mode=HookMode.BLOCKING, order=HookOrder.LATE)
    async def sentinel_check(self, **kwargs: Any) -> dict[str, Any]:
        """LLM 哨兵层(默认关,规格 §4.7);撤回能力以 spike ④ 结论为准,不能则仅日志。"""

        cfg = self.config.reply_guard
        if not self.config.plugin.enabled or not cfg.enabled or not cfg.sentinel_enabled:
            return {"action": "continue", "modified_kwargs": kwargs}
        reply_text = str(kwargs.get("reply_text") or kwargs.get("text") or "")
        if not reply_text.strip():
            return {"action": "continue", "modified_kwargs": kwargs}
        persona = self._persona_background()
        chat_context = await self._recent_context_text(str(kwargs.get("stream_id") or ""), limit=10)
        messages, _ = build_sentinel_prompt(persona, reply_text, chat_context)
        result = await self._side_llm_call(messages, cfg.sentinel_llm.model, "sentinel")
        if not result.get("success"):
            logger.warning("哨兵层 LLM 调用失败,放行回复:%s", result.get("response", "")[:200])
            return {"action": "continue", "modified_kwargs": kwargs}
        should_send, reason = parse_sentinel_response(str(result.get("response") or ""))
        if should_send is None or should_send:
            return {"action": "continue", "modified_kwargs": kwargs}
        logger.warning("哨兵层判定撤回回复:%s", reason)
        # 撤回动作(spike ④ 验证后实现:删除待发送项或调用撤回 API);当前先日志
        return {"action": "continue", "modified_kwargs": kwargs}

    # ---------- 内部辅助 ----------

    # spike ②/③/④ 结论的字段名集中于此,不符仅改此处
    _MESSAGES_KEY = "messages"
    _OUTPUT_ITEMS_KEY = "output_items"

    def _messages_from_kwargs(self, kwargs: dict[str, Any]) -> list[dict] | None:
        return kwargs.get(self._MESSAGES_KEY)

    def _system_tail_index(self, messages: list[dict]) -> int:
        """注入点 = system 消息之后(spike ② 确认的插入语义)。"""

        for i, m in enumerate(messages):
            if m.get("role") == "system":
                return i + 1
        return len(messages)  # 无 system 时追加尾部(spike ② 回退语义)

    def _build_inject_blocks(self, kwargs: dict[str, Any]) -> list[InjectionBlock]:
        cfg = self.config
        speaker = str(kwargs.get("user_id") or "")
        stream_id = str(kwargs.get("stream_id") or "")
        blocks: list[InjectionBlock] = []
        if cfg.inject.level_rule_enabled:
            rules = "\n".join(f"{i + 1}. {line}" for i, line in enumerate(cfg.favorability.level_rules.splitlines()))
            blocks.append(InjectionBlock("level_rule", "rules", f"[好感度规则] {rules}"))
        if cfg.inject.environment_enabled and cfg.time_aware.enabled:
            env = self._environment_block(stream_id)
            if env:
                blocks.append(InjectionBlock("environment", env[0], env[1]))
        if cfg.inject.memo_enabled and cfg.memo.enabled:
            entries = self.memo.read(stream_id, speaker, limit=cfg.memo.inject_max)
            if entries:
                text = "[备忘] " + ";".join(e["content"] for e in entries)
                key = "|".join(sorted(f"{e['id']}" for e in entries))
                blocks.append(InjectionBlock("memo", f"memo:{key}", text))
        if cfg.inject.favorability_enabled and cfg.favorability.enabled:
            target = speaker or str(kwargs.get("peer_id") or "")
            if target:
                blocks.append(InjectionBlock("favorability", f"fav:{target}", build_favorability_block(self.fav_engine, target, stream_id)))
        return blocks

    def _environment_block(self, stream_id: str) -> tuple[str, str] | None:
        """环境块:节日+天气;缓存 45 分钟(规格 §4.2)。"""

        del stream_id
        cfg = self.config.time_aware
        if self._env_fetched_at and (datetime.now() - self._env_fetched_at).total_seconds() < cfg.weather_refresh_minutes * 60:
            cached = self._env_cache.get("env")
            return ("env", cached) if cached else None
        return None  # 数据未就绪时跳过(首次由后台任务填充后自动出现)

    async def _refresh_environment(self) -> None:
        """后台任务:拉取节日(在线→库→内置)与天气(Open-Meteo),刷新环境块缓存。"""

        if not self.config.plugin.enabled or not self.config.time_aware.enabled:
            return
        cfg = self.config.time_aware
        today = date.today()
        online = None
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get("https://cdn.jsdelivr.net/npm/holiday-cn@latest/{today.year}.json".format(today=today))
                resp.raise_for_status()
                online = parse_holiday_cn(resp.json())
        except Exception:
            logger.warning("holiday-cn 在线数据获取失败,回退内置节日表", exc_info=True)
        try:
            from holiday_calendar import get_holidays  # manifest 声明依赖(自动安装)

            lib_data = get_holidays(today.year) if online is None else None
        except Exception:
            lib_data = None
        holidays = holiday_chain(today, {**online, **lib_data} if online and lib_data else (online or lib_data), builtin_ok=True)
        weather = None
        try:
            lat, lon = cfg.city_lat, cfg.city_lon
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(
                    "https://api.open-meteo.com/v1/forecast",
                    params={"latitude": lat, "longitude": lon, "current": "temperature_2m,weather_code", "daily": "temperature_2m_max,temperature_2m_min,weather_code", "forecast_days": 2},
                )
                resp.raise_for_status()
                data = resp.json()
                weather = {"temperature_2m": data["current"]["temperature_2m"], "weather_code": data["current"]["weather_code"]}
        except Exception:
            logger.warning("天气获取失败,本轮环境块省略天气", exc_info=True)
        # 天气快照落库供二期 2.1 联动(规格 §4.2)
        if weather is not None:
            self.store.execute(
                """
                INSERT INTO weather_snapshot (id, city, fetched_at, data) VALUES (1, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET city = excluded.city, fetched_at = excluded.fetched_at, data = excluded.data
                """,
                (cfg.city, datetime.now().strftime("%Y-%m-%dT%H:%M:%S"), json.dumps(weather, ensure_ascii=False)),
            )
        # 当天 + 临近 3 天节日/节气(规格 §4.2 注入示例)
        near_holidays: list[str] = []
        for offset in range(4):
            day = today + timedelta(days=offset)
            near_holidays.extend(holidays.get(day.strftime("%m-%d"), []))
        text = build_environment_text(today, cfg.city, weather, near_holidays, [])
        self._env_cache["env"] = text
        self._env_fetched_at = datetime.now()

    async def _cleanup_memos(self) -> None:
        removed = self.memo.cleanup()
        if removed:
            logger.info("备忘清理:%s 条过期", removed)

    async def _daily_settle(self) -> None:
        """日终兜底:对当日有消息且未日终结算的用户结算当前批次(不计提前上限,规格 §4.3)。"""

        if not self.config.plugin.enabled or not self.config.favorability.enabled:
            return
        for user_id, stream_id in self.fav_engine.iter_today_active():
            if self.fav_engine.has_daily_settle_today(user_id, stream_id):
                continue
            await self._settle_and_log(user_id, stream_id, kind="daily")

    async def _settle_and_log(self, user_id: str, stream_id: str, kind: str) -> None:
        history = await self._fetch_recent_for_history(stream_id, limit=200)
        result = await self.fav_executor.settle(user_id, stream_id, history, kind=kind)
        if result["status"] == "ok":
            logger.info("好感度结算[%s] %s/%s:delta=%s note=%s", kind, user_id, stream_id, result["delta"], result["note"])
        elif result["status"] == "carried_over":
            logger.info("好感度日终顺延 %s/%s:%s", user_id, stream_id, result["reason"])
        else:
            logger.error("好感度结算失败[%s] %s/%s:%s", kind, user_id, stream_id, result.get("error"))

    async def _side_llm_call(self, messages: list[dict], model: str, module: str) -> dict:
        """旁路 LLM 统一出口(规格 §4.10):留空 model 用主程序默认模型;用量按模块记账。"""

        result = await self.ctx.llm.generate(messages, model=model or "")
        self._record_llm_usage(module, result)
        return result

    def _record_llm_usage(self, module: str, result: dict) -> None:
        """旁路调用记账(规格 §4.10 可观测性):次数+token 按日/模块分列;超阈值告警。"""

        day = datetime.now().strftime("%Y-%m-%d")
        tokens = 0
        usage = result.get("usage")
        if isinstance(usage, dict):
            tokens = int(usage.get("total_tokens") or 0)
        self.store.execute(
            """
            INSERT INTO llm_usage (day, module, calls, tokens) VALUES (?, ?, 1, ?)
            ON CONFLICT(day, module) DO UPDATE SET calls = calls + 1, tokens = tokens + excluded.tokens
            """,
            (day, module, tokens),
        )
        rows = self.store.query(
            "SELECT SUM(calls) FROM llm_usage WHERE day = ?", (day,)
        )
        total = int(rows[0][0] or 0)
        if total == self.config.plugin.llm_daily_call_warning_threshold:
            logger.warning("旁路 LLM 当日调用次数已达阈值 %s,请注意用量", total)

    async def _fetch_recent_with_binary(self, stream_id: str, limit: int) -> list[dict]:
        """取近期消息(含图片二进制),SDK 无参透传缺口经 call_capability 绕过(spike ②)。"""

        result = await self.ctx.call_capability("message.get_recent", chat_id=stream_id, limit=limit, include_binary_data=True)
        return result.get("messages") or []

    async def _fetch_recent_for_history(self, stream_id: str, limit: int) -> list[dict]:
        """取近期消息并归一化为 build_material 所需形状 {role, user_id, stream_id, text, seq}。"""

        raw = await self._fetch_recent_with_binary(stream_id, limit)
        history: list[dict] = []
        for i, m in enumerate(raw):
            text = "".join(s.get("text", "") for s in (m.get("segments") or []) if s.get("type") == "text")
            history.append({"role": "user" if str(m.get("user_id") or "") != str(m.get("bot_id") or "") else "bot", "user_id": str(m.get("user_id") or ""), "stream_id": stream_id, "text": text, "seq": i})
        return history

    async def _fetch_message_text(self, stream_id: str, message_id: str) -> str:
        raw = await self._fetch_recent_with_binary(stream_id, 50)
        for m in raw:
            if m.get("message_id") == message_id:
                return "".join(s.get("text", "") for s in (m.get("segments") or []) if s.get("type") == "text")
        return ""

    def _notice_payload(self, kwargs: dict[str, Any]) -> dict | None:
        additional = kwargs.get("additional_config") or {}
        payload = additional.get("napcat_notice_payload")
        return payload if isinstance(payload, dict) else None

    def _output_items(self, kwargs: dict[str, Any]) -> list[dict]:
        items = kwargs.get(self._OUTPUT_ITEMS_KEY)
        return items if isinstance(items, list) else []

    def _called_tools(self, kwargs: dict[str, Any]) -> list[str]:
        """本轮 planner 调用过的工具名(spike 结论;可能来自 tool_calls 回显)。"""

        calls = kwargs.get("tool_calls") or []
        return [c.get("name") or c.get("tool_name") for c in calls if isinstance(c, dict)]

    def _context_tool_results(self, kwargs: dict[str, Any], context_tools: list[str]) -> dict[str, str]:
        """本轮上下文工具的结果(spike 结论;可能来自 tool_results 回显)。"""

        results = kwargs.get("tool_results") or {}
        return {name: str(results[name]) for name in context_tools if name in results}

    def _persona_background(self) -> str:
        return str(self.ctx.maisaka.get_personality() or "猫耳少女") if hasattr(self.ctx, "maisaka") else "猫耳少女"

    async def _recent_context_text(self, stream_id: str, limit: int) -> str:
        raw = await self._fetch_recent_with_binary(stream_id, limit)
        lines = []
        for m in raw:
            text = "".join(s.get("text", "") for s in (m.get("segments") or []) if s.get("type") == "text")
            lines.append(f"[{m.get('user_id')}] {text}")
        return "\n".join(lines)


def create_plugin() -> CatsitatePlugin:
    """插件工厂(入口约定)。"""

    return CatsitatePlugin()
```

- [ ] **Step 5: 运行测试确认通过**

Run: `python3 -m pytest tests/ -v`
Expected: 全部通过(新增 scheduler 4 个 + settlement 追加 1 个)

- [ ] **Step 6: 提交**

```bash
git add catsitate_core/services plugin.py catsitate_core/favorability.py tests/test_scheduler.py tests/test_settlement.py
git commit -m "feat: plugin.py 全组件接线(工具/命令/hook/LLMProvider)与 60s 调度器(日终结算/环境刷新/备忘清理)"
```

**注意**:`_refresh_environment` 中 holiday_calendar 库的调用方式与 `napcat_notice_payload`/`output_items`/`tool_calls` 的实际字段名以 Task 2 spike 结论为准;spike 文档(2026-08-14-spike-findings.md)是本任务的必读输入,不符处仅改 plugin.py 对应 handler 与 `_MESSAGES_KEY`/`_OUTPUT_ITEMS_KEY` 常量区。

---

### Task 15: 集成测试 + 缓存基线报告 + README 补完

**Files:**
- Create: `tests/test_integration.py`(离线装配冒烟:全部引擎串行一次)
- Create: `docs/cache-baseline.md`(缓存基线报告,含测量流程与记录表)
- Modify: `README.md`(补完启用/测试/验收/观测章节)
- Modify: `CHANGELOG.md`(v1.0.0 条目)
- Create: `docs/acceptance-checklist.md`(规格 §5 手动验收清单,实机逐项勾选)

**Interfaces:**
- Consumes: Task 3–14 全部模块与 Task 1 配置。
- Produces: 可交付的一期插件(纯逻辑离线冒烟 + 实机验收清单 + 缓存基线报告模板)。

- [ ] **Step 1: 编写集成冒烟测试**

`tests/test_integration.py`:

```python
"""离线集成冒烟:把全部引擎按 plugin.py 装配方式串起来跑一遍(不依赖 MaiBot)。"""

from __future__ import annotations

import asyncio
from datetime import date, datetime

from catsitate_core.config import CatsitateConfig
from catsitate_core.favorability import BatchEngine, SettleExecutor, build_favorability_block
from catsitate_core.inject import InjectAssembler, InjectionBlock
from catsitate_core.memo import MemoService
from catsitate_core.msg_react import MsgReactEngine, parse_choice_resp
from catsitate_core.poke import PokeEngine
from catsitate_core.reply_guard import backfill_reply_items
from catsitate_core.storage import SQLiteStore
from catsitate_core.time_aware import build_environment_text

NOW = datetime(2026, 8, 14, 12, 0, 0)


def _fake_llm(messages, model=""):
    async def call(messages, model=""):
        system_text = str(messages[0]["content"])
        if "关系评估助手" in system_text:
            return {"success": True, "response": '{"delta": 2, "note": "冒烟注记"}', "model": model}
        return {"success": True, "response": '{"emoji": "em_laugh"}', "model": model}
    return call(messages, model)


def test_full_assembly_smoke(tmp_path):
    """按 plugin.py 的装配顺序:建库→建引擎→注入→结算→贴表情→戳校验→补传。"""

    store = SQLiteStore(tmp_path / "smoke.db")
    cfg = CatsitateConfig()
    memo = MemoService(store, cfg.memo)
    react = MsgReactEngine(store, cfg.msg_react)
    poke = PokeEngine(store, cfg.poke)
    engine = BatchEngine(store, cfg.favorability)
    for service in (memo, react, poke, engine):
        service.ensure_schema()
    assembler = InjectAssembler()

    # 备忘
    assert memo.write("周四交作业", "s1", "u1", None)[0] is True
    entries = memo.read("s1", "u1", limit=5)
    assert entries and entries[0]["content"] == "周四交作业"

    # 环境块
    env_text = build_environment_text(date(2026, 8, 14), "北京", {"temperature_2m": 29.0, "weather_code": 0}, ["七夕"], [])
    assert "[环境]" in env_text and "北京" in env_text

    # 注入渲染(四块顺序)
    blocks = [
        InjectionBlock("level_rule", "rules", "[好感度规则] 规则文本"),
        InjectionBlock("environment", "env", env_text),
        InjectionBlock("memo", "memo:1", "[备忘] 周四交作业"),
        InjectionBlock("favorability", "fav:u1", build_favorability_block(engine, "u1", "s1")),
    ]
    rendered = assembler.render(blocks)
    assert [m["role"] for m in rendered] == ["user"] * 4
    assert rendered[0]["content"].startswith("[好感度规则]")
    assert rendered[1]["content"].startswith("[环境]")

    # 好感度:计数→提前触发→结算(fake LLM)
    for _ in range(20):
        engine.count_message("u1", "s1", now=lambda: NOW)
    assert engine.check_trigger("u1", "s1", now=lambda: NOW) == "early"
    executor = SettleExecutor(engine, _fake_llm)
    history = [
        {"role": "user", "user_id": "u1", "stream_id": "s1", "text": f"消息{i}", "seq": i}
        for i in range(20)
    ]
    result = asyncio.run(executor.settle("u1", "s1", history, kind="early"))
    assert result["status"] == "ok" and result["delta"] == 2
    assert "累计 2" in build_favorability_block(engine, "u1", "s1")  # 2 分仍是「陌生」级

    # 贴表情
    messages, _ = react.build_choose_prompt(["em_laugh", "em_hug"], "今天好累", "安慰")
    assert messages[0]["role"] == "system"
    choice, err = parse_choice_resp('{"emoji": "em_laugh"}', ["em_laugh", "em_hug"])
    assert choice == "em_laugh" and err == ""

    # 主动戳校验(结算后 2 分仍是"陌生",门槛"熟悉"应拒绝)
    ok, reason = poke.can_poke("u1", engine.get_best_level_for_user("u1"), now=lambda: NOW)
    assert ok is False and "熟悉" in reason

    # reply 补传
    items = [{"tool_name": "reply", "arguments": {"reply_reference": ""}}]
    out = backfill_reply_items(items, {"memo_read": "备忘内容"}, cfg.reply_guard.context_tools, ["memo_read"], "")
    assert out[0]["arguments"]["reply_reference"] == "备忘内容"

    # 日终查询
    active = engine.iter_today_active(now=lambda: NOW)
    assert ("u1", "s1") in active
```

- [ ] **Step 2: 运行测试确认通过**

Run: `python3 -m pytest tests/ -v`
Expected: 全部通过(含本任务 1 个集成测试)

- [ ] **Step 3: 写缓存基线报告模板**

`docs/cache-baseline.md`:

````markdown
# 缓存基线报告

> 目标:一期交付时给出主链路缓存命中率 ≥80% 的基线对比与验证记录(规格 §4.1/§4.10)。

## 测量方法

1. 对照组(插件关闭):运行 ≥1 小时正常聊天(或固定脚本回放 50 轮),记录主程序日志 `Planner缓存:...hit_rate=xx%` 与 `llm_cache_stats` 诊断。
2. 实验组(插件开启,仅注入模块):同量级流量,记录同样指标。
3. 逐模块开启(注入 → 好感度结算 → 备忘录 → 其余),每步重复测量,定位波动源。

## 记录表

| 组别 | 流量规模 | hit_rate | 备注 |
|---|---|---|---|
| 基线(插件关) |  |  |  |
| 仅注入 |  |  | 预期与基线持平或略升 |
| 注入+好感度+备忘 |  |  |  |
| 全部模块 |  |  |  |

## 结论

- [ ] hit_rate ≥ 80%(或给出与基线的差值说明)
- [ ] 注入块位于 system 之后、历史之前(日志确认)
- [ ] 旁路请求(好感度结算等)模板前缀跨调用稳定(连续两次结算的 prompt 首段一致)
````

- [ ] **Step 4: 写验收清单**

`docs/acceptance-checklist.md`:

```markdown
# 手动验收清单(规格 §5)

前置:插件已放入 plugins/ 并启用;WebUI 插件页打开 `plugin.enabled = true`;重启 MaiBot 后日志出现 `catsitate_core 已加载`。

- [ ] 命令:`/记一下 周四交作业` → bot 回复已记下;`/记一下` 超 80 字符 → 提示精简
- [ ] 工具(planner 自主):聊天中让 bot「帮我记一下…」/「给上一条消息贴个表情」/「戳一下他」/「看看刚才那张图里写了什么」,日志出现对应工具调用与成功/失败原因
- [ ] 注入:日志或调试输出中 `[好感度规则]`/`[环境]`/`[备忘]`/`[好感度]` 片段出现,且位于 system 之后、历史之前
- [ ] 好感度:同一用户连续发言至 early_settle_threshold,日志出现 `好感度结算[early]`;连续两天确认 `daily` 结算日志;好感度块等级/注记更新
- [ ] 戳一戳:在 QQ 上拍一拍 bot,日志出现 `戳一戳解析增强:…`;上下文中出现拟人文本(开关 inject_to_context)
- [ ] 贴表情防刷:同流 30 秒内二次调用 → 冷却提示
- [ ] 哨兵层(默认关):开启后日志出现哨兵判定调用
- [ ] 旁路记账:`data/plugins/catsitate.core/catsitate.db` 中 `llm_usage` 表按模块分列调用数
```

- [ ] **Step 5: 补完 README.md**

用以下全文**覆盖** Task 1 的 README 骨架(去掉骨架中的「启用方式」「测试」两节旧文本,以下版本为准;全文以简体中文):

```markdown
## 启用

1. 把本目录放进 MaiBot 的 `plugins/` 并重启;WebUI「插件」页确认 `catsitate.core` 已加载
2. 在插件配置页打开 `plugin.enabled = true`(总开关,默认关),按需调整各模块节
3. 想用自定义 LLM 端点:在 MaiBot `model_config.toml` 的 `api_providers` 增加一条 `client_type = "catsitate_custom"`(base_url/key),再把对应能力的 `model` 填 `catsitate_custom`
4. 表情白名单:`msg_react.emoji_whitelist` 填入 napcat 表情 id(留空则贴表情工具拒绝执行)

## 测试

- 单元测试:`cd plugins/catsitate_core_maibot && python3 -m pytest tests/ -v`(不依赖 MaiBot)
- 集成冒烟:同目录 `python3 -m pytest tests/test_integration.py -v`
- 实机验收:按 `docs/acceptance-checklist.md` 逐项勾选

## 缓存与用量观测

- 主链路命中率:对照 `docs/cache-baseline.md` 流程,看主程序日志 `Planner缓存:...hit_rate=xx%`
- 旁路 LLM 记账:`llm_usage` 表(day/module/calls/tokens)按模块分列;每日旁路调用合计超过
  `plugin.llm_daily_call_warning_threshold` 时记录告警日志

## 配置要点

- 每个 LLM 能力的 `model` 留空 = 主程序默认模型;填 task 名 = 该任务模型;填 `catsitate_custom` = 自定义端点
- 注入四块(`level_rule`/`environment`/`memo`/`favorability`)各自有开关,可独立关闭
- 哨兵层默认关(`reply_guard.sentinel_enabled`),开启后每句回复多一次旁路判定
```

- [ ] **Step 6: 更新 CHANGELOG.md**

```markdown
# Changelog

## v1.0.0(2026-08-14)

主要功能:
- 注入框架:环境/备忘/好感度块前插 system 之后(缓存友好分层注入)
- 好感度:批次结算制(提前结算 + 日终兜底顺延)、5 级规则注入、LLM 判定落库
- 备忘录:双通道(工具+命令)、单条 TTL、注入当前流+说话人两维度
- 贴表情:白名单 LLM 选表情 + 每流冷却
- 戳一戳:入站解析增强 + 主动戳工具(好感度门槛)
- reply 上下文补传(规则层)+ LLM 哨兵层(可选)
- 图片重看工具(VLM)
- 时间感知:节日/节气/天气环境块
- LLM:catsitate_custom 自定义端点 + 旁路 prompt 稳定前缀组装

细节:
- 旁路 LLM 调用记账(llm_usage)+ 每日告警阈值
- 60s 后台调度器(天气/节日/备忘清理/日终结算)
```

- [ ] **Step 7: 全量测试与提交**

Run: `python3 -m pytest tests/ -v`(预期全部通过)

```bash
git add tests/test_integration.py docs/cache-baseline.md docs/acceptance-checklist.md README.md CHANGELOG.md
git commit -m "docs: 集成冒烟测试/缓存基线报告/验收清单与 README 补完"
```

- [ ] **Step 8: 收尾检查**

```bash
git -C plugins/catsitate_core_maibot log --oneline | head -20   # 确认 15 个任务全部提交
git -C plugins/catsitate_core_maibot status --short              # 工作区干净(config.toml 已被 .gitignore 忽略)
```

