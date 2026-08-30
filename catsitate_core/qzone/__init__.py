"""QQ 空间模块(三期)。M1=感知(拉取+虚拟流注入);M2=互动;M3=表达。

平台名 "qzone-qq" 为常量:主程序 get_person_id 对含 "-" 的平台名取首段后
命名空间计算 person_id,person 因此与真实 QQ 聊天统一为同一人(spec §2.17)。
"""

QZONE_PLATFORM = "qzone-qq"
QZONE_GATEWAY_NAME = "catsitate_qzone"
