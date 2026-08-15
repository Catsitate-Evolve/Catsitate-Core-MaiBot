"""pytest 全局配置:保证从任意 cwd 运行都能导入 catsitate_core。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
