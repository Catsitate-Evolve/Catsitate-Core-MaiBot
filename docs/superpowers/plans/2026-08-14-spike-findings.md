# Spike 核查结论 — 2026-08-14(临时,待实机验证)

> **状态声明**:当前环境无运行中的 MaiBot 实例,本文件涉及的 4 项实机核查**均未执行**,
> 全部结论字段一律为「待实机验证」,**不包含任何实测结论**。本文件交付的是:
> 各项的观测步骤(看哪些日志行、触发什么行为)、结论 A/B 分支及其对应的后续任务动作,
> 以及实机验证后回填结论的占位。配套 spike 插件为插件根目录 `_spike.py`(验证后删除)。

## 验证前提与部署方法

1. 部署:将 `_spike.py` 放入插件根目录(与 plugin.py 同目录,加载器按目录扫描);启动 docker compose core 服务(或本机直跑 MaiBot)。
2. WebUI 插件页启用插件:该文件会被加载器扫到;若未出现,将 `plugin.py` 重命名为 `plugin_spike.py` 并存根后重试。
3. 各核查项的触发方式与日志行见下文各节。
4. 验证完成后:删除 `_spike.py`,重启核心服务恢复原 plugin.py(见文末清理清单)。

**参考线索(非结论,仅本机 SDK 静态观察)**:本机安装的 maibot_plugin_sdk 2.8.0
(`/home/hesitate-p/.local/lib/python3.13/site-packages/maibot_sdk/`)顶层 `__init__.py`
**未导出** `HookMode` / `HookOrder` / `ToolParameterInfo` / `ToolParamType`(这些符号定义在
`maibot_sdk.types`,未被顶层 re-export)。若实机 SDK 同样未导出,`_spike.py` 会在模块导入期
即抛 `ImportError`、整个插件无法加载——**该现象本身请在实机记录**(是否复现、主机 SDK 版本与导出面)。
另有本机 SDK 的 `maisaka.context.append` 签名线索,见第 5 项「参考线索」。

## 结论汇总表(回填区)

| 序号 | 核查项 | 结论 | 依据(日志行/行为) | 回填日期 |
|---|---|---|---|---|
| 1 | 子包导入 `catsitate_core.*` | 待实机验证 | — | — |
| 2 | `maisaka.planner.before_request` items 前插 | 待实机验证 | — | — |
| 3 | `chat.receive.before_process` 改写能力 | 待实机验证 | — | — |
| 4 | `message.get_recent` 二进制传参 | 待实机验证 | — | — |
| 5 | `ctx.maisaka.context.append` 准确签名 | 待实机验证 | — | — |

---

## 1. 子包导入:插件加载后 `catsitate_core.*` 能否绝对导入

**观测步骤**:

- 无需额外触发,插件加载即验证。判据一:插件根目录的 `plugin.py` 首行即
  `from catsitate_core.config import CatsitateConfig`(Task 1 已提交)——若插件能正常加载,
  说明插件目录已加入 `sys.path` 且绝对导入子包成立。
- 判据二:核心服务启动日志中,插件注册过程无 `ImportError` / `ModuleNotFoundError`
  (`catsitate_core.config` 相关报错);WebUI 插件页同时出现「Catsitate 核心人格行为插件」与
  「Spike 验证插件」两个条目。
- 辅助:按第 2 项向任意群/私聊发消息,若 `[spike] before_request` 日志正常出现,进一步佐证
  两个插件文件都完成加载。

**结论分支**:

- **结论 A(支持)**:插件加载后 `catsitate_core.*` 绝对导入成立,子包结构保留。
  后续任务动作:Task 3–15 全部保持现有 `catsitate_core/` 子包结构与
  `from catsitate_core.x import` 导入方式,不做任何改动。
- **结论 B(不支持)**:绝对导入不成立。
  后续任务动作:本计划所有 `catsitate_core/` 文件平铺到插件根目录,
  `from catsitate_core.x import` 全部改为 `from x import`;波及 Task 3–15 全部模块文件
  与 `tests/conftest.py`(Task 3 附带新增)的导入语句。

**实测记录(实机验证后回填)**:待实机验证。

---

## 2. `maisaka.planner.before_request` items 前插

**观测步骤**(向任意群/私聊发一条普通消息后查核心日志):

- `[spike] before_request kwargs keys: [...]` — Hook 是否被调用;kwargs 是否含 `items` 键。
- `[spike] items 类型=... 长度=... 首项=...` — items 的类型(是否 list)与首项结构
  (是否 dict、有无 `role` 字段、`item_schema_version` 等字段)。
- 判据:spike 已在 system 项之后插入 `{"role": "user", "content": "[spike] 注入探针消息"}`;
  检查该条消息处理后的 planner 请求日志(请求内容/messages 可视化),`[spike] 注入探针消息`
  是否出现在 system 之后、历史之前的**前插位置**。出现在前插位置=前插生效;
  未出现或整体报错=前插被忽略/丢弃。

**结论分支**:

- **结论 A(生效)**:`modified_kwargs` 改写 items 前插生效。
  后续任务动作:注入走前插——Task 5 `inject.py` 与 Task 14 接线按「system 后、历史前前插」
  实现(plan §4.1 前插语义成立,公共前缀与缓存纪律不损)。
- **结论 B(无效)**:前插被忽略/整体丢弃。
  后续任务动作:注入回退为追加 `items` 尾部,并在日志中标记降级(注入块无法进入公共前缀,
  缓存收益受损,实现中须记录该降级与影响)。

**实测记录(实机验证后回填)**:待实机验证。

---

## 3. `chat.receive.before_process` 改写能力

**观测步骤**(发一条戳一戳通知后查核心日志):

- `[spike] receive.before_process kwargs keys: [...]` — 判据一:kwargs 是否包含消息对象
  (如 `message` / `msg` 键及其中可写文本字段),顺带记录完整 keys 列表。
- 判据二(改写能力):当前 `_spike.py` 对 `spike_receive_before` 仅记录 keys、原样返回
  `modified_kwargs`,**并未实际改写**;故改写能否影响下游消息文本需在实机验证期补一步:
  在 spike 插件中临时给该 Hook 加一行「把消息文本字段改写为固定串后返回
  `modified_kwargs`」,观察下游通知(机器人收到的戳一戳文本)是否变为改写后的串。
  该临时改动属于实机验证期操作,完成后随 `_spike.py` 一并删除。

**结论分支**:

- **结论 A(可改)**:kwargs 含消息对象且 `modified_kwargs` 改写能影响下游消息文本。
  后续任务动作:`enhance_notice_text` 按改写实现——Task 11 poke 引擎与 Task 14 接线在
  `before_process` 中用 `modified_kwargs` 改写通知文本(补全拟人文本,plan §入站解析增强)。
- **结论 B(不可改)**:仅观察无法改写。
  后续任务动作:`enhance_notice_text` 退化为仅日志记录增强文本;
  `inject_to_context` 路径不受影响(仍可经 `ctx.maisaka.context.append` 写入当前流上下文)。

**实测记录(实机验证后回填)**:待实机验证。

---

## 4. `message.get_recent` 二进制传参

**观测步骤**(让 planner 调用 `spike_probe` 工具,如发「用 spike_probe 工具」诱导或经 debug 通道):

- `[spike] get_recent(include_binary_data) 结果: ...` — 判据:结果中是否含二进制图片段
  (图片字段/base64/文件路径段等)。含=支持;报错或仅返回文本段=不支持(顺带记录异常原文)。
- 同一次调用还会打印 `[spike] maisaka.context.append 结果: ...`(见第 5 项)。
- 工具返回文本「spike 探针执行完毕,请查看日志」或「spike 探针失败: ...」辅助确认调用链。

**结论分支**:

- **结论 A(支持)**:`ctx.call_capability("message.get_recent", chat_id=..., limit=...,
  include_binary_data=True)` 返回二进制图片段。
  后续任务动作:图片重看直读——Task 13 `image_relook.py` 与 Task 14 接线经
  `call_capability("message.get_recent", ..., include_binary_data=True)` 取图,无需额外数据库回退。
- **结论 B(不支持)**:该参数被忽略/透传失败,无法取得二进制图片段。
  后续任务动作:改用 `ctx.database.get(model_name="Images")` 补图路径后再喂给旁路 VLM。

**实测记录(实机验证后回填)**:待实机验证。

---

## 5. `ctx.maisaka.context.append` 准确签名核查清单

**如何验证**:

1. 主要依据:spike_probe 工具日志行 `[spike] maisaka.context.append 结果: ...`。
   成功则记录返回类型与值;抛异常则记录异常消息(异常消息通常暴露参数名与类型要求)。
2. 实机验证期补查:在插件内临时打印
   `inspect.signature(self.ctx.maisaka.context.append)`(临时改动,随 `_spike.py` 删除),
   核对参数名与返回注解。
3. **参考线索(非结论,本机 SDK 2.8.0 静态观察)**:本机 SDK 中
   `maisaka.context.append(stream_id: str, segments: list[dict], *, visible_text="",
   source_kind="", message_id="", **kwargs) -> Any`,第二参为**消息段列表**;
   而 spike 调用传的是字符串 `"[spike] 注入上下文探针"`。实机需重点核对:
   第二参是否接受字符串?参数名与返回是否如计划假设的 `append(stream_id, content)`?
   (计划 §ctx 能力代理 已标注「看签名后核对」,本项即为此而设。)

**结论分支**:

- **结论 A(与假设一致)**:实机签名与计划假设一致(第二参接受 content 字符串)。
  后续任务动作:`inject_to_context`(Task 11 poke 引擎)与 Task 14 接线按
  `ctx.maisaka.context.append(stream_id, content)` 调用。
- **结论 B(与假设不符)**:实机签名不同(如第二参须为 segments 列表/另有命名参数)。
  后续任务动作:按实机实际签名调整全部调用点;若 spike 调用因此失败,记录实际签名并同步
  修正计划文档对应调用示例。

**实测记录(实机验证后回填)**:待实机验证。

---

## 后续任务依赖说明

- 结论 1 影响全局:Task 3–15 全部模块的导入方式(子包保留 vs 平铺)。
- 结论 2 影响注入:Task 5 `inject.py` 与 Task 14 接线。
- 结论 3 影响戳一戳入站增强:Task 11 poke 引擎与 Task 14 接线。
- 结论 4 影响图片重看:Task 13 `image_relook.py` 与 Task 14 接线。
- 第 5 项影响 `inject_to_context` 调用点:Task 11 与 Task 14 接线。
- Task 14 为总装接线,四类分支在 Task 14 统一收敛。
- 按 controller 裁决:实机结论回填之前,后续任务按计划内保守假设实现(分支点集中于
  plugin.py 常量区与 `_fetch` 辅助,实机不符时仅需小改)。

## 验证后清理清单

- [ ] 删除插件根目录 `_spike.py`
- [ ] 重启核心服务,恢复原 plugin.py 加载
- [ ] 回填本文件「结论汇总表」与各节「实测记录」
- [ ] 若任一结论为 B,同步更新计划文档对应实现段并记入 progress.md
