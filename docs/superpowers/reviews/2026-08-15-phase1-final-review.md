# Catsitate-Core-MaiBot 一期最终审查报告(whole-branch)

- 审查日期:2026-08-15
- 审查范围:规格 §4.1–4.10 全部模块 + plugin.py 接线层 + tests/(92 个)+ 验收文档 + 缓存基线
- 审查方式:逐文件通读 + pytest 全量运行(92 passed in 0.36s)+ 针对疑似缺陷的行为验证脚本
- 结论:**有条件通过**(3 个 Major 需修复,修复后即可放行)

---

## 审查轴总览

| 轴 | 结论 |
|---|---|
| 规格符合性 | 大体符合;3 处用户联调授权偏离(注入块格式、poke 范围、emoji 白名单形式)+ 哨兵撤回未落地(规格授权退化路径) |
| 正确性 | 状态机(批次/冷却/缓存)边界总体正确;_settling 防双计有效;发现 1 个 memo 查询空参数语义缺陷(Major) |
| 错误处理 | 绝大多数异常显式暴露;发现 2 处违反「禁止静默 fallback」约束(模板缺失静默回退、get_recent 空返回静默) |
| 缓存纪律 | §4.10 落实到位:分层分条、版本化、稳定段前置、占位符进缓存键;基线 57.91% 未达 80% 目标但归因合理 |
| 安全 | SQL 全参数化、快照原子写、无概率行为;1 处路径拼接无校验(Minor,数据源可信) |
| 测试质量 | 引擎层覆盖扎实;plugin.py 接线层/hook 层基本无单测(缺口) |

---

## Major(必须修)

### M1. memo.read 空参数语义缺陷:空维度匹配所有空值行,跨流/跨用户读到无关备忘

- 位置:`catsitate_core/memo.py:76-83`(`read` 的 SELECT:`WHERE (stream_id = ? OR user_id = ?) AND expires_at > ?`)
- 问题:当某一维度参数为空串时,OR 条件变成「匹配该维度为空的所有行」,而非「不匹配」。
  - 实测验证:`read('', '')` 返回所有 stream_id='' 或 user_id='' 的备忘(跨用户);`read('s1', '')` 会把 user_id='' 的「无归属备忘」(可能是其它流/其它用户写入时缺字段产生的)一并带入 s1 流的注入与工具结果。
  - 触发链:① `memo_read` 工具参数全部可选(plugin.py:146-153),planner 只传一个维度或都不传时双空/单空命中;② 注入块 `self.memo.read(stream_id, "", limit=3)`(plugin.py:471)在 session_id 缺失时命中。
  - 与规格 §4.4「当前流相关 + 当前说话人相关」语义不符:空维度应视为「无此条件」,而非「匹配空值行」。
- 修法:进入 SQL 前归一化——两个维度都为空直接返回 `[]`;单维度为空时仅保留另一维度的条件(动态拼 WHERE)。并补测试:`read('','')`、`read('s1','')` 不返回无归属备忘。

### M2. 结算素材为空时无守卫:early 结算直接以空素材调 LLM 判定并落库

- 位置:`catsitate_core/favorability.py:339-356`(`SettleExecutor.settle` 中 `material` 空时不拦截)+ `plugin.py:699-703`(`_fetch_recent` 非 list 静默返回 `[]`)
- 问题:
  - `build_material` 返回 `[]`(get_recent 取不到消息、批次窗口过滤后无目标用户消息)时,`kind="early"` 直接构造「只有稳定段、无素材」的 prompt 调用 LLM,LLM 可能返回任意 delta 并落库——好感度被无依据修改;规格 §4.3 要求「材料构造失败:跳过本轮并记录日志」。
  - `_fetch_recent` 对 capability 返回非 list(错误 dict)时静默返回 `[]`,无任何日志,使上述空素材路径完全不可见(违反「错误必须显式暴露」)。
  - 触发概率低(get_recent 正常时,计数 ≥20 的消息必然在最近 200 条内),但一旦 API 异常即产生「静默 + 空判定落库」双重问题。
- 修法:① `settle()` 中 `material` 为空时返回 `{"status": "failed", "error": "素材为空"}`(或按 daily 顺延逻辑处理),不调 LLM;② `_fetch_recent` 对非 list 返回记录 warning 日志。

### M3. 旁路模板文件缺失时静默回退内置,违反「必须告警」全局约束

- 位置:`catsitate_core/llm_provider.py:70-77`(`load_side_system` 中 `path.stat()` OSError → `continue`,全程无日志)
- 问题:全局约束明确「文件缺失回退内置模板是规格允许的正常路径,但必须告警」;当前两个候选路径(`data/custom_prompts/zh-CN/`、`prompts/zh-CN/`)均不存在时静默返回内置模板,联调中「模板未部署」属默认常态,运维完全看不到回退发生。
- 修法:on_load 或首次调用时对每个模板做一次存在性探测,缺失时记一次 warning「旁路模板 X 未部署,使用内置默认」(避免每次调用都告警;模板文件随后被部署时,后续 stat 命中即恢复正常)。

---

## Minor(建议)

### 与规格的差异点(标注)

1. **注入块格式(用户联调授权)**——规格 §4.1 原设计「独立等级规则块(5 级全量)」;实机按联调决定改为「等级规则按等级单条注入好感度块最前」(`plugin.py:491-496`、`favorability.py:370-397`),并删除了 `BLOCK_ORDER` 中的独立 `level_rule` 块的实际使用(注入顺序变为 environment → memo → favorability)。cache-baseline 记录该优化缩短块长、提升命中。**授权偏离,按代码现状确认无问题。**
2. **poke 模块范围(用户联调授权)**——规格 §4.6 的入站解析增强(`enhance_notice_text`/`inject_to_context`)与 §6 的 `min_level_for_poke` 等级门槛均未实现,`poke.py:3-4` 注释「入站通知解析已按联调结论删除」「用户已取消好感度等级门槛」;config `PokeSection` 无对应字段。验收清单亦记录「入站戳一戳解析已按联调结论删除」。**授权偏离,标注。** 注意:README 与验收清单若仍引用旧行为需同步(未逐一核对 README)。
3. **emoji 白名单为内置表而非配置项**——规格 §6 的 `msg_react.emoji_whitelist` 未实现,改为内置 30 项精选表(`qq_emoji.py`,注释「用户精选 30 项」)。文档未明确记录为授权覆盖,建议在 README/CHANGELOG 补一句说明。
4. **哨兵层撤回未落地**——规格 §4.7「不符则撤回并闭环反馈」;`plugin.py:398-399` 撤回动作仅日志。spike ④ 仍未运行时验证,当前形态属于规格授权的退化路径(「不能则仅日志观测」)。**建议二期或 spike ④ 验证后实现;当前不阻断。**

### 其它 Minor

5. **调度周期不随热重载更新**——`plugin.py:107-110` 的 scheduler 注册间隔取 on_load 时的配置;`on_config_update`(124-136 行)只清缓存,`weather_refresh_minutes`/`window_hours` 修改后需重启才生效,与规格 §6「热重载支持」不完全一致。修法:on_config_update 里对受影响任务 unregister + 按新值 register。
6. **lunar-python 顶层 import 硬失败**——`time_aware.py:8` `from lunar_python import Solar` 在模块顶层,依赖未装上时整个插件加载失败;规格 §10 预期「安装失败时农历节日/节气缺失(日志可见)」的降级。修法:try/except ImportError 包裹,降级为无农历数据 + 显式告警(与 `holiday_calendar` 在 `_refresh_environment` 内的延迟 import 风格保持一致)。
7. **好感度计数不区分消息类型**——`fav_count`(plugin.py:333-350)对 chat.receive.after_process 的全部入站消息计数;带 `user_info` 的系统通知(如戳一戳通知)会被计入批次。建议过滤 `is_notify`/非用户消息,避免通知类消息污染计数。
8. **daily_settle_min 顺延判断统计口径偏宽**——`favorability.py:340` 用 `"(用户)" in m` 统计素材中所有用户消息(含群聊紧邻上下文的其它用户消息),与规格「批次消息数(该用户)」口径不一致;群聊中目标用户 1 条 + 邻居 2 条时不顺延。建议改为统计 `user_id == 目标` 的消息条数。
9. **日终结算按自然日去重,与 window_hours 配置语义不完全一致**——`has_daily_settle_today`(favorability.py:268-283)用 `judge_id LIKE 'daily-YYYY-MM-DD%'` 每天最多 1 次日终;window_hours 配成 <24h 时预期频率被自然日限制。默认 24h 下无影响。
10. **inspect_image 路径拼接无校验**——`plugin.py:271` `Path("/MaiMBot") / str(db_result["full_path"])`:绝对路径会覆盖前缀(`Path` 语义),`..` 可逃逸。full_path 来自主程序 Images 表(可信写入方),风险低;建议加「非绝对路径且不含 `..`」校验兜底。
11. **`_normalize_ts` 解析失败静默排除**——`plugin.py:743-756`:异常时返回原始字符串,与 ISO `window_start` 比较恒不匹配 → 该消息被静默排除出素材;且该函数无单测。建议解析失败时记日志。
12. **_persona 空值兜底无告警**——`plugin.py:830`:config.get 返回空串时不告警直接兜底「猫耳少女」,与 docstring「为空时显式告警」不符。
13. **内置 favorability 模板无 `{{delta_max}}` 占位符**——`llm_provider.py:20-25` 内置模板硬编码「-5 到 5」,而容器部署模板带占位符;未部署模板时 `delta_max` 配置静默不生效。建议内置模板也加占位符,与部署模板行为一致。
14. **测试缺口:plugin.py 接线层无单测**——tests/ 覆盖引擎层扎实,但 `_to_snapshot_item` 快照格式(联调关键契约)、`_settling` 并发防双计、`_normalize_ts`、`_side_llm_call`/`llm_usage` 记账、`_persona` 缓存、fav_count 触发、cmd_memo、sentinel_check/reply_backfill hook 入口均无测试(集成测试只覆盖引擎装配)。建议对纯逻辑部分(快照 item 结构、speaker 解析、记账)补单测。
15. **`_manifest.json` sdk min_version 声明为 2.7.1**——约束为仅用 maibot-plugin-sdk 2.8.0;min_version 放宽到 2.7.1 存在与 2.8.0 API(如 `maibot_sdk.types` 导入)不兼容的声明窗口。建议改为 2.8.0。
16. **test_msg_react.py 注释误导**——`tests/test_msg_react.py:10` 注释「内置 QQ 表情表(0 惊讶 / 1 撇嘴 / 2 色)」与实际 30 项精选表不符(0/1/2 不在表内),注释与断言语义相反。

---

## Pass(确认无误的重要点)

- **批次结算状态机**:计数走 SQLite 原子 `count+1`;daily cap(≤3)+ 日终兜底 + 顺延不丢弃消息的语义正确;`judge_id` 幂等(INSERT OR IGNORE);失败不落库不重置(有测试)。
- **并发防护**:`_settling` 集合在 fav_count 触发与 `_daily_settle` 之间防同批次双计;后台任务引用集合 + done 回调 discard + exception 上报完整,`on_unload` 正确 cancel 与 gather。
- **时间处理**:epoch 浮点 → ISO 归一化与 `window_start` 同格式,字符串比较语义正确(联调发现并修复过);`build_material` 按 `ts > window_start` 过滤批次内消息正确。
- **JSON 解析容错**:`parse_judge_response`/`parse_choice_resp`/`parse_sentinel_response` 三个解析器均处理 markdown 围栏、裸花括号、合法非对象 JSON、字段类型校验;`note`/`delta` 钳制与注记截断在落库前强制。
- **安全**:所有 SQL 参数化;JSON 快照原子写(tempfile + os.replace);无概率行为(随机数仅冷却/限频护栏);冷却状态按流/按用户隔离。
- **缓存纪律(§4.10)**:注入块前插 system 后、历史前;分层分条按稳定性降序(environment → memo → favorability);`_snapshot_cache` 文本键复用同一快照对象;旁路 prompt 稳定段前置、模板版本化 + 文本哈希 + 占位符替换值全部进 cache_key(模板/占位符变化即失效);memo/fav 注入块以语义键驱动重渲染。
- **缓存基线**:57.91% 未达 ≥80% 目标,但报告对断点(主程序每轮变化的动态块)归因充分,插件侧可影响部分全部落实;结论「主程序瓶颈单独报告」路径清晰,不构成本插件缺陷。
- **API 契约贴合实机**:`send_poke(user_id, group_id, target_id)`、`{'status':'ok'}` 判定、`message_info.user_info.user_id`、`raw_message` 段 `data` 直接为字符串、`processed_plain_text`、快照 `item_type` 定位 system、`ctx.maisaka.context` 未误用——均与 spike/联调记录一致。
- **生命周期**:on_load 失败报错拒绝加载;on_config_update 订阅 `bot`(personality 变化使注入与哨兵人设缓存失效);enabled 总开关在各入口正确短路。
- **约束符合**:未修改主程序(prompts/zh-CN/ 模板部署为授权数据部署);仅用 SDK 2.8.0 所列能力(无 @Action);用户可见文本简体中文;`HookMode`/`HookOrder`/`ToolParameterInfo` 自 `maibot_sdk.types` 导入。
- **测试执行**:92 个测试全部通过(0.36s);断言普遍有实质检查(顺序、截断、幂等、边界如锚点流首不回绕)。

---

## 总评

**有条件通过。** 三个 Major 均为可小改动修复的缺陷(M1 空参数查询语义、M2 空素材结算守卫 + 静默空返回、M3 模板缺失静默回退),修复后无需重跑联调即可放行。规格符合性良好,与规格的差异点全部有据可查(联调授权或退化路径);状态机、并发、时间归一化、缓存纪律等核心设计经代码与 92 个测试双重确认无误。建议在修复 Major 的同时顺手处理 Minor 5(热重载周期)、6(lunar-python 降级)、7(计数过滤),这三条对真实部署体验影响最大。
