# 整体架构

> 面向想理解全局的开发者。设计动机见 [philosophy.md](philosophy.md);各功能模块的完整逻辑见本目录 `modules/` 下各篇(入口地图见 [README.md](README.md))。

## 一、职责与生命周期:插件在 MaiBot 生态中的位置

Catsitate 是 MaiBot(QQ 猫娘机器人)的拟人化人格插件。它在 MaiBot 插件体系中以**独立进程**运行:主程序的插件 Runner 负责拉起插件进程,此后一切交互都经 `maibot_sdk` 提供的 RPC 通道完成。插件**不能** `import` 主程序内部模块、不能读写主程序数据结构,只能使用 SDK 声明的 API——这是硬约束,新能力必须先在 `_manifest.json` 的 `capabilities` 列表声明。

插件身份与运行约束都声明在 `_manifest.json`:

| 字段 | 内容 | 作用 |
|---|---|---|
| `id` | `catsitate.core` | 决定数据目录(`ctx.paths.data_dir`,生产容器为 `/data/plugins/catsitate.core/`)与能力归属 |
| `host_application` / `sdk` | 宿主与 SDK 版本范围 | Runner 据此拒绝不兼容的组合 |
| `capabilities` | 11 项能力白名单 | 插件可 `call_capability` 的全部能力(见下文) |
| `dependencies` | holiday-calendar、lunar-python、Pillow、curl-cffi | 自动安装的 Python 依赖 |

与宿主共存的几个既定事实(均以插件侧适配解决,不改主程序):

| 主程序行为 | 插件侧适配 |
|---|---|
| 加载器只把 `plugins/` 父目录**临时**加入 `sys.path`,插件目录本身不在 | `plugin.py` 开头自行 `sys.path.insert(0, 插件目录)` 再绝对导入 `catsitate_core.*`(修改限于插件进程内) |
| 提示词管理只扫主程序 `prompts/{locale}/` 与 `data/custom_prompts/{locale}/`,不扫插件目录 | `on_load` 调 `prompt_deploy.sync_prompt_templates()` 把 `prompt_templates/catsitate_*.prompt` 同步到主程序 `prompts/zh-CN/`;主程序 `load_prompts()` 在插件启动之后才调用,同次启动即生效,无需重启 |
| 会话摘要/记忆层不为 receive-only 虚拟流产出内容 | 插件在浏览窗口边界自行摘要(见 [modules/qzone-sense.md](modules/qzone-sense.md)) |
| planner/replyer 的消息载荷是**快照格式**(带 `item_type`/`meta`/`parts` 的 item),朴素 `{"role","content"}` dict 会被拒绝 | 注入内容经 `_to_snapshot_item` 转换后插入(见 [modules/inject.md](modules/inject.md)) |

### plugin.py 是薄接线层

`plugin.py` 约 4400 行,但只做四件事:

1. **注册交互面**——`@Tool` / `@HookHandler` / `@Command` / `@MessageGateway` 装饰的方法,是主程序经 RPC 能触达的全部入口;
2. **装配**——`on_load` 实例化 `catsitate_core` 包的各引擎并接线(注入存储、配置、旁路 LLM 调用);
3. **调度注册**——把周期任务注册进 `Scheduler`(`catsitate_core/services/scheduler.py`,60 秒 tick 的 asyncio 任务引擎,任务异常隔离不互相拖垮);
4. **胶水逻辑**——跨模块的数据搬运(如把当日到期备忘拼进行程注入块)。

业务逻辑本身(纯函数、状态机、引擎)全部在 `catsitate_core/` 包的 32 个模块文件里(顶层 18 个 + `services/` 1 个 + `qzone/` 13 个),绝大多数不依赖网络与宿主,可离线单测(`tests/` 全量跑不触网)。

### 生命周期

三个入口,均由 Runner 经 RPC 调入插件进程:

| 阶段 | 方法 | 行为 |
|---|---|---|
| 加载 | `on_load` | 部署旁路模板 → 建 `SQLiteStore`/各 `JsonSnapshot` → 实例化全部引擎(备忘/好感度/贴表情/戳一戳/睡眠/日程/空间)→ 建表(`ensure_schema`)→ 编译内容护栏 → 挂模块日志转发(`_ModuleLogForwarder`,把 `catsitate_core.*` 的 logger 输出路由到插件 ctx logger,否则不可见)→ 空间模块自检 + 网关就绪上报 → 向 `Scheduler` 注册 11 个周期任务 → 恢复当日日程 → 启动调度并立即刷一次环境数据 |
| 卸载 | `on_unload` | 停调度器、取消全部后台任务(`_background_tasks`)、摘日志转发与 debug handler、关存储 |
| 配置热重载 | `on_config_update` | `scope == "self"`:清空注入/环境/快照缓存、按新间隔重注册调度任务、重编译护栏、重跑空间自检、重校验阈值,并把各引擎(睡眠/日程/备忘/好感度批次/衰减,含衰减器内嵌批次引擎)的配置引用重指到新 config 节——SDK 热重载经 model_validate 重建整个 config 实例,不重指则引擎静默沿用旧节值直到重启;`scope == "bot"`:失效人设/风格缓存并清注入缓存(下次渲染自动生效) |

## 二、完整逻辑

### 与主程序的交互面

**(1) `@Tool`——模型可调用的工具**(planner 决策后经宿主路由回插件进程执行):

| 工具 | 职责 |
|---|---|
| `memo_write` / `memo_read` | 短时备忘读写(工具路径;另有 `/记一下` 命令路径) |
| `update_schedule` | 日程窗口的增/改/移 |
| `msg_react` | 给消息贴 QQ 表情(白名单内 LLM 选表情) |
| `poke_user` | 主动戳一戳(带冷却护栏) |
| `inspect_image` | 图片重看(VLM 问答) |
| `qzone_like` / `qzone_comment` / `qzone_reply` / `qzone_post` | 空间互动四件套(赞/评论/楼中楼/发说说) |
| `view_friend_feeds` / `view_friend_feed_detail` | 在真实聊天里查看好友说说(为动作工具提供 `feed_id`/图片素材) |

**(2) `@HookHandler`——planner/replyer 生命周期钩子**(`BLOCKING` 可改写或 abort 载荷并返回 `modified_kwargs`;`OBSERVE` 只观察;`EARLY`/`LATE` 是同钩子点内的相对顺序):

| 钩子点 | name | mode/order | 职责 |
|---|---|---|---|
| `maisaka.planner.before_request` | `catsitate_inject` | BLOCKING/LATE | 注入块插入 + 虚拟流场景手术 + 工具定义过滤(见 [modules/inject.md](modules/inject.md)) |
| `chat.receive.before_process` | `catsitate_sleep_gate` | BLOCKING/EARLY | 睡眠期绝对静默(abort 一切入站,含命令,记录进回顾缓冲);醒时刷新活动时间戳 |
| `chat.receive.after_process` | `catsitate_fav_count` | OBSERVE | 好感度消息计数与批次结算触发;记录流→最近真实说话人;收集虚拟流 session |
| `maisaka.replyer.after_response` | `catsitate_content_guard` | BLOCKING/EARLY | 内容护栏:回复命中正则 → 置空(主程序以失败处理,不发送) |
| `maisaka.replyer.after_response` | `catsitate_goodnight` | BLOCKING/LATE | 晚安短句判定 → 入睡流程 |
| `maisaka.replyer.after_response` | `catsitate_sentinel` | BLOCKING/LATE | LLM 哨兵层(默认关) |
| `maisaka.planner.after_response` | `catsitate_reply_backfill` | BLOCKING/LATE | `reply` 工具调用的上下文补传 |
| `maisaka.planner.after_response` | `catsitate_qzone_turn` | OBSERVE/LATE | 虚拟流"轮完成"信号,推进空间注入泵 |
| `maisaka.replyer.before_model_request` | `catsitate_qzone_replyer_scene` | BLOCKING/LATE | replyer 侧场景替换(replyer 的载荷在 before_request 上不带 items,须挂这里) |

**(3) `@Command`——用户命令**:`/记一下 <内容>`(别名 `/备忘`),备忘录的用户直达入口。

**(4) `@MessageGateway`——虚拟流消息网关**:`catsitate_qzone`,platform `qzone-qq`,**receive 模式(只进不出)**——QQ 空间动态经 `ctx.gateway.route_message` 投递进虚拟群会话(`qzone_feed`),bot 对说说的动作一律经 `qzone_*` 工具发出(直接打字发不出去,方法体内的出站分支只做防御性拒发)。

**(5) `call_capability`——能力调用**(`_manifest.json` 声明的 11 项):

| 能力 | 用在哪 |
|---|---|
| `llm.generate` | 旁路 LLM 统一出口 `_side_llm_call`(好感度结算/自然衰减/日程生成/晚安判定/贴表情选表情/图片重看/表达润色/日记/见闻摘要/哨兵),按模块记账进 `llm_usage` 表 |
| `message.get_recent` / `get_by_id` / `get_by_time` | 说话人解析、引用消息解析、日记时间线、回顾素材 |
| `chat.get_all_streams` | 聊天流列表缓存(说话人解析,10 分钟 TTL) |
| `config.get` | 读主程序全局配置(bot 人设/行为风格、`group_chat_prompt` 等) |
| `database.get` | 图片重看:拉主程序 `Images` 表取图片文件 |
| `api.call` | adapter API(空间 cookie 获取、贴表情、戳一戳、好友列表) |
| `maisaka.context.append` | 备忘提醒触发时向会话追加上下文 |
| `maisaka.proactive.trigger` | 日程窗口主动发言、虚拟流冷启动自举 |
| `person.get_id` | person 折叠校验(`qzone-qq` 与 `qq` 平台折叠为同一人) |

另有两类不走 `call_capability` 的 SDK 门面:`ctx.gateway.route_message / update_state`(虚拟流投递与就绪上报)与 `ctx.paths` / `ctx.logger`(路径与日志)。

### 数据流总览

**入站主链路**(一条 QQ 消息的旅程):

```
QQ adapter
  │
  ▼
chat.receive.before_process ──► sleep_gate(睡眠则 abort 进回顾缓冲;醒则刷新活动时间戳)
  │
  ▼
chat.receive.after_process ──► fav_count(OBSERVE:计数→批次触发→旁路 LLM 结算)
  │
  ▼
planner 组装 LLM 请求(载荷 items 为快照格式)
  │
  ▼
maisaka.planner.before_request ──► inject_blocks(注入块插到 system 尾;
  │                                   虚拟流另做场景替换+工具白名单过滤)
  ▼
planner LLM 决策 ──── 无 tool_calls ────► (轮结束;qzone 流发轮完成信号推进注入泵)
  │ tool_calls
  ▼
maisaka.planner.after_response ──► reply_backfill(reply 补传)/ qzone_turn_signal
  │
  ▼
工具经宿主路由回插件进程执行(memo_write / qzone_comment / msg_react / …)
  │ 调用 reply 工具时
  ▼
replyer:before_model_request ──► qzone_replyer_scene(虚拟流场景替换)
  │
  ▼
replyer LLM 生成回复文本
  │
  ▼
replyer.after_response ──► content_guard(EARLY:护栏置空)→ goodnight / sentinel(LATE)
  │
  ▼
发送(QQ adapter)
```

**后台链路**(`Scheduler` 60 秒 tick;分钟级 LLM/HTTP 任务在 tick 内只做防重入标记 + 后台派发,不拖住同 tick 的其它任务):

```
Scheduler(11 个周期任务)
 ├─ qzone_poll        拉取好友动态 → FeedInjector 队列(浏览注入源)
 ├─ qzone_notify_poll 通知扫描(评论/楼中楼/赞,高频短间隔模拟推送)
 ├─ weather / holiday 环境块缓存刷新(外网 HTTP → _env_cache)
 ├─ sleep_tick        入睡/自然醒/睡眠窗口处理
 ├─ schedule_tick     日程窗口触发(主动发言 greet / 提醒)
 ├─ remind_fallback   备忘提醒兜底触发
 ├─ daily_settle      好感度日终兜底结算(旁路 LLM)
 ├─ daily_decay       好感度自然衰减(确定性 + LLM 判定)
 ├─ memo_cleanup      过期备忘清理
 └─ qzone_data_prune  空间表数据保留期修剪

FeedInjector 注入泵(决策窗口内串行弹出)
 ──► ctx.gateway.route_message ──► 虚拟流会话 ──► 进入上方入站主链路
```

**旁路 LLM 链路**(与主链路 LLM 并行的第二类调用):所有非 planner/replyer 的 LLM 需求(结算/衰减/日程/润色/哨兵/日记……)经 `_side_llm_call` → `call_capability("llm.generate")`,模板来自 `prompt_templates/`(自动部署到主程序 `prompts/zh-CN/`,WebUI 的 `data/custom_prompts/` 改动优先被读取)。

### 目录结构一览表

```
catsitate_core_maibot/
├── plugin.py            薄接线层:交互面注册 + 装配 + 调度注册(唯一与宿主 RPC 接触的文件)
├── _manifest.json       身份/能力白名单/依赖声明
├── prompt_templates/    12 个旁路 LLM 模板(catsitate_*.prompt,on_load 自动部署)
├── catsitate_core/      业务逻辑包
│   ├── config.py        配置模型(全部配置节与默认值)
│   ├── storage.py       SQLite 薄封装 + JSON 快照(见 modules/storage.md)
│   ├── inject.py        注入块组装器:顺序/缓存纪律(见 modules/inject.md)
│   ├── time_aware.py    时间/节日/天气感知,环境块文本组装
│   ├── schedule.py      日程引擎:模型/校验/LLM 生成/窗口判定/工具修改
│   ├── sleep.py         睡眠状态机与晚安判定
│   ├── memo.py          短时备忘录(SQLite)
│   ├── favorability.py  好感度批次结算(计数触发 + LLM 结算)
│   ├── decay.py         好感度自然衰减
│   ├── msg_react.py     贴表情引擎(白名单 LLM 选表情 + 冷却)
│   ├── qq_emoji.py      贴表情可用表情表(QQ 表情 id)
│   ├── poke.py          戳一戳引擎(冷却限频)
│   ├── image_relook.py  图片重看 prompt 组装
│   ├── reply_guard.py   reply 上下文补传与哨兵判定(纯逻辑)
│   ├── guard.py         内容护栏纯匹配器(正则编译与命中)
│   ├── llm_provider.py  旁路 LLM 请求组装与模板读取(三层链)
│   ├── prompt_deploy.py 旁路模板自动部署
│   ├── services/scheduler.py  60s tick 后台任务引擎
│   └── qzone/           QQ 空间模块(见 modules/qzone-sense.md / qzone-act.md / qzone-express.md)
│       ├── injector.py      串行注入决策状态机(双优先级队列)
│       ├── client.py        HTTP 客户端与 cookie 管理
│       ├── protocol.py      空间 cgi 协议纯函数
│       ├── discovery.py     统一时间线解析器
│       ├── seen_store.py    动态去重存储(qzone_feeds)
│       ├── comment_seen.py  评论去重 + 好感度显式事件表
│       ├── like_seen.py     赞事件去重
│       ├── messages.py      虚拟流注入消息构造(对齐主程序快照格式)
│       ├── registry.py      注入上下文追踪(工具目标解析,内存 LRU)
│       ├── scene.py         虚拟流场景替换与工具白名单纯函数
│       ├── expression.py    表达润色层
│       ├── imaging.py       说说图片管线与多图拼图
│       └── wire.py          写路径纯函数(评论/回复/点赞参数)
├── tests/               离线单测(全量不触网)
└── docs/                本开发文档库(dev/,仓库唯一文档源)
```

## 三、限制与回退清单

| 限制 | 为什么存在 | 触发条件 | 行为 |
|---|---|---|---|
| 只用 SDK 声明的 API | 插件与主程序分属两个进程,唯一通道是 RPC;越界调用在运行期直接失败 | 代码试图 import 主程序模块或调未声明能力 | 不存在此路径——`capabilities` 是静态白名单,Runner 在启动期校验 |
| 主程序只读 | 主程序目录不是本插件的修改对象 | 任何想改主程序配置/代码的需求 | 插件侧规避(如场景替换在钩子里改载荷副本)或如实报告,不 patch |
| 钩子失败不阻塞主链路 | 插件是宿主人格的增强,不能因自身故障让 bot 失语 | 任一 BLOCKING 钩子内抛异常 | 钩子各自捕获:注入钩子整轮跳过注入、晚安/哨兵/护栏钩子放行原载荷,均 `logger.exception`/`warning` 显式暴露 |
| 模块日志必须转发 | 插件 Runner 只路由插件自身 logger,`catsitate_core.*` 模块级 logger 的告警原本不可见 | 模块内 `logger.warning/exception` | `_ModuleLogForwarder` 挂在 `catsitate_core` logger 根上转投 ctx logger;卸载时摘除 |
| 旁路模板部署失败不阻断加载 | 模板缺失时各调用点回退内置默认,功能不中断 | 主程序 `prompts/zh-CN/` 不存在、写入 OSError、源文件读取失败(含非 UTF-8 编码损坏) | 源读失败逐模板告警跳过;目标读失败/损坏视为内容不同,由插件内置权威源覆盖重建;目录不存在说明插件不在 `plugins/` 下,整体告警跳过 |
| 长 IO 任务不得占住调度 tick | `Scheduler` 同 tick 串行执行各任务,分钟级 HTTP/LLM 会拖住睡眠/日程判定 | 浏览轮询、结算、衰减、环境刷新 | tick 内只做防重入标记 + `_spawn_background_task` 后台派发;后台任务异常经 done 回调 `logger.exception` 上报 |
| 后台任务必须持有引用 | 裸 `asyncio.create_task` 的任务可能被垃圾回收 | 任何 `task.cancel()`/gather 清理路径 | `_spawn_background_task` 把任务存入 `_background_tasks`,done 后 discard;`on_unload` 统一取消 |
| 空间模块整体可停用 | 依赖 cookie/自检/网关三重前置,任一失败继续用其余功能 | 自检失败、网关就绪上报被拒、`bot_user_id` 为空 | `_qzone_available = False`,空间轮询/注入/工具全部短路,告警留痕 |
| 生产容器路径约定 | 本地开发与生产(`/MaiMBot`)目录布局一致 | 旁路模板部署推导主程序根 | `prompt_deploy` 从插件路径上溯四级推导,不依赖硬编码绝对路径;布局异常则告警跳过 |
