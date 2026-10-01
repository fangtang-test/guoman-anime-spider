"""编排层：把配置、存储、采集、导出串起来，并对外提供命令行入口。"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path
from typing import Any

from . import config as cfgmod
from . import db as dbmod
from . import douban, export, maoyan
from .http import HttpClient

log = logging.getLogger("animedata")

BANNER = r"""
   ____        _                 ____        _     _
  / ___|_ __  (_)_ __  ___       / ___| _ __ (_) __| | ___ _ __
  \___ \| '_ \ | | '_ \/ __|_____\___ \| '_ \| |/ _` |/ _ \ '__|
   ___) | | | || | |_) \__ \_____|___) | |_) | | (_| |  __/ |
  |____/|_| |_|/ | .__/|___/     |____/| .__/|_|\__,_|\___|_|
             |__/|_|                   |_|
        国漫数据采集  ·  豆瓣 + 猫眼  →  SQLite  →  Tableau
"""


# ---------------------------------------------------------------------------
#  基础设施
# ---------------------------------------------------------------------------
def setup_logging(verbose: bool = False, log_file: Path | None = None) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    if log_file:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        handlers=handlers,
        force=True,
    )
    # 让第三方库安静点
    for noisy in ("urllib3", "charset_normalizer", "chardet"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def open_db(cfg):
    conn = dbmod.connect(cfg.db_path)
    note = dbmod.init_and_migrate(conn)
    if note:
        print(f"  [迁移] {note}", flush=True)
    return conn


def make_client(cfg, conn) -> HttpClient:
    def on_log(source: str, f, error: str | None) -> None:
        dbmod.log_fetch(
            conn,
            source=source or "unknown",
            url=f.url,
            status=f.status,
            encoding=f.encoding or None,
            size_bytes=f.size,
            elapsed_ms=f.elapsed_ms,
            attempt=f.attempt,
            error=error or f.error,
        )

    return HttpClient(cfg.http, on_log=on_log)


# ---------------------------------------------------------------------------
#  命令
# ---------------------------------------------------------------------------
def cmd_doctor(cfg) -> int:
    """体检：环境、依赖、网络、接口是否可用。"""
    print(BANNER)
    print("=" * 68)
    print(" 环境体检")
    print("=" * 68)

    print(f"Python           : {sys.version.split()[0]}  ({sys.executable})")
    print(f"配置文件         : {cfg.source_path or '（未找到，使用内置默认值）'}")
    print(f"数据库           : {cfg.db_path}")
    print(f"导出目录         : {cfg.export_dir}")
    print(f"请求间隔         : {cfg.http.min_delay} ~ {cfg.http.max_delay} 秒")
    print(f"超时 / 重试      : {cfg.http.timeout}s / {cfg.http.retries} 次")
    print(f"代理             : {cfg.http.proxies or '直连（或跟随系统环境变量）'}")

    print("\n[依赖]")
    deps = [
        ("requests", "必需", "HTTP 请求"),
        ("lxml", "必需", "猫眼 HTML 解析"),
        ("pandas", "必需", "导出"),
        ("pyarrow", "推荐", "parquet 导出"),
        ("charset_normalizer", "推荐", "编码猜测"),
        ("fonttools", "可选", "石头字体解码（只在抠预售票房时用，主流程不需要）"),
        ("tableauhyperapi", "可选", ".hyper 直出"),
    ]
    for mod, need, why in deps:
        try:
            __import__(mod)
            print(f"  [OK]   {mod:<20} {why}")
        except ImportError:
            mark = "缺失" if need == "必需" else "未装"
            print(f"  [{mark}] {mod:<20} {why}")

    print("\n[接口连通性]")
    client = HttpClient(cfg.http)
    try:
        # 枚举接口：type 参数实测被忽略，所以这里逐个标签试，确认都能拿到数据
        for tag in cfg.douban.tags[:4]:
            f = client.get(
                douban.SEARCH_URL,
                params={"tags": tag, "sort": "U", "range": "0,10", "start": 0},
                headers=douban.SEARCH_HEADERS,
                source=f"doctor:list:{tag}",
            )
            if f.ok:
                data = f.json()
                n = len(data.get("data") or []) if isinstance(data, dict) else 0
                mark = "OK" if n else "--"
                print(f"  [{mark}]   豆瓣列表 tags={tag:<8} HTTP {f.status}  "
                      f"返回 {n} 条  编码={f.encoding}")
            else:
                print(f"  [失败] 豆瓣列表 tags={tag:<8} {f.error or f.status}")

        # 详情走 fetch_detail（movie → tv → abstract 三级兜底），这样测的是
        # 采集时真正会走的路径。直接打 /movie/ 会误报：有些剧集只认 /tv/。
        # 限流时 fetch_detail 会抛 RateLimited（而不是返回降级数据），体检必须
        # 把它当成一条**有意义的诊断**报出来，不能让它把体检整个炸掉。
        try:
            d = douban.fetch_detail(client, "36882191")
        except douban.RateLimited as exc:
            print(f"  [限流] 豆瓣详情接口      {exc}\n"
                  f"         └ 这不是故障：豆瓣按 IP 限流（HTTP 400 + code 1309），"
                  f"等几十分钟再跑即可")
            d = None
        if isinstance(d, dict) and (d.get("id") or d.get("title")):
            print(f"  [OK]   豆瓣详情接口      {len(d)} 个字段  "
                  f"命中={d.get('_kind')}  降级={bool(d.get('_degraded'))}")
        elif d is not None:
            print("  [失败] 豆瓣详情接口      movie / tv / abstract 三路全挂")

        # 猫眼票房榜：专业版接口，免签名、免浏览器。这是猫眼侧真正的数据源
        fb = client.get(
            maoyan.BOARD_URL, headers=maoyan.BOARD_HEADERS, source="doctor:board"
        )
        board_rows: list[dict] = []
        if fb.ok and isinstance(fb.json(), dict):
            board_rows, nation = maoyan.parse_board(fb.json())
            top = board_rows[0] if board_rows else {}
            print(f"  [OK]   猫眼票房榜        HTTP {fb.status}  {len(board_rows)} 部在映  "
                  f"大盘={nation.get('nation_box_desc')}  "
                  f"榜首={top.get('title')} {top.get('box_desc')}")
        else:
            print(f"  [失败] 猫眼票房榜        {fb.error or fb.status}")

        # 单片：类型 + 近 5 日逐日票房（榜单不给类型，得单独问）
        if board_rows:
            mid = board_rows[0]["movie_id"]
            f4 = client.get(
                maoyan.BOARD_URL, params={"movieId": mid},
                headers=maoyan.BOARD_HEADERS, source="doctor:movie",
            )
            if f4.ok and isinstance(f4.json(), dict):
                d = maoyan.parse_movie_detail(f4.json())
                print(f"  [OK]   猫眼单片详情      HTTP {f4.status}  {d['name']}  "
                      f"类型={d['category']}  逐日票房 {len(d['trends'])} 天")
            else:
                print(f"  [失败] 猫眼单片详情      {f4.error or f4.status}")

        # 片单页：1 = 正在热映，2 = 即将上映/预售（别搞反）
        for st, label in ((1, "正在热映"), (2, "即将上映/预售")):
            f5 = client.get(
                maoyan.LIST_URL, params={"showType": st},
                headers=maoyan.LIST_HEADERS, source=f"doctor:list:{st}",
            )
            if f5.ok:
                rows = maoyan.parse_list_html(f5.text, st)
                n_anim = sum(1 for r in rows if r.get("is_animation"))
                print(f"  [OK]   猫眼片单 {label:<11} HTTP {f5.status}  "
                      f"解析出 {len(rows)} 条（其中动画 {n_anim}）")
            else:
                print(f"  [失败] 猫眼片单 {label:<11} {f5.error or f5.status}")
    finally:
        client.close()

    print("\n体检完成。")
    return 0


def cmd_verify(cfg, samples: int = 8) -> int:
    """验证标签枚举的覆盖范围 —— 电影和剧集是不是都拿到了。

    背景：豆瓣枚举接口的 ``type`` 参数实测**被完全忽略**（传 movie / tv /
    不传返回同一批数据），所以区分电影和剧集只能靠标签。这个命令要做两件事：

    1. 看每个标签各自能翻到哪些条目；
    2. 专门挑「只被某一个标签收录」的条目去查详情，看它们里面有没有剧集。
       如果某个标签独有的条目里既有电影又有剧集，说明这组标签是必要的。

    只消耗 标签数 + 抽样数 次请求，很快。
    """
    print("验证豆瓣标签枚举是否同时覆盖电影与剧集…\n")
    conn = open_db(cfg)
    client = make_client(cfg, conn)
    try:
        print("[第一层] 各标签首页返回情况")
        tag_ids: dict[str, list[str]] = {}
        first_seen: dict[str, str] = {}
        for tag in cfg.douban.tags:
            rows = douban.fetch_list_page(client, tag, 0)
            if rows is None:
                print(f"  {tag:<10} 请求失败")
                tag_ids[tag] = []
                continue
            ids = [str(r.get("id")) for r in rows if r.get("id")]
            tag_ids[tag] = ids
            for sid in ids:
                first_seen.setdefault(sid, tag)
            titles = [r.get("title") for r in rows[:5]]
            print(f"  {tag:<10} {len(ids):>3} 条   样例: {titles}")

        print(f"\n  多个标签并集去重后共 {len(first_seen)} 条（仅首页）")

        # 每个标签各挑几条「只被自己收录」的条目 —— 这正是其它标签会漏掉的
        per_tag = max(1, samples // max(1, len(tag_ids)))
        picked: list[tuple[str, str]] = []
        for tag, ids in tag_ids.items():
            only = [i for i in ids if first_seen.get(i) == tag]
            for sid in only[:per_tag]:
                picked.append((sid, tag))
        if not picked:
            print("  [i] 没有标签独有条目可抽样（首页重叠较多），跳过第二层")
            return 0

        print(f"\n[第二层] 抽样查详情：挑 {len(picked)} 条「只被单个标签收录」的条目")
        movies = tv = other = fail = 0
        for sid, tag in picked:
            raw = douban.fetch_detail(client, sid)
            if not raw:
                fail += 1
                print(f"  {sid:<10} [失败]")
                continue
            is_tv = bool(raw.get("is_tv"))
            kind = "剧集/番剧" if is_tv else "电影"
            if is_tv:
                tv += 1
            else:
                movies += 1
            title = douban.clean_title(raw.get("title"))
            eps = raw.get("episodes_count") or 0
            print(f"  {sid:<10} [{tag:<8}] {kind:<8} 集数={eps:<3} {title}")

        print()
        print(f"  抽样结果：电影 {movies} 部，剧集 {tv} 部，失败 {fail} 条")
        if movies and tv:
            print("  [OK] 电影和剧集都覆盖到了，config.toml 的 tags 组合没问题。")
        elif movies and not tv:
            print("  [!] 抽样里只有电影 —— 剧集可能漏了，考虑给 tags 补上「日本动画」。")
        elif tv and not movies:
            print("  [!] 抽样里只有剧集 —— 电影可能漏了，考虑给 tags 补上「动画」。")
        else:
            print("  [!] 抽样全部失败，先跑 doctor 看看网络和接口。")
    finally:
        client.close()
        conn.close()
    return 0


def cmd_selftest(cfg) -> int:
    """离线自检：编码自适应、字段解析、宽表逻辑。

    这几处没法靠联网验证 —— 豆瓣和猫眼现在返回的都是 UTF-8，老页面的
    GBK 场景碰不上。所以这里用**构造出来的字节流**直接打进去测，
    不消耗任何网络请求，随时可以重跑。

    编码这块尤其值得测：真实世界的坑是「响应头写 ISO-8859-1（HTTP/1.1
    的默认值），内容其实是 GBK」，光看响应头一定解错。
    """
    import json as _json

    from . import export as expmod
    from .http import decode_bytes, looks_garbled

    results: list[tuple[str, bool, str]] = []

    def check(name: str, got: Any, want: Any) -> None:
        # pandas 的 NA 参与比较会得到 NA，直接 if 会抛
        # TypeError: boolean value of NA is ambiguous —— 那是「不相等」，
        # 不是测试框架该崩的地方。
        try:
            ok = bool(got == want)
        except (TypeError, ValueError):
            ok = False
        results.append((name, ok, "" if ok else f"得到 {got!r}，期望 {want!r}"))

    def check_true(name: str, got: Any) -> None:
        results.append((name, bool(got), "" if got else f"实际是 {got!r}"))

    print(BANNER)
    print("=" * 68)
    print(" 离线自检（不联网）")
    print("=" * 68)

    # ---- 编码自适应 -------------------------------------------------------
    print("\n[编码自适应]")
    zh = "国产动画《雾山五行》评分 9.4 分，导演 林魂"
    gbk = zh.encode("gb18030")
    zh_long = (zh + "。") * 8
    gbk_long = zh_long.encode("gb18030")

    # 1. 最经典的坑：响应头谎报 latin-1，内容其实是 GBK
    text, enc = decode_bytes(gbk, "text/html; charset=ISO-8859-1")
    check("响应头谎报 ISO-8859-1、实际 GBK", text, zh)

    # 2. 响应头没写 charset，编码只在 meta 里
    html = b'<html><head><meta charset="gb2312"></head><body>' + gbk + b"</body></html>"
    text, enc = decode_bytes(html, "text/html")
    check_true("无 charset 响应头、meta 写 gb2312", zh in text)

    # 3. 响应头明确写 gbk / gb2312，应归一到 gb18030（超集，解码更宽容）
    text, enc = decode_bytes(gbk, "text/html; charset=gbk")
    check("响应头 charset=gbk 归一到 gb18030", (text, enc), (zh, "gb18030"))

    # 4. 纯 ASCII 配可疑编码 —— 应当接受，不该被跳过后解错
    text, enc = decode_bytes(b"hello world", "text/html; charset=iso-8859-1")
    check("纯 ASCII + 可疑编码", (text, enc), ("hello world", "ascii"))

    # 5. UTF-8 BOM 必须被剥离（ALIASES 里若把 utf-8-sig 归一成 utf-8 就会残留 \ufeff）
    text, enc = decode_bytes(b"\xef\xbb\xbf" + zh.encode("utf-8"), "text/html")
    check("UTF-8 BOM 被正确剥离", (text, enc), (zh, "utf-8-sig"))

    # 6. 毫无线索的 UTF-8
    text, enc = decode_bytes(zh.encode("utf-8"), None)
    check("无任何线索 → UTF-8", (text, enc), (zh, "utf-8"))

    # 7. 毫无线索的 GBK（响应头连 Content-Type 都没有）
    text, enc = decode_bytes(gbk_long, None)
    check("无任何线索的老 GBK 页面也能救回来", text, zh_long)

    # 8. 乱码检测
    check_true("乱码检测：坏解码能识别出来", looks_garbled("\ufffd" * 200))
    check("乱码检测：正常中文不误报", looks_garbled(zh * 20), False)

    # ---- 字段解析 ---------------------------------------------------------
    print("\n[字段解析]")
    check("时长「144分钟」", douban.parse_duration_min(["144分钟"]), 144)
    check("时长「1小时30分钟」", douban.parse_duration_min(["1小时30分钟"]), 90)
    check("时长「24min」", douban.parse_duration_min(["24min"]), 24)
    check("时长缺省", douban.parse_duration_min(None), None)
    check("title 清理尾部年份", douban.clean_title("八仙！\u200e (2026)"), "八仙！")
    check("title 清理不动正常标题", douban.clean_title("雾山五行"), "雾山五行")

    raw = {
        "id": "34780991",
        "title": "哪吒之魔童闹海",
        "year": "2025",
        "subtype": "movie",
        "is_tv": False,
        "rating": {"value": 8.5, "count": 1203456, "star_count": 4.5},
        "genres": ["喜剧", "动画", "奇幻"],
        "countries": ["中国大陆"],
        "languages": ["汉语普通话"],
        "durations": ["144分钟"],
        "pubdate": ["2025-01-29(中国大陆)"],
        "episodes_count": 0,
        "directors": [{"name": "饺子"}],
        "actors": [{"name": "吕艳婷"}, {"name": "囧森瑟夫"}],
        "intro": "  哪吒与敖丙……  ",
        "comment_count": 500000,
        "review_count": 3000,
        "cover_url": "https://img.example/x.jpg",
        "url": "https://movie.douban.com/subject/34780991/",
        "aka": ["Ne Zha 2"],
    }
    row = douban.flatten_detail("34780991", raw)
    check("flatten: is_tv", row["is_tv"], 0)
    check("flatten: 年份", row["year"], 2025)
    check("flatten: 上映年", row["release_year"], 2025)
    check("flatten: 评分", row["rating_value"], 8.5)
    check("flatten: 评分人数", row["rating_count"], 1203456)
    check("flatten: 时长分钟", row["duration_min"], 144)
    check("flatten: 类型", _json.loads(row["genres"]), ["喜剧", "动画", "奇幻"])
    check("flatten: 导演", _json.loads(row["directors"]), ["饺子"])
    check("flatten: 简介去空白", row["intro"], "哪吒与敖丙……")

    tv_raw = dict(raw, is_tv=True, subtype="tv", episodes_count=12,
                  durations=["24分钟"], title="雾山五行")
    tv_row = douban.flatten_detail("1", tv_raw)
    check("flatten: 剧集 is_tv", tv_row["is_tv"], 1)
    check("flatten: 剧集集数", tv_row["episodes_count"], 12)

    # ---- 猫眼 -------------------------------------------------------------
    print("\n[猫眼解析]")
    check("上映状态「上映52天」", maoyan.release_days("上映52天"), 52)
    check("上映状态「上映首日」", maoyan.release_days("上映首日"), 1)
    check("上映状态「点映」", maoyan.release_days("点映"), None)
    check("类型含动画", maoyan.is_animation("神话,喜剧,冒险,动画"), 1)
    check("类型不含动画", maoyan.is_animation("犯罪"), 0)
    check("类型为空", maoyan.is_animation(None), 0)
    check("逐日票房日期 20261001", maoyan.fmt_box_date(20261001), "2026-10-01")

    # 榜单：box 单位是「分」，除 100 才是元
    board, nation = maoyan.parse_board({
        "movieList": {
            "list": [{
                "avgSeatView": "6.9%", "avgShowView": "10.5",
                "box": 5220501131, "boxRate": "38.0%",
                "boxSplitUnit": {"num": "5220.50", "unit": "万"},
                "movieInfo": {"movieId": 1552906, "movieName": "神探之痕迹",
                              "releaseInfo": "上映首日"},
                "showCount": 138879, "showCountRate": "29.4%",
                "splitBox": 4605855160, "splitBoxRate": "37.9%",
                "sumBoxDesc": "5220.5万", "sumSplitBoxDesc": "4605.8万",
            }],
            "nationBoxInfo": {
                "nationBoxSplitUnit": {"num": "13740.2", "unit": "万"},
                "nationSplitBoxSplitUnit": {"num": "12132.5", "unit": "万"},
                "showCountDesc": "47.1万", "title": "实时大盘",
                "viewCountDesc": "379.0万",
            },
            "updateInfo": {"updateGapSecond": 5, "updateTimestamp": 1790846718434},
        }
    })
    check("榜单条数", len(board), 1)
    check("榜单排名是真排名（从 1 开始）", board[0]["rank_no"], 1)
    check("榜单 box 分 → 元", board[0]["box_yuan"], 52205011.31)
    check("榜单票房文本", board[0]["box_desc"], "5220.5万")
    check("榜单上映天数", board[0]["release_days"], 1)
    check("大盘票房", nation["nation_box_desc"], "13740.2万")
    check("大盘观影人次", nation["view_count_desc"], "379.0万")

    # 单片：这里的 box 单位是「元」，跟榜单的「分」不一样
    d = maoyan.parse_movie_detail({
        "movieInfo": {
            "movieInfo": {"movieId": 1552906, "name": "神探之痕迹",
                          "category": "神话,喜剧,冒险,动画", "releaseInfo": "上映首日"},
            "boxTrends": [{"box": 10633807, "boxDesc": "1063.3万",
                           "date": 20260927, "releaseDay": False}],
        }
    })
    check("单片类型", d["category"], "神话,喜剧,冒险,动画")
    check("单片逐日票房单位是元（不再除以 100）", d["trends"][0]["box_yuan"], 10633807)
    check("单片逐日日期", d["trends"][0]["box_date"], "2026-09-27")

    # 片单：页面上 movie-item 有 3 倍重复节点（真卡片 + hover 浮层 + 纯标题），
    # 只有 film-channel 是真卡片。这里就是这么构造的 —— 解析出 3 条就是回归了
    card_html = """
    <html><body>
      <div class="movie-item movie-item-hover film-channel">
        <a href="/films/1552906" data-val="{movieid:1552906}">
          <div class="channel-detail movie-item-title" title="神探之痕迹">神探之痕迹</div>
        </a>
        <div class="channel-detail">购票 神探之痕迹 类型: 犯罪 主演: 张译／马丽／陈明昊
          上映时间: 2026-10-01</div>
      </div>
      <div class="movie-item movie-item-hover">
        <a href="/films/1552906"><div class="movie-item-title" title="神探之痕迹">神探之痕迹</div></a>
      </div>
      <div class="movie-item">
        <div class="channel-detail movie-item-title" title="神探之痕迹">神探之痕迹</div>
      </div>
      <div class="movie-item movie-item-hover film-channel">
        <a href="/films/1578266"><div class="movie-item-title" title="重生2">重生2</div></a>
        <div class="channel-detail">预售 重生2 类型: 犯罪／动作 主演: 梁洛施／文俊辉
          上映时间: 2026-10-03</div>
      </div>
    </body></html>
    """
    lrows = maoyan.parse_list_html(card_html, 1)
    check("片单只认真卡片（不是 3 倍重复）", len(lrows), 2)
    check("片单 movie_id", [r["movie_id"] for r in lrows], ["1552906", "1578266"])
    check("片单片名", lrows[0]["title"], "神探之痕迹")
    check("片单购票状态", lrows[0]["status"], "购票")
    check("片单预售状态", lrows[1]["status"], "预售")
    check("片单类型", lrows[0]["category"], "犯罪")
    check("片单类型（多值）", lrows[1]["category"], "犯罪／动作")
    check("片单主演", lrows[0]["actors"], "张译／马丽／陈明昊")
    check("片单上映日期", lrows[1]["release_date"], "2026-10-03")

    # ---- 宽表 -------------------------------------------------------------
    print("\n[Tableau 宽表]")
    import pandas as pd

    wide = expmod.build_tableau(
        pd.DataFrame([row, tv_row]), pd.DataFrame()
    )
    check("宽表行数", len(wide), 2)
    check("宽表：作品类型判定", list(wide["作品类型"]), ["电影", "剧集/番剧"])
    check("宽表：是否国产", list(wide["是否国产"]), ["是", "是"])
    check("宽表：评分档", list(wide["评分档"]), ["8.0-8.9", "8.0-8.9"])
    check("宽表：主类型", list(wide["主类型"]), ["喜剧", "喜剧"])
    check("宽表：类型数", list(wide["类型数"]), [3, 3])
    check("宽表：集数（电影为空）", wide["集数"].isna().tolist(), [True, False])

    # 猫眼并入：连接键只有片名，归一化后**精确**匹配，匹配不上必须留空
    board_df = pd.DataFrame([
        {"snapshot_date": "2026-10-01", "movie_id": 9001, "title": "雾山五行 ",
         "box_desc": "1.2亿", "box_rate": "8.0%", "show_count_rate": "5.0%",
         "avg_seat_view": "9.0%", "avg_show_view": "12.0", "show_count": 100,
         "rank_no": 3, "release_info": "上映10天", "category": "动作,动画"},
        {"snapshot_date": "2026-10-01", "movie_id": 9002, "title": "雾山五行第二季",
         "box_desc": "9.9亿", "box_rate": "1.0%", "show_count_rate": "1.0%",
         "avg_seat_view": "1.0%", "avg_show_view": "1.0", "show_count": 1,
         "rank_no": 9, "release_info": "上映1天", "category": "动画"},
    ])
    merged = expmod.build_tableau(pd.DataFrame([row, tv_row]), board_df)
    check("并猫眼：片名归一化后匹配（尾部空格不算差异）",
          list(merged["猫眼累计票房"]), ["", "1.2亿"])
    check("并猫眼：排名（匹配上的才有值）",
          merged["猫眼当日排名"].isna().tolist(), [True, False])
    check("并猫眼：不误配近似片名（雾山五行第二季不能被算进来）",
          list(merged["猫眼类型"]), ["", "动作,动画"])

    # 弱键（去副标题）必须**两边都真的去掉了副标题**才敢用
    duo = pd.DataFrame([
        dict(row, title="复仇者联盟4：终局之战", aka=["Avengers 4"]),
        dict(row, title="小猪佩奇", aka=[]),
        dict(row, title="这个杀手不太冷", aka=["Leon", "终极追杀令"]),
    ])
    weak = pd.DataFrame([
        {"snapshot_date": "2026-10-01", "movie_id": 248172,
         "title": "复仇者联盟4：终局之战（加码臻享版）", "box_desc": "43.68亿",
         "rank_no": 4, "category": "动作,科幻"},
        {"snapshot_date": "2026-10-01", "movie_id": 1528975,
         "title": "小猪佩奇·完美假期", "box_desc": "1786.3万",
         "rank_no": 3, "category": "动画,喜剧"},
        {"snapshot_date": "2026-10-01", "movie_id": 660,
         "title": "Leon", "box_desc": "1.0亿", "rank_no": 5, "category": "动作"},
    ])
    w = expmod.build_tableau(duo, weak)
    check("并猫眼：去副标题后能匹配（复联4 ↔ 复联4加码臻享版）",
          w["猫眼累计票房"].iloc[0], "43.68亿")
    check("并猫眼：弱键不得把《小猪佩奇·完美假期》挂到《小猪佩奇》上",
          w["猫眼累计票房"].iloc[1], "")
    check("并猫眼：豆瓣别名能当连接键（Leon）",
          w["猫眼累计票房"].iloc[2], "1.0亿")
    check("并猫眼：弱键匹配后排名也带过来",
          w["猫眼当日排名"].iloc[0], 4)

    missing = [name for name, _t, _d in expmod.FIELD_DICT if name not in wide.columns]
    check("字段说明覆盖了宽表所有列", missing, [])
    undocumented = [c for c in wide.columns if c not in {n for n, _t, _d in expmod.FIELD_DICT}]
    check("宽表没有未写进字段说明的列", undocumented, [])

    # ---- 限流恢复 ---------------------------------------------------------
    # 豆瓣限流返回的「HTTP 200 + 空数据」跟真的翻到底长得一模一样，
    # 唯一的区别是「等一会儿再问就有了」。这里用假数据源把各种情形跑一遍，
    # 等待时间压到毫秒级，所以这组测试是瞬间完成的。
    print("\n[限流恢复]")

    def _run(fetcher, tags=("动画",), **kw):
        kw.setdefault("max_start", 40)
        kw.setdefault("empty_recheck_wait", 0.01)
        kw.setdefault("empty_recheck_max", 0.02)
        kw.setdefault("empty_retries", 2)
        kw.setdefault("empty_pages_stop", 2)
        kw.setdefault("tag_cooldown", 0.0)
        return list(douban.iter_list(None, list(tags), fetcher=fetcher, **kw))

    def _pages(pages, empty_first=0):
        """同一个 start 前 empty_first 次返回空，之后正常返回 pages 条。"""
        seen: dict[int, int] = {}

        def f(_client, _tag, start):
            seen[start] = seen.get(start, 0) + 1
            if seen[start] <= empty_first:
                return []
            return [{"id": f"{start}-{i}"} for i in range(pages)]

        return f

    # 1. 先空一次、重试就有数据 —— 限流不能吃掉数据
    check("空页重试后数据照收（2 页 × 20 条）", len(_run(_pages(20, empty_first=1))), 40)

    # 2. 一直空 —— 必须收工，不能死循环
    check("一直空页 → 收工且不产出", len(_run(_pages(20, empty_first=99))), 0)

    # 3. 首页空页容忍更多次重试（第 4 次才恢复，普通位置只给 2 次）
    seen: dict[int, int] = {}

    def _slow_first(_client, _tag, start):
        seen[start] = seen.get(start, 0) + 1
        if start == 0 and seen[start] <= 3:
            return []
        return [{"id": f"{start}-{i}"} for i in range(20)]

    check("首页空页多试几次也能救回来", len(_run(_slow_first)), 40)

    # 4. 请求彻底失败（None）就停在原地，已拿到的照常留下，下轮续采
    def _fail_at_20(_client, _tag, start):
        return None if start >= 20 else [{"id": f"{start}-{i}"} for i in range(20)]

    check("请求失败 → 已拿到的留下、start 不推进", len(_run(_fail_at_20)), 20)

    # 5. 断点续爬：resume 说这个标签上一轮翻到 20 了，就该从 20 接着来
    started_at: list[int] = []

    def _record(_client, _tag, start):
        started_at.append(start)
        return [{"id": str(start)}]

    list(douban.iter_list(None, ["动画"], max_start=40, empty_pages_stop=1,
                          empty_recheck_wait=0, tag_cooldown=0,
                          resume=lambda _t: 20, fetcher=_record))
    check("断点续爬从上次的 start 接着翻", started_at[0], 20)

    # ---- 详情补全的优先级 -------------------------------------------------
    # --limit 是按这个顺序截断的，所以顺序不是小事：正在上映的新片在豆瓣
    # 常常还没出分，按评分排必然排到最后 —— 而那恰恰是唯一能跟猫眼票房
    # 对上的部分（实测猫眼在映榜有 17 部在豆瓣列表层，却只有 4 部进了详情层）。
    print("\n[详情补全优先级]")
    import sqlite3 as _sqlite3

    mem = _sqlite3.connect(":memory:")
    mem.row_factory = _sqlite3.Row
    dbmod.init_db(mem)
    dbmod.upsert_list_rows(mem, [
        {"subject_id": "1", "title": "高分老片", "rate": 9.8},
        {"subject_id": "2", "title": "在映新片", "rate": None},
    ])
    dbmod.upsert_maoyan_board(mem, [
        {"snapshot_date": "2026-10-01", "movie_id": "9", "title": "在映新片", "rank_no": 1},
    ])
    check("补详情优先补猫眼榜上的在映片（哪怕它还没出分）",
          dbmod.pending_detail_ids(mem), ["2", "1"])

    # 没有猫眼表的时候不能崩（旧库 / 只跑豆瓣的场景）
    bare = _sqlite3.connect(":memory:")
    bare.row_factory = _sqlite3.Row
    dbmod.init_db(bare)
    dbmod.upsert_list_rows(bare, [{"subject_id": "1", "title": "甲", "rate": 1.0}])
    bare.execute("DROP TABLE maoyan_board")
    check("没有猫眼表时退化成按评分排序", dbmod.pending_detail_ids(bare), ["1"])

    # 降级行（已入库但是错数据）要排在「完全没抓过」的行前面 —— 前者会直接
    # 进宽表，后者只是缺。用 --limit 截断时才不会一直修不到坏数据。
    dbmod.upsert_details(mem, [{
        "subject_id": "1", "title": "高分老片", "detail_json": '{"_degraded": true}',
    }])
    check("降级行排在没抓过的行前面（先修坏数据）",
          dbmod.pending_detail_ids(mem), ["1", "2"])
    mem.close()
    bare.close()

    # ---- 豆瓣限流（HTTP 400 + code 1309）---------------------------------
    # 这是本项目最贵的一个坑：限流走 400 而不是 429，早期版本把它当成
    # 「端点坏了」直接降级，结果 161 条电影的详情被静默削成轻量字段，
    # 而且因为行已存在，再也不会被重抓。
    print("\n[豆瓣限流]")
    import json as _json

    from animedata.http import Fetched as _Fetched

    def _resp(status: int, body) -> _Fetched:
        raw = _json.dumps(body).encode("utf-8")
        return _Fetched(url="u", status=status, content=raw, text=raw.decode("utf-8"))

    _rl = _resp(400, {"request": "GET /v2/movie/1", "msg": "subject_ip_rate_limit",
                      "code": 1309, "localized_message": "您所在的网络存在异常"})
    check("限流页认得出（code 1309）", douban._is_rate_limited(_rl), True)
    check("普通 404 不算限流",
          douban._is_rate_limited(_resp(404, {"msg": "not found"})), False)
    check("非 JSON 的 400 不算限流",
          douban._is_rate_limited(_Fetched(url="u", status=400,
                                           content=b"<html>", text="<html>")), False)
    check("成功的响应不算限流",
          douban._is_rate_limited(_resp(200, {"id": "1"})), False)

    class _FakeClient:
        """按脚本吐响应，并记录打过哪些 URL。"""

        def __init__(self, script):
            self.script = script
            self.calls = []

        def get(self, url, **_kw):
            self.calls.append(url)
            kind = "tv" if "/tv/" in url else "movie"
            return self.script(kind, len(self.calls))

        def get_json(self, url, **_kw):
            self.calls.append(url)
            if "subject_abstract" in url:
                return {"r": 0, "subject": {"title": "轻量版", "rate": "7.0"}}
            return None

    # 把等待压到 0，否则一条测试要等 135 秒
    _real_waits = douban._RATE_LIMIT_WAITS
    douban._RATE_LIMIT_WAITS = (0.0, 0.0)
    try:
        # 1) 两个 kind 都被限流 → 抛 RateLimited，且**不许**退回 abstract
        c = _FakeClient(lambda kind, n: _rl)
        try:
            douban.fetch_detail(c, "1")
            raised = False
        except douban.RateLimited:
            raised = True
        check("一直限流时抛 RateLimited（而不是降级）", raised, True)
        check("限流时不打 abstract 接口",
              any("subject_abstract" in u for u in c.calls), False)
        check("movie 限流后会再给 tv 一次机会",
              any("/tv/" in u for u in c.calls), True)

        # 2) movie 限流但 tv 通（实测就是这样：电影走 /movie/、剧集走 /tv/）
        c2 = _FakeClient(
            lambda kind, n: _resp(200, {"id": "5", "title": "剧"}) if kind == "tv" else _rl
        )
        got = douban.fetch_detail(c2, "5")
        check("movie 被限流但 tv 通时照样拿到详情",
              (got or {}).get("title"), "剧")

        # 3) 两个 kind 都 404（这部片真的没有 rexxar 详情）→ 退回 abstract
        c3 = _FakeClient(lambda kind, n: _resp(404, {"msg": "not found"}))
        got3 = douban.fetch_detail(c3, "5") or {}
        check("两个 kind 都 404 时才降级", got3.get("title"), "轻量版")
        check("降级行打了 _degraded 标记", got3.get("_degraded"), True)
        check("降级确实打了 abstract",
              any("subject_abstract" in u for u in c3.calls), True)
    finally:
        douban._RATE_LIMIT_WAITS = _real_waits

    # 降级行必须能被重抓，否则残缺数据永久卡在库里
    dg = _sqlite3.connect(":memory:")
    dg.row_factory = _sqlite3.Row
    dbmod.init_db(dg)
    dbmod.upsert_list_rows(dg, [{"subject_id": "7", "title": "被削过的电影", "rate": 9.0}])
    dbmod.upsert_details(dg, [{"subject_id": "7", "title": "被削过的电影",
                               "detail_json": '{"_degraded": true, "title": "被削过的电影"}'}])
    check("降级行会被重新排队抓详情", dbmod.pending_detail_ids(dg), ["7"])
    dg.close()

    # ---- 导出目录的清理 ---------------------------------------------------
    # 这个逻辑会**删文件**，所以更得有测试：只许删「自己上次写过、这次不写了」的，
    # 手放在 exports/ 里的东西一律不碰。
    # 用项目目录下的临时文件夹，不用 tempfile —— 受限沙箱里系统 TEMP 删不掉，
    # TemporaryDirectory.__exit__ 会抛 PermissionError: [WinError 5]。
    print("\n[导出清理]")
    import shutil as _shutil

    scratch = cfg.root / ".selftest_scratch"
    _shutil.rmtree(scratch, ignore_errors=True)
    scratch.mkdir(parents=True, exist_ok=True)
    try:
        for n in ("豆瓣明细.csv", "猫眼在映时序.csv", "我的备注.txt"):
            (scratch / n).write_text("x", encoding="utf-8")
        # 第一次：把其中两个登记成「上次产出」（模拟旧版 schema 留下的文件）
        expmod._sync_manifest(scratch, {"豆瓣明细.csv", "猫眼在映时序.csv"})
        # 第二次：这次只产出豆瓣明细，猫眼在映时序就成过期产物了
        pruned = expmod._sync_manifest(scratch, {"豆瓣明细.csv"})
        check("导出时清掉上次产出的过期文件", pruned, ["猫眼在映时序.csv"])
        check("过期文件确实被删了", (scratch / "猫眼在映时序.csv").exists(), False)
        check("还在产出的文件不能被删", (scratch / "豆瓣明细.csv").exists(), True)
        check("自己放进目录的文件不能碰", (scratch / "我的备注.txt").exists(), True)
    finally:
        _shutil.rmtree(scratch, ignore_errors=True)

    # ---- 汇总 -------------------------------------------------------------
    passed = sum(1 for _n, ok, _d in results if ok)
    failed = len(results) - passed
    print()
    print("=" * 68)
    for name, ok, detail in results:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"   ← {detail}" if detail else ""))
    print("=" * 68)
    print(f"  {passed} 项通过，{failed} 项失败")
    return 1 if failed else 0


def cmd_sync(cfg, limit: int | None = None, skip_detail: bool = False,
             skip_list: bool = False) -> int:
    """豆瓣主流程：先全量枚举列表，再增量补详情。"""
    print(BANNER)
    conn = open_db(cfg)
    client = make_client(cfg, conn)
    started = time.monotonic()
    try:
        if cfg.douban.enabled:
            print("─" * 68)
            print(" 第一层：豆瓣全量枚举")
            print("─" * 68)
            if skip_list:
                print("  （--skip-list，跳过列表层，直接用库里已有的条目）")
            else:
                added, updated = douban.sync_list(client, cfg, conn)
                print(f"  列表层完成：新增 {added} 条，更新 {updated} 条")

        # 猫眼放在补详情之前：正在上映的新片在豆瓣往往还没出分，
        # 按评分排序一定排到最后 —— 先把票房榜拿回来，
        # pending_detail_ids 才能优先补「榜上有名」的那些（见 db.pending_detail_ids）。
        if cfg.maoyan.enabled:
            print()
            print("─" * 68)
            print(" 猫眼：在映快照")
            print("─" * 68)
            res = maoyan.snapshot(client, cfg, conn)
            print(f"  猫眼完成：{res}")

        if cfg.douban.enabled:
            if cfg.douban.fetch_detail and not skip_detail:
                print()
                print("─" * 68)
                print(" 第二层：豆瓣详情补全")
                print("─" * 68)
                res = douban.sync_details(client, cfg, conn, limit=limit)
                print(f"  详情层完成：{res}")
            elif skip_detail:
                print("  （--skip-detail，跳过详情）")

        dbmod.set_state(conn, "last_run", dbmod.now_iso())
        dbmod.prune_fetch_log(conn, cfg.storage.fetch_log_keep)

        print()
        print("─" * 68)
        print(" 本次采集统计")
        print("─" * 68)
        for k, v in dbmod.stats(conn).items():
            print(f"  {k:<12}: {v}")
        print(f"\n  总耗时 {time.monotonic() - started:.1f} 秒")
        print(f"  请求数 {client.request_count}，失败 {client.error_count}")
        print(f"  数据库 {cfg.db_path}")
    finally:
        client.close()
        conn.close()
    return 0


def cmd_maoyan(cfg, trends_limit: int | None = None) -> int:
    conn = open_db(cfg)
    client = make_client(cfg, conn)
    try:
        print("抓取猫眼：票房榜 + 大盘 + 片单 + 单片逐日票房…")
        res = maoyan.snapshot(client, cfg, conn, trends_limit=trends_limit)
        print(f"  完成：{res}")
        print(f"  请求数 {client.request_count}，失败 {client.error_count}")
    finally:
        client.close()
        conn.close()
    return 0


def cmd_export(cfg) -> int:
    conn = open_db(cfg)
    try:
        print("导出中…")
        report = export.export_all(cfg, conn)
        print(f"\n明细 {report['明细行数']} 行 → 宽表 {report['宽表行数']} 行")
        for t in report.get("按作品类型统计", []):
            print(f"  {t['作品类型']}: {t['数量']} 部，平均分 {t['平均评分']}")
        print(f"\n产物（{cfg.export_dir}）：")
        for p in report["产物"]:
            print(f"  {p}")
        for name in report.get("清理的过期产物", []):
            print(f"  （已清理上次留下的过期文件：{name}）")
    finally:
        conn.close()
    return 0


def cmd_stats(cfg) -> int:
    conn = open_db(cfg)
    try:
        print("当前数据库概览：")
        for k, v in dbmod.stats(conn).items():
            print(f"  {k:<12}: {v}")

        print("\n最近 10 次请求：")
        rows = conn.execute(
            "SELECT ts, source, status, encoding, size_bytes, elapsed_ms, error "
            "FROM fetch_log ORDER BY id DESC LIMIT 10"
        ).fetchall()
        for r in rows:
            err = f"  <{r['error'][:40]}>" if r["error"] else ""
            print(f"  {r['ts'][11:19]} {r['source'][:22]:<22} "
                  f"HTTP {str(r['status']):<4} {r['size_bytes'] or 0:>7}B "
                  f"{r['encoding'] or '-':<12} {r['elapsed_ms'] or 0:>5}ms{err}")

        print("\n编码分布（排查乱码用）：")
        for r in conn.execute(
            "SELECT encoding, COUNT(*) c FROM fetch_log WHERE encoding IS NOT NULL "
            "GROUP BY encoding ORDER BY c DESC"
        ):
            print(f"  {r['encoding']:<16} {r['c']}")
    finally:
        conn.close()
    return 0


# ---------------------------------------------------------------------------
#  CLI
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="anime-spider",
        description="国漫数据采集：豆瓣全量 + 猫眼票房 → SQLite → Tableau 宽表",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
常用例子
--------
  python run.py doctor                 先体检，确认环境与接口都通
  python run.py selftest               离线自检（不联网），改完代码先跑这个
  python run.py verify                 验证电影和剧集是不是都覆盖到了
  python run.py sync --tags 国产动画 --max-start 200 --limit 30
                                       小批量试跑（两分钟出结果，建议先跑这个）
  python run.py sync --limit 200       正式跑，但本次只补 200 条详情
  python run.py sync                   全量采集（耗时较长，可 Ctrl+C 后重跑续采）
  python run.py sync --skip-detail     只刷列表，不抓详情（很快）
  python run.py sync --skip-list       跳过列表层，只补详情 + 刷猫眼（修数据时用）
  python run.py maoyan --limit 20      只抓猫眼（票房榜 + 片单 + 前 20 部的逐日票房）
  python run.py maoyan                 猫眼全量（约 73 部逐日票房，2~3 分钟）
  python run.py stats                  看进度与编码分布
  python run.py export                 导出 CSV / parquet / Tableau 宽表
  python run.py all                    采集 + 导出一条龙

每天跑一次 `maoyan`，票房榜就会攒成时间序列 —— 猫眼不给历史数据。
""",
    )
    p.add_argument("-c", "--config", default=None, help="配置文件路径（默认 config.toml）")
    p.add_argument("-v", "--verbose", action="store_true", help="输出 DEBUG 日志")
    p.add_argument("--log-file", default=None, help="同时把日志写到文件")

    sub = p.add_subparsers(dest="command")

    sub.add_parser("doctor", help="环境与接口体检")
    sub.add_parser("selftest", help="离线自检：编码自适应 / 字段解析 / 宽表（不联网）")
    sub.add_parser("stats", help="查看数据库概览")
    sub.add_parser("export", help="导出 CSV / parquet / Tableau 宽表")
    m = sub.add_parser("maoyan", help="抓猫眼票房榜 + 大盘 + 片单 + 单片逐日票房")
    m.add_argument("--limit", type=int, default=None,
                   help="只抓榜单前 N 部的逐日票房（默认按配置，0 = 全部）")
    m.add_argument("--no-trends", action="store_true",
                   help="跳过单片逐日票房（快，1~2 秒出结果）")

    v = sub.add_parser("verify", help="验证标签枚举是否同时覆盖电影与剧集")
    v.add_argument("--samples", type=int, default=8, help="抽样查详情的条数（默认 8）")

    s = sub.add_parser("sync", help="豆瓣采集（列表 + 详情）")
    s.add_argument("--limit", type=int, default=None, help="本次最多抓多少条详情")
    s.add_argument("--skip-detail", action="store_true", help="只刷列表，不抓详情")
    s.add_argument("--skip-list", action="store_true",
                   help="跳过列表层，只用库里已有条目补详情/刷猫眼（修数据时用）")
    s.add_argument("--tags", default=None, help="临时覆盖标签，逗号分隔（例如 国产动画,动画）")
    s.add_argument("--max-start", type=int, default=None,
                   help="临时覆盖每标签翻页上限（试跑时给个 200 就很快）")
    s.add_argument("--all-details", action="store_true",
                   help="重抓所有详情（默认只补没抓过的）")

    sub.add_parser("all", help="采集 + 导出")

    return p


def apply_overrides(cfg, args) -> list[str]:
    """把命令行上的临时覆盖应用到配置上，返回人类可读的覆盖说明。"""
    notes: list[str] = []

    tags = getattr(args, "tags", None)
    if tags:
        cfg.douban.tags = [t.strip() for t in str(tags).split(",") if t.strip()]
        notes.append(f"标签覆盖为 {cfg.douban.tags}")

    max_start = getattr(args, "max_start", None)
    if max_start is not None:
        # 注意别写成 `if max_start:` —— 那会让 `--max-start 0`（= 不翻列表）
        # 被当成「没传」，然后照样去翻 8000 页。
        cfg.douban.max_start = int(max_start)
        notes.append(f"翻页上限覆盖为 start<{cfg.douban.max_start}")

    if getattr(args, "all_details", False):
        cfg.douban.detail_only_new = False
        notes.append("详情将全量重抓")

    return notes


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cfg = cfgmod.load(args.config)
    log_file = Path(args.log_file) if args.log_file else (cfg.root / "logs" / "run.log")
    setup_logging(args.verbose, log_file)

    cmd = args.command or "doctor"
    try:
        if cmd == "doctor":
            return cmd_doctor(cfg)
        if cmd == "selftest":
            return cmd_selftest(cfg)
        if cmd == "verify":
            return cmd_verify(cfg, samples=getattr(args, "samples", 8))
        if cmd == "sync":
            for note in apply_overrides(cfg, args):
                print(f"  · {note}")
            return cmd_sync(cfg, limit=getattr(args, "limit", None),
                            skip_detail=getattr(args, "skip_detail", False),
                            skip_list=getattr(args, "skip_list", False))
        if cmd == "maoyan":
            if getattr(args, "no_trends", False):
                cfg.maoyan.box_trends = False
            return cmd_maoyan(cfg, trends_limit=getattr(args, "limit", None))
        if cmd == "export":
            return cmd_export(cfg)
        if cmd == "stats":
            return cmd_stats(cfg)
        if cmd == "all":
            rc = cmd_sync(cfg)
            return rc if rc else cmd_export(cfg)
        print(f"未知命令: {cmd}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\n已中断。断点已保存，重跑 `python run.py sync` 会接着上次继续。",
              file=sys.stderr)
        return 130


__all__: list[Any] = ["main", "build_parser", "cmd_doctor", "cmd_sync", "cmd_export"]
