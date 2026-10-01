"""豆瓣采集。

三层结构，越往下越贵：

1. **列表层（便宜）** —— ``/j/new_search_subjects`` JSON 接口，一次 20 条，
   能一路翻到 4000+。用来把动画片库整个枚举出来。
2. **详情层（贵）** —— ``m.douban.com/rexxar/api/v2/movie/{id}``，
   一次一部，**77 个字段**，评分/上映日期/集数/国家/类型全都有。
3. **兜底层** —— ``/j/subject_abstract``，字段少但极轻，rexxar 挂了时顶上。

三个接口实测都**不需要 Cookie 或登录**（网页版详情页反而被反爬拦，
所以整条链路刻意绕开了网页版）。

实测踩到的三个坑（都已在代码里绕开）
------------------------------------
1. **``type`` 参数完全无效**。``/j/new_search_subjects`` 传
   ``type=movie`` / ``type=tv`` / 不传，返回的是**同一批数据**，
   电影和剧集混在一起。所以不能靠 type 分开枚举，必须靠**标签**：
   「动画」标签基本只挂电影，剧集挂在「日本动画」这类标签下。
2. **限流是真实存在的**。无间隔连打几十次会被临时拉黑，接口开始返回空
   数据（HTTP 仍是 200！），大约 90 秒后自动恢复。所以 ``http.min_delay``
   不能关，且「返回空页」和「被限流」必须区分处理 —— 见 ``iter_list``。
3. **``/rexxar/api/v2/movie/{id}`` 通吃电影和剧集**（剧集 ID 传进去照样返回
   77 个字段且 ``is_tv=True``），所以一条数据只要一次请求。但豆瓣会**单独对
   ``/movie/`` 做 IP 限流**，而且用的是 HTTP 400 + ``code 1309`` —— 见
   ``fetch_detail`` 的注释，那是本题最容易静默出错的地方。
"""

from __future__ import annotations

import json
import logging
import random
import re
import time
from datetime import datetime, timezone
from typing import Any, Callable, Iterator

log = logging.getLogger("animedata.douban")

SEARCH_URL = "https://movie.douban.com/j/new_search_subjects"
REXXAR_URL = "https://m.douban.com/rexxar/api/v2/{kind}/{sid}"
ABSTRACT_URL = "https://movie.douban.com/j/subject_abstract"

PAGE_SIZE = 20

#: 豆瓣限流时的业务错误码（HTTP 状态是 400，不是 429，很容易被漏掉）
RATE_LIMIT_CODE = 1309

#: 被限流后每个 kind 允许的等待序列（秒），第一项 0 表示先立刻试一次
_RATE_LIMIT_WAITS = (0.0, 45.0, 90.0)

RATE_LIMIT_HINT = (
    "豆瓣把本机 IP 限流了（code 1309）。这不是「这部片没有详情」，"
    "所以本轮不写降级数据；等几分钟到几十分钟再跑一次 run.py sync 就会接着抓。"
)


class RateLimited(RuntimeError):
    """豆瓣返回 code 1309：IP 被限流。调用方应中止本轮，而不是降级。"""

    def __init__(self, sid: str, kind: str) -> None:
        super().__init__(f"douban subject_ip_rate_limit (code {RATE_LIMIT_CODE}) "
                         f"at /{kind}/{sid}")
        self.sid = sid
        self.kind = kind


def _is_rate_limited(f) -> bool:
    """判断一个失败的响应是不是豆瓣的限流页。

    限流走的是 HTTP 400 + JSON 体，所以光看状态码会把它误当成「端点坏了」。
    """
    if f is None or f.ok:
        return False
    body = None
    try:
        body = f.json()
    except Exception:  # noqa: BLE001 —— 不是 JSON 就一定不是限流
        return False
    if not isinstance(body, dict):
        return False
    if body.get("code") == RATE_LIMIT_CODE:
        return True
    return "rate_limit" in str(body.get("msg") or "")


SEARCH_HEADERS = {
    "Referer": "https://movie.douban.com/explore",
    "X-Requested-With": "XMLHttpRequest",
    "Accept": "application/json, text/javascript, */*; q=0.01",
}


def rexxar_headers(sid: str) -> dict[str, str]:
    return {
        "Referer": f"https://m.douban.com/movie/subject/{sid}/",
        "Accept": "application/json, text/plain, */*",
        "X-Requested-With": "XMLHttpRequest",
    }


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
#  第一层：全量枚举
# ---------------------------------------------------------------------------
def fetch_list_page(client, tag: str, start: int) -> list[dict[str, Any]] | None:
    """取一页列表。

    返回 ``None`` 表示这次请求失败；返回 ``[]`` 表示这一页确实没数据了。
    两者必须分开 —— 被限流时接口返回的也是空数据，但 HTTP 状态是 200，
    如果当成「翻到底」就会静悄悄地少抓一大截。
    """
    params = {
        "sort": "U",       # U = 综合排序
        "range": "0,10",   # 评分区间，0-10 表示不限
        "tags": tag,
        "start": start,
    }
    f = client.get(
        SEARCH_URL, params=params, headers=SEARCH_HEADERS, source=f"douban:list:{tag}"
    )
    if not f.ok:
        return None
    try:
        data = f.json()
    except ValueError:
        return None
    if isinstance(data, dict):
        rows = data.get("data")
        return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []
    return []


def _fetch_page_recovering(
    client,
    tag: str,
    start: int,
    *,
    fetcher: Callable[[Any, str, int], list[dict[str, Any]] | None],
    empty_recheck_wait: float,
    empty_recheck_max: float,
    empty_retries: int,
) -> list[dict[str, Any]] | None:
    """取一页；把「空页」当限流来处理，返回 ``None`` = 请求失败，``[]`` = 确认空页。

    为什么空页不能直接信：豆瓣限流时返回的是「HTTP 200 + 空数据」，
    跟真的翻到底长得一模一样。实测同一个 ``start`` 前一次返回空、
    隔几十秒再请求就有数据。

    最硬的证据是 **``start=0`` 的首页永远不可能为空** —— 任何标签至少有一页。
    所以首页空了一定是在限流，这时要给足重试次数和等待时间，
    否则整个标签会被静悄悄地跳过（实测：「中国大陆」连翻 10 页之后，
    「华语」的首页和次页一起变空，差点被当成「华语没数据」）。
    """
    rows = fetcher(client, tag, start)
    if rows or rows is None:
        return rows

    at_first_page = start == 0
    tries = 0
    wait = empty_recheck_wait
    limit = 5 if at_first_page else max(0, empty_retries)

    while tries < limit and wait > 0:
        tries += 1
        log.warning(
            "[%s] start=%d 空页%s，等 %.0fs 后重试（第 %d/%d 次）",
            tag, start,
            "（首页为空 → 这是限流，不是翻到底）" if at_first_page else "",
            wait, tries, limit,
        )
        time.sleep(wait)
        rows = fetcher(client, tag, start)
        if rows is None or rows:
            return rows
        wait = min(wait * 2, empty_recheck_max)

    return []


def iter_list(
    client,
    tags: list[str],
    max_start: int = 8000,
    empty_pages_stop: int = 2,
    empty_recheck_wait: float = 20.0,
    empty_recheck_max: float = 90.0,
    empty_retries: int = 2,
    tag_cooldown: float = 15.0,
    resume: Callable[[str], int] | None = None,
    on_page: Callable[[int, str, int], None] | None = None,
    fetcher: Callable[[Any, str, int], list[dict[str, Any]] | None] | None = None,
) -> Iterator[dict[str, Any]]:
    """按标签翻页枚举，逐条 yield。

    ``resume(tag)`` 返回该标签上次翻到的 start（断点续爬）；
    ``on_page(page, tag, start)`` 每页回调一次，用来存断点。

    关于限流，见 ``_fetch_page_recovering``。这里额外做了两件事：

    * **换标签前先歇 ``tag_cooldown`` 秒** —— 实测「中国大陆」连翻 10 页后
      紧接着抓「华语」，对方的响应立刻全变空页。换个说法：限流是按 IP 的
      总量控制，不会因为你换了个 tag 就清零。
    * ``empty_pages_stop`` 默认收到 2（配合上面的递增重试，一个位置最多
      探 3 次、等 60 秒才判定为空），两端加起来是「连续两个位置、共约 2 分钟
      都是空的」才认定翻到底，比原来「3 次空页」扎实得多。

    ``fetcher`` 只给自检用（注入假数据源），线上留空即用 ``fetch_list_page``。
    """
    fetch = fetcher or fetch_list_page

    for idx, tag in enumerate(tags):
        start = max(0, resume(tag)) if resume else 0
        if start >= max_start:
            log.info("[%s] 上次已翻完，跳过", tag)
            continue

        if idx > 0 and tag_cooldown > 0:
            log.info("换个标签前先歇 %.0fs（限流是按 IP 算的，换标签不会清零）", tag_cooldown)
            time.sleep(tag_cooldown)

        empties = 0
        page = 0
        log.info("[%s] 从 start=%d 开始翻页（上限 %d）", tag, start, max_start)

        while start < max_start:
            rows = _fetch_page_recovering(
                client, tag, start,
                fetcher=fetch,
                empty_recheck_wait=empty_recheck_wait,
                empty_recheck_max=empty_recheck_max,
                empty_retries=empty_retries,
            )
            page += 1

            if rows is None:
                # 请求彻底失败（含重试）—— 不推进 start，下次从这儿接着来
                log.warning("[%s] start=%d 请求失败，本轮就此打住，下次接着翻", tag, start)
                break

            if not rows:
                empties += 1
                log.info("[%s] start=%d 确认空页（第 %d/%d 次）",
                         tag, start, empties, empty_pages_stop)
                if empties >= empty_pages_stop:
                    log.info("[%s] 连续 %d 页确认无数据，判定翻到底", tag, empties)
                    break
            else:
                empties = 0
                for r in rows:
                    r = dict(r)
                    r["_tag"] = tag
                    yield r

            start += PAGE_SIZE
            if on_page:
                on_page(page, tag, start)
            if page % 10 == 0:
                log.info("[%s] 已翻 %d 页，start=%d", tag, page, start)

        if on_page:
            on_page(page, tag, start)


# ---------------------------------------------------------------------------
#  第二层：详情
# ---------------------------------------------------------------------------
def fetch_detail(client, sid: str) -> dict[str, Any] | None:
    """抓一部作品的完整详情。

    ``/movie/{id}`` 对剧集同样有效（已验证），所以正常情况一次就够；
    ``/tv/{id}`` 留作兜底，万一以后豆瓣改了行为。
    两个都失败就退回轻量的 ``subject_abstract``。

    **这里最容易踩的坑：被限流时豆瓣返回的是 HTTP 400，不是 429。**
    响应体形如::

        {"request": "GET /v2/movie/1292052", "msg": "subject_ip_rate_limit",
         "code": 1309, "localized_message": "您所在的网络存在异常，请登录后重试。"}

    早期版本把 400 当成「端点不可用」直接降级到 abstract，结果 390 条详情里
    有 161 条（全是电影）没了简介/语言/时长/原名，而且**已入库的降级行不会被
    重抓**，错误就永久留在库里。所以现在：识别出 ``code 1309`` 就按限流处理 ——
    递增等待重试；等待用完还不行就抛 :class:`RateLimited`，
    由 ``sync_details`` 中止本轮（不写降级行，下次重跑自然接着抓）。
    """
    saw_rate_limit = False
    for kind in ("movie", "tv"):
        for _attempt, wait in enumerate(_RATE_LIMIT_WAITS):
            if wait:
                pause = wait + random.uniform(0, 5)
                log.warning("豆瓣限流（code %s），等 %.0fs 后重试 %s/%s",
                            RATE_LIMIT_CODE, pause, kind, sid)
                time.sleep(pause)
            f = client.get(
                REXXAR_URL.format(kind=kind, sid=sid),
                headers=rexxar_headers(sid),
                source=f"douban:detail:{kind}",
            )
            if f.ok:
                data = f.json()
                if isinstance(data, dict) and data.get("id"):
                    data.setdefault("_kind", kind)
                    return data
                break  # 200 但不是详情（极少见），换下一个 kind 试
            if not _is_rate_limited(f):
                break  # 404 之类：这部片确实不在这个 kind 下，换 kind
        else:
            # 这个 kind 的等待全用完了，还在限流。**不能立刻放弃** ——
            # 实测豆瓣会单独限 /movie/，而同一时刻 /tv/ 对剧集仍是 200，
            # 所以还得给 tv 一个机会，两个都不行才算真被限流。
            saw_rate_limit = True

    if saw_rate_limit:
        raise RateLimited(sid, "movie/tv")

    return fetch_abstract(client, sid)


def fetch_abstract(client, sid: str) -> dict[str, Any] | None:
    """轻量兜底：只有评分、类型、地区、年份、导演、演员。"""
    data = client.get_json(
        ABSTRACT_URL,
        params={"subject_id": sid},
        headers=SEARCH_HEADERS,
        source="douban:abstract",
    )
    if isinstance(data, dict) and isinstance(data.get("subject"), dict):
        sub = dict(data["subject"])
        sub["_kind"] = "abstract"
        sub["_degraded"] = True
        return sub
    return None


# ---------------------------------------------------------------------------
#  扁平化
# ---------------------------------------------------------------------------
_DUR_RE = re.compile(r"(?:(\d+)\s*(?:小时|h))?\s*(?:(\d+)\s*(?:分钟|min))?")


def parse_duration_min(durations: list[str] | str | None) -> int | None:
    """把 ``"144分钟"`` / ``"1小时30分钟"`` 解析成分钟数。"""
    if not durations:
        return None
    if isinstance(durations, str):
        durations = [durations]
    for text in durations:
        if not text:
            continue
        t = str(text).strip()
        m = _DUR_RE.search(t)
        if m and (m.group(1) or m.group(2)):
            return int(m.group(1) or 0) * 60 + int(m.group(2) or 0)
        m2 = re.search(r"(\d+)", t)
        if m2:
            return int(m2.group(1))
    return None


def _names(items: Any) -> list[str]:
    out: list[str] = []
    if isinstance(items, list):
        for it in items:
            if isinstance(it, dict):
                n = it.get("name")
                if n:
                    out.append(str(n).strip())
            elif isinstance(it, str) and it.strip():
                out.append(it.strip())
    return out


def _as_list(v: Any) -> list[str]:
    if isinstance(v, list):
        return [str(x).strip() for x in v if str(x).strip()]
    if isinstance(v, str) and v.strip():
        return [v.strip()]
    return []


def _year_from_pubdate(pubdate: Any) -> int | None:
    for d in _as_list(pubdate):
        m = re.search(r"(19|20)\d{2}", d)
        if m:
            return int(m.group(0))
    return None


def _num(v: Any, cast):
    try:
        return cast(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


_TITLE_TAIL_RE = re.compile(r"[\s\u200e\u200f]*\((19|20)\d{2}\)\s*$")


def clean_title(title: Any) -> str:
    """去掉 abstract 接口那种 ``"八仙！‎ (2026)"`` 的尾部年份和不可见字符。"""
    return _TITLE_TAIL_RE.sub("", str(title or "")).strip()


def flatten_detail(sid: str, raw: dict[str, Any]) -> dict[str, Any]:
    """把 rexxar / abstract 的返回统一成一行数据库记录。"""
    rating = raw.get("rating") if isinstance(raw.get("rating"), dict) else {}

    durations = _as_list(raw.get("durations") or raw.get("duration"))
    pubdate = _as_list(raw.get("pubdate"))

    year = _num(raw.get("year") or raw.get("release_year"), int)
    if year is None:
        year = _year_from_pubdate(pubdate)

    # is_tv 是判断电影/剧集的唯一可靠来源（type 参数不可靠，见模块文档）
    is_tv = raw.get("is_tv")
    if is_tv is None:
        subtype = str(raw.get("subtype") or "").lower()
        is_tv = subtype in ("tv", "series") or raw.get("_kind") == "tv"
    is_tv = 1 if is_tv else 0

    title = clean_title(raw.get("title"))

    return {
        "subject_id": str(sid),
        "title": title,
        "original_title": raw.get("original_title") or None,
        "aka": json.dumps(_as_list(raw.get("aka")), ensure_ascii=False),
        "year": year,
        "subtype": raw.get("subtype") or raw.get("_kind"),
        "is_tv": is_tv,
        "rating_value": _num(rating.get("value") or raw.get("rate"), float),
        "rating_count": _num(rating.get("count") or raw.get("comment_count"), int),
        "rating_star": _num(rating.get("star_count") or raw.get("star"), float),
        "genres": json.dumps(_as_list(raw.get("genres") or raw.get("types")), ensure_ascii=False),
        "countries": json.dumps(
            _as_list(raw.get("countries") or raw.get("region")), ensure_ascii=False
        ),
        "languages": json.dumps(_as_list(raw.get("languages")), ensure_ascii=False),
        "durations": json.dumps(durations, ensure_ascii=False),
        "duration_min": parse_duration_min(durations),
        "episodes_count": _num(raw.get("episodes_count"), int),
        "pubdate": json.dumps(pubdate, ensure_ascii=False),
        "release_year": _year_from_pubdate(pubdate) or year,
        "directors": json.dumps(_names(raw.get("directors")), ensure_ascii=False),
        "actors": json.dumps(_names(raw.get("actors") or raw.get("casts")), ensure_ascii=False),
        "intro": (raw.get("intro") or "").strip() or None,
        "comment_count": _num(raw.get("comment_count"), int),
        "review_count": _num(raw.get("review_count"), int),
        "card_subtitle": raw.get("card_subtitle") or None,
        "cover_url": raw.get("cover_url") or None,
        "url": raw.get("url") or f"https://movie.douban.com/subject/{sid}/",
        "detail_json": json.dumps(raw, ensure_ascii=False),
        "fetched_at": now_iso(),
    }


# ---------------------------------------------------------------------------
#  编排
# ---------------------------------------------------------------------------
def sync_list(client, cfg, conn) -> tuple[int, int]:
    """枚举列表并入库，带断点续爬。返回 (新增, 更新)。"""
    from . import db as dbmod

    added = updated = 0
    buf: list[dict] = []
    total = 0

    def on_page(page: int, tag: str, start: int) -> None:
        dbmod.set_state(conn, f"list_start:{tag}", start)

    for row in iter_list(
        client, cfg.douban.tags,
        max_start=cfg.douban.max_start,
        empty_pages_stop=cfg.douban.empty_pages_stop,
        empty_recheck_wait=cfg.douban.empty_recheck_wait,
        empty_recheck_max=cfg.douban.empty_recheck_max,
        empty_retries=cfg.douban.empty_retries,
        tag_cooldown=cfg.douban.tag_cooldown,
        resume=lambda tag: int(dbmod.get_state(conn, f"list_start:{tag}", 0) or 0),
        on_page=on_page,
    ):
        buf.append(row)
        total += 1
        if len(buf) >= 200:
            a, u = dbmod.upsert_list_rows(conn, buf)
            added += a
            updated += u
            buf.clear()
            print(f"  列表已入库 {total} 条（新增 {added} / 更新 {updated}）", flush=True)

    if buf:
        a, u = dbmod.upsert_list_rows(conn, buf)
        added += a
        updated += u
    return added, updated


def sync_details(client, cfg, conn, limit: int | None = None) -> dict[str, int]:
    """补齐详情。默认只抓没抓过的（增量），按评分从高到低抓。"""
    from . import db as dbmod

    ids = dbmod.pending_detail_ids(conn, only_new=cfg.douban.detail_only_new)
    if limit:
        ids = ids[:limit]
    total = len(ids)
    if not total:
        print("  没有待补的详情（全部已抓过）")
        return {"total": 0, "ok": 0, "fail": 0}

    est_min = total * ((cfg.http.min_delay + cfg.http.max_delay) / 2) / 60
    print(f"  待补详情 {total} 条，预计需要 {est_min:.0f} 分钟（可随时 Ctrl+C，重跑续采）",
          flush=True)

    ok = fail = 0
    stopped = False
    started = time.monotonic()
    buf: list[dict] = []

    for i, sid in enumerate(ids, 1):
        try:
            raw = fetch_detail(client, sid)
        except RateLimited as exc:
            # 别硬撑：继续打只会让限流升级，而且写进去的全是降级数据。
            # 直接收工 —— 已抓到的照常入库，剩下的下次重跑接着来。
            log.warning("%s", exc)
            print(f"\n  ⚠ {RATE_LIMIT_HINT}")
            stopped = True
            break
        if raw:
            buf.append(flatten_detail(sid, raw))
            ok += 1
        else:
            fail += 1
            log.warning("详情抓取失败: %s", sid)

        if len(buf) >= 20:
            dbmod.upsert_details(conn, buf)
            buf.clear()

        if i % 20 == 0 or i == total:
            spent = time.monotonic() - started
            rate = i / spent if spent else 0
            eta = (total - i) / rate if rate else 0
            print(
                f"  进度 {i}/{total}  成功 {ok} 失败 {fail}  "
                f"{rate:.2f} 条/秒  预计还需 {eta/60:.1f} 分钟",
                flush=True,
            )
        if i % 200 == 0:
            dbmod.prune_fetch_log(conn, cfg.storage.fetch_log_keep)

    if buf:
        dbmod.upsert_details(conn, buf)
    if stopped:
        print(f"  豆瓣详情本轮中止：{ok} 条已入库，{total - ok} 条留待下次")
    return {"total": total, "ok": ok, "fail": fail, "stopped": int(stopped)}
