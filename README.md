# catsitate_core_maibot

Catsitate 的 MaiBot 核心人格行为插件。仓库地址:https://github.com/Catsitate-Evolve/Catsitate-Core-MaiBot 。详细设计见 `docs/superpowers/specs/`。

## 启用

1. 把本目录放进 MaiBot 的 `plugins/` 并重启;WebUI「插件」页确认 `catsitate.core` 已加载
2. 在插件配置页打开 `plugin.enabled = true`(总开关,默认关),按需调整各模块节
3. 想用自定义 LLM 端点:在 MaiBot `model_config.toml` 的 `api_providers` 增加一条 `client_type = "catsitate_custom"`(base_url/key),再把对应能力的 `model` 填 `catsitate_custom`
4. 表情白名单:`msg_react.emoji_whitelist` 填入 napcat 表情 id(留空则贴表情工具拒绝执行)

## 测试

- 单元测试:`cd plugins/catsitate_core_maibot && python3 -m pytest tests/ -v`(不依赖 MaiBot)
- 集成冒烟:同目录 `python3 -m pytest tests/test_integration.py -v`
- 实机验收:按 `docs/acceptance-checklist.md` 逐项勾选

## 缓存与用量观测

- 主链路命中率:对照 `docs/cache-baseline.md` 流程,看主程序日志 `Planner缓存:...hit_rate=xx%`
- 旁路 LLM 记账:`llm_usage` 表(day/module/calls/tokens)按模块分列;每日旁路调用合计超过
  `plugin.llm_daily_call_warning_threshold` 时记录告警日志

## 配置要点

- 每个 LLM 能力的 `model` 留空 = 主程序默认模型;填 task 名 = 该任务模型;填 `catsitate_custom` = 自定义端点
- 注入四块(`level_rule`/`environment`/`memo`/`favorability`)各自有开关,可独立关闭
- 哨兵层默认关(`reply_guard.sentinel_enabled`),开启后每句回复多一次旁路判定
