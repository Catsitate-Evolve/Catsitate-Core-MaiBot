# AGENTS.md — catsitate_core_maibot

Catsitate 的 MaiBot 核心人格行为插件(独立 git repo;运行宿主为 MaiBot 主程序——本插件置于其 `plugins/` 目录下,主程序文件对本仓库只读)。本指南用于插件的开发、测试与审查。

## 项目定位

- 一个插件 `catsitate.core`,多模块:注入框架 / 时间节日天气感知 / 好感度(按人)/ 自然衰减 / 睡眠与日程 / 主动问候 / 备忘录提醒 / 贴表情 / 戳一戳 / reply 补传 / 图片重看 / 回复质检哨兵 / QQ空间(动态浏览·互动·表达)/ 内容护栏 / 数据迁移。
- 人格与行为风格素材在独立仓库 Catsitate-Soul(人格描述与行为风格以其为准);插件 prompt 模板是其运行形态之一。

## 硬约束(必须遵守)

- **只改插件目录,不改 MaiBot 主程序**(含 `prompts/`、配置模板、`.meta.toml`);主程序缺陷在插件侧规避或上报,不绕道改主程序。
- **简体中文**:日志、注释、文档、用户可见文本。
- **错误显式暴露**:禁止静默 fallback——回退必须 `logger.warning/exception` 告警;配置错误拒绝加载;测试覆盖失败路径。
- **只使用 maibot-plugin-sdk**:capability 必须在 `_manifest.json` 声明;不 import 主程序内部模块。
- **文档/注释/prompt 自包含**:禁止只有开发对话参与者才能理解的表述——不引用「用户裁定」「联调缺陷#N」「深度审查」「AR-N」「Q21=a」「spec §N.M」「Task N」等内部代号/编号/章节号;写**为什么这样设计**(当前行为的原因),不写**什么时候讨论的**(开发历史)。代码注释解释当前行为,不解释修改过程。工具描述/prompt 模板面向零上下文的 LLM 和开发者,CHANGELOG 面向第一次接触本项目的运维者。
- 生产容器根为 `/MaiMBot`(`catsitate_core/llm_provider.py` 的 `_PROJECT_ROOT`);生产部署由用户执行,本地只开发+单测+可选集成冒烟。

## 开发流程

- **分支纪律**:一切开发在 `dev` 分支进行;开发周期完毕后合并回 `main`。`main` 上只出现完整的周期成果,不出现中间态。
- **版本纪律**:`CHANGELOG.md` 只记录正式版——开发迭代不单独立版本条目,周期完毕随下一个正式版一并记录;`_manifest.json` 的 `version` 与 CHANGELOG 最新正式版一致。正式版之间必须提供数据迁移脚本(`catsitate_core/migrations.py`,handler 幂等)。版本发布需用户同意。
- **改动自验证**:改动后运行全量测试;提交信息简体中文、说清「改了什么、为什么」;涉及生产部署注意事项(模板变更→WebUI 自定义是否需同步、数据迁移等)在提交信息或报告中写明。

## 文档库维护(docs/dev/ 为仓库唯一文档源)

- 结构:`README.md`(索引)/ `philosophy.md`(设计思想)/ `architecture.md`(整体架构)/ `testing.md`(测试体系)/ `history.md`(里程碑)/ `modules/*.md`(各领域模块:inject、memo、favorability、sleep-schedule、qzone-sense、qzone-act、qzone-express、guard、storage)。
- **随代码同步**:行为语义变化的同一提交必须同步对应模块文档——文档滞后即缺陷,不留「下次补」。
- **按实际情况写**:落笔前核对代码事实(接口名/数值/边界),禁止照抄旧文档或臆测;限制与回退必须如实列出,不得美化。
- **自包含**:不引用已删除的文档/规格/计划,不使用内部代号(见硬约束);面向第一次接触本仓库的开发者。
- 历史记录不改写:CHANGELOG 旧条目、history.md 已定稿的里程碑描述保持原样,勘误以追加方式表达。

## 目录结构

- `plugin.py` — 插件入口(生命周期/调度/工具注入);顶部 `sys.path.insert(0, 插件目录)` 后绝对导入 `catsitate_core.*`
- `catsitate_core/` — 领域模块,各模块独立、解耦
  - `config.py` 配置树(叶子字段为准,11 个 `*_timeout_ms` 字段中 9 个默认 **0**=主程序默认(其余:日程生成 120s、空间 HTTP 10s);tomlkit 无法序列化 None)
  - `llm_provider.py` 旁路 LLM 模板与请求组装:`load_side_system` 读取链 `data/custom_prompts/zh-CN/catsitate_{id}.prompt` → `prompts/zh-CN/catsitate_{id}.prompt` → 内置默认(mtime 缓存,缺文件每进程告警一次)
  - `prompt_deploy.py` 模板自动部署:`on_load` 时把 `prompt_templates/catsitate_*.prompt`(12 个)同步到主程序 `prompts/zh-CN/`(内容一致跳过、变更覆盖、结构异常显式告警不阻断)
  - `guard.py` 内容护栏纯匹配器(正则编译与命中)
  - `migrations.py` 数据迁移(版本表+步进注册表+链式执行)
  - `favorability.py` 好感度引擎:`LEVELS`/`EXCLUSIVE_LEVEL`(特别等级全表独占,钳制 99/挚友)
  - `decay.py`/`schedule.py`/`sleep.py`/`memo.py`/`msg_react.py`/`poke.py`/`reply_guard.py`/`image_relook.py`/`inject.py`/`time_aware.py`/`storage.py`/`services/scheduler.py`
  - `qzone/` — QQ空间模块(虚拟聊天平台):`client.py`(协议客户端)/`discovery.py`(统一时间线解析)/`injector.py`(双优先级注入泵)/`registry.py`(注入上下文追踪)/`messages.py`(消息构造)/`wire.py`(写路径表单)/`comment_seen.py`(评论去重+好感度事件)/`seen_store.py`(动态去重)/`scene.py`(场景替换+工具隔离)/`imaging.py`(图片管线与多图拼图)/`expression.py`(表达润色)/`protocol.py`(协议纯函数)
- `prompt_templates/` — 旁路模板源(内置默认的出处,自动部署时推送)
- `tests/` — pytest 单测(不依赖 MaiBot 主程序)
- `_manifest.json`、`CHANGELOG.md`、`README.md`、`docs/dev/`(文档库)

## 测试

```bash
# 必须在本仓库根目录下运行(不在宿主主程序目录,否则 pytest 会收集主程序测试并报 ImportError)
python3 -m pytest tests/ -q        # 全量(当前 626 用例)
python3 -m pytest tests/test_integration.py -v   # 集成冒烟
```

依赖:若未装 pytest-asyncio(async 用例会被静默跳过),`python3 -m pip install --break-system-packages pytest-asyncio`。改版本号时同步 `_manifest.json` 的 `version` 与 `CHANGELOG.md` 条目。

## 核心语义速查(实现与文档均以此为准,勿回退旧措辞)

- **好感度按人**:唯一标识 = 用户 QQ(`user_id`),单行按人存储;`batch_counter` 仅作 (user, stream) 活跃账本;结算素材跨流聚合,空间互动走显式事件表;结算/衰减取数按滚动窗(不按自然日,跨零点不丢昨晚事件)。「特别」全表独占。
- **睡眠窗口 = 可入睡时间**(窗口起点~终点,到点自然醒):晚安判定入睡(仅窗口内,与静默开关无关);静默关 = 窗口起点直接入睡;静默开 = 窗口起点后安静满 `silent_sleep_minutes` 分钟(基准 = max(窗口起点, 最后活动));窗口终点未入睡 → 不入睡但补执行入睡任务(生成次日日程,每窗口一次,`_sleep_window_settled`)。睡眠期间绝对静默拦截(唯一例外:次日日程生成)。跨午夜保留旧日程活跃睡眠窗口。
- **日程**:1 睡眠 + 1~8 活动窗口;`kind=greeting`(问候/陪伴类,窗口起点触发主动问候)/ `kind=daily`(日常);入睡生成 + 窗口终点补生成是仅有的两条生成路径。
- **QQ空间虚拟流**:`qzone-qq` 伪群流,receive 网关(只进不出);动作一律经工具(评论/楼中楼回复/点赞/发说说),注入消息带 ID 锚,工具目标三级解析(registry→seen→awaiting);通知走 P1 优先级、推送语义不依赖浏览窗口;浏览窗口(read_qzone)结束收 P2 队列并生成「空间见闻」(素材=近 24h 滚动窗,截断保留最新)。
- **内容护栏**:`guard.patterns` 全局正则单列表,三拦截点(空间动作工具/日记/replyer);命中即取消发布或置空,warning 可审计;非法正则整组拒绝。
- **prompt 模板版本化**:模板须带 `version` 字段,变更时升版本号(`SIDE_TEMPLATES` 与 `prompt_templates/*.prompt` 同步,镜像测试锁定一致)。
