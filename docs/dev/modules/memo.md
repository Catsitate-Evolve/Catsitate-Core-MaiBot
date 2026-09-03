# 备忘录模块

> 源码:`catsitate_core/memo.py`(服务与校验)、`plugin.py`(工具/命令、注入块、提醒兜底 tick、清理调度)、`catsitate_core/storage.py`(SQLiteStore)。

备忘录是 bot 的短时记忆:用户一句「记一下」或 LLM 自主调用工具写入,条目在有效期内注入对话上下文,带提醒时刻的条目到点触发提醒,过期自动清理。

## 一、模块职责与生命周期

### 职责

- **短时记忆写入**:`/记一下` 命令(用户侧)与 `memo_write` 工具(LLM 侧)两个入口,单条备忘带有效期(TTL)与可选提醒时刻(`remind_at`)。
- **按人跨流可见**:条目归属 = 主 QQ(`user_id`)+ 附带 QQ 列表(`extra_user_ids`,≤5 个)+ 所在流(`stream_id` 元数据)。读取是 OR 语义:所在流命中,或当前说话人是主 QQ/任一附带 QQ 命中,即返回——主 QQ 在任何聊天流里说话都能看到自己的备忘。
- **到点提醒**:`remind_at` 到点的条目经日程收录或兜底 tick 注入对应流。
- **过期清理**:每小时删除过期条目。

### 生命周期(单条备忘)

```
写入(命令/工具,校验长度/TTL/remind_at 格式)
  → 注入(有效期内,每次 planner 请求按"流 ∪ 说话人"带入上下文,≤ inject_max 条)
  → 提醒(remind_at 到点:日程收录 或 remind_fallback 兜底注入)
  → 清理(expires_at 到期,每小时 tick 删除)
```

### 存储表 `memo`

| 列 | 说明 |
|---|---|
| `id` | 自增主键 |
| `content` | 内容(≤ `entry_max_chars` 字符) |
| `stream_id` | 写入时所在流(元数据,兜底提醒的注入目标) |
| `user_id` | 主 QQ |
| `expires_at` / `created_at` | 过期/创建时刻(ISO) |
| `remind_at` | 可选提醒时刻,ISO(YYYY-MM-DDTHH:MM[:SS]) |
| `extra_user_ids` | 附带 QQ 列表,JSON 数组字符串 |

旧库缺 `remind_at` / `extra_user_ids` 列时 `ensure_schema` 自动 `ALTER TABLE` 补列。

## 二、完整逻辑

### 1. 写入

**命令入口** `/记一下 <内容>`(别名 `/备忘`,`cmd_memo`):检查 `memo.command_enabled` 与内容长度后写入,TTL 用缺省值(不传 TTL,不传 remind_at/附带 QQ)。

**工具入口 `memo_write`**(LLM 调用,`memo.tool_enabled` 控制):

- 参数:`content`(必填)、`stream_id`/`user_id`(缺省当前流/当前说话人)、`ttl_hours`(单条有效期)、`remind_at`(ISO 提醒时刻)、`related_user_ids`(附带 QQ,逗号分隔,兼容中文逗号/顿号)。
- 群聊 `user_id` 常为空:以 `fav_count` Hook 维护的 `_last_speaker_map`(流 → 最近真实说话人)兜底归属;该映射纯内存,重启丢失(可接受,`_resolve_speaker` 回退仍在)。
- `remind_at` 先过 `validate_remind_at`(正则 `^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2})?$`),非法显式返回中文错误给 LLM——拒绝写入而非静默丢提醒。

**`MemoService.write` 源头校验**(两入口共用):

1. 内容非空、≤ `entry_max_chars`(默认 80)字符;
2. TTL 缺省取 `default_ttl_hours`(默认 24),必须 > 0 且 ≤ `max_ttl_hours`(默认 168)小时;
3. `remind_at` 格式非法拒绝;
4. 附带 QQ 清洗:去空、去重(保序)、剔除主 QQ 自身,超 5 个截断并在返回文案中提示;
5. 落库,`expires_at = now + ttl`。

写入成功返回拟人文案「已记下(N 小时内有效)」;失败返回 `(False, 原因)`,由入口原样展示给用户/LLM。

### 2. 读取与跨流可见性(`MemoService.read`)

- 查询条件为 OR:所在流精确命中 **或** 说话人命中(主 QQ 精确匹配 **或** 附带 QQ 列表命中——JSON 形态如 `["10001","10002"]`,LIKE `"qq"` 带引号精确段匹配并用 `ESCAPE` 转义,防 `1002` 误中 `10002`)。
- 流与说话人双空 = 无归属条件,直接返回空(不匹配全表)。
- 只返回未过期条目,按 `created_at DESC, id DESC` 取前 `limit` 条,附剩余有效小时数。

`memo_read` 工具按当前流/说话人读取,返回「内容(剩余 N.N 小时)」列表。

### 3. 注入(到期前持续可见)

`planner.before_request` 注入 Hook(`inject_blocks`)中,`inject.memo_enabled` 开启时:

- `memo.read(stream_id, speaker, limit=inject_max)` 一次查询覆盖「流 ∪ 主 QQ ∪ 附带 QQ」三个维度,按 id 去重后截 `inject_max`(默认 5)条;
- 渲染为 `[备忘] 内容1;内容2;…` 注入块,key 为条目 id 排序串(内容变化即换 key)。

另外,日程注入块行末会附当日到期备忘(≤3 条,按说话人/流过滤);睡醒回顾报告会静态附列睡眠期到期的备忘(不占 LLM 额度,备忘不丢失)。

### 4. 到点提醒(双路)

- **日程收录(主路)**:次日日程生成时(`_generate_tomorrow_schedule`),目标日到期备忘(`due_on`)作为 prompt 素材传入,由 LLM 排进作息(如在提醒时刻安排活动/计划发言)。当天持有 LLM 生成的日程时,兜底 tick 主动让位不重复提醒。
- **兜底 tick(`_remind_fallback_tick`,每 300s)**:当天**没有**生成日程(模板撑场或日程未启用)时,对当日到期、`remind_at ≤ 现在` 且 `stream_id` 非空的条目,经 `maisaka.context.append` 把 `[备忘提醒] {内容}` 注入条目归属流;睡眠中不执行。成功后记 `_remind_fired["remind:{id}"]`(持久化到 `remind_fired.json`,跨重启去重);失败不标记,留待下轮重试。

`due_on(day)` 的取数口径:`remind_at` 前缀匹配该自然日且未过期,按提醒时刻升序。

### 5. 清理

`memo_cleanup` 调度项每小时执行 `cleanup()`:删除 `expires_at <= now` 的条目并记日志条数。提醒去重表同 tick 顺手清理跨天键(`_prune_day_keys`,只保留当天,防 `remind_fired.json` 无限增长)。

## 三、限制与回退清单

| 场景 | 行为 |
|---|---|
| 内容超长 | 命令入口直接拒绝并提示精简;工具/服务入口写入前强制校验,拒绝落库 |
| TTL 非法(≤0 或 > max_ttl_hours) | 拒绝写入,返回原因 |
| `remind_at` 格式非法 | 显式拒绝写入(防到期永不匹配导致静默丢提醒),错误文本返回给 LLM |
| 附带 QQ 超 5 个 | 截断保留前 5 个,返回文案提示 |
| 注入条数 | 每轮 planner 注入合计 ≤ `inject_max`(默认 5)条,超出截断 |
| 未设 `remind_at` 的备忘 | 只注入、不提醒,到期随清理删除 |
| 兜底提醒目标 | 仅注入条目自身 `stream_id`(写入时所在流);`stream_id` 为空的条目不参与兜底提醒 |
| 有生成日程的当天 | 兜底 tick 让位(提醒交日程收录),不会双份注入 |
| 提醒注入失败 | 不标记已触发,下轮 tick 重试 |
| 睡眠期 | 兜底提醒不执行;睡眠期到期的备忘在睡醒回顾报告中静态补列 |
| 提醒去重 | `remind:{id}` → 触发时刻,持久化 `remind_fired.json`,跨重启不重复;跨天键自动清理 |
| 写入时群聊 user_id 缺省 | 以最近说话人内存映射兜底;映射重启丢失,归属退化为纯流维度 |
| 条目无流无主 QQ 维度命中 | `read` 双空条件直接返回空,不做全表匹配 |
