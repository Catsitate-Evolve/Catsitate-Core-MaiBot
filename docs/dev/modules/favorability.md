# 好感度模块

> 源码:`catsitate_core/favorability.py`(批次引擎与结算执行)、`catsitate_core/decay.py`(自然衰减)、`catsitate_core/qzone/comment_seen.py`(空间互动事件表)、`plugin.py`(计数 Hook、结算调度、素材聚合、注入块)。

好感度是按人跟踪的关系模型:bot 对每个 QQ 用户维护一个分数与等级,由 LLM 依据真实聊天素材判定变化,配合确定性时间衰减,让关系「有来有往、也会生疏」。

## 一、模块职责与生命周期

### 职责

- 以 QQ 号(`user_id`)为主键跟踪好感度,**跨聊天流聚合**——同一人在私聊、多个群的互动共同累积到同一个分数上。
- 五级等级:陌生 → 熟悉 → 亲近 → 挚友 → 特别。分数到等级的映射是纯函数:

| 等级 | 分数区间 |
|---|---|
| 陌生 | 0–9 |
| 熟悉 | 10–29 |
| 亲近 | 30–59 |
| 挚友 | 60–99 |
| 特别 | ≥100 |

- 「特别」全局独占:全表最多 1 人(`EXCLUSIVE_LEVEL = 4`,`is_exclusive_holder` 全表查询)。
- 提供注入块(`build_favorability_block`):每次 planner 请求前把当前等级/分数/注记(可选带当前等级的行为规则单条)注入上下文,供回复时把握关系距离。

### 生命周期

1. **计数**(常驻):`chat.receive.after_process` 观察 Hook `catsitate_fav_count` 对每条真实用户消息 bump 活跃账本 `batch_counter(user_id, stream_id)`;达到阈值即触发提前结算。睡眠期不计数;通知类消息(戳一戳等)与 QQ 空间虚拟流消息不计数。
2. **结算**(事件驱动 + 周期兜底):提前结算(early)由计数触发;日终结算(daily)由调度器周期 tick(`daily_settle`,周期 = `favorability.window_hours`,默认 24h)兜底执行,醒来还会补跑。
3. **衰减**(周期):`daily_decay` 调度项每 24h 一次,与日终结算同 tick 执行、先衰减后结算。
4. **注入**(每轮 planner 请求):当前说话人的等级块注入上下文。

### 存储表

| 表 | 主键 | 用途 |
|---|---|---|
| `favorability` | `user_id` | 按人的等级/分数/注记/结算窗口起点 `window_start`/最近判定时刻 `judged_at` |
| `favorability_log` | `judge_id` | 判定日志(幂等键),daily/early 防重与衰减判重依据 |
| `batch_counter` | `(user_id, stream_id)` | 活跃账本:该人在该流自上次结算以来的消息数 |
| `qzone_fav_events` | id | 空间互动显式事件(评论/点赞/出站),结算素材与衰减计时数据源 |

`ensure_schema` 检测到旧表形状(按流存储时代的 `stream_id` 列、账本死列)会直接重建,不做数据迁移。

## 二、完整逻辑

### 1. 计数与提前结算触发

`fav_count`(OBSERVE Hook)对每条入站真实消息:

1. `count_message` 对 `batch_counter` 的 `(user, stream)` 行 `count + 1` 并刷新 `last_bump`;
2. `check_trigger` 按人判定:**总计数 = 该人跨全部流的 `SUM(count)`**,当总数 ≥ `early_settle_threshold`(默认 20)且当日 early 结算次数 < `daily_max_early_settle`(默认 3)时返回 `"early"`;
3. 触发后 spawn 后台任务执行 `_settle_and_log(user_id, kind="early")`。

顺带维护 `_last_speaker_map`(流 → 最近真实说话人),供备忘录工具在群聊 `user_id` 缺省时兜底归属。

### 2. 结算执行(`SettleExecutor.settle`)

`_settle_and_log` 先聚合素材,再调用 `settle`:

**素材构造**(跨流聚合):

- 该人活跃账本里的全部流,逐流 `message.get_recent`(每流 50 条)拉近期消息,归一化为 `{role, user_id, stream_id, text, seq, ts, is_group, addressed}`;单流失败仅跳过该流并告警。
- bot 消息判定:`user_id == favorability.bot_user_id` 即 bot(配置留空则不识别)。群聊 bot 消息的 `addressed`(@ 命中或 quote 段经 `message.get_by_id` 解析原发送者命中目标)在此阶段批量解析。
- **空间互动事件并入**:`fav_events_since(user_id, window_start)` 以 `created_at > window_start` 的滚动窗取事件(排他下界,ISO 字符串比较),取最近 5 条,合成 user 消息追加(`stream_id="qzone-events"` 合成流隔离邻居,`ts` 用事件原始时刻)。窗口起点空串 = 全量事件。事件读取失败则本次结算不含事件素材,显式告警。

**`BatchEngine.build_material` 过滤与裁剪**:

1. `window_start`(上次结算时刻)之前的老消息剔除——同日多次结算时已判过的素材不重判(事件与消息同机制);
2. 锚点 = 目标用户本人最近 `material_max_messages`(默认 30)条消息(配置 0/负数取全量);
3. 每个锚点前后各带 1 条同流邻居消息(上下文);
4. bot 消息仅随附目标用户发过言的流:私聊全收,群聊只收 `addressed` 命中该人的;
5. 单条素材截断 `material_message_max_chars`(默认 200)字符。

**LLM 判定**:

- 素材为空(窗口过滤后无目标用户消息):不调 LLM、不落库,返回 failed「素材为空」。
- daily 结算要求素材中目标用户本人消息 ≥ `daily_settle_min`(默认 3)条,不足则返回 `carried_over`(顺延)——**批次不清零、窗口不前移**,消息留到下一轮(顺延不丢弃)。
- 旁路 prompt = 稳定段(system 模板 `favorability` + 人设 → 行为风格 → 5 级规则,顺序固定保前缀缓存)+ 变量尾(素材行)。模型与超时取 `favorability.llm_model` / `llm_timeout_ms`。
- 判定 JSON `{"delta": int, "note": str}` 解析容忍 markdown 围栏;delta 钳制在 ±`delta_max`(默认 5)。

**落库(`apply_delta`)**:

- `score = max(0, 旧分 + delta)`:负分钳到 0(结算与衰减共用此入口);
- 重算等级;若升入「特别」但该位已被他人占据 → 钳制在 99 分/挚友,返回 `clamped_exclusive`(settle 层透传为 `exclusive_clamped` 字段,由调用方记告警日志);
- 注记强制截断 `note_max_chars`(默认 40)字符;
- `judged_at` 更新为本次判定时刻;`window_start` 按 `advance_window` 参数两路取值——结算(early/daily)消费了批次素材,推进到判定时刻(默认 True);衰减不消费素材,保留既有 `window_start`(False,无既有记录时首条无旧窗可保仍取判定时刻),否则按窗口过滤素材会把未结算消息/事件永久排除(与「批次顺延不丢」一致);判定日志 `INSERT OR IGNORE`,幂等键 `judge_id = {kind}-{judged_at}-{user_id}`(用户后缀防同秒多人撞键);
- 结算成功后 `reset_batch` 把该人全部流计数清零。

### 3. 日终结算(`_daily_settle`)

- 睡眠期直接返回,醒来由 `_wake_up` 补跑(与调度 tick 共享防重入守卫 `_daily_settle_running`,在飞则跳过本轮)。
- 先执行 `_daily_decay`(先衰减后结算)。
- 候选 = `iter_today_active`(当日有消息且批次未清零的人)∪ 回看窗 `fav_events_window(now - window_hours)` 内有空间互动事件的人(纯空间互动者没有 batch 行,并集令其也进入日终兜底;bot 自身排除)。事件反查失败则候选仅用 batch 活跃,显式告警。
- 逐人检查 `has_daily_settle_today`(judge_id 前缀 `daily-YYYY-MM-DD`),未结算的执行 `_settle_and_log(kind="daily")`;单用户失败隔离告警,不拖垮整轮。

### 4. 自然衰减(`decay.py` + `_daily_decay`)

- 扫描对象 = `favorability` 全表 `score > 0` 的人(不能用当日活跃——衰减对象恰是长期未互动者)。
- **计时基准**按人取三者最大:
  1. 各活跃流内最近一次 bot **直接回应**该用户的时间(`last_bot_interaction_time`:私聊任意 bot 消息即算;群聊须 bot 消息 @ 该人或 quote 该人——quote 原发送者由 `_daily_decay` 预解析注入 `resolved_quote_user_id`,解析失败该条只按 @ 判定;bot 回应他人不重置本用户计时);
  2. 最近一次空间互动事件时刻(`last_fav_interaction`);
  3. 最近一次衰减判定时刻(`favorability_log` 中 `decay-` 前缀的最新 `judged_at`)——衰减判定本身即一次「想起」,7 天内不重复衰减。
- 基准为空(从未直接互动)时回退 `favorability.judged_at`;仍为空则跳过。
- **未互动天数用浮点比较**(`total_seconds()/86400 > decay_after_days`,默认 7 天),消除整天截断偏差。
- 超过阈值且 score > 0 → 旁路 LLM(`decay` 模板,模型 `decay_llm_model`)判定衰减:JSON `{"delta": int 或 float, "note": str}`,delta 语义为 `[-decay_max, 0]` 区间数值(默认上限 3,正值直接判解析失败;LLM 返回 `-1.0` 这类浮点形态同样接受,布尔拒绝),note 为拟人化新注记。
- 落库复用 `apply_delta`(judge_id = `decay-{judged_at}-{user_id}`,`advance_window=False`,钳制取整后落库)。衰减 delta 恒 ≤ 0、分数只会降,不可能触发「升特别被占位」钳制;衰减不消费批次素材,`window_start` 保持不动,未结算消息留待下轮结算(顺延不丢)。
- 单流取消息失败跳过该流;流已消亡(不在流缓存)跳过并告警;LLM 失败或判定 JSON 解析失败均告警后跳过该人。
- 实例级 `_decaying` 在飞标记防并发双计(醒后补跑与调度 tick 可并发),finally 复位。

### 5. 注入块

`build_favorability_block(engine, user_id, include_rule)`:

- 无记录 = 等级「陌生」、0 分、无注记;
- 正文 `[好感度] {user_id}:等级「{名}」(累计 {分})`,有注记则附 `注记:{...}`;
- `inject.level_rule_enabled` 开启时,按当前等级注入对应**单条**行为规则(非 5 级全量),置于块最前。

## 三、限制与回退清单

| 场景 | 行为 |
|---|---|
| LLM 调用异常/返回失败 | 结算返回 failed,旧值保留、批次不清零(素材仍在,下轮重判);衰减告警后跳过该人本轮。异常日志仅记类型不落响应原文(防 PII) |
| 判定 JSON 解析失败 | 同上,failed 返回,不落库;衰减侧告警后跳过该人 |
| 结算素材为空 | 不调 LLM 不落库,failed「素材为空,跳过结算」 |
| daily 素材不足 `daily_settle_min` | `carried_over` 顺延:不落库不清零,消息留待下轮 |
| 分数降到 0 以下 | `apply_delta` 统一钳到 0,不出现负分 |
| 升「特别」但位被他人占 | 钳 99 分/挚友,`exclusive_clamped` 告警 |
| delta 超限 | 结算钳 ±`delta_max`;衰减钳 `[-decay_max, 0]`,正 delta 判解析失败 |
| 同秒多人结算 | judge_id 带用户后缀 + `INSERT OR IGNORE`,日志不丢 |
| 同用户并发结算 | `_settling` 集合按人在飞即跳过(kind 不限),防 delta 双计 |
| 日终/衰减任务重入 | `_daily_settle_running` / `_daily_decay_running` / `_decaying` 三层标记,finally 复位 |
| 睡眠期 | 计数暂停、结算与衰减静默跳过,醒来补跑 |
| 单流取消息失败 / 流消亡 | 跳过该流并告警,不拖垮整次结算/衰减 |
| 单用户日终失败 | 异常隔离,继续下一用户 |
| 空间事件读取失败 | 结算不含事件素材/衰减不含事件基准,显式告警 |
| bot_user_id 未配置 | 素材中不识别 bot 消息(全部按 user),`addressed` 判定失效 |
| 旧表形状 | 检测到即重建表,不迁移数据 |
