"""HTTP 客户端：限速 + 重试退避 + **编码自适应**。

编码这块是重点。国内这些老站点最常见的坑是：

1. 响应头写 ``Content-Type: text/html``（压根没有 charset）；
2. 或者更坏 —— 写 ``charset=ISO-8859-1``，但那只是 HTTP/1.1 的默认值，
   页面真实编码其实是 GBK/GB2312；
3. 真实编码只写在 HTML 的 ``<meta charset=...>`` 里。

所以这里的策略是「**先按可靠性排序收集候选编码，再逐个严格试解**」：

    BOM  >  HTTP 头（且值不是可疑默认值）  >  HTML meta / XML 声明
         >  UTF-8 严格试解  >  chardet 猜测  >  GB18030 兜底  >  替换字符兜底

其中 ``GB2312`` / ``GBK`` 一律归一到 ``GB18030``（前两者的超集，解码更宽容），
而 ``ISO-8859-1`` 这类可疑默认值会先试 ASCII —— 纯 ASCII 才认它，否则跳过。
"""

from __future__ import annotations

import json as _json
import logging
import random
import re
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

import requests

from .config import HttpConfig

log = logging.getLogger(__name__)

CHARSET_HEADER_RE = re.compile(rb"charset\s*=\s*[\"']?\s*([A-Za-z0-9_\-]+)", re.I)
META_CHARSET_RE = re.compile(
    rb"<meta[^>]+?charset\s*=\s*[\"']?\s*([A-Za-z0-9_\-]+)", re.I
)
XML_ENCODING_RE = re.compile(rb"<\?xml[^>]+?encoding\s*=\s*[\"']([A-Za-z0-9_\-]+)[\"']", re.I)

# 这些值出现在响应头里基本等于「没写」，需要继续往下找
SUSPECT_ENCODINGS = {
    "iso-8859-1", "latin-1", "latin1", "latin", "ascii", "us-ascii",
    "cp1252", "windows-1252", "none", "unknown", "binary", "text",
}

# 别名归一化：GB2312/GBK 全部走 GB18030（前两者的超集，解码更宽容）
# 注意：这里**不能**把 utf-8-sig 归一成 utf-8 —— detect_bom() 会返回
# "utf-8-sig"，一旦被归一掉，BOM 就不再被剥离，文本开头会多一个 \ufeff。
ALIASES = {
    "gb2312": "gb18030", "gbk": "gb18030", "gb_2312": "gb18030",
    "gb_2312-80": "gb18030", "csgb2312": "gb18030", "x-gbk": "gb18030",
    "chinese": "gb18030", "csgb18030": "gb18030", "gb18030-2000": "gb18030",
    "utf8": "utf-8", "utf8mb4": "utf-8",
    "big5": "big5hkscs", "big-5": "big5hkscs", "cp950": "big5hkscs",
    "shift-jis": "cp932", "sjis": "cp932", "shift_jis": "cp932",
}

BOMS = [
    (b"\xef\xbb\xbf", "utf-8-sig"),
    (b"\xff\xfe\x00\x00", "utf-32-le"),
    (b"\x00\x00\xfe\xff", "utf-32-be"),
    (b"\xff\xfe", "utf-16-le"),
    (b"\xfe\xff", "utf-16-be"),
]

REPLACEMENT = "\ufffd"


def normalize_encoding(name: str | None) -> str | None:
    if not name:
        return None
    n = name.strip().strip("\"'").lower()
    return ALIASES.get(n, n)


def detect_bom(content: bytes) -> str | None:
    for bom, enc in BOMS:
        if content.startswith(bom):
            return enc
    return None


def sniff_candidates(content: bytes, content_type: str | None) -> list[str]:
    """按可靠性从高到低给出候选编码列表（已归一化、去重）。"""
    cands: list[str] = []

    def push(v: str | None) -> None:
        n = normalize_encoding(v)
        if n and n not in cands:
            cands.append(n)

    bom = detect_bom(content)
    if bom:
        push(bom)

    if content_type:
        m = CHARSET_HEADER_RE.search(content_type.encode("latin-1", "ignore"))
        if m:
            push(m.group(1).decode("ascii", "ignore"))

    head = content[:8192]
    m = META_CHARSET_RE.search(head) or XML_ENCODING_RE.search(head)
    if m:
        push(m.group(1).decode("ascii", "ignore"))

    push("utf-8")
    return cands


def decode_bytes(content: bytes, content_type: str | None = None) -> tuple[str, str]:
    """把响应体解成文本，返回 ``(文本, 实际使用的编码)``。

    逐个候选严格试解，第一个不抛异常的胜出。可疑的 ISO-8859-1 只在内容
    确实是纯 ASCII 时才被接受，否则继续往下找 —— 这一步专门救那些
    「响应头谎报 latin-1、实际是 GBK」的老页面。
    """
    if not content:
        return "", "empty"

    cands = sniff_candidates(content, content_type)

    for enc in cands:
        if enc in SUSPECT_ENCODINGS:
            try:
                return content.decode("ascii"), "ascii"
            except UnicodeDecodeError:
                continue
        try:
            return content.decode(enc), enc
        except (UnicodeDecodeError, LookupError):
            continue

    # 交给 charset_normalizer / chardet 猜一把
    guessed: str | None = None
    try:
        from charset_normalizer import from_bytes  # type: ignore

        best = from_bytes(content).best()
        guessed = normalize_encoding(best.encoding) if best else None
    except Exception:
        pass
    if not guessed:
        try:
            import chardet  # type: ignore

            guessed = normalize_encoding(chardet.detect(content).get("encoding"))
        except Exception:
            guessed = None

    if guessed and guessed not in cands and guessed not in SUSPECT_ENCODINGS:
        try:
            return content.decode(guessed), guessed
        except (UnicodeDecodeError, LookupError):
            pass

    # GB18030 是中国老页面的最大公约数，最后再试一次
    if "gb18030" not in cands:
        try:
            return content.decode("gb18030"), "gb18030"
        except UnicodeDecodeError:
            pass

    return content.decode("utf-8", errors="replace"), "utf-8/replace"


def looks_garbled(text: str, sample: int = 4000) -> bool:
    """粗判乱码：替换字符比例过高就认为解错了。"""
    if not text:
        return False
    s = text[:sample]
    return s.count(REPLACEMENT) / max(len(s), 1) > 0.02


@dataclass
class Fetched:
    """一次请求的结果。失败时 ``ok=False``，``error`` 里有原因。"""

    url: str
    status: int | None = None
    content: bytes = b""
    text: str = ""
    encoding: str = ""
    elapsed_ms: int = 0
    attempt: int = 0
    error: str | None = None
    header_content_type: str | None = None
    headers: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.error is None and self.status is not None and 200 <= self.status < 400

    @property
    def size(self) -> int:
        return len(self.content)

    def json(self) -> Any:
        """按 JSON 规范（UTF-8）解析，绕开 requests 的编码猜测。"""
        raw = self.content
        bom = detect_bom(raw)
        for enc in (bom, "utf-8", "gb18030"):
            if not enc:
                continue
            try:
                return _json.loads(raw.decode(enc))
            except (UnicodeDecodeError, _json.JSONDecodeError):
                continue
        return _json.loads(self.text)

    def __repr__(self) -> str:  # pragma: no cover
        state = f"HTTP {self.status}" if self.ok else f"ERR({self.error})"
        return f"<Fetched {state} {self.size}B enc={self.encoding} {self.url[:70]}>"


class HttpClient:
    """带令牌桶式限速与指数退避的会话封装。

    同一个 host 的请求会保证间隔 ``[min_delay, max_delay]`` 秒的随机等待，
    这是比任何 Header 伪装都管用的防封手段。
    """

    def __init__(self, cfg: HttpConfig, on_log=None):
        self.cfg = cfg
        self.on_log = on_log
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": cfg.user_agent,
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Connection": "keep-alive",
        })
        self.session.proxies.update(cfg.proxies or {})
        self._last_hit: dict[str, float] = {}
        self.request_count = 0
        self.error_count = 0

    # -- 限速 ---------------------------------------------------------------
    def _throttle(self, url: str) -> None:
        host = urlparse(url).netloc
        last = self._last_hit.get(host)
        if last is not None:
            want = random.uniform(self.cfg.min_delay, self.cfg.max_delay)
            gap = time.monotonic() - last
            if gap < want:
                time.sleep(want - gap)
        self._last_hit[host] = time.monotonic()

    # -- 主请求 -------------------------------------------------------------
    def get(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        source: str = "",
        allow_empty: bool = False,
    ) -> Fetched:
        attempts = max(1, self.cfg.retries + 1)
        last: Fetched | None = None

        for attempt in range(1, attempts + 1):
            self._throttle(url)
            started = time.monotonic()
            try:
                resp = self.session.get(
                    url, headers=headers, params=params, timeout=self.cfg.timeout
                )
                elapsed = int((time.monotonic() - started) * 1000)
                ctype = resp.headers.get("Content-Type")
                text, enc = decode_bytes(resp.content, ctype)
                f = Fetched(
                    url=resp.url,
                    status=resp.status_code,
                    content=resp.content,
                    text=text,
                    encoding=enc,
                    elapsed_ms=elapsed,
                    attempt=attempt,
                    header_content_type=ctype,
                    headers=dict(resp.headers),
                )
                self.request_count += 1

                if looks_garbled(text):
                    f.error = f"疑似乱码（编码判定为 {enc}，替换字符过多）"

                if resp.status_code in self.cfg.retry_status and attempt < attempts:
                    self._emit(source, f, error=f"HTTP {resp.status_code}，准备重试")
                    self._backoff(attempt, resp.status_code)
                    last = f
                    continue

                last = f
                self._emit(source, f)
                if resp.status_code in self.cfg.retry_status:
                    self.error_count += 1
                return f

            except requests.RequestException as exc:
                elapsed = int((time.monotonic() - started) * 1000)
                f = Fetched(
                    url=url, attempt=attempt, elapsed_ms=elapsed,
                    error=f"{type(exc).__name__}: {exc}",
                )
                self.error_count += 1
                last = f
                self._emit(source, f)
                if attempt < attempts:
                    self._backoff(attempt, None)
                    continue
                return f

        return last or Fetched(url=url, error="未知失败")

    def _backoff(self, attempt: int, status: int | None) -> None:
        if status == 429:
            # 被限流了，等久一点
            wait = self.cfg.backoff ** attempt * 2 + random.uniform(0, 2)
        else:
            wait = self.cfg.backoff ** attempt + random.uniform(0, 1.5)
        log.warning("第 %d 次重试，等待 %.1fs（status=%s）", attempt, wait, status)
        time.sleep(wait)

    def _emit(self, source: str, f: Fetched, error: str | None = None) -> None:
        if self.on_log:
            try:
                self.on_log(source, f, error)
            except Exception:  # 日志失败绝不影响主流程
                pass

    # -- 便捷方法 -----------------------------------------------------------
    def get_json(self, url: str, **kw) -> Any | None:
        f = self.get(url, **kw)
        if not f.ok:
            return None
        try:
            return f.json()
        except ValueError:
            log.warning("JSON 解析失败: %s", url)
            return None

    def close(self) -> None:
        self.session.close()

    def __enter__(self) -> "HttpClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
