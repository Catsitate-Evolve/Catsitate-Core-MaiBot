# QQ空间 M3（表达）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax.

**Goal:** 交付 M3 表达四项：`qzone_post` 工具（bot 自主发说说）+ 回注（self 消息注入虚拟流）+ 日记（入睡旁路 LLM 生成+直发+延迟回注）+ 真实聊天见闻摘要注入。

**Architecture:** 说说发布走工具（`qzone_post(content)`，与 qzone_comment 同模式）；日记走旁路 LLM（入睡任务扩展，`catsitate_qzone_diary.prompt` 模板）；两者发布后均回注虚拟流 self 消息（无 is_mentioned，仅入历史）。见闻摘要走现有 inject 框架（真实聊天流的 qzone 注入块）。

**Spec:** §3.8（原设计）+ 工具驱动架构适配（本轮修订）

## Global Constraints
- 只改插件目录；简体中文；错误显式暴露；SDK-only；测试无网络
- 回注消息**不带 is_mentioned**（bot 无需对自己的说说强制触发轮）
- 睡眠绝对静默例外清单从「次日日程生成」扩为「入睡任务（日程+日记+发布）」
- 不考虑开发版数据迁移

---

### Task 1: 发布 API（wire + client）

**Files:** wire.py, client.py; Test tests/test_qzone_wire.py, tests/test_qzone_client.py

- `wire.py` `build_publish_form(*, content: str, bot_uin: str) -> dict`——端点 `emotion_cgi_publish_v6`，参数集参照 Maizone `publish_emotion`（`con=content, photos=''` 简单文本说说）
- `client.py` `async def do_publish(self, *, content: str) -> bool`——`_post(PUBLISH_URL, form, referer_uin=bot_uin)`，返回 True/抛出

TDD: 表单字段断言 + client 请求/响应解析。

### Task 2: qzone_post 工具 + 回注

**Files:** plugin.py, messages.py; Test tests/test_qzone_wiring.py

- `@Tool("qzone_post", ...)` 参数 `content: string`（≤500 字），硬门控同 qzone_comment
- 成功后：
  1. `client.do_publish(content=...)` 
  2. 回注：`route_message` 构造 self 消息（`user_id=bot_uin, nickname=bot 昵称, platform=qzone-qq, group_info 同其他`），`raw_message=[{"type":"text","data":content}]`，**无 is_mentioned**，timestamp=publish 时刻
  3. registry 登记（owner=bot, kind="self_post"）——后续好友评论此说说时，工具能解析
  4. 日志：`QQ空间说说发布成功: {前30字}`
- AuthError/Exception 处理同 qzone_comment

TDD: 工具行为测试（成功→do_publish+route_message 被调/失败/长度校验）。

### Task 3: 日记（入睡任务扩展）

**Files:** plugin.py, llm_provider.py, prompt_templates/; Test tests/test_qzone_wiring.py

- `SIDE_TEMPLATES` 增 `"qzone_diary"`（version 1），prompt_templates/ 建 `catsitate_qzone_diary.prompt`
- 模板要点：以 bot 视角写一篇空间日记（当日生活回顾：日程执行/聊天见闻/空间见闻/备忘提醒），风格自然不刻意，80-200 字
- `plugin.py` `_enter_sleep()` / `_maybe_settle_passed_sleep_window()` 中，日程生成后追加：
  ```python
  self._spawn_background_task(self._generate_and_publish_diary())
  ```
- `_generate_and_publish_diary()`:
  1. 组装素材（今日日程执行概览+备忘+空间见闻+近期聊天摘要）→ `build_side_prompt("qzone_diary", stable_ctx, variable_tail)` → `_side_llm_call`
  2. LLM 产出日记文本 → `client.do_publish(content=...)` 直发（API 不经消息链，不受睡眠拦截）
  3. 日记文本+发布时刻存入 `_pending_diary_echo`（JsonSnapshot）——醒来后首个 tick 补注
- 醒来后补注：`_sleep_tick` 检测醒态时，读 `_pending_diary_echo` → `route_message` self 消息 → 清空
- `config.py` qzone 节增 `diary_enabled: bool = _f(True, "日记功能开关(入睡时生成并发布空间日记)", label="日记开关")`、`diary_llm_model`/`diary_llm_timeout_ms`

TDD: 模板存在 + 入睡生成调用链 + 醒来补注 + 配置开关。

### Task 4: 见闻摘要 + 场景更新 + 收尾

**Files:** plugin.py, scene.py, config.py; Test 全量

- `_qzone_block` 真实聊天分支：`recent_seen` 非空时输出 `[空间] 近期刷到: {昵称}发了「{摘要}」; …`（最多 summary_count 条）——当前已有此分支但可能未激活，验证+格式优化
- 场景 prompt（qzone_scene v3）：追加「你也可以发自己的说说（qzone_post）」+ 工具参数说明
- 白名单默认值加 `qzone_post`
- manifest 0.8.0 + CHANGELOG + manual + CONTEXT + milestone-map 更新

TDD: 见闻摘要注入块测试 + 白名单默认值。

## Self-Review
- 冲突排查：qzone_post 发布后的回注消息经 route_message 进虚拟流→planner 可见→后续评论有上下文 ✓
- 日记在睡眠中生成（旁路 LLM + API 直发，均不受消息链拦截）✓；回注延迟到醒来（防 sleep_gate abort）✓
- 见闻摘要复用现有 inject 框架与 recent_seen 数据，零新基建 ✓
- 睡眠例外清单扩展须同步 CONTEXT/manual（二期语义「唯一 LLM 调用=日程生成」→「入睡任务」）✓
