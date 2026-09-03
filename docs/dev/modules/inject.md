# 上下文注入(inject)

> 源码:`catsitate_core/inject.py`(64 行,组装器)+ `plugin.py` 中 `inject_blocks` / `_build_inject_blocks` / `_to_snapshot_item` 一组方法(接线与块构建)。

## 一、模块职责与生命周期

**注入块**是本插件影响 bot 行为的核心手段:planner 每次要向 LLM 发请求前,插件在其上下文(**system 消息之后、聊天历史之前**)插入若干条结构化的 `role=user` 消息,内容是 bot 此刻"应当知道"的事实——

- 现在几点、什么节日、天气如何(**环境**);
- 按日程现在处于哪个窗口、接下来做什么(**日程**);
- 刚刷到的好友动态、当日空间见闻(**空间**);
- 与当前说话人相关的未过期备忘(**备忘**);
- 与当前说话人的关系等级及行为规则(**好感度**)。

设计立场:注入只提供**事实**,不提供指令倾向——bot 拿到"[备忘] 周四交作业"后怎么用是 planner 的事(见 [../philosophy.md](../philosophy.md) "工具 > 替代 LLM 做选择")。

**生命周期**:`InjectAssembler` 在 `on_load` 实例化;配置热重载(`on_config_update`,self 与 bot 两个 scope)时 `assembler.reset()` 清空渲染缓存(人设/配置变了,旧渲染作废);快照项缓存 `_snapshot_cache` 同步清空。

## 二、完整逻辑

### 总流程(`inject_blocks`,钩子 `maisaka.planner.before_request`,BLOCKING/LATE)

```
开关检查(plugin.enabled + inject.enabled,关则原样 continue)
  → _build_inject_blocks(kwargs)         构建本轮各模块的 InjectionBlock
  → messages = kwargs["items"]           主程序载荷键是 "items"(快照格式)
  → [虚拟流] apply_scene_surgery         场景替换 + deferred 剥除(见文末)
  → [虚拟流] filter_qzone_tools_for_stream  工具定义白名单过滤
  → assembler.render(blocks)             定序 + 渲染缓存 → [{"role":"user","content":...}]
  → _to_snapshot_item(每条)             转成主程序要求的快照 item
  → _system_tail_index(messages)         定位 system 尾部
  → 拼回 new_kwargs 返回 {"action":"continue","modified_kwargs":new_kwargs}
```

任何一步抛异常都被最外层 `try/except` 接住:**只记日志,原样放行**(见限制清单)。

### 注入块模型(inject.py)

`InjectionBlock` 是冻结 dataclass,三元组:`module`(块类别)、`content_key`(语义键,用于缓存比对)、`text`(完整块文本,含 `[环境]`/`[备忘]` 等标签前缀)。

`InjectAssembler.render()` 做三件事:

1. **定序**——按 `BLOCK_ORDER = ("level_rule", "environment", "schedule", "qzone", "memo", "favorability")` 排列,顺序按内容稳定性降序(越稳定的越靠前)。`level_rule` 槽位保留但已不再单独产块——等级规则并入好感度块首行(见下),实际每轮最多 5 块;
2. **每模块一块**——同一 `module` 出现两块直接抛 `ValueError`(调用方错误显式暴露,不静默覆盖);不在 `BLOCK_ORDER` 里的模块块被丢弃;
3. **渲染缓存**——缓存键 = `module|content_key|text`(含全量文本),命中则**字节级复用上一轮的同一消息对象**并刷新 LRU 新近度;上限 `CACHE_MAX = 512`,超限逐最旧。这个"前缀缓存纪律"的意义:内容不变的块每轮生成完全相同的文本,LLM 服务端的前缀缓存才能命中,省 token 省时延。

### 各注入块的构建(`_build_inject_blocks`)

先确定**说话人**:`stream_id` 取 `kwargs["session_id"]`;说话人 = 虚拟流 ? `qzone_injector.awaiting_author`(当前注入的动态作者) : `_resolve_speaker(stream_id)`。`_resolve_speaker` 的逻辑:经 `chat.get_all_streams` 建流信息缓存(10 分钟 TTL),私聊流取对端 `user_id`;群聊取最近 3 条消息里最新的非 bot 发送者(取数失败告警后回退流信息里的 `user_id`)。

| 块 | 构建函数 | 开关 | 文本形态 | content_key |
|---|---|---|---|---|
| environment | `_environment_block` | `inject.environment_enabled` + `time_aware.enabled` | `[环境] 今天 9月3日 周三;杭州:晴,26°C;节日:…;临近:…。` | `"env"` |
| schedule | `_build_inject_blocks` 内联 | `schedule.enabled` + `time_aware.enabled` + `memo.enabled` | `[日程] 写代码;接下来:睡觉;备忘:周四交作业(该窗口已过)(正在刷QQ空间)` | `sch:{窗口start}|{是否已触发}` |
| qzone | `_qzone_block` | `qzone.enabled` + 模块可用 | 三分支,见下 | 按分支携带状态摘要 |
| memo | `_build_inject_blocks` 内联 | `inject.memo_enabled` + `memo.enabled` | `[备忘] 内容1;内容2`(取数与截断均按 `memo.inject_max`) | `memo:{条目id集合}` |
| favorability | `favorability.build_favorability_block` | `inject.favorability_enabled` + `favorability.enabled` | `[好感度] 规则「熟悉」:…。` + `[好感度] 10001:等级「熟悉」(累计 120),注记:…。`(规则行仅 `inject.level_rule_enabled` 时有;无记录渲染为「陌生」) | `fav:{说话人}` |

要点:

- **环境块是纯缓存读**:文本由后台任务 `_refresh_environment`(节日双源 + Open-Meteo 天气 → `time_aware.build_environment_text`)预生成进 `_env_cache`,TTL 即 `time_aware.weather_refresh_minutes`;数据未就绪时 `_environment_block` 返回 `None`,块不注入(首轮后台填充后自动出现),**不编造数据**。
- **日程块**:取 `current_window`(非 sleep 窗口),拼"接下来"(下一窗口,睡觉窗口显示「睡觉」)、当日到期备忘(前 3 条,来自 `memo.due_on`)、`read_qzone` 窗口追加"(正在刷QQ空间)"——让 planner 知道此刻"刷手机"具体在刷什么;已触发过的窗口追加"(该窗口已过)"。
- **空间块三分支**(`_qzone_block`):①虚拟流 → `[空间] {describe_current() 当前浏览状态}`;②真实流且当日有见闻摘要(`qzone_digest.json`)→ `[空间见闻] {摘要}`;③否则回退 `[空间] 近期刷到: 昵称发了「摘要」;…`(取自 `qzone_feeds` 表 recent_seen,纯图说说以「图片」占位)。
- **备忘块**:`memo.read(stream_id, speaker)` 是 OR 语义(流相关 ∪ 说话人相关,说话人维度含主 QQ 与附带 QQ);结果按条目 id 去重(防御)后截断到 `inject_max`。
- **好感度块目标**:说话人,空则回退 `kwargs["peer_id"]`;两者都空则不注入。

### 快照格式(`_to_snapshot_item`)

主程序 planner 载荷里的消息是**快照 item** 而非朴素 dict——朴素 `{"role","content"}` 会被主程序直接拒绝。渲染输出的每条文本须转成:

```python
{
    "item_type": "UserMessageItem",
    "meta": {
        "item_id": "catsitate-inject-" + sha256(text)[:16],  # 稳定 id:不能用内置 hash()(进程内随机化)
        "logical_turn_id": None,
        "timestamp": 创建时刻的 isoformat,                     # 随对象绑定,缓存复用不刷新
    },
    "parts": [{"type": "text", "text": 块文本}],
}
```

**同文本返回同一对象**(缓存键 = 文本本身,`_snapshot_cache` LRU 上限 `SNAPSHOT_CACHE_MAX = 256`):`item_id` 稳定 + 对象恒等,同样是前缀缓存纪律的一部分;文本键无界增长,超限逐最旧。

**插入点** `_system_tail_index`:items 没有 `role` 字段,按 `item_type == "SystemMessageItem"`(兼容 `role == "system"`)定位,**插在最后一条 system 之后**;找不到时告警并回退追加到列表尾部。

### 虚拟流的特殊注入(qzone 流)

注入钩子对 qzone 虚拟流会话额外做两件事,普通聊天流跳过:

1. **场景替换** `qzone/scene.py → apply_scene_surgery`:system 首项文本里,把主程序配置 `chat.reply_style.group_chat_prompt` 的**当前值**原位替换为空间场景文案(教模型"你正在刷QQ空间,互动要走工具")。按配置值匹配(用户改过配置也能命中);同时剥除独立的 `<system-reminder>` 提醒项。场景文案经三层链读取(WebUI `custom_prompts` → 主程序 `prompts` → 插件内置),替换状态三值:`replaced` / `empty_config`(配置为空,告警)/ `miss`(配置非空但未命中,主程序模板改版风险,告警)。replyer 侧的请求组装是另一份载荷,场景替换在 `qzone_replyer_scene` 钩子(`maisaka.replyer.before_model_request`)再做一次。详见 [qzone-sense.md](qzone-sense.md)。
2. **工具定义过滤** `filter_qzone_tools_for_stream`:`qzone_*` 工具全域放行(虚拟流与真实流都可用);其余工具在虚拟流内走 `qzone.tool_whitelist` 白名单,真实流原样放行(主程序自选)。

虚拟流判定 = 运行时收集到的 session 集合 ∪ 按主程序 session_id 公式本地计算的预期值(`md5(platform[+account]+group_id)`),后者保证冷启动首轮也能命中。

## 三、限制与回退清单

| 限制/回退 | 为什么存在 | 触发条件 | 行为 |
|---|---|---|---|
| 注入失败仅日志、不阻塞 | 注入是增强不是前提;插件故障不能让 bot 失语或报错 | `inject_blocks` 内任何异常(含 render 的模块重复 `ValueError`) | `logger.exception` 后返回原 kwargs,本轮无注入——模型缺这些上下文但链路完好 |
| 开关关闭即跳过 | 总开关与注入分开关都提供 | `plugin.enabled` 或 `inject.enabled` 为假 | debug 日志,原样 continue |
| items 取不到跳过 | 主程序载荷形态变化时不应硬崩 | `kwargs["items"]` 缺失或非 list | debug 日志,原样 continue |
| render 结果为空跳过 | 全部块都不满足条件(如数据未就绪)属正常轮次 | 无任何可用块 | debug 日志,原样 continue |
| 无 system 定位失败回退尾部 | 缺 system 说明主程序模板形态变化 | items 中找不到 SystemMessageItem | warning(缓存纪律受损——块落在历史中段,前缀缓存命中率下降)+ 追加尾部 |
| 环境块数据未就绪不注入 | 不编造天气/节日数据(错误显式暴露纪律的正面形态) | 首次启动、后台刷新尚未完成;或天气拉取失败 | 块缺席;天气失败另有告警,节日走在线→库→内置回退链 |
| 快照 item 的 timestamp 不刷新 | 同文本必须字节级复用同一对象(前缀缓存纪律) | 缓存命中的块 | timestamp 停留在首次创建时刻;主程序按自身 24h 窗口过滤旧消息,注入时刻即阅读时刻,天然兼容 |
| 每模块每轮一块,重复即抛 | 重复块属调用方 bug | `_build_inject_blocks` 产出同 module 两块 | `ValueError` → 被外层捕获 → 整轮跳过注入并留异常日志 |
| 渲染缓存/快照缓存有 LRU 上限 | 内容键含全量文本,长周期运行会无界增长 | 缓存超过 512 条(渲染)/ 256 条(快照) | 逐最旧;代价是老内容重新进入时重建对象 |
| 配置/人设热重载必须清缓存 | 旧渲染基于旧配置,复用会注入过期内容 | `on_config_update`(self 或 bot scope) | `assembler.reset()` + `_snapshot_cache.clear()` + 环境缓存失效 |
| 场景替换失败仅告警不阻断 | 场景说明是引导性文本,缺失时工具链仍完整可用 | `empty_config` / `miss` / 文案链路异常 | warn-once 告警,本轮无场景说明;虚拟流注入块只保留动态状态,不重复承载场景语义 |
| 群聊说话人每轮可能变化 | 设计预期:注入面向"最近发言的人" | 群聊轮次 | 每轮经 `_resolve_speaker` 重取(流信息有 10 分钟 TTL,消息级取数每轮实时) |
