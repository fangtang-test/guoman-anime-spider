"""国漫数据采集工具包。

模块划分::

    config   读 config.toml
    db       SQLite 建表 / 去重 / 断点状态
    http     限速 + 重试 + 编码自适应的 HTTP 客户端
    douban   豆瓣采集（全量枚举 + rexxar 详情）
    maoyan   猫眼采集（在映片单 + 可选浏览器票房）
    export   导出 CSV / parquet / Tableau 宽表
    pipeline 编排与命令行入口
"""

__version__ = "1.0.0"
__all__ = ["config", "db", "http", "douban", "maoyan", "export", "pipeline"]
