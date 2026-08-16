"""B站排行榜抓取与多语言标题词云。"""

from importlib.metadata import PackageNotFoundError, version

try:
    # 版本号只在 pyproject 里写一次，从已安装的包元数据读，避免两处不同步。
    __version__ = version("bilibili-ranking-wordcloud")
except PackageNotFoundError:  # 直接从源码树 import（未安装）时没有元数据
    __version__ = "0.0.0.dev0"

__all__ = ["__version__"]
