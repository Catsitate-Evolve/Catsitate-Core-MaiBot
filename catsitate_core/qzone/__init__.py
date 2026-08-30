"""QQ 空间模块(三期)。M1=感知(拉取+虚拟流注入);M2=互动;M3=表达。

平台名 "qzone-qq" 为常量:主程序 get_person_id 对含 "-" 的平台名取连字符后段
(split 后第 2 段,如 qzone-qq → qq)计算命名空间,person 因此与真实 QQ 聊天
统一为同一人(spec §2.17)。
"""

QZONE_PLATFORM = "qzone-qq"
QZONE_GATEWAY_NAME = "catsitate_qzone"
