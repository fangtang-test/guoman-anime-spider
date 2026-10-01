"""配置加载：把 config.toml 读成带默认值的强类型对象。

用 tomllib（Python 3.11+ 标准库），所以不引入 pyyaml 之类的额外依赖。
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

PKG_DIR = Path(__file__).resolve().parent
ROOT = PKG_DIR.parent
DEFAULT_CONFIG_PATH = ROOT / "config.toml"

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


@dataclass
class HttpConfig:
    timeout: float = 25.0
    retries: int = 3
    backoff: float = 2.5
    min_delay: float = 1.2
    max_delay: float = 2.8
    retry_status: list[int] = field(default_factory=lambda: [429, 500, 502, 503, 504])
    user_agent: str = DEFAULT_UA
    proxies: dict[str, str] = field(default_factory=dict)


@dataclass
class DoubanConfig:
    enabled: bool = True
    # 注意：没有 types/movie/tv 这种字段。枚举接口的 type 参数实测被豆瓣
    # 完全忽略（传 movie / tv / 不传返回同一批数据，老接口甚至把
    # 《眷思量 第二季》标成 movie），所以电影/剧集一律看详情里的 is_tv。
    # 标签只决定「能捞到哪些作品」——「国产动画」的深处才是国漫番剧大本营，
    # 因此 max_start 必须开得足够大，否则会漏掉剧集。
    # 后三个是**档期标签**，跟题材无关，但它们才能让猫眼票房对上号：
    # 实测「中国大陆」一页 20 条有 7 条精确命中猫眼在映榜。只抓题材标签的话
    # 库里全是老片，猫眼那几列会 100% 空着。
    tags: list[str] = field(
        default_factory=lambda: [
            "动画", "日本动画", "国产动画", "中国动画", "国漫", "欧美动画",
            "中国大陆", "华语", "豆瓣高分",
        ]
    )
    max_start: int = 8000
    empty_pages_stop: int = 2
    # 空页第一次等这么久，之后翻倍，上限 empty_recheck_max。
    # 豆瓣限流返回的是「HTTP 200 + 空数据」，跟真翻到底一模一样；
    # 实测「中国大陆」连翻 10 页后「华语」整段变空，几十秒后才恢复。
    empty_recheck_wait: float = 20.0
    empty_recheck_max: float = 90.0
    # 同一位置最多重试几次（start=0 的首页空页另有更宽的次数，见 douban.py）
    empty_retries: int = 2
    # 换标签前先歇一会儿：限流按 IP 总量算，换标签不会清零
    tag_cooldown: float = 15.0
    fetch_detail: bool = True
    detail_only_new: bool = True


@dataclass
class MaoyanConfig:
    # 注意：这里没有 browser / render_wait 了。早期以为票房得靠浏览器渲染
    # 猫眼详情页再解「石头字体」，后来发现专业版接口
    # https://piaofang.maoyan.com/dashboard-ajax/movie 免签名直接给真票房，
    # 所以整条浏览器路线不再需要。（石头字体解码留在 stonefont.py 备用，
    # 只有当你想抠「预售票房」时才用得上 —— 那个数字在片单页里是混淆的。）
    enabled: bool = True
    # 票房榜：无参数一次拿回 70+ 部在映影片的累计票房/排片/上座/场次 + 大盘
    board: bool = True
    # 单片类型 + 近 5 日逐日票房。每部一次请求，是耗时大头（约 2 秒/部）
    box_trends: bool = True
    # 只抓榜单前 N 部。0 = 全部（约 73 部、2~3 分钟）
    box_trends_limit: int = 0
    # 片单页：1 = 正在热映，2 = 即将上映/预售。
    # 【别搞反】showType=2 不是「正在热映」，早期抓错就是栽在这
    list_show_types: list[int] = field(default_factory=lambda: [1, 2])


@dataclass
class StorageConfig:
    db_path: str = "data/anime.db"
    export_dir: str = "exports"
    fetch_log_keep: int = 20000


@dataclass
class ExportConfig:
    csv: bool = True
    parquet: bool = True
    json: bool = False
    tableau_wide: bool = True
    hyper: bool = False


@dataclass
class Config:
    http: HttpConfig = field(default_factory=HttpConfig)
    douban: DoubanConfig = field(default_factory=DoubanConfig)
    maoyan: MaoyanConfig = field(default_factory=MaoyanConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    export: ExportConfig = field(default_factory=ExportConfig)
    root: Path = ROOT
    source_path: Path | None = None

    # -- 路径 ---------------------------------------------------------------
    @property
    def db_path(self) -> Path:
        p = Path(self.storage.db_path)
        return p if p.is_absolute() else (self.root / p)

    @property
    def export_dir(self) -> Path:
        p = Path(self.storage.export_dir)
        return p if p.is_absolute() else (self.root / p)


def _pick(cls, data: dict):
    """只挑 dataclass 声明过的键，忽略配置文件里的多余/写错的键。"""
    allowed = {f for f in cls.__dataclass_fields__}
    return cls(**{k: v for k, v in (data or {}).items() if k in allowed})


def load(path: str | Path | None = None) -> Config:
    """读配置。文件不存在时返回全默认配置（不会崩）。"""
    p = Path(path) if path else DEFAULT_CONFIG_PATH
    raw: dict = {}
    if p.exists():
        with p.open("rb") as fh:
            raw = tomllib.load(fh)

    cfg = Config(
        http=_pick(HttpConfig, raw.get("http")),
        douban=_pick(DoubanConfig, raw.get("douban")),
        maoyan=_pick(MaoyanConfig, raw.get("maoyan")),
        storage=_pick(StorageConfig, raw.get("storage")),
        export=_pick(ExportConfig, raw.get("export")),
        source_path=p if p.exists() else None,
    )
    # [proxies] 是顶层表，塞进 http 里
    proxies = raw.get("proxies") or {}
    cfg.http.proxies = {str(k).lower(): str(v) for k, v in proxies.items() if v}
    return cfg
