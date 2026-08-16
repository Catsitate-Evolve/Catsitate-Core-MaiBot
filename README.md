# catsitate_core_maibot

Catsitate 的 MaiBot 核心人格行为插件。仓库地址:https://github.com/Catsitate-Evolve/Catsitate-Core-MaiBot 。详细设计见 `docs/superpowers/specs/`;公测使用手册(功能详解/配置项全表/日志关键词/已知限制)见 `docs/plugin-manual.md`。

## 启用

1. 把本目录放进 MaiBot 的 `plugins/` 并重启;WebUI「插件」页确认 `catsitate.core` 已加载
2. 在插件配置页打开 `plugin.enabled = true`(总开关,默认关),按需调整各模块节
3. 贴表情:内置 30 项精选 QQ 表情表(`catsitate_core/qq_emoji.py` 的 AVAILABLE_REACT_EMOJIS,联调决定替代可配置白名单),仅群聊可用

## 主程序配置(模型 task 分配)

插件旁路 LLM 请求(好感度结算/选表情/哨兵/图片重看)统一经主程序 `model_task_config` 路由。
主程序 task 集合固定(replyer/planner/memory/mid_memory/utils/learner/expression_use,WebUI「功能分配」页可见),**不能新增自定义 task**。

- 插件各能力 `llm_model` 默认填 `utils`(主程序轻量小任务,契合旁路判定),可自定义改填任意已配置 task 名(如 `planner`/`memory`);
- `llm_model` 填的是 **task 名**(节名),填模型标识会报「未找到名为 … 的模型配置」;
- 留空 = 主程序默认(取首个可用 task,不可控,不推荐)。

## 测试

- 单元测试:`cd plugins/catsitate_core_maibot && python3 -m pytest tests/ -v`(不依赖 MaiBot)
- 集成冒烟:同目录 `python3 -m pytest tests/test_integration.py -v`
- 依赖:若未安装 pytest-asyncio(async 用例会被静默跳过),先执行 `python3 -m pip install --break-system-packages pytest-asyncio`
- 实机验收:按 `docs/acceptance-checklist.md` 逐项勾选

## 缓存与用量观测

- 主链路命中率:对照 `docs/cache-baseline.md` 流程,看主程序日志 `Planner缓存:...hit_rate=xx%`
- 旁路 LLM 记账:`llm_usage` 表(day/module/calls/tokens)按模块分列;每日旁路调用合计超过
  `plugin.llm_daily_call_warning_threshold` 时记录告警日志

## 配置要点

- 每个 LLM 能力的 `llm_model` 默认 `utils`(主程序轻量任务);可自定义填任意已配置 task 名
- 注入四块(`level_rule`/`environment`/`memo`/`favorability`)各自有开关,可独立关闭
- 哨兵层默认关(`reply_guard.sentinel_enabled`),开启后每句回复多一次旁路判定

## 二期模块(2026-08-15)

- 好感度自然衰减:未互动 N 天由 LLM 判定拟人化衰减(0~-decay_max),群聊 quote/@ 防误判,以 `favorability.judged_at` 起算
- 睡眠管理:睡眠=日程窗口(LLM 自主作息)、睡眠中消息绝对静默拦截、晚安判定入睡、睡醒回顾报告(`data/plugins/catsitate.core/sleep_review/reports/`)
- 日程:入睡时生成次日动态活动日程(1 睡眠 + 1~8 活动),`[日程]` 块注入 planner;到达活动窗口且存在满足门槛的活跃流 → 日程窗口 trigger 主动发言(表达权交主程序);`update_schedule` 工具可改次日日程
- 主动问候:仅「特别」等级用户(全表唯一)且存在私聊流,greeting 窗口起点触发(日志『主动问候触发[day] -> user』);无每日一次限制
- 备忘录提醒:备忘新增 remind_at 提醒时间,随日程注入归属流;无日程时到点独立兜底注入
