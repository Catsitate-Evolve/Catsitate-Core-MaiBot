# catsitate_core_maibot

Catsitate 的 MaiBot 核心人格行为插件。详细设计见 `docs/superpowers/specs/`。

## 启用方式

1. 将本目录放入 MaiBot 的 `plugins/` 目录(或经插件市场安装);
2. 启动后进入 WebUI → 插件页 → 启用 `catsitate.core`;
3. 配置各模块开关(默认关闭总开关,逐项开启)。

## 测试

```bash
python3 -m pytest tests/ -v
```
