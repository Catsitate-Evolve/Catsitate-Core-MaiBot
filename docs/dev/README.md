# Catsitate 开发文档库

> 面向开发者和学习者的完整仓库文档。按实际代码行为编写,与 v1.0.0 代码一一对应。

## 这是什么

Catsitate 是 MaiBot(QQ 机器人)的拟人化人格插件——让 bot 像真人一样有作息、有记忆、有情感,会刷空间、写日记、回应互动。

## 阅读顺序

| 顺序 | 文件 | 内容 | 适合谁 |
|---|---|---|---|
| ① | [philosophy.md](philosophy.md) | 设计哲学——为什么这样设计 | 所有人(先读这篇) |
| ② | [architecture.md](architecture.md) | 整体架构与数据流 | 想理解全局的人 |
| ③ | modules/ 目录 | 每个功能模块的完整逻辑 | 按需查阅或逐篇阅读 |
| ④ | [testing.md](testing.md) | 测试体系 | 贡献代码前必读 |
| ⑤ | [history.md](history.md) | 里程碑时间线 | 了解演进脉络 |

## 模块地图

| 文件 | 模块 | 核心职责 |
|---|---|---|
| [modules/inject.md](modules/inject.md) | 上下文注入 | 往模型上下文注入环境/人格/备忘等系统信息 |
| [modules/memo.md](modules/memo.md) | 备忘录 | 短时记忆与提醒 |
| [modules/favorability.md](modules/favorability.md) | 好感度 | 按人跟踪关系变化(LLM 判定+确定性衰减) |
| [modules/sleep-schedule.md](modules/sleep-schedule.md) | 睡眠与日程 | LLM 生成作息、入睡/自然醒、备忘提醒 |
| [modules/qzone-sense.md](modules/qzone-sense.md) | QQ空间·感知 | 浏览好友动态、注入虚拟流 |
| [modules/qzone-act.md](modules/qzone-act.md) | QQ空间·互动 | 评论/回复/点赞/发说说工具与通知 |
| [modules/qzone-express.md](modules/qzone-express.md) | QQ空间·表达 | 润色层/日记/见闻/图片拼接 |
| [modules/storage.md](modules/storage.md) | 存储层 | SQLite/JSON 持久化 |
| [modules/guard.md](modules/guard.md) | 内容护栏 | 正则拦截违规文本 |

## 运维手册

日常配置与排障请查 [docs/plugin-manual.md](../plugin-manual.md)(面向部署者,与本文档库互补)。

## 快速上手

```bash
# 跑全量测试(561 用例,离线 stub,不触网)
cd MaiBot-dev/plugins/catsitate_core_maibot && python3 -m pytest tests/ -q

# 查看当前版本
cat _manifest.json | grep version
```
