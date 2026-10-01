"""SQLite 存储层。

设计要点
--------
* **以豆瓣 subject_id 为主键**，天然去重 —— 反复跑不会产生重复数据。
* 列表层与详情层分表：列表页便宜（一次 20 条），详情页贵（一条一个请求），
  分开存才能做到「列表全量刷、详情只补新的」。
* ``crawl_state`` 表存断点，Ctrl+C 之后重跑会接着上次的位置继续。
* ``fetch_log`` 表存每次请求的状态与编码，出问题时能直接定位。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA synchronous  = NORMAL;

-- 列表层：豆瓣搜索接口返回的轻量记录
CREATE TABLE IF NOT EXISTS douban_list (
    subject_id  TEXT PRIMARY KEY,
    title       TEXT,
    rate        REAL,
    star        REAL,
    directors   TEXT,
    casts       TEXT,
    cover       TEXT,
    url         TEXT,
    tags        TEXT,          -- 命中的采集标签，逗号分隔
    first_seen  TEXT,
    last_seen   TEXT
);
CREATE INDEX IF NOT EXISTS idx_douban_list_rate ON douban_list(rate);

-- 详情层：rexxar 接口的扁平化结果
CREATE TABLE IF NOT EXISTS douban_detail (
    subject_id      TEXT PRIMARY KEY,
    title           TEXT,
    original_title  TEXT,
    aka             TEXT,      -- JSON 数组
    year            INTEGER,
    subtype         TEXT,
    is_tv           INTEGER,
    rating_value    REAL,
    rating_count    INTEGER,
    rating_star     REAL,
    genres          TEXT,      -- JSON 数组
    countries       TEXT,      -- JSON 数组
    languages       TEXT,      -- JSON 数组
    durations       TEXT,      -- JSON 数组
    duration_min    INTEGER,   -- 解析出来的分钟数，方便做数值分析
    episodes_count  INTEGER,
    pubdate         TEXT,      -- JSON 数组
    release_year    INTEGER,
    directors       TEXT,      -- JSON 数组
    actors          TEXT,      -- JSON 数组
    intro           TEXT,
    comment_count   INTEGER,
    review_count    INTEGER,
    card_subtitle   TEXT,
    cover_url       TEXT,
    url             TEXT,
    detail_json     TEXT,      -- 原始 JSON 全量存档，字段永远不丢
    fetched_at      TEXT
);
CREATE INDEX IF NOT EXISTS idx_douban_detail_year ON douban_detail(year);

-- 断点续爬状态
CREATE TABLE IF NOT EXISTS crawl_state (
    key        TEXT PRIMARY KEY,
    value      TEXT,
    updated_at TEXT
);

-- 猫眼票房榜（每天一次快照 → 攒成时间序列。猫眼不给历史，只能自己攒）
-- 单位坑：box_fen 是「分」，box_yuan 是除过 100 的元，box_desc 是官方文本
CREATE TABLE IF NOT EXISTS maoyan_board (
    snapshot_date   TEXT,
    movie_id        TEXT,
    rank_no         INTEGER,   -- 榜单顺序即当日票房排名（真排名）
    title           TEXT,
    release_info    TEXT,      -- 上映首日 / 上映52天 / 点映 / 展映
    release_days    INTEGER,   -- 从 release_info 解析出上映天数
    category        TEXT,      -- 类型，来自 ?movieId=N（榜单本身不给）
    is_animation    INTEGER,
    box_fen         INTEGER,
    box_yuan        REAL,
    box_desc        TEXT,
    split_box_fen   INTEGER,
    split_box_yuan  REAL,
    split_box_desc  TEXT,
    box_rate        TEXT,      -- 票房占比
    show_count      INTEGER,   -- 场次
    show_count_rate TEXT,      -- 排片占比
    avg_seat_view   TEXT,      -- 上座率
    avg_show_view   TEXT,      -- 场均人次
    detail_url      TEXT,
    fetched_at      TEXT,
    PRIMARY KEY (snapshot_date, movie_id)
);

-- 单片近 5 日逐日票房（猫眼只给 5 天，且与上映天数无关）
-- 单位坑：这里的 box_yuan 本来就是「元」，不要再去当分换算
CREATE TABLE IF NOT EXISTS maoyan_box_trend (
    snapshot_date  TEXT,
    movie_id       TEXT,
    box_date       TEXT,
    box_yuan       INTEGER,
    box_desc       TEXT,
    release_day    INTEGER,    -- 是否上映首日
    fetched_at     TEXT,
    PRIMARY KEY (snapshot_date, movie_id, box_date)
);

-- 实时大盘（每天一行）
CREATE TABLE IF NOT EXISTS maoyan_nation (
    snapshot_date        TEXT PRIMARY KEY,
    nation_box_num       REAL,
    nation_box_unit      TEXT,
    nation_box_desc      TEXT,
    nation_split_num     REAL,
    nation_split_unit    TEXT,
    nation_split_desc    TEXT,
    show_count_desc      TEXT,
    view_count_desc      TEXT,   -- 观影人次
    update_timestamp     INTEGER,
    update_gap_second    INTEGER,
    fetched_at           TEXT
);

-- 片单（showType=1 正在热映 / showType=2 即将上映·预售）
-- 票房榜只覆盖「已经上映、当天有排片」的片子，待映片只能从这里拿
CREATE TABLE IF NOT EXISTS maoyan_list (
    snapshot_date  TEXT,
    movie_id       TEXT,
    show_type      INTEGER,
    title          TEXT,
    status         TEXT,       -- 购票 / 预售
    category       TEXT,
    is_animation   INTEGER,
    actors         TEXT,
    release_date   TEXT,
    score          TEXT,
    detail_url     TEXT,
    raw            TEXT,
    fetched_at     TEXT,
    PRIMARY KEY (snapshot_date, movie_id, show_type)
);

-- 请求日志
CREATE TABLE IF NOT EXISTS fetch_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT,
    source      TEXT,
    url         TEXT,
    status      INTEGER,
    encoding    TEXT,
    size_bytes  INTEGER,
    elapsed_ms  INTEGER,
    attempt     INTEGER,
    error       TEXT
);
CREATE INDEX IF NOT EXISTS idx_fetch_log_ts ON fetch_log(ts);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def connect(db_path: str | Path) -> sqlite3.Connection:
    p = Path(db_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p), timeout=30.0)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def init_and_migrate(conn: sqlite3.Connection) -> str | None:
    """建表 + 结构迁移。返回迁移说明（没迁移就是 None）。"""
    init_db(conn)
    note = migrate_maoyan(conn)
    if note:
        init_db(conn)  # 迁移删了旧表后，把新表补建一次
    return note


# ---------------------------------------------------------------------------
#  列表层
# ---------------------------------------------------------------------------
def upsert_list_rows(conn: sqlite3.Connection, rows: Iterable[dict[str, Any]]) -> tuple[int, int]:
    """写入列表层记录。

    返回 ``(新增条数, 更新条数)``。靠主键冲突判断是不是新条目。

    ``tags`` 是**累积**的：同一部作品被多个标签命中时，标签会叠加而不是覆盖，
    这样才能事后查出「这条是从哪个标签捞到的」。
    """
    added = updated = 0
    ts = now_iso()
    for r in rows:
        sid = str(r.get("id") or r.get("subject_id") or "").strip()
        if not sid:
            continue
        exists = conn.execute(
            "SELECT tags FROM douban_list WHERE subject_id = ?", (sid,)
        ).fetchone()

        tags = _merge_csv(exists["tags"] if exists else "", r.get("_tag"))

        conn.execute(
            """
            INSERT INTO douban_list
                (subject_id, title, rate, star, directors, casts, cover, url,
                 tags, first_seen, last_seen)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(subject_id) DO UPDATE SET
                title      = excluded.title,
                rate       = excluded.rate,
                star       = excluded.star,
                directors  = excluded.directors,
                casts      = excluded.casts,
                cover      = excluded.cover,
                url        = excluded.url,
                tags       = excluded.tags,
                last_seen  = excluded.last_seen
            """,
            (
                sid,
                r.get("title"),
                _to_float(r.get("rate")),
                _to_float(r.get("star")),
                json.dumps(r.get("directors") or [], ensure_ascii=False),
                json.dumps(r.get("casts") or [], ensure_ascii=False),
                r.get("cover"),
                r.get("url"),
                tags,
                ts,
                ts,
            ),
        )
        if exists:
            updated += 1
        else:
            added += 1
    conn.commit()
    return added, updated


def _merge_csv(old: str | None, new: Any) -> str:
    parts = [p for p in (old or "").split(",") if p]
    if new and str(new) not in parts:
        parts.append(str(new))
    return ",".join(parts)


def _to_float(v: Any) -> float | None:
    try:
        if v is None or v == "":
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _to_int(v: Any) -> int | None:
    try:
        if v is None or v == "":
            return None
        return int(v)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
#  详情层
# ---------------------------------------------------------------------------
DETAIL_COLUMNS = [
    "subject_id", "title", "original_title", "aka", "year", "subtype", "is_tv",
    "rating_value", "rating_count", "rating_star", "genres", "countries",
    "languages", "durations", "duration_min", "episodes_count", "pubdate",
    "release_year", "directors", "actors", "intro", "comment_count",
    "review_count", "card_subtitle", "cover_url", "url", "detail_json",
    "fetched_at",
]


def upsert_details(conn: sqlite3.Connection, rows: Iterable[dict[str, Any]]) -> int:
    n = 0
    placeholders = ",".join("?" * len(DETAIL_COLUMNS))
    cols = ",".join(DETAIL_COLUMNS)
    for r in rows:
        if not r.get("subject_id"):
            continue
        conn.execute(
            f"INSERT OR REPLACE INTO douban_detail ({cols}) VALUES ({placeholders})",
            [r.get(c) for c in DETAIL_COLUMNS],
        )
        n += 1
    conn.commit()
    return n


def detail_done_ids(conn: sqlite3.Connection) -> set[str]:
    return {row[0] for row in conn.execute("SELECT subject_id FROM douban_detail")}


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return bool(row)


def pending_detail_ids(conn: sqlite3.Connection, only_new: bool = True) -> list[str]:
    """按需要抓详情的 subject_id 列表。``only_new=False`` 时返回全部。

    **排序不是随便定的**：默认按评分从高到低，但正在上映的新片往往还没出分
    （或者分很低），一排就排到最后 —— 而那恰恰是唯一能跟猫眼票房对上的部分。
    实测跑完 330 条详情后，猫眼在映榜有 17 部出现在豆瓣**列表层**，
    却只有 4 部进了**详情层**，宽表因此只能匹配上 3 条。
    所以这里先补「片名在猫眼票房榜上」的那些（片名精确相等即可，不做归一化 ——
    归一化的匹配逻辑在 export.py，这里只要一个便宜的优先信号）。

    另外**降级行也要重抓**：``fetch_detail`` 在 rexxar 拿不到时会退回轻量的
    ``subject_abstract``，并在原始 JSON 里打 ``_degraded`` 标记。这种行字段
    残缺（没有简介/语言/时长/原名），但因为它已经在 ``douban_detail`` 里了，
    只按「没有详情」来找就永远看不见它 —— 实测有 161 条这样的电影被永久卡住。

    **降级行排在「完全没抓过」的行前面**（``degraded_first``）：降级行是已经躺在
    库里的**错数据**，会直接进宽表；没抓过的行只是缺，不污染已有结果。所以先修坏的。
    """
    if not only_new:
        return [row[0] for row in conn.execute(
            "SELECT subject_id FROM douban_list ORDER BY subject_id")]

    prefer = (
        "EXISTS(SELECT 1 FROM maoyan_board b WHERE b.title = l.title) DESC, "
        if _has_table(conn, "maoyan_board") else ""
    )
    degraded_first = "CASE WHEN d.detail_json LIKE '%_degraded%' THEN 0 ELSE 1 END, "
    sql = (
        "SELECT l.subject_id FROM douban_list l "
        "LEFT JOIN douban_detail d ON d.subject_id = l.subject_id "
        "WHERE d.subject_id IS NULL OR d.detail_json LIKE '%_degraded%' "
        f"ORDER BY {degraded_first}{prefer}l.rate DESC NULLS LAST, l.subject_id"
    )
    return [row[0] for row in conn.execute(sql)]


# ---------------------------------------------------------------------------
#  断点状态
# ---------------------------------------------------------------------------
def set_state(conn: sqlite3.Connection, key: str, value: Any) -> None:
    conn.execute(
        "INSERT INTO crawl_state (key, value, updated_at) VALUES (?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
        "updated_at = excluded.updated_at",
        (key, json.dumps(value, ensure_ascii=False), now_iso()),
    )
    conn.commit()


def get_state(conn: sqlite3.Connection, key: str, default: Any = None) -> Any:
    row = conn.execute("SELECT value FROM crawl_state WHERE key = ?", (key,)).fetchone()
    if not row:
        return default
    try:
        return json.loads(row[0])
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
#  猫眼
# ---------------------------------------------------------------------------
BOARD_COLUMNS = [
    "snapshot_date", "movie_id", "rank_no", "title", "release_info",
    "release_days", "category", "is_animation", "box_fen", "box_yuan",
    "box_desc", "split_box_fen", "split_box_yuan", "split_box_desc",
    "box_rate", "show_count", "show_count_rate", "avg_seat_view",
    "avg_show_view", "detail_url",
]

LIST_COLUMNS = [
    "snapshot_date", "movie_id", "show_type", "title", "status", "category",
    "is_animation", "actors", "release_date", "score", "detail_url", "raw",
]

NATION_COLUMNS = [
    "snapshot_date", "nation_box_num", "nation_box_unit", "nation_box_desc",
    "nation_split_num", "nation_split_unit", "nation_split_desc",
    "show_count_desc", "view_count_desc", "update_timestamp",
    "update_gap_second",
]


def _upsert(conn: sqlite3.Connection, table: str, columns: list[str], rows) -> int:
    """通用批量 upsert。列名全部来自模块内常量，不拼用户输入。"""
    n = 0
    cols = ",".join(columns)
    placeholders = ",".join("?" * len(columns))
    sql = f"INSERT OR REPLACE INTO {table} ({cols}) VALUES ({placeholders})"
    for r in rows:
        conn.execute(sql, [r.get(c) for c in columns])
        n += 1
    conn.commit()
    return n


def upsert_maoyan_board(conn: sqlite3.Connection, rows) -> int:
    """写票房榜。同一天重复跑会覆盖当天行，不堆重复数据。"""
    payload = [{**r, "fetched_at": now_iso()} for r in rows]
    return _upsert(conn, "maoyan_board", BOARD_COLUMNS + ["fetched_at"], payload)


def upsert_maoyan_trends(
    conn: sqlite3.Connection, snapshot_date: str, movie_id: str, trends
) -> int:
    payload = [
        {"snapshot_date": snapshot_date, "movie_id": str(movie_id), **t,
         "fetched_at": now_iso()}
        for t in trends
    ]
    return _upsert(
        conn, "maoyan_box_trend",
        ["snapshot_date", "movie_id", "box_date", "box_yuan", "box_desc",
         "release_day", "fetched_at"],
        payload,
    )


def upsert_maoyan_nation(conn: sqlite3.Connection, snapshot_date: str, nation: dict) -> int:
    payload = [{**nation, "snapshot_date": snapshot_date, "fetched_at": now_iso()}]
    return _upsert(conn, "maoyan_nation", NATION_COLUMNS + ["fetched_at"], payload)


def upsert_maoyan_list(conn: sqlite3.Connection, rows) -> int:
    payload = [{**r, "fetched_at": now_iso()} for r in rows]
    return _upsert(conn, "maoyan_list", LIST_COLUMNS + ["fetched_at"], payload)


def migrate_maoyan(conn: sqlite3.Connection) -> str | None:
    """把旧的 ``maoyan_snapshot`` 表挪走。

    旧表结构（score/box_office 全空，因为那时还没找到真票房接口）已经被
    拆成 board / box_trend / nation / list 四张表。**不直接删** ——
    里面有真实数据就改名保留，纯空占位才删掉。

    注意空串和 NULL 一样算「空」：旧代码写进去的是 ``''`` 而不是 NULL，
    只看 ``IS NOT NULL`` 会把一张空表误判成有数据。
    """
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='maoyan_snapshot'"
    ).fetchone()
    if not row:
        return None

    meaningful = conn.execute(
        "SELECT COUNT(*) FROM maoyan_snapshot "
        "WHERE TRIM(COALESCE(score, '')) <> '' OR TRIM(COALESCE(box_office, '')) <> ''"
    ).fetchone()[0]
    if meaningful:
        target = "maoyan_snapshot_legacy"
        n = 2
        while conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (target,)
        ).fetchone():
            target = f"maoyan_snapshot_legacy{n}"
            n += 1
        conn.execute(f"ALTER TABLE maoyan_snapshot RENAME TO {target}")
        conn.commit()
        return f"旧表 maoyan_snapshot 有 {meaningful} 行数据，已改名为 {target} 保留"
    conn.execute("DROP TABLE maoyan_snapshot")
    conn.commit()
    return "旧表 maoyan_snapshot 是空占位（score/box_office 全空），已删除"


# ---------------------------------------------------------------------------
#  请求日志
# ---------------------------------------------------------------------------
def log_fetch(
    conn: sqlite3.Connection,
    *,
    source: str,
    url: str,
    status: int | None,
    encoding: str | None = None,
    size_bytes: int | None = None,
    elapsed_ms: int | None = None,
    attempt: int | None = None,
    error: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO fetch_log (ts, source, url, status, encoding, size_bytes,"
        " elapsed_ms, attempt, error) VALUES (?,?,?,?,?,?,?,?,?)",
        (now_iso(), source, url, status, encoding, size_bytes, elapsed_ms, attempt, error),
    )
    conn.commit()


def prune_fetch_log(conn: sqlite3.Connection, keep: int) -> int:
    if keep <= 0:
        return 0
    cur = conn.execute(
        "DELETE FROM fetch_log WHERE id NOT IN "
        "(SELECT id FROM fetch_log ORDER BY id DESC LIMIT ?)",
        (keep,),
    )
    conn.commit()
    return cur.rowcount or 0


# ---------------------------------------------------------------------------
#  概览
# ---------------------------------------------------------------------------
def stats(conn: sqlite3.Connection) -> dict[str, Any]:
    q = lambda sql: conn.execute(sql).fetchone()[0]  # noqa: E731
    out: dict[str, Any] = {
        "列表层条目": q("SELECT COUNT(*) FROM douban_list"),
        "详情层条目": q("SELECT COUNT(*) FROM douban_detail"),
        "待补详情": q(
            "SELECT COUNT(*) FROM douban_list l LEFT JOIN douban_detail d "
            "ON d.subject_id = l.subject_id WHERE d.subject_id IS NULL"
        ),
        "其中电影": q("SELECT COUNT(*) FROM douban_detail WHERE is_tv = 0"),
        "其中剧集": q("SELECT COUNT(*) FROM douban_detail WHERE is_tv = 1"),
        "有评分": q("SELECT COUNT(*) FROM douban_detail WHERE rating_value IS NOT NULL"),
        "猫眼票房榜行": q("SELECT COUNT(*) FROM maoyan_board"),
        "猫眼票房榜天数": q("SELECT COUNT(DISTINCT snapshot_date) FROM maoyan_board"),
        "猫眼逐日票房行": q("SELECT COUNT(*) FROM maoyan_box_trend"),
        "猫眼片单行": q("SELECT COUNT(*) FROM maoyan_list"),
        "猫眼大盘天数": q("SELECT COUNT(*) FROM maoyan_nation"),
        "请求日志": q("SELECT COUNT(*) FROM fetch_log"),
    }
    row = conn.execute(
        "SELECT MIN(rating_value), MAX(rating_value), AVG(rating_value) "
        "FROM douban_detail WHERE rating_value IS NOT NULL"
    ).fetchone()
    if row and row[0] is not None:
        out["评分范围"] = f"{row[0]} ~ {row[1]}"
        out["平均评分"] = round(row[2], 2)
    last = conn.execute(
        "SELECT value FROM crawl_state WHERE key = 'last_run' "
    ).fetchone()
    if last:
        out["上次运行"] = last[0]
    return out
