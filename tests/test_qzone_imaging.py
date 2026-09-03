"""说说图片出口公共管线与多图拼图合成测试(Task 4,C 方案 2026-09-03 用户裁定)。

compose_numbered_grid:输出合法 JPEG(魔数)/画布尺寸随行列数增长/白底
letterbox/序号角标用**原始序号**(下载失败的序号跳格不重排——第2图缺时
格1画图1、格2画图3,空位示诚实)。
run_feed_image_pipeline:单图直返、多图合成、下载失败跳过+告警
(异常与返回 None 两种失败形态都要覆盖——client 契约失败返回 None)、
合成失败显式回退全占位(不静默)。
"""

from __future__ import annotations

import asyncio
import hashlib
import io

import pytest

from catsitate_core.qzone.imaging import (
    GRID_COLUMNS,
    compose_numbered_grid,
    run_feed_image_pipeline,
)

JPEG_MAGIC = b"\xff\xd8"


def _png(color: tuple[int, int, int], size: tuple[int, int] = (80, 40)) -> bytes:
    """纯色小 PNG 测试输入(非正方形,可验 letterbox 白边)。"""
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="PNG")
    return buf.getvalue()


RED = (255, 0, 0)
BLUE = (0, 0, 255)


class _Recorder:
    """记录 warning/exception 的最小日志出口(管线告警断言用)。"""

    def __init__(self):
        self.warnings: list[str] = []
        self.exceptions: list[str] = []

    def warning(self, msg, *args):
        self.warnings.append(msg % args if args else msg)

    def exception(self, msg, *args):
        self.exceptions.append(msg % args if args else msg)


def _open(data: bytes):
    from PIL import Image

    return Image.open(io.BytesIO(data))


# ---- compose_numbered_grid:纯函数 ----


def test_compose_output_is_jpeg_and_canvas_grows_with_rows():
    """输出恒 JPEG(魔数);画布恒 3 列宽,高度按行数增长(4 图=2 行)。"""
    cell = 64
    for count, rows in ((1, 1), (3, 1), (4, 2), (7, 3)):
        out = compose_numbered_grid([(i, _png(RED)) for i in range(1, count + 1)], cell_px=cell)
        assert out[:2] == JPEG_MAGIC, f"{count} 图输出应为 JPEG"
        assert _open(out).size == (GRID_COLUMNS * cell, rows * cell), f"{count} 图应 {rows} 行"


def _near(px: tuple[int, int, int], target: tuple[int, int, int], tol: int = 6) -> bool:
    """JPEG 有损压缩对纯色有 ±1 级漂移,按容差比较。"""
    return all(abs(a - b) <= tol for a, b in zip(px, target))


def test_compose_letterbox_white_and_slot_placement():
    """白底 letterbox:小图缩放居中、空余白底;格位按列表顺序铺放,
    第 3 格(无图)纯白。"""
    cell = 128
    out = compose_numbered_grid([(1, _png(RED, (80, 40))), (3, _png(BLUE, (40, 80)))], cell_px=cell)
    img = _open(out).convert("RGB")
    # 格1 中心=红图(80x40 居中);格1 右上角=letterbox 白边(避开左上角角标)
    assert _near(img.getpixel((64, 64)), RED)
    assert _near(img.getpixel((120, 8)), (255, 255, 255), tol=2)
    # 格2 中心=蓝图(纵图,左右白边)
    assert _near(img.getpixel((192, 64)), BLUE)
    assert _near(img.getpixel((150, 64)), (255, 255, 255), tol=2)
    # 格3 无图:整格纯白(含若画了角标必然非白的左上圆心区域)
    assert _near(img.getpixel((320, 64)), (255, 255, 255), tol=2)
    assert _near(img.getpixel((280, 24)), (255, 255, 255), tol=2)


def test_compose_badge_present_on_filled_cells():
    """有图格左上角画角标:圆形底区域内必有黑底像素与白色字/描边像素;
    空格无角标。"""
    cell = 128
    out = compose_numbered_grid([(1, _png(RED)), (3, _png(BLUE))], cell_px=cell)
    img = _open(out).convert("RGB")
    badge_box = img.crop((0, 0, 64, 64))  # 格1 角标区(半径 cell//8=16,中心 24,24)
    raw = badge_box.tobytes()
    pixels = [tuple(raw[i:i + 3]) for i in range(0, len(raw), 3)]
    assert any(max(px) < 60 for px in pixels), "角标应有黑底像素"
    assert any(min(px) > 240 for px in pixels), "角标应有白字/白描边像素"
    empty_raw = img.crop((256, 0, 256 + 64, 64)).tobytes()  # 格3 同位区域:无角标
    empty_pixels = [empty_raw[i:i + 3] for i in range(0, len(empty_raw), 3)]
    assert empty_pixels == [b"\xff\xff\xff"] * len(empty_pixels)


def test_compose_badge_numbers_use_original_ordinals_not_renumbered():
    """序号跳格不重排:同为格2 位置,原图序号 3 与 2 的角标字形不同
    (差分断言,不逐像素对版);格1 同序号同图则完全一致(确定性)。"""
    cell = 96
    red, blue = _png(RED), _png(BLUE)
    with3 = _open(compose_numbered_grid([(1, red), (3, blue)], cell_px=cell)).convert("RGB")
    with2 = _open(compose_numbered_grid([(1, red), (2, blue)], cell_px=cell)).convert("RGB")
    slot2_badge = (cell, 0, cell * 2, cell // 2)  # 格2 上半区(角标所在)
    assert with3.crop(slot2_badge).tobytes() != with2.crop(slot2_badge).tobytes(), \
        "格2 角标应显示原始序号(3≠2),不得重排为 2"
    slot1_badge = (0, 0, cell, cell // 2)
    assert with3.crop(slot1_badge).tobytes() == with2.crop(slot1_badge).tobytes()


def test_compose_rejects_empty_and_garbage():
    """空列表=调用错误(ValueError);损坏字节显式抛错交调用方告警回退。"""
    with pytest.raises(ValueError):
        compose_numbered_grid([])
    with pytest.raises(Exception):
        compose_numbered_grid([(1, b"not-an-image")])


# ---- run_feed_image_pipeline:公共出口助手 ----


def test_pipeline_single_image_passes_through_direct():
    """单图直返(不进合成):字节原样、锚维持「图1(hash)」、composed=False。"""
    png1 = _png(RED)
    rec = _Recorder()

    async def dl(url):
        assert url == "u1"
        return png1

    pack = asyncio.run(run_feed_image_pipeline(["u1"], downloader=dl, log=rec))
    assert pack.segments == [("u1", png1)]
    assert pack.composed is False
    assert pack.anchor == f"图1({hashlib.sha256(png1).hexdigest()[:8]})"
    assert not rec.warnings and not rec.exceptions


def test_pipeline_multi_image_composes_single_segment():
    """多图合成:恒单段 (原图任一url, 合成JPEG);锚单条
    「图1-图N(拼接,hash=合成图sha256前8)」,不再逐图列 hash。"""
    pngs = [_png(RED), _png(BLUE), _png((0, 200, 0))]

    async def dl(url):
        return pngs[int(url[1:]) - 1]

    pack = asyncio.run(run_feed_image_pipeline(["u1", "u2", "u3"], downloader=dl, log=_Recorder()))
    composite = compose_numbered_grid([(i, d) for i, d in enumerate(pngs, start=1)])
    assert pack.composed is True
    assert pack.segments == [("u1", composite)]
    assert pack.segments[0][1][:2] == JPEG_MAGIC
    assert pack.anchor == f"图1-图3(拼接,hash={hashlib.sha256(composite).hexdigest()[:8]})"


def test_pipeline_download_failure_skips_and_keeps_ordinals():
    """下载失败(抛异常形态)跳过+告警;序号保持原始位置——第2图缺,
    合成仍含 1/3 号角标,锚仍「图1-图3」(角标空位示诚实)。"""
    png1, png3 = _png(RED), _png(BLUE)
    rec = _Recorder()

    async def dl(url):
        if url == "u2":
            raise OSError("boom")
        return png1 if url == "u1" else png3

    pack = asyncio.run(run_feed_image_pipeline(["u1", "u2", "u3"], downloader=dl, log=rec))
    composite = compose_numbered_grid([(1, png1), (3, png3)])
    assert pack.segments == [("u1", composite)]
    assert pack.anchor.startswith("图1-图3(拼接,hash=")
    assert len(rec.exceptions) == 1 and "u2" in rec.exceptions[0]


def test_pipeline_none_return_counts_as_failure():
    """client 契约:失败也可返回 None(非抛错)——同样跳过+告警,不静默。"""
    rec = _Recorder()

    async def dl(url):
        return None if url == "u1" else _png(BLUE)

    pack = asyncio.run(run_feed_image_pipeline(["u1", "u2"], downloader=dl, log=rec))
    assert pack.composed is False  # 只剩 1 张成功图 → 单图直返
    assert pack.segments == [("u2", _png(BLUE))]
    assert pack.anchor.startswith("图1(")
    assert any("u1" in w for w in rec.warnings)


def test_pipeline_all_failed_returns_empty_pack():
    """全失败:空 segments(注入侧交 build_feed_message 既有 [图片] 占位、
    工具侧无图项),锚空串。"""
    rec = _Recorder()

    async def dl(url):
        raise OSError("net down")

    pack = asyncio.run(run_feed_image_pipeline(["u1", "u2"], downloader=dl, log=rec))
    assert pack.segments == [] and pack.anchor == "" and pack.composed is False
    assert len(rec.exceptions) == 2


def test_pipeline_compose_failure_falls_back_explicitly():
    """合成失败(损坏字节)必须告警+显式回退全占位(segments 空),不静默兜底。"""
    rec = _Recorder()

    async def dl(url):
        return b"junk-bytes-not-image"

    pack = asyncio.run(run_feed_image_pipeline(["u1", "u2"], downloader=dl, log=rec))
    assert pack.segments == [] and pack.anchor == ""
    assert any("合成失败" in m for m in rec.exceptions), "合成失败须 exception 级告警"


def test_pipeline_no_urls_is_noop():
    """无图说说:零下载零告警,空 pack(与纯文说说既有行为一致)。"""
    rec = _Recorder()

    async def dl(url):  # pragma: no cover - 不应被调用
        raise AssertionError("无图说说不应触发下载")

    pack = asyncio.run(run_feed_image_pipeline([], downloader=dl, log=rec))
    assert pack.segments == [] and pack.anchor == ""
    assert not rec.warnings and not rec.exceptions
