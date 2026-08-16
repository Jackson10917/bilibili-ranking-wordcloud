# B站排行榜数据与标题词云

抓取B站排行榜，将榜单整理为CSV，并根据视频标题生成词云图。

## 功能

- 请求B站排行榜接口（瞬时网络故障自动重试，被风控拦截时刷新 buvid 后重试），记录数量以接口实际返回为准；
- 按BV号去重并输出CSV；
- 提取标题片段；
- 使用 `jieba` 进行分词；
- 使用 `stopwordsiso` 和项目词表过滤停用词；
- 忽略 Emoji、标点和纯符号标题；
- 按候选文件名查找 Windows、macOS 和 Linux 字体，也可显式指定字体；
- 正常榜单产生非空词频且字体可用时，每次成功运行新增排行榜 CSV 和词云图。

## 项目结构

```
├─ src/bilibili_ranker/
│  ├─ client.py          # API 请求与响应校验
│  ├─ models.py          # API 字段解析
│  ├─ cleaner.py         # 去重、分词和停用词过滤
│  ├─ stopwords.py       # 多语言停用词与保留词策略
│  ├─ fonts.py           # 跨平台字体查找
│  ├─ wordcloud.py       # 词云图生成
│  ├─ storage.py         # CSV 与输出路径
│  ├─ cli.py             # 命令行流程
│  ├─ __init__.py        # 包标记
│  ├─ __main__.py        # 模块入口
│  └─ resources/stopwords/
│     ├─ custom_stopwords.txt
│     ├─ allowlist.txt
│     └─ README.md
├─ tests/
│  └─ test_core.py
├─ .gitignore
├─ LICENSE
├─ pyproject.toml
└─ README.md
```

## 环境要求

- Python 3.10 或更高版本
- 可访问B站排行榜

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
--resource-dir PATH            覆盖内置停用词资源目录，该目录必须同时包含
                               custom_stopwords.txt 和 allowlist.txt，缺任一个直接报错退出
--font-path PATH               指定 TTF、TTC 或 OTF 字体
--languages zh,en,ja,ko        指定停用词语言，大小写不敏感（ZH 与 zh 等价）
--minimum-token-length 2       普通词最短长度
--width 1920                   词云图宽度
--height 1080                  词云图高度
--max-words 300                词云图最大词数
--timeout 15                   API 请求超时秒数，取值范围 0 到 86400（超大值会让底层
                               socket 报错，直接拒绝）
```

## 输出

正常榜单产生非空词频且字体可用时，每次成功运行会在输出目录新增两个带 UTC 时间标识的文件：

```
output/
├─ ranking_YYYYMMDDTHHMMSSZ.csv
└─ wordcloud_YYYYMMDDTHHMMSSZ.png
```

CSV 使用 `utf-8-sig` 编码，可直接使用 Excel 打开。标题和 UP 主名称若以 `=`、`+`、`-`、`@` 开头，会加单引号前缀，避免电子表格把投稿内容当公式求值。再次运行不会删除已有结果；同一秒内多次运行时，新结果会在文件名中追加 `-2`、`-3` 后缀，避免覆盖。文件名通过 `O_CREAT|O_EXCL` 原子占位，多进程并行且落在同一秒时同样不会互相覆盖。若标题清洗后没有可用词元，或词云生成失败（如缺少字体），则只生成 CSV，并在失败时给出警告。

若接口返回成功但整榜记录全部无法解析（例如上游字段变更），退出码为 1，同时仍写出只含表头的 CSV 便于排查——自动化任务不会把这种情况误判为成功。

### 退出码

| 码 | 含义 |
| --- | --- |
| 0 | 成功（含「只输出 CSV」的降级情况） |
| 1 | 运行期失败：网络/风控、整榜解析失败、语言代码不被支持、`--resource-dir` 缺文件 |
| 2 | argparse 参数格式错误：未知参数、类型不符、`--timeout`/`--width`/`--height`/`--max-words`/`--minimum-token-length` 取值越界 |
| 130 | Ctrl+C 中断 |

注意 `--languages zh,xx` 与 `--resource-dir` 缺文件属于「值有效但资源不可用」，在流程内报错，退出码是 1 而非 2。

## CSV 字段

| 表头 | 含义 |
| --- | --- |
| 排名 | 本次榜单顺序（去重后可能不连续） |
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

标题首先进行 Unicode NFKC 归一化、剔除零宽等不可见字符（`Cf` 类）并压缩空白，避免「防和谐」标题里插入的零宽空格把词拆碎。随后剥除链接（`http(s)://…`、`www.…`）和 BV 号，避免 `https`、`b23.tv`、`bv1xx411c7md` 这类标识符片段进入词云。中文使用 `jieba` 分词；其他语言按连续字符片段提取。未列出的文字系统不会进入词频。

默认加载以下语言：

```
zh, en, ja, ko, fr, de, es, ru
```

项目停用词位于 `custom_stopwords.txt`，需要保留的短词位于 `allowlist.txt`。保留词优先于基础停用词和项目停用词。

Emoji、标点及其他符号不参与词频统计。标题中只有 Emoji 或符号时，该标题不会向词云图提供词元。纯数字词元（如年份 2024）同样不参与词频统计。

## 字体

字体文件按以下顺序查找：

1. `--font-path` 指定的字体；
2. 环境变量 `BILIBILI_WORDCLOUD_FONT`；
3. 系统中的 Noto Sans CJK、思源黑体、微软雅黑、黑体、苹方或文泉驿字体；
4. Linux `fontconfig` 返回的字体。

Linux 推荐安装 Noto Sans CJK。仓库不包含专有字体文件。显式指定的字体（`--font-path` 或 `BILIBILI_WORDCLOUD_FONT`）会校验后缀与 sfnt 容器魔数，内容损坏时直接报错，不会拖到渲染阶段；自动查找只确认候选字体文件存在，不检查完整字形覆盖，若词云出现缺字，请使用 `--font-path` 指定包含所需字符的字体。

`.ttc` 是字体集合容器，内部按语言分多个 face。`wordcloud` 调用 PIL 时不传 `index`，恒取 face 0——`NotoSansCJK-Regular.ttc` 的 face 0 是日文，简体汉字会以日文字形变体渲染（如「直」「骨」的写法差异），不是缺字。候选列表已把单体 `NotoSansCJKsc-Regular.otf` 排在 `.ttc` 之前；Debian 系的 `fonts-noto-cjk` 只提供 `.ttc`，若在意字形，用 `--font-path` 指定单体 SC 字体（`NotoSansSC-Regular.otf` 等）。

## 环境变量

| 变量 | 作用 |
| --- | --- |
| `BILIBILI_WORDCLOUD_FONT` | 指定词云字体路径，优先级低于 `--font-path` |
| `BILIBILI_UA` | 覆盖请求排行榜接口使用的 User-Agent，浏览器版本过时时无需改代码 |

## 数据来源

排行榜页面：<https://www.bilibili.com/v/popular/rank/all>

项目使用的公开接口：

```
https://api.bilibili.com/x/web-interface/ranking/v2?rid=0&type=all
```

## 许可证

本项目使用 [MIT License](LICENSE)。
