from typing import Dict

from gsuid_core.utils.plugins_config.models import (
    GSC,
    GsBoolConfig,
    GsIntConfig,
)

CONFIG_DEFAULT: Dict[str, GSC] = {
    "RETENTION_DAYS": GsIntConfig(
        "AT记录保留天数",
        "AT记录在数据库中保留的天数，超过此天数的记录会被删除",
        3,
        5,
    ),
    "CACHE_SIZE": GsIntConfig(
        "消息缓存大小",
        "用于保存群组最近消息的缓存大小",
        5,
        20,
    ),
    "TRACKING_COUNT": GsIntConfig(
        "追踪消息数量",
        "检测到AT后追踪发送者后续消息的数量",
        10,
        20,
    ),
    "EnableAvatarCache": GsBoolConfig(
        "启用头像缓存",
        "是否缓存用户QQ头像，启用可加快图片生成速度但会占用更多磁盘空间",
        False,
    ),
}
