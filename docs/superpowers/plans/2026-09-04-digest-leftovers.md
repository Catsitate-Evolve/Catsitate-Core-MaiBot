# 见闻修复遗留清理(2026-09-04 批准:「先处理遗留」)

## 背景

上一轮「见闻素材统一 24h 滚动窗」(07acd30..a0a7bcf)完成后,终审与分诊留下四项遗留,用户指示先行处理:

1. **`fav_events_on` 删除**:生产代码无调用方(仅测试消费,验证 `fav_event()` 写入/去重语义用)。删除需重接 4 处测试 + 清理引用它的注释。
2. **plugin.py:235、1484 两处「当日」措辞张力**:与 24h 滚动窗口径不一致(终审 nit,235/1484 在上轮修复清单外)。
3. **24h 窗上界未测**:若 `since` 回归放宽(如 48h)现有测试仍绿——补「now−25h 事件不进素材」断言。
4. **`qzone_digest` 模板「回想一下今天」措辞**:与 24h 滚动素材有语义张力——改「最近」,模板版本 v2→v3(人工约定,CHANGELOG/文档在跟踪版本号)。

附带(终审 Minor,本次顺带):`fav_events_window` docstring 补「见闻素材消费方」一句(本次触碰该文件)。

## 已核实的事实(实现者直接依赖,不必重查)

- 模板重部署机制:prompt_deploy.py 按内容比对(目标文件内容不同即重写,下次 on_load 生效),**不靠版本号**;`_version_tag` = 内置版本号+文本哈希,文本变即缓存失效。改模板无需任何部署侧改动。
- 模板三层读取顺序:data/custom_prompts(WebUI 编辑)> prompts/zh-CN(部署)> 内置——已是文档化行为,本次不改。
- `fav_events_since(user_id, since_iso)`:排他下界 `created_at > since`,升序;`fav_events_window(since_iso)`:含等下界 `created_at >= since`,升序,全用户。
- `fav_events_on` 测试消费点(共 4 处调用 + 2 处注释):
  - tests/test_qzone_comment_seen.py:30(`test_fav_events_roundtrip`)
  - tests/test_qzone_comment_seen.py:166(`test_fav_event_same_day_dedup`)
  - tests/test_qzone_comment_seen.py:207-223(`test_fav_events_on_today_misses_last_night_but_since_yesterday_gets`,整测即锁定该方法语义)
  - tests/test_qzone_wiring.py:1715(源C 赞事件记账断言)
  - tests/test_settlement.py:60、419(注释引用)
  - plugin.py:3955(注释引用)
- 模板原文「回想一下今天在QQ空间的事」出现在:prompt_templates/catsitate_qzone_digest.prompt:1、llm_provider.py:150(内置)、tests/test_llm_provider.py:123(断言)、docs/dev/modules/qzone-express.md:52(文档引用,含 v2 字样)、CHANGELOG.md:107(**历史条目,不改写**)。

## 全局约束

- 只改插件目录(`MaiBot-dev/plugins/catsitate_core_maibot/`),MaiBot 主程序只读。
- 简体中文注释/文档/日志;注释禁一头雾水(不得引用已删方法当现状);历史记录不改写(CHANGELOG 旧版本条目、docs/superpowers 计划快照不动);错误显式暴露。
- 零行为变化原则:本轮全部是删死代码、注释/模板措辞、补测试——**不改任何生产逻辑**(`fav_events_on` 本就无生产调用方;模板文本变化属措辞非逻辑)。
- 模板两侧必须同步改:prompt_templates/catsitate_qzone_digest.prompt 与 llm_provider.py SIDE_TEMPLATES `qzone_digest`(有同步测试锁定 builtin==file)。

## Task 1:代码+测试+模板

### 1a. comment_seen.py

- 删 `fav_events_on` 方法(约 185-200 行)。
- `fav_events_window` docstring(219-224 行)补一句消费方:除日终候选并集(C-N1)外,亦作见闻素材取数(2026-09-04,近 24h 滚动窗)。措辞精炼,对齐周边注释密度。

### 1b. 测试重接(tests/)

- `test_fav_events_roundtrip`(约 24-33 行):`s.fav_events_on(today, "10001")` → `s.fav_events_since("10001", (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S"))`(登记时刻为 now,昨日下界必取到;升序=登记序,`rows[0]["kind"]=="COMMENT"` 断言语义保持)。文件若未导入 timedelta 则补。
- `test_fav_event_same_day_dedup`(约 156-167 行):同款改法;`len(rows)==3` 断言不变。
- `test_fav_events_on_today_misses_last_night_but_since_yesterday_gets`(约 207-223 行):**整测删除**(锁定的方法已删);若 docstring 之外的测试(如 191 行 docstring 提及 fav_events_on 的测试)仅注释提及,一并把措辞改为不引用已删方法。
- tests/test_qzone_wiring.py:1715:`p.qzone_comment_seen.fav_events_on(today, "20000")` → `p.qzone_comment_seen.fav_events_since("20000", (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S"))`(该文件已导入 timedelta 与 datetime);`any(...)` 断言不变;行上方注释若提及自然日语义需同步措辞。
- tests/test_settlement.py:60、419 注释:改为不引用已删方法(如「旧自然日取数」或直接对比 fav_events_since),断言不动。

### 1c. plugin.py 注释清理

- 235 行:「空间见闻(read_qzone 窗口结束旁路 LLM 摘要的当日印象);持久化跨重启引用」→「当日印象」改为与 24h 滚动素材不矛盾的定性(如「空间印象」),其余保留。
- 1484 行:「窗口边界把当日浏览+互动摘要为空间见闻」→「窗口边界把近 24h 滚动窗内的浏览与互动摘要为空间见闻」。
- 3955 行注释:引用 `fav_events_on` 的表述改为不引用已删方法的等义说法(保留 H-2 历史语义说明)。

### 1d. 24h 窗上界测试

扩展 tests/test_qzone_wiring.py 的 `test_digest_fav_events_24h_rolling_window_catches_last_night`:在现有 now−1h 事件(day=昨日)之外,再直插一条 `created_at = now − timedelta(hours=25)`、`day = (now − 25h).date()`、文本如 `"前夜陈年评论:不该进素材"` 的事件;断言该文本**不出现**在捕获的素材段(锁 24h 上界,防 since 放宽回归)。docstring 补一句上界锁定说明。

### 1e. 模板措辞 v2→v3

- prompt_templates/catsitate_qzone_digest.prompt:「回想一下**今天**在QQ空间的事」→「回想一下**最近**在QQ空间的事」,其余逐字不动。
- llm_provider.py SIDE_TEMPLATES `qzone_digest`:system 同步同改;`"version": 2` → `"version": 3`。
- tests/test_llm_provider.py:123:断言短语同步为「回想一下最近在QQ空间的事」。

### 验证与提交

- `python3 -m pytest tests/ -q` 全绿(基数 567 − 删 1 测 = 566 预期,以实际为准)。
- 全仓检索 `fav_events_on`(排除 .superpowers、docs/superpowers 计划快照、CHANGELOG 历史条目):零活引用。
- 单 commit:`chore(qzone): 遗留清理——删 fav_events_on、注释口径对齐、24h 窗上界测试、见闻模板 v3「最近」措辞`。

## Task 2:文档同步

前提:Task 1 已合入。

- docs/dev/modules/qzone-express.md:52:「模板 `qzone_digest` v2:「回想一下今天在QQ空间的事……」」→ v3 + 「最近」新短语(仅改版本号与引用短语,其余不动)。
- CHANGELOG.md「未发布(将并入 v1.0.0)」段:追加清理条目——`fav_events_on` 查询删除(生产无调用方)、见闻模板 v3(「今天」→「最近」)、见闻素材 24h 窗上界测试与注释口径对齐。对齐该段既有子分类风格(现有「修复」小节;若维护类内容放修复小节措辞别扭,可按文件既有风格加合适小节,没有先例就并入修复并在条目里写明是清理)。
- 全仓检索 `fav_events_on` 与 `回想一下今天`(md 文档范围):确认无活引用(CHANGELOG 历史条目、计划快照豁免);plugin-manual.md 若提及 fav_events_on 一并同步。
- 单 commit:`docs: 遗留清理同步——见闻模板 v3 措辞与 fav_events_on 删除`。
