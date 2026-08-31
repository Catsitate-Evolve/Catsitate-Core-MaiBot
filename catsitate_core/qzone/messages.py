"""虚拟流注入消息构造(message_dict 对齐主程序 plugin_runtime/host/message_utils.py 格式)。

纪律(联调修正 2026-08-31):timestamp=**发布时间**(原时间,防 bot 把老说说当成刚发生——
联调缺陷#5);正文带相对时间前缀(今天 HH:MM / M月d日 HH:MM)使模型可感知动态新旧;
message_id 全局唯一(tid+序号);is_mentioned 嵌在 message_info.additional_config
(主程序只读该位置);图片段带 binary_data_base64,下载失败的图以 [图片] 占位;
纯图说说省略空文本段(图段承载内容,联调缺陷#4)。图片**不设质量上限,但必须压缩到
RPC 帧预算以内**(用户裁定 2026-08-31:主程序入站压缩发生在 RPC 之后,帧限必须插件侧保证;
体积治理=压缩而非拒收,超预算极端情况丢弃最大图保帧)。
"""

from __future__ import annotations

import base64
import hashlib
import io
import logging
from datetime import datetime

from catsitate_core.qzone import QZONE_PLATFORM
from catsitate_core.qzone.protocol import FeedItem

logger = logging.getLogger(__name__)

# RPC 帧物理硬限 16MB(base64 后);消息其余部分与开销留余量,图片 base64 总预算 12MB
RPC_IMAGE_BUDGET_BYTES = 12 * 1024 * 1024

# 压缩阶梯(最长边, JPEG 质量):从「几乎无损」逐级收紧
COMPRESSION_LADDER: tuple[tuple[int, int], ...] = (
    (4096, 85), (2560, 80), (1792, 75), (1280, 70), (1024, 60), (768, 55), (512, 45),
)

try:  # PIL 由主程序环境提供(manifest 已声明依赖);缺失时告警并走丢弃路径保帧限
    from PIL import Image as _PILImage  # type: ignore

    _HAS_PIL = True
except ImportError:  # pragma: no cover - 环境异常路径
    _PILImage = None
    _HAS_PIL = False

# PIL 缺失告警只在 fit_images_to_rpc_budget 入口打一次(模块级 flag):
# _pil_compress 会被压缩阶梯 7 档×N 图反复调用,逐次告警会刷屏
_PIL_MISSING_WARNED = False


def _b64_total(images: list[tuple[str, bytes | None]]) -> int:
    return sum(len(base64.b64encode(d)) for _, d in images if d)


def _pil_compress(data: bytes, max_dim: int, quality: int) -> bytes:
    if not _HAS_PIL:
        return data  # 告警收敛到 fit_images_to_rpc_budget 入口(一次性)
    img = _PILImage.open(io.BytesIO(data))
    img = img.convert("RGB")
    img.thumbnail((max_dim, max_dim))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality)
    out = buf.getvalue()
    return out if len(out) < len(data) else data  # 压缩反而更大(如小图)则保留原样


def fit_images_to_rpc_budget(
    images: list[tuple[str, bytes | None]],
    *,
    budget_bytes: int = RPC_IMAGE_BUDGET_BYTES,
    compress=None,
    on_drop=None,
) -> list[tuple[str, bytes | None]]:
    """把图片列表的 base64 总量压到 RPC 帧预算内(体积治理=压缩,非拒收)。

    逐级压缩阶梯全员收紧直至达标;仍超(极端)则从最大的图开始置 None
    (调用方以 [图片] 占位),on_drop(url) 逐次回调供告警。PIL 缺失时
    跳过压缩直接走丢弃路径(保帧限,显式告警)。
    """
    compress = compress or _pil_compress
    global _PIL_MISSING_WARNED
    if not _HAS_PIL and not _PIL_MISSING_WARNED:
        _PIL_MISSING_WARNED = True
        logger.warning("PIL 不可用,跳过图片压缩(依赖缺失,极端大图将走丢弃路径)")
    current: list[tuple[str, bytes | None]] = [(u, d) for u, d in images]
    if _b64_total(current) <= budget_bytes:
        return current
    for max_dim, quality in COMPRESSION_LADDER:
        current = [(u, None if d is None else compress(d, max_dim, quality)) for u, d in current]
        if _b64_total(current) <= budget_bytes:
            return current
    while any(d for _, d in current):
        idx = max((i for i, (_, d) in enumerate(current) if d), key=lambda i: len(current[i][1]))
        u, _d = current[idx]
        current[idx] = (u, None)
        if on_drop:
            on_drop(u)
        if _b64_total(current) <= budget_bytes:
            break
    return current


def _time_prefix(post_dt: datetime, now_dt: datetime) -> str:
    """相对时间前缀:同日=今天HH:MM,不同日=M月d日HH:MM,跨年补年份。"""

    if (post_dt.year, post_dt.month, post_dt.day) == (now_dt.year, now_dt.month, now_dt.day):
        return f"(今天{post_dt:%H:%M})"
    if post_dt.year != now_dt.year:
        return f"({post_dt:%Y年%m月%d日 %H:%M})"
    return f"({post_dt:%m月%d日 %H:%M})"


def build_feed_message(
    feed: FeedItem,
    *,
    seq: int,
    group_id: str,
    group_name: str,
    images: list[tuple[str, bytes]],
    now_epoch: float,
) -> dict:
    """构造一条说说注入消息。images 为 (url, bytes) 列表,下载失败(None)的图以占位呈现。

    timestamp 取 feed.abstime(发布时间);abstime 非法/缺失时回退注入时刻且不加前缀。
    """

    text = feed.content.strip()
    post_epoch: float | None = None
    try:
        candidate = float(str(feed.abstime or "").strip())
        if candidate > 0:
            post_epoch = candidate
    except ValueError:
        post_epoch = None

    raw: list[dict] = []
    for url, data in images:
        if data is None:
            text += " [图片]"
            continue
        # 组件形态对齐 napcat-adapter(联调缺陷#15):data 必须**留空**——它是描述槽,
        # 填占位文本会被主程序当成已有描述入库存证,VLM 视觉管线永不运行;
        # hash 显式给 sha256(与 adapter 一致)
        raw.append({
            "type": "image",
            "data": "",
            "hash": hashlib.sha256(data).hexdigest(),
            "binary_data_base64": base64.b64encode(data).decode("ascii"),
        })
    if not raw and feed.image_urls and not images:
        text += " [图片]"  # 有图但全未下载成功的占位

    if post_epoch is None:
        logger.debug("空间动态 abstime 非法/缺失(tid=%s),时间戳回退注入时刻(不加前缀)", feed.tid)
    timestamp = post_epoch if post_epoch is not None else now_epoch
    if post_epoch is not None:
        prefix = _time_prefix(datetime.fromtimestamp(post_epoch), datetime.fromtimestamp(now_epoch))
    else:
        prefix = ""
    # 文本段:正文→前缀+正文;纯图→仅时间前缀(无时间则整段省略,图段承载内容);
    # 无正文无图→前缀+占位
    if text:
        body = f"{prefix}{text}".strip()
    elif raw:
        body = prefix
    else:
        body = f"{prefix}(无文字内容)".strip()
    if body:
        raw.insert(0, {"type": "text", "data": body})
    return {
        "message_id": f"qzone_{feed.tid}_{seq}",
        "platform": QZONE_PLATFORM,
        "timestamp": str(int(timestamp)),
        "message_info": {
            "user_info": {"user_id": str(feed.uin), "user_nickname": feed.nickname},
            "group_info": {"group_id": group_id, "group_name": group_name},
            # is_mentioned 必须嵌在 message_info.additional_config 内:主程序
            # is_mentioned_bot_in_message 只读该位置(联调缺陷#3,顶层键会被丢弃)
            "additional_config": {"is_mentioned": 1.0},
        },
        "raw_message": raw,
    }
