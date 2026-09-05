"""QQ 空间模块:感知(拉取+虚拟流注入)、互动、表达三层能力。

平台名 "qzone-qq" 为常量:主程序 get_person_id 对含 "-" 的平台名取连字符后段
(split 后第 2 段,如 qzone-qq → qq)计算命名空间,person 因此与真实 QQ 聊天
统一为同一人。
"""

QZONE_PLATFORM = "qzone-qq"
QZONE_GATEWAY_NAME = "catsitate_qzone"
# 虚拟流会话身份(不可配置项,常量固化)——伪群号若可配置,
# 会被改成与真实群号相同/已存在的值,主程序会话路由与 person 折叠随之漂移;
# 显示名仅 UI 展示。改值需动代码并过全量测试,不给配置留口子。
QZONE_VIRTUAL_GROUP_ID = "qzone_feed"
QZONE_VIRTUAL_GROUP_NAME = "QQ空间"
