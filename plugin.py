"""Catsitate 核心人格行为插件 — 薄入口。"""

from maibot_sdk import MaiBotPlugin

from catsitate_core.config import CatsitateConfig


class CatsitatePlugin(MaiBotPlugin):
    """Catsitate 核心人格行为插件。"""

    config_model = CatsitateConfig

    async def on_load(self) -> None:
        """插件加载:各模块初始化在后续任务接入。"""

    async def on_unload(self) -> None:
        """插件卸载:优雅停止后台任务与关闭存储。"""

    async def on_config_update(self, scope: str, config_data: dict, version: str) -> None:
        """配置热重载:刷新派生缓存(后续任务接入)。"""


def create_plugin() -> CatsitatePlugin:
    """创建插件实例。"""

    return CatsitatePlugin()
