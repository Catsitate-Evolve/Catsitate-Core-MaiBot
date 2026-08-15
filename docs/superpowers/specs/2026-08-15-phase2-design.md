# Catsitate 二期设计规格(自然衰减 / 睡眠 / 日程 / 主动私聊)

> 一期基线:docs/superpowers/specs/2026-08-14-catsitate-core-maibot-design.md(已验收,本规格只描述二期增量,不改一期行为)

## 1. 范围

- **2.0 好感度自然衰减**(规划外新增,联调确定)
- **2.1 生活日常 & 自主规划日程**(一期 §5 原规划)
- **睡眠管理**(联调新增;机制参考 goodnight_sleep_manager,按 Catsitate 拟人化裁剪)
- **2.3 主动私聊**(一期 §5 原规划:好感度达"挚友"级用户,由日程窗口触发私聊问候)

## 2. 全局决策(联调对齐)

1. 主动发言统一受好感度门槛约束,各门槛独立配置,默认:日程日常发言=熟悉(≥10)、晚安/问候=亲近(≥30)、主动私聊=挚友(≥60)。
2. 睡眠为**全局统一状态**(一个身体一个作息),不做分群作息,不做任何睡眠命令(无 /sleep_now 等)。
3. 所有 LLM 旁路请求沿用一期规范(§4.9/§4.10):task 名路由、独立超时配置、稳定段前置、模板接入主程序 prompt 管理、模板变更即缓存失效。
4. 拒绝纯概率行为:发言/入睡判定均由 LLM 或确定性规则决定,随机数仅限冷却/醒来浮动。

## 3. 模块设计

### 3.1 好感度自然衰减(schedule 内每日调度,先衰减后结算)

- **互动定义**:「bot 回应过该用户」= 流内最近 bot 消息时间;距今 ≤ `decay_after_days` 视为有互动。群聊中用户发言但 bot 未回应不算互动;私聊同理(bot 不回应即关系降温)。
- **触发**:每日调度(与日终结算同 tick,先衰减后结算):扫描 `favorability` 表 score>0 的流,距 bot 最后回应 > `decay_after_days`(默认 7 天)者进入衰减判定。每流每日最多一次;判定后重置计时。
- **LLM 判定衰减**:prompt = 人设 + 上次等级/分数/注记 + 未互动天数 → 输出 `{"delta": 整数(-decay_max 到 0), "note": "新注记(≤40 字)"}`;delta=0 表示关系稳定不减。模板 `catsitate_decay.prompt` 进主程序 prompt 管理,含 `{{decay_max}}` 占位符。
- **落库**:`apply_delta` + `favorability_log`(judge_id=`decay-时间戳`);分数可降到 0,等级按分数自然降级,注记更新为拟人化描述。
- **错误处理**:LLM 失败 → 跳过本轮、显式日志,次日重试。
- **配置**(favorability 节):`decay_enabled`(默认 true)、`decay_after_days`(默认 7)、`decay_max`(默认 3)、`decay_llm_model`(默认 memory)、`decay_llm_timeout_ms`(默认 None)。
- **测试**:候选流扫描(超期/未超期/0 分/有互动)、判定解析、delta 钳制、失败不落库。

### 3.2 睡眠管理(全局状态机)

- **状态**:`awake / sleep` 全局唯一;持久化 `sleep_state.json`(重启恢复,过期自动清理)。
- **作息时间由日程 LLM 自主决定**(联调新增):睡前生成次日日程时,一并生成当晚 `sleep_at`(计划入睡时间)与次日 `wake_at`(计划醒来时间),写入日程 JSON;未生成日程(LLM 失败/禁用)时用配置默认值兜底。
- **入睡机制**(参考 goodnight_sleep_manager,裁剪版):
  - 到达 `sleep_at` 后进入「可睡期」(至 `wake_at` 止):
    - bot 自发晚安短句(「我睡了」「晚安」等)出站时经 **AI 判定器** 判定 `SLEEP / NOT_SLEEP / UNSURE`(模板 `catsitate_sleep_confirm.prompt` 进主程序 prompt 管理;正则兜底),`SLEEP` → 入睡;
    - 用户催睡(自然语言,如「去睡觉」)正常走 planner,bot 回复的晚安短句同样经判定器确认后才入睡(防诱导:只接受短句、自我入睡;带 @/称呼他人/引用回复不触发);
    - **到点强制入睡兜底**:可睡期结束时仍未入睡 → 自动入睡(保证作息下限);
  - **可用工具推迟睡眠**(联调新增):`update_schedule` 工具支持修改当晚 `sleep_at`/次日 `wake_at`(如用户挽留「再聊会」时 planner 可推迟入睡时间;推迟幅度受 `max_sleep_minutes` 等约束检查);
  - **静默入睡**(默认关,可配置):无任何入站/出站消息满 N 分钟自动入睡(不调 LLM);
  - **唤醒**:到 `wake_at` 自动醒;醒来浮动随机分钟(仅此处允许随机);睡眠中被 @ 时若 `wake_on_mention` 开启 → 立即唤醒(全局);
  - **最短/最长睡眠**:不足最短睡眠分钟时不醒(顺延到最短),超过最长强制醒(约束 LLM 生成的 sleep_at/wake_at 与工具推迟范围)。
- **睡眠期间表现(联调 Q5=A)**:新消息在 `chat.receive.before_process` BLOCKING 拦截(allow_abort,不进入 planner、不回复、不思考),**命令同样拦截**(联调 Q12=A);被拦截消息进入睡眠回顾记录。
- **睡醒回顾**(默认开,可配置关):醒来时对睡眠期间被拦截消息生成**单份聚合报告文件**(`data/plugins/catsitate.core/sleep_review/reports/`),含每流消息数、摘要、重要消息(LLM 总结);不补发历史回复、不注入对话上下文(联调 Q10=参考 goodnight_sleep_manager)。
- **配置**(新 sleep 节):`enabled`、`default_sleep_at`(默认 23:00,无日程时的入睡兜底)、`default_wake_at`(默认 08:00,无日程时的醒来兜底)、`min_sleep_minutes`(默认 240)、`max_sleep_minutes`(默认 660)、`wakeup_jitter_minutes`(默认 20)、`wake_on_mention`(默认 false)、`silent_sleep_enabled`(默认 false)、`silent_sleep_minutes`(默认 60)、`review_enabled`(默认 true)、`review_llm_model`(默认 memory)、`review_llm_timeout_ms`(默认 None)。
- **测试**:入睡判定器解析、窗口边界(含跨午夜)、最短/最长睡眠、拦截 hook 行为、状态持久化、静默入睡计时。

### 3.3 日程(2.1:睡前生成 + 窗口执行 + 可被工具修改)

- **生成**:睡前(固定 21:30,早于计划入睡时间)LLM 生成次日日程;**固定 5 窗口框架**(早晨/上午/午后/傍晚/睡前),每窗口 = 活动描述(内部状态)+ 是否计划发言 + 发言主题。启动时若无有效日程则补生成当天。窗口时间段固定(不可配置,保证结构可测):早晨 06:00-09:00、上午 09:00-12:00、午后 12:00-18:00、傍晚 18:00-21:00、睡前 21:00-24:00。
- **结构**:日程存储 JSON(`schedule_state.json`):`{date, generated_at, sleep_at, wake_at, windows: [{name, time_range, activity, plan_speak, topic}]}`;`sleep_at`/`wake_at` 为当日计划入睡/醒来时间(睡眠模块读取,联调新增)。
- **注入**:「日程块」作为独立注入块,插在环境块之后、备忘块之前;内容 = 当前窗口活动+主题(如 `[日程] 午后:发呆看雨,不想说话。`);窗口切换才变化(半天级稳定,缓存友好)。
- **执行**:每窗口触发时(到窗口起点)经 `maisaka.proactive.trigger` 主动发言;发言内容由**执行时 LLM 判定**(联调 Q24=B):输入人设+日程窗口+当前天气+目标流好感度等级/注记 → 是否发言+发言文本(可为空=沉默);受好感度门槛约束(等级不足不发言,不调 LLM)。
- **可被工具修改**:`@Tool("update_schedule")`(visible):planner 可调用以增改日程窗口(参数:窗口名/新活动/是否发言/主题)或修改 `sleep_at`/`wake_at`(推迟睡眠等场景);约束:窗口框架 5 个不变、内容长度上限、睡眠时长在 min/max_sleep_minutes 内;修改落盘并立即反映到注入块与睡眠状态机。
- **生成/执行模型**:`schedule_llm_model`(默认 memory)、`schedule_llm_timeout_ms`;执行发言判定另用 `speak_llm_model`(默认 memory)。
- **配置**(新 schedule 节):`enabled`、`generate_time`(默认 21:30)、`speak_threshold_level`(默认 熟悉)、`greet_threshold_level`(默认 亲近)、`private_threshold_level`(默认 挚友)、`speak_llm_model`、`speak_llm_timeout_ms`、`schedule_llm_model`、`schedule_llm_timeout_ms`、`daily_speak_limit`(默认 5,全天主动发言次数上限)。
- **与睡眠交互**:睡眠中窗口触发 → 跳过(醒来后不补发);发言计入 `daily_speak_limit`。
- **测试**:5 窗口校验、睡前生成调度、窗口命中判断、工具修改边界、门槛过滤(等级不足不发言不调 LLM)。

### 3.4 主动私聊(2.3)

- 日程窗口触发时,对好感度 ≥ `private_threshold_level`(默认挚友)的**私聊流**用户,在问候窗口(上午/晚间)LLM 生成私聊问候并经 `send.text` 发送;每流每日最多一次(与 `daily_speak_limit` 共享上限)。
- 睡眠/静默期跳过;被拦截/失败显式日志。
- **测试**:门槛过滤、每日一次限制、睡眠期跳过。

## 4. 数据流与模块边界

- `catsitate_core/decay.py`(衰减执行器,复用 SettleExecutor 模式:扫描+判定+apply_delta)
- `catsitate_core/sleep.py`(状态机+入睡判定器解析+回顾生成)
- `catsitate_core/schedule.py`(日程生成/解析/窗口判定/工具修改校验)
- `plugin.py` 接线:hook(receive 拦截、出站晚安判定)、scheduler 注册(衰减/睡前生成/窗口触发/睡眠检查)、tools(update_schedule)、注入块(日程块)
- 全部旁路 prompt 模板:`catsitate_decay.prompt`、`catsitate_sleep_confirm.prompt`、`catsitate_schedule_generate.prompt`、`catsitate_speak.prompt`、`catsitate_greet.prompt` 进主程序 prompt 管理。

## 5. 配置模型(新增节,中文 label)

- `sleep` 节:3.2 所列字段
- `schedule` 节:3.3 所列字段
- `favorability` 节增:3.1 所列衰减字段
- 所有 LLM 字段沿用一期平铺风格(task 名 + 独立 timeout_ms)

## 6. 错误处理原则(沿用一期 §7)

- 所有 LLM/API 失败显式日志并跳过本轮(次日/下窗口重试);拦截路径异常放行(不阻断主链路)并记录;静默回退仅限规格明示路径且必须告警。

## 7. 测试方式

- 引擎层单测(衰减扫描/判定、睡眠状态机/判定器/窗口、日程生成解析/窗口/工具校验、门槛过滤),沿用一期 pytest 结构;
- 实机验收清单追加二期条目(衰减日志、睡眠拦截、睡醒回顾文件、日程块注入、主动发言)。

## 8. 交付物

1. 二期代码(4 模块 + plugin.py 接线 + 5 个 prompt 模板)
2. tests/ 二期单测
3. 验收清单二期条目
4. CHANGELOG 更新
