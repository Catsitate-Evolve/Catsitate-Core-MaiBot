"""Catsitate 核心人格行为插件 — 薄入口。"""

import sys
from pathlib import Path

from maibot_sdk import MaiBotPlugin

# spike ① 实测结论:加载器仅将 plugins 父目录临时加入 sys.path,插件目录本身不在,
# 绝对导入 catsitate_core.* 会失败。在此自行注册插件目录(sys.path 修改限于插件进程内)。
sys.path.insert(0, str(Path(__file__).resolve().parent))

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
