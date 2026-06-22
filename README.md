# B站排行榜数据与标题词云

抓取B站排行榜，将榜单整理为CSV，并根据视频标题生成词云图。

## 功能

- 请求B站排行榜接口，记录数量以接口实际返回为准；
- 按BV号去重并输出CSV；
- 提取标题片段；
- 使用 `jieba` 进行分词；
- 使用 `stopwordsiso` 和项目词表过滤停用词；
- 忽略 Emoji、标点和纯符号标题；
- 按候选文件名查找 Windows、macOS 和 Linux 字体，也可显式指定字体；
- 正常榜单产生非空词频且字体可用时，每次成功运行新增排行榜 CSV 和词云 PNG。

## 项目结构

```
├─ src/bilibili_ranker/
│  ├─ client.py          # API 请求、重试与响应校验
│  ├─ models.py          # API 字段解析
│  ├─ cleaner.py         # 去重、分词和停用词过滤
│  ├─ stopwords.py       # 多语言停用词与保留词策略
│  ├─ fonts.py           # 跨平台字体查找
│  ├─ wordcloud.py       # 词云图生成
│  ├─ storage.py         # CSV 与输出路径
│  ├─ cli.py             # 命令行流程
│  └─ resources/stopwords/
│     ├─ custom_stopwords.txt
│     ├─ allowlist.txt
│     └─ README.md
├─ .gitignore
├─ pyproject.toml
└─ README.md
```

## 环境要求

- Python 3.10 或更高版本
- 可访问B站排行榜的网络环境

## 安装

Windows PowerShell：

```
py -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e .
```

macOS / Linux：

```
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e .
```

## 运行

```
bilibili-rank --output-dir output
```

也可以使用模块入口：

```
python -m bilibili_ranker --output-dir output
```

常用参数：

```
--output-dir PATH              输出目录，默认 output
--resource-dir PATH            覆盖内置停用词资源目录
--font-path PATH               指定 TTF、TTC 或 OTF 字体
--languages zh,en,ja,ko        指定停用词语言
--minimum-token-length 2       普通词最短长度
--width 1920                   词云图宽度
--height 1080                  词云图高度
--max-words 300                词云图最大词数
--timeout 15                   API 请求超时秒数
```

## 输出

正常榜单产生非空词频且字体可用时，每次成功运行会在输出目录新增两个带 UTC 时间标识的文件：

```
output/
├─ ranking_YYYYMMDDTHHMMSSZ.csv
└─ wordcloud_YYYYMMDDTHHMMSSZ.png
```

CSV 使用 `utf-8-sig` 编码，可直接使用 Excel 打开。再次运行不会删除已有结果；新结果使用新的时间标识保存。若标题清洗后没有可用词元，则只生成 CSV。

## CSV 字段

| 表头 | 含义 |
| --- | --- |
| 排名 | 本次榜单顺序 |
| BV号 | 视频BV号 |
| 视频链接 | B站视频页面链接 |
| 视频标题 | 视频标题原文 |
| 视频分区 | 视频所属细分分区 |
| 主分区 | 视频所属上级分区 |
| UP主 | 投稿账号名称 |
| 发布时间（北京时间） | 视频发布时间，格式为 `YYYY-MM-DD HH:MM:SS` |
| 视频时长（秒） | 视频总时长 |
| 播放量 | 视频播放次数 |
| 弹幕数 | 弹幕数量 |
| 评论数 | 评论数量 |
| 收藏数 | 收藏数量 |
| 投币数 | 投币数量 |
| 分享数 | 分享数量 |
| 点赞数 | 点赞数量 |

## 标题处理

标题首先进行 Unicode NFKC 和空白归一化。中文使用 `jieba` 分词；其他语言按连续字符片段提取。未列出的文字系统不会进入词频。

默认加载以下语言：

```
zh, en, ja, ko, fr, de, es, ru
```

项目停用词位于 `custom_stopwords.txt`，需要保留的短词位于 `allowlist.txt`。保留词优先于基础停用词和项目停用词。

Emoji、标点及其他符号不参与词频统计。标题中只有 Emoji 或符号时，该标题不会向词云图提供词元。

## 字体

字体文件按以下顺序查找：

1. `--font-path` 指定的字体；
2. 环境变量 `BILIBILI_WORDCLOUD_FONT`；
3. 系统中的 Noto Sans CJK、思源黑体、微软雅黑、黑体、苹方或文泉驿字体；
4. Linux `fontconfig` 返回的字体。

Linux 推荐安装 Noto Sans CJK。仓库不包含专有字体文件。自动查找只确认候选字体文件存在，不检查完整字形覆盖；若词云出现缺字，请使用 `--font-path` 指定包含所需字符的字体。

## 数据来源

排行榜页面：https://www.bilibili.com/v/popular/rank/all
