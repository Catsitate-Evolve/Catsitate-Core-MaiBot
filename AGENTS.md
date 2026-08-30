# AGENTS.md — catsitate_core_maibot

Catsitate 的 MaiBot 核心人格行为插件(独立 git repo,直接提交 main;主程序在父目录 `../../../`,只读)。本指南用于插件的开发、测试与审查。

## 项目定位

- 一个插件 `catsitate.core`,多模块:注入框架 / 时间节日天气感知 / 好感度(按人)/ 自然衰减 / 睡眠与日程 / 主动问候 / 备忘录提醒 / 贴表情 / 戳一戳 / reply 补传 / 图片重看 / 回复质检哨兵。
- 人格与行为风格以 `Catsitate-Soul`(工作区同级仓库)为准;插件 prompt 模板是其运行形态之一。

## 硬约束(必须遵守)

- **只改插件目录,不改 MaiBot 主程序**(含 `prompts/`、配置模板、`.meta.toml`);主程序缺陷在插件侧规避或上报,不绕道改主程序。
- **简体中文**:日志、注释、文档、用户可见文本。
- **错误显式暴露**:禁止静默 fallback——回退必须 `logger.warning/exception` 告警;配置错误拒绝加载;测试覆盖失败路径。
- **只使用 maibot-plugin-sdk**:capability 必须在 `_manifest.json` 声明;不 import 主程序内部模块。
- 生产容器根为 `/MaiMBot`(`catsitate_core/llm_provider.py` 的 `_PROJECT_ROOT`);生产部署由用户执行,本地只开发+单测+可选集成冒烟。

## 目录结构

- `plugin.py` — 插件入口(生命周期/调度/工具注入);顶部 `sys.path.insert(0, 插件目录)` 后绝对导入 `catsitate_core.*`
- `catsitate_core/` — 领域模块,各模块独立、解耦
  - `config.py` 配置树(叶子字段为准,7 个 `*_timeout_ms` 默认 **0**=主程序默认;tomlkit 无法序列化 None)
  - `llm_provider.py` 旁路 LLM 模板与请求组装:`load_side_system` 读取链 `data/custom_prompts/zh-CN/catsitate_{id}.prompt` → `prompts/zh-CN/catsitate_{id}.prompt` → 内置默认(mtime 缓存,缺文件每进程告警一次)
  - `prompt_deploy.py` 模板自动部署:`on_load` 时把 `prompt_templates/catsitate_*.prompt`(8 个)同步到主程序 `prompts/zh-CN/`(内容一致跳过、变更覆盖、结构异常显式告警不阻断)
  - `favorability.py` 好感度引擎:`LEVELS`/`EXCLUSIVE_LEVEL`(特别等级全表独占,钳制 99/挚友)
  - `decay.py`/`schedule.py`/`sleep.py`/`memo.py`/`msg_react.py`/`poke.py`/`reply_guard.py`/`image_relook.py`/`inject.py`/`time_aware.py`/`storage.py`/`services/scheduler.py`
- `prompt_templates/` — 旁路模板源(内置默认的出处,自动部署时推送)
- `tests/` — pytest 单测(不依赖 MaiBot 主程序)
- `_manifest.json`、`CHANGELOG.md`、`CONTEXT.md`、`README.md`、`docs/plugin-manual.md`、`docs/superpowers/specs/`

## 测试

```bash
# 必须在此插件目录下运行(不在 MaiBot-dev 根,否则 pytest 会收集主程序测试并报 ImportError)
python3 -m pytest tests/ -q        # 全量(当前 198 用例)
python3 -m pytest tests/test_integration.py -v   # 集成冒烟
```

依赖:若未装 pytest-asyncio(async 用例会被静默跳过),`python3 -m pip install --break-system-packages pytest-asyncio`。改版本号时同步 `_manifest.json.version` 与 `CHANGELOG.md` 条目。

## 核心语义速查(实现与文档均以此为准,勿回退旧措辞)

- **好感度按人**:唯一标识 = 用户 QQ(`user_id`),单行按人存储;`batch_counter` 仅作 (user, stream) 活跃账本;结算素材跨流聚合。「特别」全表独占。
- **睡眠窗口 = 可入睡时间**(窗口起点~终点,到点自然醒):晚安判定入睡(仅窗口内,与静默开关无关);静默关 = 窗口起点直接入睡;静默开 = 窗口起点后安静满 `silent_sleep_minutes` 分钟(基准 = max(窗口起点, 最后活动));窗口终点未入睡 → 不入睡但补执行入睡任务(生成次日日程,每窗口一次,`_sleep_window_settled`)。睡眠期间绝对静默拦截(唯一例外:次日日程生成)。跨午夜保留旧日程活跃睡眠窗口。
- **日程**:1 睡眠 + 1~8 活动窗口;`kind=greeting`(问候/陪伴类,窗口起点触发主动问候)/ `kind=daily`(日常);入睡生成 + 窗口终点补生成是仅有的两条生成路径。
- **主动问候**:仅「特别」等级 + 存在私聊流,greeting 窗口起点触发,无每日一次限制,受 daily_speak_limit 约束。
- **prompt 模板版本化**:模板须带 `version` 字段,变更时升版本号(`SIDE_TEMPLATES` 与 `prompt_templates/*.prompt` 同步)。

## 流程约定

- 改动后运行全量测试,提交到 main(插件仓库无需 feature 分支);生产部署由用户执行,改动应附生产部署注意事项(模板变更→WebUI 自定义是否需同步)。
