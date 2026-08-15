# Spike 核查结论 — 2026-08-15(实机验证)

> **状态**:①-③ 已于 2026-08-15 实机验证完成(MaiBot docker 实例 maim-bot-core + maim-bot-napcat);
> ④-⑤ 待运行时触发验证(spike_probe 工具已就绪)。验证过程发现并修复了 3 个计划缺陷
> (见「过程发现」)。配套 spike 插件为独立目录 `catsitate_spike/`(验证后删除)。

## 部署方法(实测修正)

1. spike 是**独立插件目录**(含 `plugin.py` 入口 + `_manifest.json` + `_spike.py`),不能只把 `_spike.py` 放根目录——加载器按目录扫描,要求每个插件目录有 `plugin.py`(实测 `_spike.py` 单独存在时插件完全不被发现)。
2. `docker cp <src> <container>:/MaiMBot/plugins/` 后 `docker restart maim-bot-core` 生效(napcat 容器无需动,QQ 不掉线);重启后日志 `[扩展插件] 已启动…已加载=1(catsitate.spike)`。
3. 加载器要求插件类实现 `on_load`(否则 `TypeError: 插件必须实现 on_load()`,**加载被拒**)。
4. 插件 logger(`logging.getLogger("catsitate.spike")`)的日志**不桥接主进程**;观测信息经「探针文本 → LLM prompt → 主程序日志/LLM reasoning」管道可视化。

## 结论汇总表(已回填 ①-③)

| 序号 | 核查项 | 结论 | 依据(日志行/行为) | 回填日期 |
|---|---|---|---|---|
| 1 | 子包导入 `catsitate_core.*` | **B:不支持**(加载器仅将 `plugins/` 父目录临时加入 sys.path,插件目录本身不在)→ 采用方案 C 解决 | `ModuleNotFoundError: No module named 'catsitate_core'`(加载失败日志);plugin.py 自注册 sys.path 后加载成功 | 2026-08-15 |
| 2 | `maisaka.planner.before_request` items 前插 | **A:生效**(须用合法快照格式 item;朴素 dict 被拒) | 探针 UserMessageItem 出现在 LLM 请求 items 且 LLM reasoning 可见探针文本 | 2026-08-15 |
| 3 | `chat.receive.before_process` 改写能力 | **A:生效**(BLOCKING 返回 modified_kwargs.message,主程序反序列化后下游可见) | `[所见] [Starry Lights]Hesitate_P:[spike改写] @Catsitate-dev 对` | 2026-08-15 |
| 4 | `message.get_recent` 二进制传参 | 待验证 | — | — |
| 5 | `ctx.maisaka.context.append` 准确签名 | 静态确认:`(stream_id, segments: list[dict], *, visible_text="")`;运行时待 ④ 同次验证 | 本机 SDK 源码 + 容器内 SDK 一致 | — |

---

## 1. 子包导入:结论 B → 方案 C(plugin.py 自注册)

**实测**:插件加载器(`plugin_loader.py`)以 `spec_from_file_location` 执行 `plugin.py`,临时把
`src_root` 与 **`plugin_parent_dir`(=plugins 目录,非插件自身目录)** 加入 sys.path 后移出。
因此 `from catsitate_core.config import ...` 抛 `ModuleNotFoundError`,插件加载失败。

**方案 C(采用)**:`plugin.py` 顶部在子包导入前自行注册插件目录:

```python
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
```

已实测生效(catsitate.core 加载成功,总开关关→「未激活」状态正常)。Task 14 的完整 plugin.py 已同步该段。

**过程发现(重要)**:先加载插件对 sys.path 的注册**持久于 runner 进程**,会污染后续插件的模块
解析——spike 的 `from _spike import` 曾命中 catsitate_core_maibot 目录下的同名 `_spike.py` 副本,
导致"已实现 on_load 却报必须实现"的诡异错误。教训:插件内部模块名须足够独特,避免与他插件同名;
spike 入口同样自注册目录后解决。

## 2. items 前插:结论 A(合法快照格式生效)

**实测过程**:朴素 `{"role": "user", "content": ...}` 探针被主程序拒绝:
`Hook maisaka.planner.before_request 返回的 items 无法反序列化，已忽略: 快照中的 item_type 必须是非空字符串`。

改用合法快照格式(`request_snapshot.py` 的 `deserialize_context_item_snapshot` 要求)后生效:

```python
{
    "item_type": "UserMessageItem",
    "meta": {"item_id": "…", "logical_turn_id": None, "timestamp": "…isoformat…"},
    "parts": [{"type": "text", "text": "[spike] 注入探针消息"}],
}
```

**生效证据**:主程序日志文件(`logs/app_*.log.jsonl`)中 LLM 请求快照出现
`parts=(ContextTextPart(text='[spike] 注入探针消息'),)` 且紧邻 SystemMessageItem 之后;
LLM reasoning 内容提及探针文本(LLM「看见了」注入)。定位 system 用 `item_type == "SystemMessageItem"`
(快照无 `role` 字段)。

**Task 14 注入实现要点**:注入块 = 合法快照格式 UserMessageItem;插到 SystemMessageItem 之后;
BLOCKING/LATE 返回 `{"action": "continue", "modified_kwargs": kwargs}`。

## 3. before_process 改写:结论 A(生效)

**实测过程**:`chat.receive.before_process`(BLOCKING)kwargs 仅两键:`['hook_name', 'message']`;
message 键列表:`message_id / timestamp / platform / message_info / raw_message / is_mentioned /
is_at / is_emoji / is_picture / is_command / is_notify / session_id`。

改写 `message.raw_message`(段列表)头部插入 `{"type": "text", "data": "[spike改写]"}` 后返回
modified_kwargs,主程序 `deserialize_session_message` 重建消息并继续入站管线。

**生效证据**:`[所见] [Starry Lights]Hesitate_P:[spike改写] @Catsitate-dev 对`。

**段格式要点**:`raw_message` 为**段列表**;text 段格式 `{"type": "text", "data": "文本"}`
(**data 直接是字符串**,不是 `{"text": ...}`——写错会被解析为空文本组件,反序列化不报错但改写消失);
at 段格式 `{"type": "at", "data": {"target_user_id": ...}}`。

**Task 14 戳一戳要点**:`enhance_notice_text` 可走改写 message.raw_message 路径(结论 A);
`inject_to_context` 用 `ctx.maisaka.context.append(stream_id, segments=[{"type": "text", "text": ...}])`
(注意:maisaka 的 segments 与消息段格式不同,maisaka 用 `{"type": "text", "text": ...}`)。

## 4. message.get_recent 二进制传参(待验证)

**观测步骤**:诱导 planner 调用 `spike_probe` 工具(如发送「调用 spike_probe 工具」),观察
`spike_probe` 返回内容(工具执行结果进入聊天上下文,LLM 会引用):返回含图片二进制即为支持;
仅 hash 则为不支持(图片重看改用 `ctx.database.get(model_name="Images")` 补图路径)。

**结论分支**:

- **结论 A(支持)**:Task 13/14 图片重看直读二进制;
- **结论 B(不支持)**:Task 14 `inspect_image` 仅 hash 时经主程序图片库补读(计划已有路径)。

**实测记录**:待验证。

## 5. ctx.maisaka.context.append 签名(静态已确认)

- 签名:`append(stream_id: str, segments: list[dict], *, visible_text: str = "", source_kind: str = "", message_id: str = "", **kwargs)`。
- 文本消息:`segments=[{"type": "text", "text": "…"}]`(与第 3 项的消息段格式**不同**,勿混用)。
- 运行时行为(返回值/上下文可见性)待 ④ 同次验证。

## 过程发现(计划缺陷修复记录)

1. **`holiday-calendar>=1.0.0` 不可解析**(仓库实际 ≤0.1.3):Host 依赖流水线解析失败会**阻止整个插件加载**(比规格"退化"预估更严重)。已降为 `>=0.1.0`(提交 7a0bf19)。
2. **MaiBotPlugin 契约强制 on_load/on_unload/on_config_update**(缺失即加载被拒,TypeError)。
3. **插件 logger 不桥接主进程日志**:正式插件须确认 `ctx.logger` 或 logger 名桥接规则,否则插件日志不可见。

## 清理清单(全部验证完成后执行)

- [ ] 删除容器内 `/MaiMBot/plugins/catsitate_spike/` 与本地 spike-package
- [ ] 删除插件仓库 `_spike.py`(验证后),提交移除
- [ ] `docker restart maim-bot-core` 恢复干净状态
