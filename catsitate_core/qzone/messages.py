"""虚拟流注入消息构造(message_dict 对齐主程序 plugin_runtime/host/message_utils.py 格式)。

纪律(联调修正 2026-08-31 方案 B):timestamp=**注入时刻(阅读时间)**——消息流时钟
单调递增,主程序时序机制(get_recent 24h 窗/间隔样本/连发过滤)消费正确的到达
语义;**发布时间由正文相对时间前缀承载**(今天 HH:MM / M月d日 HH:MM,联调缺陷#5
防 bot 把老说说当刚发生);message_id 全局唯一(tid+时间播种序号);is_mentioned
仅浏览注入设置(嵌 message_info.additional_config,主程序只读该位置;通知消息
不设——走自然回复概率,2026-09-02);图片段带 binary_data_base64,
下载失败的图以 [图片] 占位;文本段末尾带参数独立尾行「〔说说ID=xxx〕」(tid 前 12 位,
工具驱动 2026-09-01;可读性优化后换行独立成行,纯图说说也保留文本段承载锚);
纯图说说正文为空时参数行即整段内容。
图片体积治理=压缩到 RPC 帧预算内(12MB,用户裁定:压缩而非拒收)。
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


def clip_text(text: str, limit: int) -> str:
    """可见内容截断(2026-09-02 用户裁定):超长截断时尾部加 "...",
    让模型/用户知道内容被截断了;未超长原样返回。"""

    text = str(text or "")
    return text if len(text) <= limit else text[:limit] + "..."


def _time_prefix(post_dt: datetime, now_dt: datetime) -> str:
    """相对时间前缀:同日=今天HH:MM,不同日=M月d日HH:MM,跨年补年份。"""

    if (post_dt.year, post_dt.month, post_dt.day) == (now_dt.year, now_dt.month, now_dt.day):
        return f"(今天{post_dt:%H:%M})"
    if post_dt.year != now_dt.year:
        return f"({post_dt:%Y年%m月%d日 %H:%M})"
    return f"({post_dt:%m月%d日 %H:%M})"


def comment_time_prefix(create_time: str, now_epoch: float) -> str:
    """评论注入正文的时间前缀薄封装(终审 I2,方案 B 同款语义):发布时间由正文承载。

    调用方为 format_comment_param_line(M3-r2 Task 3,2026-09-01):通知参数行的
    动作时间(评论于/回复于)经本函数产出;通知内容的时间前缀仍由
    build_feed_message 从 abstime 统一处理,本函数不直接面向轮询侧;
    语义与 _time_prefix 一致。

    create_time 为 msglist commentlist 的 epoch 秒字符串;空/非法/非正返回空串
    (回退形态,调用方不截断注入)。新鲜度截断的判定在 plugin 轮询侧,此处只管前缀。
    """

    try:
        candidate = float(str(create_time or "").strip())
    except ValueError:
        return ""
    if candidate <= 0:
        return ""
    return _time_prefix(datetime.fromtimestamp(candidate), datetime.fromtimestamp(now_epoch))


def format_comment_param_line(
    *, feed_tid: str, comment_tid: str = "", commenter_uin: str = "",
    action: str = "评论", create_time: str = "", now_epoch: float = 0.0,
) -> str:
    """通知参数行:ID 锚 + 动作时间(评论于/回复于 (今天HH:MM)/(M月d日 HH:MM),括号形态承 comment_time_prefix)。

    动作时间让 bot 分得清互动新旧(新鲜度窗口内 3 天前的评论与 3 分钟前的
    长得一样);create_time 缺失/非法时省略时间段,不编造时间。
    """
    parts = [f"说说ID={str(feed_tid)[:12]}"]
    if str(comment_tid or "").strip():
        parts.append(f"评论ID={comment_tid}")
    if str(commenter_uin or "").strip():
        parts.append(f"评论者QQ={commenter_uin}")
    tag = comment_time_prefix(create_time, now_epoch)
    if tag:
        parts.append(f"{action}于{tag}")
    return "〔" + " ".join(parts) + "〕"


def build_notify_message(
    feed: FeedItem, *, group_id: str, group_name: str, now_epoch: float,
    reply_target_id: str = "", reply_target_sender: str = "",
) -> dict:
    """构造通知注入消息——带 reply 段引用原说说(napcat quote 式上下文关联)。

    与 build_feed_message 的分工(联调修正):通知不走浏览动态的图片/时间前缀
    管线,正文由通知轮询侧精简构造(reply 段已带原说说上下文,正文不重复引用
    原文);reply 段置首,target_message_id=原说说**注入时的消息 id**(泵侧经
    seen_store.get_message_id(origin_tid) 查得),target_message_content 直接取
    feed.origin_content(可读性优化 2026-09-01:**原说说正文**前 60 字,非通知
    文本——bot 一眼看到「这条评论发生在哪条说说下」);原说说未注入过时调用方
    传空 id → reply 段省略(回退纯文本)。timestamp 同方案 B=注入时刻。
    """

    raw: list[dict] = []
    if reply_target_id:
        raw.append({
            "type": "reply",
            "data": {
                "target_message_id": reply_target_id,
                "target_message_content": clip_text(feed.origin_content, 60),
                "target_message_sender_id": reply_target_sender,
            },
        })
    raw.append({"type": "text", "data": feed.content})
    return {
        "message_id": f"qzone_notify_{feed.tid}_{int(now_epoch)}",
        "platform": QZONE_PLATFORM,
        "timestamp": str(int(now_epoch)),
        "message_info": {
            "user_info": {"user_id": str(feed.uin), "user_nickname": feed.nickname},
            "group_info": {"group_id": group_id, "group_name": group_name},
            # 不设 is_mentioned(2026-09-02 用户裁定):通知走主程序自然回复概率,
            # 不再强制触发 planner 轮——bot 看到通知但不必然回应(拟人化留白);
            # 浏览注入(build_feed_message)保留强制=串行浏览决策环的设计依赖
        },
        "raw_message": raw,
    }


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

    时间语义(方案 B,用户裁定 2026-08-31):timestamp=**注入时刻(阅读时间)**——
    消息流的时钟单调递增(自然阅读序),主程序时序机制(间隔样本/连发过滤/
    get_recent 24h 窗)全部拿到正确的到达语义;**发布时间由正文前缀承载**
    (今天 HH:MM / M月d日 HH:MM),abstime 非法/缺失时不加前缀。
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

    if post_epoch is not None:
        prefix = _time_prefix(datetime.fromtimestamp(post_epoch), datetime.fromtimestamp(now_epoch))
    else:
        logger.debug("空间动态 abstime 非法/缺失(tid=%s),正文不加发布时间前缀", feed.tid)
        prefix = ""
    # 文本段:正文→前缀+正文;纯图→仅时间前缀(无时间则空);无正文无图→前缀+占位
    if text:
        body = f"{prefix}{text}".strip()
    elif raw:
        body = prefix
    else:
        body = f"{prefix}(无文字内容)".strip()
    # 工具参数独立尾行(可读性优化 2026-09-01,Q1=a+Q4=a):换行+〔说说ID=…〕
    # 独立成行——消除与正文/时间前缀的行内语义混淆(旧「(说说 xxx)」行内尾注
    # 易被模型当正文一部分);tid 取前 12 位短码,模型照抄给
    # qzone_comment/qzone_like 的 feed_id;纯图说说也保证有文本段——参数行
    # 丢失则工具无从解析目标。时间前缀语义不变,参数行只追加在段尾。
    param_line = f"〔说说ID={feed.tid[:12]}〕"
    body = f"{body}\n{param_line}" if body else param_line
    raw.insert(0, {"type": "text", "data": body})
    return {
        "message_id": f"qzone_{feed.tid}_{seq}",
        "platform": QZONE_PLATFORM,
        "timestamp": str(int(now_epoch)),
        "message_info": {
            "user_info": {"user_id": str(feed.uin), "user_nickname": feed.nickname},
            "group_info": {"group_id": group_id, "group_name": group_name},
            # is_mentioned 必须嵌在 message_info.additional_config 内:主程序
            # is_mentioned_bot_in_message 只读该位置(联调缺陷#3,顶层键会被丢弃)。
            # 浏览注入保留强制触发:串行浏览决策环(每条说说注入后 planner 轮决定
            # 互动与否)依赖它;通知消息已移除(走自然概率,见 build_notify_message)
            "additional_config": {"is_mentioned": 1.0},
        },
        "raw_message": raw,
    }
