# 见闻素材统一 24h 滚动窗 + 截断保留最新(2026-09-04 已批准)

## 背景

用户在翻阅文档时发现两处见闻(`qzone_digest`)素材口径缺陷并已批准修复方案:

1. **截断方向不一致**:浏览素材 `recent_seen(limit=15, days=1)` 按注入时间倒序(保留最新),互动素材 `fav_events_day(day)` 按 id 升序 + `events[:10]`(保留最早)。当日量大时前者"忘掉上午刷的"、后者"丢掉下午互动的"。
2. **跨零点窗口 bug**:见闻生成的互动素材按生成时刻的自然日查(`fav_events_day(今天)`),read_qzone 窗口跨零点(如 23:50–00:30)结束时零点后生成,新日无事件 → 互动素材全空;见闻日期归属错位。

**裁定方案**(对齐既有跨零点模式,零新状态):统一到 C-N1 式固定回看窗锚——互动素材改 `fav_events_window(now - 24h)`,与浏览侧 `days=1` 同窗长同锚型。见闻语义从"自然日印象"重新定性为"近 24h 滚动印象"(单份快照每日覆盖,滚动窗让覆盖连续,自然日切割会在 00:00 制造"遗忘缝")。H-2 当时"见闻保留自然日"的旧裁定翻案。

## 全局约束

- 只改插件目录(`MaiBot-dev/plugins/catsitate_core_maibot/`),MaiBot 主程序只读。
- 用户可见文本/日志/注释/文档一律简体中文。
- 错误显式暴露:禁止静默 fallback;回退必须告警。
- 死代码即删;注释禁一头雾水(引用已删方法的注释必须同步修)。
- **不动**:`fav_events_window`/`fav_events_since` 的排序与语义(有 H-2 测试锚定);见闻 `date` 字段与注入门 `date == today`(生成时刻当日);浏览素材调用 `recent_seen(limit=15, days=1, now=now)`;`_qzone_generate_digest` 函数签名。
- **不扩权**:`fav_events_on` 生产代码无调用方(仅测试用),本次**不删**(不在批准范围;其删除需重接 4+ 处测试),仅在最终报告中作为后续清理候选上报。
- 注释中不得保留对已删 `fav_events_day` 的引用。

## Task 1:代码修复与测试

### 1a. plugin.py `_qzone_generate_digest`(约 2850–2897 行)

现状(2863–2875 行):

```python
now = datetime.now()
day = now.strftime("%Y-%m-%d")
seen = self.qzone_seen.recent_seen(limit=15, days=1, now=now)
lines = [
    f"{e['author_nickname'] or e['author_uin']}发了「{clip_text(e['summary'] or '图片', 20)}」"
    for e in seen
]
try:
    events = self.qzone_comment_seen.fav_events_day(day)
except Exception:
    self.ctx.logger.exception("QQ空间见闻素材(互动事件)读取失败,本轮按空处理")
    events = []
lines += [clip_text(e["text"], 40) for e in events[:10]]
```

改为:

- 互动素材查询:`fav_events_day(day)` → `fav_events_window((now - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S"))`(`timedelta` 已在 plugin.py:9 导入;ISO 格式必须与 `comment_seen._ISO` 的 `%Y-%m-%dT%H:%M:%S` 一致,ISO 字符串比较)。
- 截断:`events[:10]` → `events[-10:]`(升序取尾 = 保留最新 10 条,与结算路径 `events[-5:]` 同款)。
- 注释:在素材查询处加一行简体中文注释说明"素材锚点统一为近 24h 滚动窗(浏览 recent_seen days=1 与互动 fav_events_window 同窗),跨零点会话昨晚素材自然衔接,不按自然日切割(2026-09-04 翻案 H-2 旧保留裁定)";在截断处注释"截断统一保留最新"。注释密度对齐周边(周边是密集中文注释风格),不写超长论文。
- `day` 变量、`stable_ctx`、快照保存、告警文案全部不动。

### 1b. comment_seen.py 删 `fav_events_day`

- 删 `fav_events_day` 方法(236–242 行)——唯一生产消费方是见闻生成,已改走 `fav_events_window`。
- 修 `fav_events_on` docstring(186–190 行)中对 `fav_events_day` 的过时引用:"自然日语义(H-2 裁定保留):见闻生成 fav_events_day 同口径,按登记时写入的 day 匹配;结算取数勿用本方法——跨零点结算时昨晚事件 day=昨日会漏,改走 fav_events_since 滚动窗。" → 改写为不引用已删方法、且如实反映现状的版本(说明本方法为自然日查询、见闻素材已改 fav_events_window 滚动窗、结算走 fav_events_since)。

### 1c. 测试

`tests/test_qzone_comment_seen.py`:

- 删 `test_fav_events_day`(145–153 行)。

`tests/test_qzone_wiring.py`(M3 见闻系统测试区,2241 行起):

- `test_digest_generated_on_window_end`(2244 行)签名未变无需大改,但在源码断言区(2269–2275 行)追加一行 `assert "fav_events_day" not in src`(死代码删除的防回归断言)。
- 新增 `test_digest_fav_events_24h_rolling_window_catches_last_night(tmp_path)`:跨零点测试。`p = _make_plugin(tmp_path)`;直插一行 `day=昨日、created_at=now-1h` 的事件(压缩模拟"零点前登记、零点后生成",任何时刻跑都确定):

  ```python
  now = datetime.now()
  yday = (now - timedelta(days=1)).strftime("%Y-%m-%d")
  at = (now - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S")
  p.qzone_comment_seen.store.execute(
      "INSERT INTO qzone_fav_events (day, user_id, kind, text, created_at) "
      "VALUES (?, '10001', 'COMMENT', '昨夜评论了你的说说:好看', ?)", (yday, at))
  ```

  桩 LLM 捕获 messages(closure 列表),预置 `p._persona_cache`/`p._style_cache`(对齐 2262–2264 行既有模式),`asyncio.run(p._qzone_generate_digest())`,断言素材段(captured messages 全文拼接)包含 `"昨夜评论了你的说说:好看"`,且快照 `date` 为今天。docstring 说明:旧代码按生成日自然日查(day=昨日取不到)、新代码 24h 窗取到。
- 新增 `test_digest_fav_events_truncated_to_latest_10(tmp_path)`:截断方向测试。循环直插 12 条 `text=f"事件{i:02d}"`、created_at 自 `now-30min` 起每条 +1 分钟递增(全部落在 24h 窗内,升序可分辨);同上桩捕获;断言素材包含 `事件03`..`事件12` 的文本、不包含 `事件01`/`事件02`。
- 两个新测试的 docstring 一律简体中文,写明裁定日期与语义(对齐本文件既有测试风格)。

验证:`cd MaiBot-dev/plugins/catsitate_core_maibot && python -m pytest tests/ -q` 全绿(原 566 项基数:-1 删、+2 增 ≈ 567 项,以实际为准)。

提交:一个 commit,`fix(qzone): 见闻素材统一近 24h 滚动窗+截断保留最新(跨零点修复,H-2 自然日旧裁定翻案)`。

## Task 2:文档同步

前提:Task 1 已合入。

- `docs/dev/modules/qzone-sense.md`:找到见闻(`qzone_digest`)小节,素材口径改为"近 24h 滚动窗(浏览 `recent_seen(days=1)` 与互动 `fav_events_window(now-24h)` 同窗)、截断统一保留最新(浏览 15 条/互动最新 10 条)";写明跨零点会话昨晚素材自然衔接、见闻语义为"近 24h 滚动印象"(H-2 旧自然日裁定 2026-09-04 翻案)。按实际情况写,先读代码再落笔。
- `docs/plugin-manual.md` 380 行见闻条目:把"当日素材(近 1 天已 seen 的 15 条动态叙事+当日 `qzone_fav_events` 前 10 条互动文本)"同步为新口径(近 24h 滚动、互动取最新 10 条);检查全文其它提及见闻素材口径处一并同步(如日志关键词 423 行仅涉及读取失败告警,措辞不变则不动)。
- `CHANGELOG.md`:顶部若无未发布段,新建「未发布(将并入 v1.0.0)」段,在"修复"下记一条:见闻互动素材由自然日改近 24h 滚动窗(跨零点会话互动素材不再切空),素材截断方向统一保留最新;`fav_events_day` 查询删除。简体中文,对齐该文件既有条目风格。
- 全文检索 `fav_events_day`:文档/注释中不得残留对它的活引用(历史条目/CHANGELOG 旧版本记录除外——历史记录不改写)。

提交:一个 commit,`docs: 见闻素材口径同步 24h 滚动窗与截断语义`。
