"""说说图片出口公共管线 + 多图拼图合成(2026-09-03 Task 4,用户裁定 C 方案)。

三个图片出口(浏览注入 plugin._qzone_inject_one / 工具 view_friend_feeds /
view_friend_feed_detail)此前各持一份「下载→压缩→打包」拷贝,本模块统一:
run_feed_image_pipeline = 逐张下载(失败跳过+告警)→ 单图直返 / 多图合成
(compose_numbered_grid:3 列网格、白底 letterbox、左上角序号角标)→
to_thread 压缩到 RPC 帧预算(messages.fit_images_to_rpc_budget)。

多图合成动机(用户裁定):拼图后恒单图,省 VLM token 与注入上下文;序号角标
保住「图3是什么」的可问性——模型经 inspect_image 对拼图提问,单图细看不再
需要懒取层。[:3] 截断随之删除(合成后无 media 项爆炸面,QQ 上限 9 图封顶)。

纪律(错误显式暴露):合成失败(含 PIL 缺失、损坏字节)必须告警并显式回退
全占位(segments 空列表——注入侧走 build_feed_message 既有 [图片] 占位、
工具侧无图项),不做静默兜底。
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import logging
import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from catsitate_core.qzone.messages import fit_images_to_rpc_budget

logger = logging.getLogger(__name__)

try:  # PIL 由主程序环境提供(manifest 已声明依赖);缺失时合成显式失败交调用方回退
    from PIL import Image as _PILImage
    from PIL import ImageDraw as _PILImageDraw
    from PIL import ImageFont as _PILImageFont

    _HAS_PIL = True
except ImportError:  # pragma: no cover - 环境异常路径
    _PILImage = None
    _PILImageDraw = None
    _PILImageFont = None
    _HAS_PIL = False

# 3 列网格(需求 1):QQ 上限 9 图封顶时恰 3 行
GRID_COLUMNS = 3

# 下载器契约:client.download_image 失败可返回 None 或抛错,两种形态都按失败处理
Downloader = Callable[[str], "Awaitable[bytes | None]"]


def _badge_font(px: int):
    """PIL 默认字体:新版 Pillow 支持指定字号,旧版回退位图默认字体(纯数字可用)。"""
    try:
        return _PILImageFont.load_default(size=px)
    except TypeError:  # pragma: no cover - 旧版 Pillow 无 size 参数
        return _PILImageFont.load_default()


def _flatten_to_rgb(img):
    """透明通道压白底(RGBA/LA/调色板透明),其余直接转 RGB——防透明区转黑。"""
    if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
        rgba = img.convert("RGBA")
        bg = _PILImage.new("RGB", rgba.size, (255, 255, 255))
        bg.paste(rgba, mask=rgba.getchannel("A"))
        return bg
    return img.convert("RGB")


def _draw_number_badge(canvas, cell_x: int, cell_y: int, ordinal: int, cell_px: int) -> None:
    """格左上角圆形底数字角标:黑底白字+白描边(任意底色可辨),字号随格宽缩放。"""
    radius = max(cell_px // 8, 12)
    margin = max(radius // 2, 6)
    cx, cy = cell_x + margin + radius, cell_y + margin + radius
    text = str(ordinal)
    font = _badge_font(int(radius * 1.1))
    draw = _PILImageDraw.Draw(canvas)
    draw.ellipse(
        (cx - radius, cy - radius, cx + radius, cy + radius),
        fill=(0, 0, 0), outline=(255, 255, 255), width=max(2, radius // 12),
    )
    bbox = draw.textbbox((0, 0), text, font=font)
    # textbbox 原点不一定在 (0,0),按包围盒实际偏移居中
    draw.text(
        (cx - (bbox[2] + bbox[0]) / 2, cy - (bbox[3] + bbox[1]) / 2),
        text, font=font, fill=(255, 255, 255),
    )


def compose_numbered_grid(images: list[tuple[int, bytes]], *, cell_px: int = 640) -> bytes:
    """多图说说拼成一张带序号角标的合成图(纯函数;单图不进本函数——调用方直发)。

    输入 (原始序号, 图字节) 列表(下载失败的图不进列表);布局=3 列网格,
    每格 cell_px×cell_px 白底 letterbox(保持纵横比,只缩不放,居中);每格
    左上角画圆形底数字角标,数字=入参原始序号——失败序号跳格不重排(角标
    空位即诚实示缺)。输出 JPEG。空列表(调用错误)/损坏字节/PIL 缺失直接
    抛错:显式失败交调用方告警回退,不静默。
    """
    if not images:
        raise ValueError("compose_numbered_grid 需要至少一张图(空列表为调用错误)")
    if not _HAS_PIL:  # pragma: no cover - 环境异常路径
        raise RuntimeError("PIL 不可用,无法合成多图拼图(manifest 已声明依赖,请检查主程序环境)")
    rows = math.ceil(len(images) / GRID_COLUMNS)
    canvas = _PILImage.new("RGB", (GRID_COLUMNS * cell_px, rows * cell_px), (255, 255, 255))
    for slot, (ordinal, data) in enumerate(images):
        row, col = divmod(slot, GRID_COLUMNS)
        img = _flatten_to_rgb(_PILImage.open(io.BytesIO(data)))
        img.thumbnail((cell_px, cell_px))
        cell_x, cell_y = col * cell_px, row * cell_px
        canvas.paste(img, (cell_x + (cell_px - img.width) // 2, cell_y + (cell_px - img.height) // 2))
        _draw_number_badge(canvas, cell_x, cell_y, ordinal, cell_px)
    buf = io.BytesIO()
    canvas.save(buf, format="JPEG", quality=88)
    return buf.getvalue()


@dataclass(frozen=True)
class FeedImagePack:
    """图片出口管线产物(注入/工具两侧共用的中间形态)。

    segments:压缩预算后的图片段(注入消息形态 (url, bytes|None));多图合成后
    恒单项 [(原图任一 url, 合成 JPEG)],极端超预算丢弃置 None 交占位逻辑。
    anchor:工具侧图标注文案——单图「图1(hash)」/多图「图1-图N(拼接,hash=…)」
    单条;hash=**拟合后实际送出字节**的 sha256 前 8(与 content_items/注入段
    一致,修复环 I1:压缩阶梯重编码后不得用压缩前 hash,inspect_image 前缀
    反查口径);全失败或丢弃=空串(摘要行只列实际可反查的图)。
    composed:合成图标记(工具侧 mime 恒 image/jpeg;单图原图直发才需魔数探测)。
    """

    segments: list[tuple[str, "bytes | None"]]
    anchor: str = ""
    composed: bool = False


async def run_feed_image_pipeline(
    image_urls: list[str],
    *,
    downloader: Downloader,
    log,
    scene: str = "",
) -> FeedImagePack:
    """三个图片出口的公共管线(Downloader 与日志出口经参数注入,便于测试)。

    逐张下载(失败跳过+告警,序号保持原始位置)→ 单图直返 / 多图合成 →
    to_thread 压缩预算(fit_images_to_rpc_budget:压缩阶梯+丢最大图)。
    合成失败(含 PIL 缺失、损坏字节)告警后显式回退全占位(segments 空列表),
    不做静默兜底;下载失败的 None 返回形态同样告警(client 内部已告警,
    此处出口侧再记一条,保证跳过动作可见)。scene 仅用于日志区分出口。
    """
    prefix = f"QQ空间{scene}" if scene else "QQ空间"
    downloaded: list[tuple[int, str, bytes]] = []
    for ordinal, url in enumerate(image_urls, start=1):
        try:
            data = await downloader(url)
        except Exception:
            log.exception("%s图片下载异常(%s),该图跳过", prefix, url)
            continue
        if not data:
            log.warning("%s图片下载失败(%s),该图跳过", prefix, url)
            continue
        downloaded.append((ordinal, url, data))
    if not downloaded:
        return FeedImagePack([])
    if len(downloaded) == 1:
        _ordinal, url, data = downloaded[0]
        segments: list[tuple[str, bytes]] = [(url, data)]
        span = 1  # 锚文案的 N:单图恒 1
        composed = False
    else:
        try:
            composite = await asyncio.to_thread(
                compose_numbered_grid, [(o, d) for o, _u, d in downloaded])
        except Exception:
            log.exception("%s多图拼图合成失败(共%d张),显式回退全占位不送图", prefix, len(downloaded))
            return FeedImagePack([])
        # 合成后恒单图:url 取原图任一(段内仅作来源标注,hash/b64 才是载荷)
        segments = [(downloaded[0][1], composite)]
        span = downloaded[-1][0]  # 锚文案的 N=实际入图的最大原始序号(角标空位示缺)
        composed = True
    fitted = await asyncio.to_thread(
        fit_images_to_rpc_budget, segments,
        on_drop=lambda u: log.warning("%s图片压缩后仍超 RPC 帧预算,丢弃保帧: %s", prefix, u),
    )
    # 锚 hash 取**拟合后实际送出**的字节(修复环 I1,2026-09-03):压缩阶梯会
    # 重编码(字节已变),取压缩前 hash 会让 inspect_image 的 hash 前缀反查对
    # 不上 content_items/注入段送出的字节(反查契约回归)。丢弃(None)则清空锚。
    sent = fitted[0][1]
    if sent is None:
        anchor = ""  # 极端丢弃:摘要不列不可反查的图(误导 inspect_image hash 反查)
    elif composed:
        anchor = f"图1-图{span}(拼接,hash={hashlib.sha256(sent).hexdigest()[:8]})"
    else:
        anchor = f"图1({hashlib.sha256(sent).hexdigest()[:8]})"
    return FeedImagePack(segments=list(fitted), anchor=anchor, composed=composed)
