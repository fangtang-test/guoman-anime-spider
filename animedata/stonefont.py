"""猫眼「石头字体」反爬解码（best-effort）。

猫眼把票房 / 评分里的数字换成私有区（PUA）码位，再配一个随机生成的 woff 字体
把字形画回正确的数字。所以页面文本里取到的是乱码，肉眼看着正常、程序取出来是错的。

解码思路：字体里通常**同时保留**了正常 ASCII 数字字形（``0x30``–``0x39``）和
被换掉的 PUA 字形，两套字形的**轮廓完全一致**。于是：

1. 用 fontTools 读出 cmap 与每个字形的轮廓；
2. 给轮廓算一个指纹（坐标序列的哈希）；
3. 用 ASCII 数字的指纹建立 ``指纹 -> 数字`` 参照表；
4. 拿 PUA 字形的指纹去查表，就能反推真实数字。

需要 ``fonttools``（``pip install fonttools brotli``）。没装就安静跳过。
"""

from __future__ import annotations

import hashlib
import io
import logging
from typing import Any

log = logging.getLogger(__name__)

PUA_START = 0xE000
PUA_END = 0xF8FF
ASCII_DIGITS = {0x30 + i: str(i) for i in range(10)}


def _outline_signature(glyphset, glyph_name: str) -> str | None:
    """给一个字形算轮廓指纹。同一个字形复制到不同码位时指纹相同。"""
    try:
        from fontTools.pens.recordingPen import RecordingPen
    except ImportError:  # pragma: no cover
        return None
    try:
        pen = RecordingPen()
        glyphset[glyph_name].draw(pen)
    except Exception:
        return None
    if not pen.value:
        return None
    # 只取坐标，四舍五入到 1/100，避免浮点抖动影响哈希
    parts: list[str] = []
    for op, args in pen.value:
        parts.append(op)
        for pt in args:
            if isinstance(pt, tuple) and len(pt) == 2:
                parts.append(f"{round(float(pt[0]), 2)},{round(float(pt[1]), 2)}")
            else:
                parts.append(str(pt))
    return hashlib.md5("|".join(parts).encode("utf-8")).hexdigest()


def decode_font(data: bytes) -> tuple[dict[str, str], str]:
    """解一个 woff/ttf，返回 ``({混淆字符: 真实数字}, 说明)``。

    失败时返回 ``({}, 原因)``，不抛异常。
    """
    try:
        from fontTools.ttLib import TTFont
    except ImportError:
        return {}, "未安装 fonttools（pip install fonttools brotli）"

    try:
        font = TTFont(io.BytesIO(data), fontNumber=0, lazy=True)
    except Exception as exc:
        return {}, f"字体解析失败: {type(exc).__name__}: {exc}"

    try:
        cmap: dict[int, str] = dict(font.getBestCmap() or {})
    except Exception as exc:
        return {}, f"cmap 读取失败: {exc}"
    if not cmap:
        return {}, "字体里没有 cmap"

    try:
        glyphset = font.getGlyphSet()
    except Exception as exc:
        return {}, f"字形集读取失败: {exc}"

    # 1) 用 ASCII 数字字形建参照表
    sig_to_digit: dict[str, str] = {}
    for code, digit in ASCII_DIGITS.items():
        gname = cmap.get(code)
        if not gname:
            continue
        sig = _outline_signature(glyphset, gname)
        if sig:
            sig_to_digit[sig] = digit

    if not sig_to_digit:
        return {}, "字体里没有 ASCII 数字字形，无法建立参照表"

    # 2) 用参照表反查被混淆的码位
    mapping: dict[str, str] = {}
    for code, gname in cmap.items():
        if code in ASCII_DIGITS:
            continue
        if not (PUA_START <= code <= PUA_END or code < 0x30):
            continue
        sig = _outline_signature(glyphset, gname)
        if sig and sig in sig_to_digit:
            mapping[chr(code)] = sig_to_digit[sig]

    if not mapping:
        return {}, "参照表建好了，但没有 PUA 字形能匹配上（字体结构可能变了）"

    note = f"已解出 {len(mapping)} 个混淆字符（参照 {len(sig_to_digit)} 个 ASCII 数字字形）"
    return mapping, note


def deobfuscate(text: str, mapping: dict[str, str]) -> str:
    """用映射表把文本里的混淆字符替换成真实数字。"""
    if not mapping or not text:
        return text
    return "".join(mapping.get(ch, ch) for ch in text)


def find_font_urls(html: str) -> list[str]:
    """从 HTML 里找出所有字体文件 URL。"""
    import re

    urls = re.findall(r"url\(\s*[\"']?(//[^\"')]+\.(?:woff2?|ttf|eot))[\"']?\s*\)", html, re.I)
    urls += re.findall(r"url\(\s*[\"']?(https?://[^\"')]+\.(?:woff2?|ttf|eot))[\"']?\s*\)", html, re.I)
    out: list[str] = []
    for u in urls:
        u = u if u.startswith("http") else "https:" + u
        if u not in out:
            out.append(u)
    return out


def build_decoder(client, html: str) -> tuple[dict[str, str], str]:
    """给一段 HTML，自动抓字体并解出映射表。"""
    urls = find_font_urls(html)
    if not urls:
        return {}, "页面里没找到字体文件"
    notes: list[str] = []
    for u in urls[:3]:
        f = client.get(u, source="maoyan:font")
        if not f.ok:
            notes.append(f"{u.rsplit('/', 1)[-1]}: 下载失败")
            continue
        mapping, note = decode_font(f.content)
        if mapping:
            return mapping, note
        notes.append(f"{u.rsplit('/', 1)[-1]}: {note}")
    return {}, "; ".join(notes) or "字体解码无结果"


__all__ = ["decode_font", "deobfuscate", "find_font_urls", "build_decoder"]
