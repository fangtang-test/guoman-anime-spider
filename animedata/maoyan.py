"""猫眼采集。

结论先写在这里（全部是实测出来的，别再走弯路）：

1. **票房不要从 ``www.maoyan.com/films`` 的 HTML 里抠。** 那个页面只给片单
   （片名/类型/主演/上映日期），数字要么根本没有、要么被「石头字体」换成
   PUA 码位（预售票房就是这么混淆的）。详情页 ``/films/{id}`` 更是纯 JS
   骨架，requests 拿到的 HTML 里连片名都没有。

2. **真票房在专业版接口，而且免费、无签名、无需登录、无字体混淆**::

       GET https://piaofang.maoyan.com/dashboard-ajax/movie

   **不传任何参数**就返回 70+ 部在映影片的累计票房 / 票房占比 / 排片占比 /
   上座率 / 场均人次 / 场次，外加当日大盘。两个单位坑：
   ``movieList[].box`` 单位是**分**，``boxTrends[].box`` 单位是**元**。
   给人看的文本在 ``sumBoxDesc`` / ``boxSplitUnit`` 里，优先用它。

3. **``showType`` 的含义和直觉相反**：``showType=1`` = 正在热映，
   ``showType=2`` = 即将上映 / 预售。只抓 2 等于把正在上映的片子全漏掉。

4. **历史票房不能回溯。** ``date`` 参数是摆设（传 2025-10-01 和传
   2026-11-30，返回的 ``calendar.selectDate`` 都还是今天，只是数字随实时微涨）；
   ``?movieId=N`` 只给**最近 5 天**的逐日票房，跟这片上了 52 天还是 83 天无关。

   → 所以「票房时间序列」只能靠**每天跑一次快照**慢慢攒，这正是本模块的定位。

5. ``?movieId=N`` 还能拿到单片的 ``category``（类型，如 "神话,喜剧,冒险,动画"），
   这是我们判断「是不是动画片」的依据 —— 榜单本身不给类型。
"""

from __future__ import annotations

import logging
import re
from datetime import date
from typing import Any

from . import db as dbmod

log = logging.getLogger("animedata.maoyan")

# 专业版票房接口（主力数据源）
BOARD_URL = "https://piaofang.maoyan.com/dashboard-ajax/movie"
PIAOFANG_REFERER = "https://piaofang.maoyan.com/dashboard"

# 片单页（辅助：片名 / 类型 / 主演 / 上映日期 / 购票状态）
LIST_URL = "https://www.maoyan.com/films"
DETAIL_URL = "https://www.maoyan.com/films/{mid}"

BOARD_HEADERS = {
    "Referer": PIAOFANG_REFERER,
    "Accept": "application/json, text/plain, */*",
    "X-Requested-With": "XMLHttpRequest",
}

LIST_HEADERS = {
    "Referer": "https://www.maoyan.com/",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Upgrade-Insecure-Requests": "1",
}

FILMS_HREF_RE = re.compile(r"/films/(\d+)")

# 片单卡片文本形如：
#   购票 神探之痕迹 类型: 犯罪 主演: 张译／马丽／陈明昊 上映时间: 2026-10-01
_RE_CATEGORY = re.compile(r"类型\s*[:：]\s*(.+?)(?=\s*(?:主演|上映时间|$))")
_RE_ACTORS = re.compile(r"主演\s*[:：]\s*(.+?)(?=\s*(?:类型|上映时间|$))")
_RE_RELEASE = re.compile(r"上映时间\s*[:：]\s*(\d{4}-\d{2}-\d{2})")
_RE_RELEASE_DAYS = re.compile(r"上映\s*(\d+)\s*天")
_SEP_RE = re.compile(r"[／/、,，|]+")

ANIMATION_TAGS = ("动画", "动漫")


# ---------------------------------------------------------------------------
#  小工具
# ---------------------------------------------------------------------------
def _to_int(v: Any) -> int | None:
    try:
        if v is None or v == "":
            return None
        return int(v)
    except (TypeError, ValueError):
        return None


def _to_float(v: Any) -> float | None:
    try:
        if v is None or v == "":
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _split_unit(d: Any) -> tuple[float | None, str | None]:
    """``{"num": "5220.50", "unit": "万"}`` → ``(5220.5, "万")``。"""
    if not isinstance(d, dict):
        return None, None
    return _to_float(d.get("num")), (d.get("unit") or None)


def _unit_desc(num: float | None, unit: str | None) -> str | None:
    if num is None:
        return None
    txt = f"{num:g}"
    return f"{txt}{unit}" if unit else txt


def fmt_box_date(v: Any) -> str | None:
    """``20261001`` → ``"2026-10-01"``。"""
    s = str(v or "").strip()
    if len(s) == 8 and s.isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:]}"
    return s or None


def release_days(release_info: str | None) -> int | None:
    """``"上映52天"`` → 52，``"上映首日"`` → 1，``点映``/``展映`` → None。"""
    s = (release_info or "").strip()
    if not s:
        return None
    if "首日" in s:
        return 1
    m = _RE_RELEASE_DAYS.search(s)
    if m:
        return int(m.group(1))
    return None


def is_animation(category: str | None) -> int:
    cats = [c for c in _SEP_RE.split(category or "") if c]
    return 1 if any(any(t in c for t in ANIMATION_TAGS) for c in cats) else 0


# ---------------------------------------------------------------------------
#  一、专业版票房榜（主力）
# ---------------------------------------------------------------------------
def fetch_board(client, movie_id: str | None = None):
    """抓票房榜。``movie_id=None`` 时是在映全榜单 + 大盘。"""
    params = {"movieId": movie_id} if movie_id else None
    return client.get(
        BOARD_URL,
        params=params,
        headers=BOARD_HEADERS,
        source=f"maoyan:board:{movie_id or 'all'}",
    )


def parse_board(raw: dict, snapshot_date: str | None = None) -> tuple[list[dict], dict]:
    """解析榜单 JSON → ``(影片行列表, 大盘字典)``。

    ``rank_no`` 用列表顺序 —— 这个接口的列表本身就是按当日票房降序排的，
    是**真排名**（不像 HTML 片单页，那里的节点顺序混了 hover 浮层）。
    """
    snapshot_date = snapshot_date or date.today().isoformat()
    ml = raw.get("movieList") or {}
    nation = parse_nation(ml.get("nationBoxInfo") or {}, ml.get("updateInfo") or {})

    rows: list[dict[str, Any]] = []
    for rank, item in enumerate(ml.get("list") or [], 1):
        info = item.get("movieInfo") or {}
        mid = str(info.get("movieId") or "").strip()
        if not mid:
            continue
        box_fen = _to_int(item.get("box"))
        split_fen = _to_int(item.get("splitBox"))
        num, unit = _split_unit(item.get("boxSplitUnit"))
        release = (info.get("releaseInfo") or "").strip() or None

        rows.append({
            "snapshot_date": snapshot_date,
            "movie_id": mid,
            "rank_no": rank,
            "title": (info.get("movieName") or "").strip() or None,
            "release_info": release,
            "release_days": release_days(release),
            # box 单位是「分」，除 100 才是元。boxSplitUnit/sumBoxDesc 是官方文本
            "box_fen": box_fen,
            "box_yuan": round(box_fen / 100, 2) if box_fen is not None else None,
            "box_desc": (item.get("sumBoxDesc") or "").strip() or _unit_desc(num, unit),
            "split_box_fen": split_fen,
            "split_box_yuan": round(split_fen / 100, 2) if split_fen is not None else None,
            "split_box_desc": (item.get("sumSplitBoxDesc") or "").strip() or None,
            "box_rate": (item.get("boxRate") or "").strip() or None,
            "show_count": _to_int(item.get("showCount")),
            "show_count_rate": (item.get("showCountRate") or "").strip() or None,
            "avg_seat_view": (item.get("avgSeatView") or "").strip() or None,
            "avg_show_view": (item.get("avgShowView") or "").strip() or None,
            # 类型要另外请求 ?movieId=N 才知道，这里先留空，抓完回填
            "category": None,
            "is_animation": None,
            "detail_url": DETAIL_URL.format(mid=mid),
        })
    return rows, nation


def parse_nation(nbi: dict, update: dict) -> dict:
    """实时大盘。"""
    num, unit = _split_unit(nbi.get("nationBoxSplitUnit"))
    snum, sunit = _split_unit(nbi.get("nationSplitBoxSplitUnit"))
    return {
        "nation_box_num": num,
        "nation_box_unit": unit,
        "nation_box_desc": _unit_desc(num, unit),
        "nation_split_num": snum,
        "nation_split_unit": sunit,
        "nation_split_desc": _unit_desc(snum, sunit),
        "show_count_desc": (nbi.get("showCountDesc") or "").strip() or None,
        "view_count_desc": (nbi.get("viewCountDesc") or "").strip() or None,
        "update_timestamp": _to_int(update.get("updateTimestamp")),
        "update_gap_second": _to_int(update.get("updateGapSecond")),
    }


# ---------------------------------------------------------------------------
#  二、单片：类型 + 近 5 日逐日票房
# ---------------------------------------------------------------------------
def parse_movie_detail(raw: dict) -> dict:
    """解析 ``?movieId=N`` 的返回。"""
    mi = raw.get("movieInfo") or {}
    inner = mi.get("movieInfo") or {}
    trends: list[dict[str, Any]] = []
    for t in mi.get("boxTrends") or []:
        trends.append({
            "box_date": fmt_box_date(t.get("date")),
            # 这里的 box 单位是「元」，和榜单的「分」不一样！
            "box_yuan": _to_int(t.get("box")),
            "box_desc": (t.get("boxDesc") or "").strip() or None,
            "release_day": 1 if t.get("releaseDay") else 0,
        })
    return {
        "movie_id": str(inner.get("movieId") or "").strip() or None,
        "name": (inner.get("name") or "").strip() or None,
        "category": (inner.get("category") or "").strip() or None,
        "release_info": (inner.get("releaseInfo") or "").strip() or None,
        "img_url": inner.get("imgUrl"),
        "trends": trends,
    }


# ---------------------------------------------------------------------------
#  三、片单页（正在热映 + 即将上映/预售，含上映日期与主演）
# ---------------------------------------------------------------------------
def fetch_list_html(client, show_type: int = 1) -> str | None:
    f = client.get(
        LIST_URL, params={"showType": show_type},
        headers=LIST_HEADERS, source=f"maoyan:list:{show_type}",
    )
    if not f.ok:
        log.warning("猫眼片单页抓取失败（showType=%s）：%s", show_type, f.error or f.status)
        return None
    return f.text


def parse_list_html(
    html_text: str, show_type: int = 1, snapshot_date: str | None = None
) -> list[dict[str, Any]]:
    """解析片单页。

    **只认 ``film-channel`` 这一个类名。** 页面上 ``div.movie-item`` 有 54 个，
    但只有 18 个是「真卡片」，另外 36 个是 hover 浮层和纯标题节点、内容重复。
    按 ``movie-item`` 抓会得到 3 倍的行、还会把枚举序号当成排名（1,4,7,10…）。
    """
    from lxml import html as lxml_html

    snapshot_date = snapshot_date or date.today().isoformat()
    try:
        doc = lxml_html.fromstring(html_text)
    except Exception as exc:
        log.warning("猫眼片单页解析失败: %s", exc)
        return []

    cards = doc.xpath('//div[contains(@class,"film-channel")]')
    rows: list[dict[str, Any]] = []
    for card in cards:
        mid = None
        for a in card.xpath('.//a[contains(@href,"/films/")]'):
            m = FILMS_HREF_RE.search(a.get("href") or "")
            if m:
                mid = m.group(1)
                break
        if not mid:
            continue

        title = None
        for el in card.xpath(".//*[@title]"):
            t = (el.get("title") or "").strip()
            if t:
                title = t
                break
        if not title:
            for el in card.xpath('.//*[contains(@class,"movie-item-title")]'):
                t = (el.text_content() or "").strip()
                if t:
                    title = t
                    break

        text = re.sub(r"\s+", " ", card.text_content() or "").strip()

        # 购票 = 正在热映可买票；预售 = 还没上映
        if re.search(r"预售", text):
            status = "预售"
        elif re.search(r"购票|选座", text):
            status = "购票"
        else:
            status = None

        m_cat = _RE_CATEGORY.search(text)
        category = m_cat.group(1).strip() if m_cat else None
        m_act = _RE_ACTORS.search(text)
        actors = m_act.group(1).strip() if m_act else None
        m_rel = _RE_RELEASE.search(text)
        release_date = m_rel.group(1) if m_rel else None

        score = None
        for el in card.xpath('.//*[contains(@class,"score")]'):
            t = re.sub(r"\s+", "", el.text_content() or "")
            if t:
                score = t
                break

        rows.append({
            "snapshot_date": snapshot_date,
            "movie_id": mid,
            "show_type": int(show_type),
            "title": title,
            "status": status,
            "category": category,
            "is_animation": is_animation(category),
            "actors": actors,
            "release_date": release_date,
            "score": score,
            "detail_url": DETAIL_URL.format(mid=mid),
            "raw": text[:2000],
        })
    return rows


# ---------------------------------------------------------------------------
#  四、编排
# ---------------------------------------------------------------------------
def snapshot(client, cfg, conn, *, trends_limit: int | None = None) -> dict[str, Any]:
    """跑一次猫眼快照：票房榜 + 大盘 + 片单 + （可选）单片逐日票房。

    同一天重复跑会覆盖当天的行，不会堆重复数据（主键含 snapshot_date）。
    """
    if not cfg.maoyan.enabled:
        return {"skipped": "config 里 maoyan.enabled = false"}

    today = date.today().isoformat()
    result: dict[str, Any] = {"snapshot_date": today}
    board_rows: list[dict[str, Any]] = []

    # -- 1. 票房榜 ---------------------------------------------------------
    if cfg.maoyan.board:
        f = fetch_board(client)
        raw = f.json() if f.ok else None
        if isinstance(raw, dict):
            board_rows, nation = parse_board(raw, today)
            n = dbmod.upsert_maoyan_board(conn, board_rows)
            dbmod.upsert_maoyan_nation(conn, today, nation)
            result["board"] = {"rows": n, "nation": nation.get("nation_box_desc")}
            top = board_rows[0] if board_rows else {}
            print(f"  猫眼票房榜 {n} 条；大盘 {nation.get('nation_box_desc')}；"
                  f"榜首 {top.get('title')} {top.get('box_desc')}", flush=True)
        else:
            result["board"] = {"ok": False, "error": f.error or f.status}
            print(f"  [失败] 猫眼票房榜：{f.error or f.status}", flush=True)
    else:
        result["board"] = "未启用（maoyan.board = false）"

    # -- 2. 片单页（在映 + 待映/预售）--------------------------------------
    list_rows: list[dict[str, Any]] = []
    if cfg.maoyan.list_show_types:
        result["lists"] = {}
        for st in cfg.maoyan.list_show_types:
            html_text = fetch_list_html(client, st)
            if not html_text:
                result["lists"][st] = {"ok": False, "rows": 0}
                continue
            rows = parse_list_html(html_text, st, today)
            result["lists"][st] = {"ok": True, "rows": len(rows)}
            list_rows.extend(rows)
            label = "正在热映" if st == 1 else ("即将上映/预售" if st == 2 else f"showType={st}")
            print(f"  猫眼片单 showType={st}（{label}）解析到 {len(rows)} 条", flush=True)
        if list_rows:
            dbmod.upsert_maoyan_list(conn, list_rows)

    # -- 3. 单片逐日票房 + 类型回填 ----------------------------------------
    if cfg.maoyan.box_trends and board_rows:
        limit = cfg.maoyan.box_trends_limit if trends_limit is None else trends_limit
        targets = board_rows if not limit or limit <= 0 else board_rows[:limit]
        print(f"  抓单片类型 + 近 5 日逐日票房：{len(targets)} 部…", flush=True)
        done = trend_rows = failed = 0
        for i, r in enumerate(targets, 1):
            f = fetch_board(client, r["movie_id"])
            raw = f.json() if f.ok else None
            if isinstance(raw, dict):
                detail = parse_movie_detail(raw)
                r["category"] = detail["category"]
                r["is_animation"] = is_animation(detail["category"])
                if detail["trends"]:
                    trend_rows += dbmod.upsert_maoyan_trends(
                        conn, today, r["movie_id"], detail["trends"]
                    )
                done += 1
            else:
                failed += 1
            if i % 10 == 0 or i == len(targets):
                print(f"    {i}/{len(targets)}（逐日 {trend_rows} 行，失败 {failed}）", flush=True)
        # 类型是这一步才知道的，回填榜单
        if done:
            dbmod.upsert_maoyan_board(conn, targets)
        result["trends"] = {"done": done, "failed": failed, "rows": trend_rows}
    elif not cfg.maoyan.box_trends:
        result["trends"] = "未启用（maoyan.box_trends = false）"
    else:
        result["trends"] = "跳过（榜单没数据）"

    result["list_rows"] = len(list_rows)
    return result
