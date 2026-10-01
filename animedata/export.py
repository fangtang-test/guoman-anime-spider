"""导出层：把 SQLite 变成能直接拖进 Tableau 的宽表。

产物
----
``豆瓣明细.csv/.parquet``    扁平原始明细，一行一部作品
``Tableau_作品宽表.csv/.parquet``  已做好分档、拆列、中文字段的成品表
``猫眼票房榜.csv``            在映影片的票房/排片/上座/场次（每天一次快照攒时序）
``猫眼逐日票房.csv``          单片最近 5 天的逐日票房（猫眼只给 5 天）
``猫眼大盘.csv``              每日全国大盘（总票房、场次、观影人次）
``猫眼片单.csv``              正在热映 + 即将上映/预售的片单（含上映日期与主演）
``字段说明.csv``              每个字段的含义，直接贴进数据字典
``概览.json``                 行数、评分分布、猫眼匹配率等
``作品宽表.hyper``            可选，需要 tableauhyperapi

CSV 一律用 ``utf-8-sig`` —— 这样 Excel 双击打开不会中文乱码。
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

log = logging.getLogger(__name__)

CSV_ENCODING = "utf-8-sig"


# ---------------------------------------------------------------------------
#  读取
# ---------------------------------------------------------------------------
def load_detail_frame(conn) -> pd.DataFrame:
    sql = """
        SELECT
            d.subject_id, d.title, d.original_title, d.aka, d.year, d.subtype,
            d.is_tv, d.rating_value, d.rating_count, d.rating_star,
            d.genres, d.countries, d.languages, d.durations, d.duration_min,
            d.episodes_count, d.pubdate, d.release_year, d.directors, d.actors,
            d.intro, d.comment_count, d.review_count, d.card_subtitle,
            d.cover_url, d.url, d.fetched_at,
            d.detail_json,
            l.tags
        FROM douban_detail d
        LEFT JOIN douban_list l ON l.subject_id = d.subject_id
    """
    return pd.read_sql_query(sql, conn)


def load_maoyan_frames(conn) -> dict[str, pd.DataFrame]:
    """读猫眼四张表。任何一张不存在就返回空表，不影响导出。"""
    spec = {
        "board": "SELECT * FROM maoyan_board "
                 "ORDER BY snapshot_date DESC, rank_no",
        "trend": "SELECT * FROM maoyan_box_trend "
                 "ORDER BY snapshot_date DESC, box_date",
        "nation": "SELECT * FROM maoyan_nation ORDER BY snapshot_date DESC",
        "list": "SELECT * FROM maoyan_list "
                "ORDER BY snapshot_date DESC, show_type, movie_id",
    }
    out: dict[str, pd.DataFrame] = {}
    for key, sql in spec.items():
        try:
            out[key] = pd.read_sql_query(sql, conn)
        except Exception:
            out[key] = pd.DataFrame()
    return out


def _norm_title(s: Any) -> str:
    """片名归一化，用来把豆瓣和猫眼对上。

    两边片名不可能完全一致（豆瓣可能带副标题、猫眼可能有书名号），所以只保留
    中英文数字，去掉空格与所有标点，再比。匹配不上的宁可留空，不硬凑。
    """
    if s is None:
        return ""
    return re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "", str(s)).lower()


# 副标题分隔符（猫眼用「：」，豆瓣常用「·」「—」）
_RE_SUBTITLE = re.compile(r"[：:·・—–]|（|\(|【|\[")
# 「…第二季」「…第3部」这类续集标记
_RE_SEASON_TAIL = re.compile(r"第[0-9一二三四五六七八九十百]+[季部篇章节集]$")


def _main_title(s: Any) -> str:
    """去掉副标题和「第N季/第N部」之后的主体片名。

    **这是弱键，只在两边都唯一时才敢用。** 否则《小猪佩奇》（剧集）会和
    《小猪佩奇·完美假期》（电影）错配成同一部片 —— 这正是下面
    ``_merge_maoyan`` 里那两道唯一性检查要挡的事。
    """
    t = str(s or "").strip()
    t = _RE_SUBTITLE.split(t, maxsplit=1)[0]
    t = _RE_SEASON_TAIL.sub("", t)
    return _norm_title(t)


def latest_maoyan_board(board: pd.DataFrame) -> pd.DataFrame:
    """只取最新一天的票房榜快照。"""
    if board.empty:
        return board
    latest = board["snapshot_date"].max()
    return board[board["snapshot_date"] == latest].copy()


# ---------------------------------------------------------------------------
#  加工
# ---------------------------------------------------------------------------
def _jlist(v: Any) -> list[str]:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return []
    if isinstance(v, list):
        return [str(x) for x in v]
    try:
        out = json.loads(v)
        return [str(x) for x in out] if isinstance(out, list) else ([str(out)] if out else [])
    except (TypeError, ValueError):
        return [str(v)] if str(v).strip() else []


def _join(v: Any, sep: str = " / ", limit: int | None = None) -> str:
    items = _jlist(v)
    if limit:
        items = items[:limit]
    return sep.join(items)


def _first(v: Any) -> str:
    items = _jlist(v)
    return items[0] if items else ""


def _first_date(v: Any) -> str:
    items = _jlist(v)
    return items[0] if items else ""


def _rating_band(x: Any) -> str:
    try:
        if x is None or pd.isna(x):
            return "暂无评分"
        x = float(x)
    except (TypeError, ValueError):
        return "暂无评分"
    if x >= 9.0:
        return "9.0 分以上"
    if x >= 8.0:
        return "8.0-8.9"
    if x >= 7.0:
        return "7.0-7.9"
    if x >= 6.0:
        return "6.0-6.9"
    return "6.0 以下"


def _heat_band(x: Any) -> str:
    try:
        if x is None or pd.isna(x):
            return "未知"
        x = float(x)
    except (TypeError, ValueError):
        return "未知"
    if x >= 500_000:
        return "50 万+"
    if x >= 100_000:
        return "10-50 万"
    if x >= 50_000:
        return "5-10 万"
    if x >= 10_000:
        return "1-5 万"
    if x >= 1_000:
        return "1000-1 万"
    return "1000 以下"


def _decade(y: Any) -> str:
    try:
        if y is None or pd.isna(y):
            return "未知"
        y = int(y)
    except (TypeError, ValueError):
        return "未知"
    if y < 1990:
        return "1990 前"
    return f"{y // 10 * 10} 年代"


def _duration_band(m: Any) -> str:
    try:
        if m is None or pd.isna(m):
            return "未知"
        m = float(m)
    except (TypeError, ValueError):
        return "未知"
    if m <= 15:
        return "15 分钟以内"
    if m <= 30:
        return "15-30 分钟"
    if m <= 60:
        return "30-60 分钟"
    if m <= 100:
        return "60-100 分钟"
    return "100 分钟以上"


def build_tableau(df: pd.DataFrame, maoyan: pd.DataFrame | None = None) -> pd.DataFrame:
    """把明细表加工成 Tableau 友好的中文宽表。"""
    if df.empty:
        return df

    out = pd.DataFrame()
    out["作品ID"] = df["subject_id"].astype(str)
    out["作品名称"] = df["title"].fillna("")
    out["原名"] = df["original_title"].fillna("")
    out["别名"] = df["aka"].map(lambda v: _join(v, " / "))
    out["作品类型"] = df["is_tv"].map(lambda v: "剧集/番剧" if v == 1 else "电影")

    # 数据完整度。豆瓣详情接口限流时返回的是 HTTP 400 + code 1309，早期版本
    # 把它当成「端点坏了」静默降级到轻量接口，于是这些行的简介/原名/语言/时长
    # 全是空的 —— 而在 Tableau 里，空值看起来和「这部片确实没有简介」一模一样。
    # 所以必须带一个显式标记，让分析时能一眼把它们筛掉（修复见 README 坑 4b）。
    out["详情完整"] = df["detail_json"].map(
        lambda v: "否" if "_degraded" in str(v if v is not None else "") else "是"
    )
    out["年份"] = pd.to_numeric(df["year"], errors="coerce").astype("Int64")
    out["年代"] = df["year"].map(_decade)
    out["上映日期"] = df["pubdate"].map(_first_date)
    out["上映年"] = pd.to_numeric(df["release_year"], errors="coerce").astype("Int64")

    out["评分"] = pd.to_numeric(df["rating_value"], errors="coerce")
    out["评分档"] = df["rating_value"].map(_rating_band)
    out["评分星级"] = pd.to_numeric(df["rating_star"], errors="coerce")
    out["评分人数"] = pd.to_numeric(df["rating_count"], errors="coerce").astype("Int64")
    out["热度档"] = df["rating_count"].map(_heat_band)

    out["主类型"] = df["genres"].map(_first)
    out["类型2"] = df["genres"].map(lambda v: (_jlist(v) + ["", ""])[1])
    out["类型3"] = df["genres"].map(lambda v: (_jlist(v) + ["", "", ""])[2])
    out["类型数"] = df["genres"].map(lambda v: len(_jlist(v)))
    out["标签全集"] = df["genres"].map(lambda v: _join(v, " "))

    out["地区"] = df["countries"].map(lambda v: _join(v, " / "))
    # 宽口径：制片国家里只要出现「中国大陆」就算（合拍片也算国产）。
    # 想要严口径（只认中国大陆主投）请配合「主出品地区」列自己过滤。
    out["是否国产"] = df["countries"].map(
        lambda v: "是" if any("中国大陆" in c or "中国" == c for c in _jlist(v)) else "否"
    )
    out["主出品地区"] = df["countries"].map(lambda v: (_jlist(v) + [""])[0])
    out["语言"] = df["languages"].map(lambda v: _join(v, " / "))
    out["时长分钟"] = pd.to_numeric(df["duration_min"], errors="coerce").astype("Int64")
    out["时长档"] = df["duration_min"].map(_duration_band)

    # 电影没有「集数」这个概念，但豆瓣对电影返回的是 0 而不是空；
    # 少数剧集（综艺、栏目）也会返回 0。两种 0 都没有意义，一律置空 ——
    # 否则 Tableau 里「0 集」会被当成一个真实取值，做平均值/计数时算进去就错了。
    _eps = pd.to_numeric(df["episodes_count"], errors="coerce").astype("Int64")
    _is_tv = pd.to_numeric(df["is_tv"], errors="coerce").fillna(0).astype(int).eq(1)
    out["集数"] = _eps.where(_is_tv & _eps.gt(0))

    out["导演"] = df["directors"].map(lambda v: _join(v, " / "))
    out["主演"] = df["actors"].map(lambda v: _join(v, " / ", limit=5))
    out["主演人数"] = df["actors"].map(lambda v: len(_jlist(v)))
    out["短评数"] = pd.to_numeric(df["comment_count"], errors="coerce").astype("Int64")
    out["影评数"] = pd.to_numeric(df["review_count"], errors="coerce").astype("Int64")
    out["简介"] = df["intro"].fillna("").str.replace(r"\s+", " ", regex=True).str[:500]
    out["封面图"] = df["cover_url"].fillna("")
    out["豆瓣链接"] = df["url"].fillna("")
    out["数据更新时间"] = df["fetched_at"].fillna("")

    # 关联猫眼最新一天的票房榜
    out = _merge_maoyan(out, maoyan)

    return out


# 猫眼榜单字段 → 宽表中的中文列名。顺序即宽表列顺序。
MAOYAN_COLUMNS: list[tuple[str, str]] = [
    ("猫眼快照日期", "snapshot_date"),
    ("猫眼累计票房", "box_desc"),
    ("猫眼票房占比", "box_rate"),
    ("猫眼排片占比", "show_count_rate"),
    ("猫眼上座率", "avg_seat_view"),
    ("猫眼场均人次", "avg_show_view"),
    ("猫眼场次", "show_count"),
    ("猫眼当日排名", "rank_no"),
    ("猫眼上映状态", "release_info"),
    ("猫眼类型", "category"),
]

_MAOYAN_TEXT = {
    "猫眼快照日期", "猫眼累计票房", "猫眼票房占比", "猫眼排片占比",
    "猫眼上座率", "猫眼场均人次", "猫眼上映状态", "猫眼类型",
}


def _unique_keys(sub: pd.DataFrame) -> dict[str, Any]:
    """构造「键 → 行」的查找表，只保留**唯一**的键。

    一个键如果在猫眼那边对应不止一部片（``movie_id`` 数 > 1），这个键就不可用 ——
    宁可留空，也不能随便挑一部把票房挂上去。
    """
    if sub.empty:
        return {}
    counts = sub.groupby("_key")["movie_id"].nunique()
    ok = set(counts[counts == 1].index)
    uniq = sub.drop_duplicates(subset=["_key"], keep="first")
    return {r["_key"]: r for _, r in uniq.iterrows() if r["_key"] in ok}


def _pick_join_keys(out: pd.DataFrame, exact: dict, mains: dict) -> list[str]:
    """给宽表每一行挑连接键，按优先级：完全相等 → 豆瓣别名 → 去副标题。

    主体名（``~`` 前缀）是最弱的一档，挑不到就留空，绝不错配。
    """
    keys: list[str] = []
    for _, row in out.iterrows():
        title = row.get("作品名称")
        k = _norm_title(title)
        if k and k in exact:
            keys.append(k)
            continue

        hit = ""
        for alias in str(row.get("别名") or "").split(" / "):
            ka = _norm_title(alias)
            if ka and ka in exact:
                hit = ka
                break
        if hit:
            keys.append(hit)
            continue

        km = _main_title(title)
        # 两边都必须「确实去掉了副标题」才敢用弱键：豆瓣这边没副标题
        # （《小猪佩奇》）就不该去认领猫眼那边有副标题的《小猪佩奇·完美假期》
        if km and km != k and km in mains:
            keys.append("~" + km)
        else:
            keys.append("")
    return keys


def _merge_maoyan(out: pd.DataFrame, board: pd.DataFrame | None) -> pd.DataFrame:
    """把猫眼票房榜并进宽表。

    猫眼不给「它自己的 movieId ↔ 豆瓣 subject_id」的对照关系，唯一可用的连接键
    是片名。所以按优先级匹配：**完全相等 → 豆瓣别名 → 去副标题后的主体名**，
    后两档都加了唯一性检查。匹配不上的宁可留空 ——
    把 A 片的票房挂到 B 片上，比留空有害得多。
    """
    cn_cols = [c for c, _ in MAOYAN_COLUMNS]
    if board is None or board.empty:
        for c in cn_cols:
            out[c] = ""
        return out

    b = latest_maoyan_board(board)
    b = b.assign(_exact=b["title"].map(_norm_title), _main=b["title"].map(_main_title))
    b = b[b["_exact"] != ""]

    exact = _unique_keys(b.assign(_key=b["_exact"]))
    # 主体名只有当猫眼那边**确实去掉了副标题**时才当候选。否则《小猪佩奇》
    # （无副标题）会凭主体名去认领《小猪佩奇·完美假期》的票房。
    sub_titled = b[b["_main"] != b["_exact"]]
    mains = _unique_keys(sub_titled.assign(_key=sub_titled["_main"]))
    mains = {k: v for k, v in mains.items() if k}

    keys = _pick_join_keys(out, exact, mains)
    # 反向唯一性：豆瓣这边有两部片共用同一个主体名时，谁都别挂（否则一部片的
    # 票房会被复制到多行上，求和直接翻倍）
    shared = pd.Series([k for k in keys if k.startswith("~")]).value_counts()
    keys = [k if not (k.startswith("~") and shared.get(k, 0) > 1) else "" for k in keys]

    table = {**exact, **{f"~{k}": v for k, v in mains.items()}}
    slim = pd.DataFrame([
        {"_key": k, **{cn: r.get(src) for cn, src in MAOYAN_COLUMNS}}
        for k, r in table.items()
    ])
    out["_key"] = keys
    out = out.merge(slim, on="_key", how="left").drop(columns=["_key"])

    for c in cn_cols:
        if c in _MAOYAN_TEXT:
            out[c] = out[c].fillna("")
    out["猫眼场次"] = pd.to_numeric(out["猫眼场次"], errors="coerce").astype("Int64")
    out["猫眼当日排名"] = pd.to_numeric(out["猫眼当日排名"], errors="coerce").astype("Int64")
    return out


FIELD_DICT: list[tuple[str, str, str]] = [
    ("作品ID", "文本", "豆瓣 subject_id，全表唯一主键，可用来去重与回溯"),
    ("作品名称", "文本", "豆瓣主标题"),
    ("原名", "文本", "外语片的原文名"),
    ("别名", "文本", "又名 / 港台译名，多个用 / 分隔"),
    ("作品类型", "维度", "电影 或 剧集/番剧"),
    ("详情完整", "维度", "是/否。否=豆瓣详情被限流降级，简介/原名/语言/时长等字段是空的，分析时建议筛掉"),
    ("年份", "数值", "豆瓣标注年份"),
    ("年代", "维度", "按十年归并，如 2020 年代"),
    ("上映日期", "日期", "首个上映日期，格式 yyyy-mm-dd(地区)"),
    ("上映年", "数值", "从上映日期里抽出的年份"),
    ("评分", "数值", "豆瓣评分，0-10"),
    ("评分档", "维度", "评分区间分档，适合做颜色/图例"),
    ("评分星级", "数值", "豆瓣 1-5 星制"),
    ("评分人数", "数值", "打分总人数，衡量热度"),
    ("热度档", "维度", "评分人数分档"),
    ("主类型", "维度", "豆瓣类型标签的第一个"),
    ("类型2", "维度", "第二个类型标签"),
    ("类型3", "维度", "第三个类型标签"),
    ("类型数", "数值", "类型标签个数，可衡量题材复合度"),
    ("标签全集", "文本", "全部类型标签，空格分隔，便于做标签筛选"),
    ("地区", "维度", "制片国家/地区"),
    ("是否国产", "维度", "宽口径：制片国家里只要含「中国大陆」就算「是」，合拍片（如 法国/中国大陆）也算；想只看纯国产请用「主出品地区」过滤"),
    ("主出品地区", "维度", "豆瓣列出的第一个制片国家，即主出品方所在地"),
    ("语言", "维度", "对白语言"),
    ("时长分钟", "数值", "单集或片长，已统一换算成分钟"),
    ("时长档", "维度", "时长区间分档"),
    ("集数", "数值", "剧集的总集数；电影一律留空，豆瓣对少数栏目/综艺剧集也返回 0，同样置空（0 集无意义，留着会污染平均与计数）"),
    ("导演", "文本", "导演，多个用 / 分隔"),
    ("主演", "文本", "前 5 位主演"),
    ("主演人数", "数值", "演职员表里的演员数"),
    ("短评数", "数值", "豆瓣短评条数"),
    ("影评数", "数值", "豆瓣长影评条数"),
    ("简介", "文本", "剧情简介，截断到 500 字"),
    ("封面图", "文本", "海报 URL，Tableau 里可用图像角色"),
    ("豆瓣链接", "文本", "详情页 URL"),
    ("数据更新时间", "日期", "该行数据的抓取时间"),
    ("猫眼快照日期", "日期", "猫眼票房榜快照日期。按归一化片名匹配（去空格与标点），"
                          "匹配不上为空 —— 留空好过把别的片子票房挂到这行上"),
    ("猫眼累计票房", "文本", "猫眼累计票房（官方文本，含亿/万单位），如 5220.5万"),
    ("猫眼票房占比", "文本", "当日票房占大盘比例，如 38.0%"),
    ("猫眼排片占比", "文本", "当日场次占大盘比例，如 29.4%"),
    ("猫眼上座率", "文本", "当日平均上座率，如 6.9%"),
    ("猫眼场均人次", "数值", "当日平均每场观影人次"),
    ("猫眼场次", "数值", "当日排映场次"),
    ("猫眼当日排名", "数值", "猫眼当日票房榜名次，1 = 冠军"),
    ("猫眼上映状态", "维度", "上映首日 / 上映52天 / 点映 / 展映"),
    ("猫眼类型", "文本", "猫眼自己的类型标签，逗号分隔，如 神话,喜剧,冒险,动画"),
]


# ---------------------------------------------------------------------------
#  写盘
# ---------------------------------------------------------------------------
def _write(df: pd.DataFrame, base: Path, cfg, name: str) -> list[str]:
    made: list[str] = []
    if cfg.export.csv:
        p = base / f"{name}.csv"
        df.to_csv(p, index=False, encoding=CSV_ENCODING)
        made.append(str(p))
    if cfg.export.parquet:
        try:
            p = base / f"{name}.parquet"
            df.to_parquet(p, index=False)
            made.append(str(p))
        except Exception as exc:
            log.warning("parquet 写入失败（%s），跳过。装一下 pyarrow 即可。", exc)
    if cfg.export.json:
        p = base / f"{name}.json"
        p.write_text(df.to_json(orient="records", force_ascii=False, indent=2), encoding="utf-8")
        made.append(str(p))
    return made


def write_hyper(df: pd.DataFrame, path: Path) -> str | None:
    """可选：直接产出 Tableau .hyper 提取文件。没装 hyperapi 就返回 None。"""
    try:
        from tableauhyperapi import (
            Connection, CreateMode, HyperProcess, Inserter, SqlType, TableDefinition,
            TableName, Telemetry,
        )
    except ImportError:
        log.info("未安装 tableauhyperapi，跳过 .hyper（pip install tableauhyperapi）")
        return None

    def sql_type(series: pd.Series) -> Any:
        if pd.api.types.is_integer_dtype(series):
            return SqlType.big_int()
        if pd.api.types.is_float_dtype(series):
            return SqlType.double()
        return SqlType.text()

    columns = [(c, sql_type(df[c])) for c in df.columns]
    table = TableDefinition(TableName("Extract", "作品宽表"), columns)

    try:
        with HyperProcess(Telemetry.DO_NOT_SEND_USAGE_DATA_TO_TABLEAU) as hyper:
            with Connection(hyper.endpoint, str(path), CreateMode.CREATE_AND_REPLACE) as conn:
                conn.catalog.create_schema("Extract")
                conn.catalog.create_table(table)
                rows = [
                    [None if (v is None or (isinstance(v, float) and pd.isna(v))) else v
                     for v in rec]
                    for rec in df.itertuples(index=False, name=None)
                ]
                with Inserter(conn, table) as ins:
                    ins.add_rows(rows)
                    ins.execute()
        return str(path)
    except Exception as exc:
        log.warning(".hyper 生成失败：%s", exc)
        return None


MANIFEST_NAME = "导出清单.json"


def _sync_manifest(base: Path, produced: set[str]) -> list[str]:
    """记下本次产出的文件名，并清掉「上次产出、这次不产出了」的残留。

    为什么需要：schema 改过一次（旧的 `猫眼在映时序.csv` 换成了四张新表），
    旧文件会一直躺在 `exports/` 里，拖进 Tableau 就是错的列、还没有任何提示。
    这里只删**本程序自己上次写过的**文件，你手动放进 `exports/` 的东西不会被动。
    """
    mf = base / MANIFEST_NAME
    old: list[str] = []
    if mf.exists():
        try:
            old = [str(n) for n in (json.loads(mf.read_text(encoding="utf-8")).get("产物") or [])]
        except (OSError, ValueError):
            old = []

    pruned: list[str] = []
    for name in old:
        # 只认目录下的普通文件名，挡掉 "..\\" 这类意外
        if name in produced or name == MANIFEST_NAME or Path(name).name != name:
            continue
        p = base / name
        if p.is_file():
            p.unlink()
            pruned.append(name)

    mf.write_text(
        json.dumps({"产物": sorted(produced | {MANIFEST_NAME})}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return pruned


def export_all(cfg, conn) -> dict[str, Any]:
    """跑完整套导出，返回产物清单。"""
    base = cfg.export_dir
    base.mkdir(parents=True, exist_ok=True)

    detail = load_detail_frame(conn)
    my = load_maoyan_frames(conn)
    wide = build_tableau(detail, my["board"])

    made: list[str] = []
    report: dict[str, Any] = {
        "导出时间": datetime.now().isoformat(timespec="seconds"),
        "明细行数": int(len(detail)),
        "宽表行数": int(len(wide)),
        "猫眼票房榜行数": int(len(my["board"])),
        "猫眼逐日票房行数": int(len(my["trend"])),
        "猫眼片单行数": int(len(my["list"])),
    }

    if not detail.empty:
        made += _write(detail, base, cfg, "豆瓣明细")
    if not wide.empty:
        made += _write(wide, base, cfg, "Tableau_作品宽表")

    # 猫眼四份表各自独立成文件，都是可以单独拖进 Tableau 的
    for key, name in (
        ("board", "猫眼票房榜"),
        ("trend", "猫眼逐日票房"),
        ("nation", "猫眼大盘"),
        ("list", "猫眼片单"),
    ):
        if not my[key].empty:
            made += _write(my[key], base, cfg, name)

    # 宽表里到底有多少条真匹配上了猫眼（连接键只有片名，值得看一眼）
    if not wide.empty and "猫眼累计票房" in wide.columns:
        matched = int((wide["猫眼累计票房"].astype(str) != "").sum())
        report["猫眼匹配"] = {
            "宽表行数": int(len(wide)),
            "匹配上猫眼票房榜": matched,
            "匹配率": f"{matched / max(len(wide), 1):.1%}",
        }

    if not wide.empty and "详情完整" in wide.columns:
        bad = int((wide["详情完整"] == "否").sum())
        report["详情完整度"] = {
            "完整": int(len(wide)) - bad,
            "被限流降级": bad,
            "说明": "降级行的简介/原名/语言/时长是空的；再跑一次 run.py sync --skip-list 会自动优先补这些行",
        }

    # 字段说明
    fd = pd.DataFrame(FIELD_DICT, columns=["字段名", "类型", "说明"])
    if not wide.empty:
        fd["是否有值"] = fd["字段名"].map(
            lambda c: "有" if c in wide.columns else "无"
        )
    made += _write(fd, base, cfg, "字段说明")

    # .hyper
    if cfg.export.hyper and not wide.empty:
        hp = write_hyper(wide, base / "作品宽表.hyper")
        if hp:
            made.append(hp)
            report["hyper"] = hp

    # 概览
    if not wide.empty:
        by_type = wide.groupby("作品类型").agg(
            数量=("作品ID", "count"),
            平均评分=("评分", "mean"),
            评分人数中位数=("评分人数", "median"),
        ).round(2).reset_index()
        report["按作品类型统计"] = by_type.to_dict(orient="records")

        top = wide.dropna(subset=["评分"]).nlargest(10, "评分")[
            ["作品名称", "作品类型", "年份", "评分", "评分人数"]
        ]
        report["评分前十"] = top.to_dict(orient="records")

    overview = base / "概览.json"
    made.append(str(overview))

    # 先把「这次产出了什么」算清楚，再据此清理上次的残留（schema 改过就会留下）
    produced = {Path(p).name for p in made}
    pruned = _sync_manifest(base, produced)
    if pruned:
        log.info("清理了 %d 个上次留下的过期产物：%s", len(pruned), "、".join(pruned))
    report["清理的过期产物"] = pruned
    report["产物"] = [Path(p).name for p in made]

    overview.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report
