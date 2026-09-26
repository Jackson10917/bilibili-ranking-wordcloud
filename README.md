# B站排行榜词云

这个工具会抓取 B 站全站排行榜，把每个视频的标题、UP 主、播放量等信息存成表格，再把标题拆成词，计算词出现的频率，最后画成一张词云图：出现越多的词字越大。

每天运行一次，数据就会慢慢攒起来。之后可以看这段时间一共哪些词最多，也可以看最近一周哪些词变多了、哪些词变少了。

注意：统计只看视频标题，不代表视频本身的内容，也不代表整个 B 站的热度。

## 安装

需要：

- Python 3.10 或更新的版本
- 能正常打开 B 站的网络
- 电脑里装有中文字体（画词云要用）。Windows 和 macOS 一般自带，Linux 可能要自己装。

下载代码：

```bash
git clone https://github.com/Jackson10917/bilibili-ranking-wordcloud.git
cd bilibili-ranking-wordcloud
```

安装（Windows PowerShell）：

```powershell
py -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -e .
```

安装（macOS / Linux）：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
```

## 抓取流程

```bash
python -m bilibili_ranker --output-dir output
```

运行完，`output` 文件夹里会多出三个文件：

| 文件 | 里面是什么 |
| --- | --- |
| `ranking_时间.csv` | 榜单：排名、标题、UP 主、分区、播放量、点赞数等 |
| `word_frequency_时间.csv` | 每个词在这次榜单标题里出现了几次 |
| `wordcloud_时间.png` | 词云图 |

如果电脑里找不到中文字体，表格照样会保存，只是不出词云图，终端里会有提示。

## 怎么看累计和变化

每天都存到同一个文件夹，并加上 `--aggregate --trend`：

```bash
python -m bilibili_ranker --output-dir output --aggregate --trend
```

除了当天的文件，还会更新三个文件：

| 文件 | 里面是什么 |
| --- | --- |
| `word_frequency_aggregate.csv` | 把所有天加在一起，每个词一共出现了几次 |
| `wordcloud_aggregate.png` | 用上面这份累计数据画的词云（加了 `--aggregate` 就不再单独画当天的词云） |
| `word_frequency_trend.csv` | 最近 7 天和之前 7 天比，每个词的排名是升了、降了，还是新出现、掉出去了 |

- **如果同一天跑多次**，只算当天最后一次的结果，不会重复计数。
- **一个视频如果连续三天在榜**，它的标题就会被算三次。所以累计次数也反映了类似的话题在榜上待了多久。
- **变化表至少要有 8 天的数据**才会生成；满 14 天后，「最近 7 天」和「之前 7 天」才都是完整的一周。

不想再访问 B 站，只想用已有的数据重新画图、重新算变化：

```bash
python -m bilibili_ranker --output-dir output --no-fetch --aggregate --trend
```

默认抓的是全站榜。想抓某个分区的榜，可以用 `--rid` 加上 B 站的分区编号；不同分区请存到不同文件夹，不要混在一起。

## 停用词过滤逻辑

标题里有很多词出现得很频繁，却说明不了视频在讲什么，比如「完整版」「全网首发」「这种」「到底」，还有各种活动标签。这类词叫做**停用词**，统计时会被去掉，否则词云里全是它们。

同时ranker会尽量保留游戏名、歌曲名等有意义的词。

项目自带三份词表，放在 [resources/stopwords](src/bilibili_ranker/resources/stopwords/) 文件夹里：

| 文件 | 作用 |
| --- | --- |
| `custom_stopwords.txt` | 要去掉的词 |
| `title_phrases.txt` | 要整段去掉的短语，比如活动标签「全民制作人」。整段去掉，是为了不连累「制作」这种普通词 |
| `allowlist.txt` | 无论如何都要保留的词，优先级最高 |

### B站专属的停用词表

B 站的热点天天在变，但有些词不管热点怎么变都一直在，比如「到底」「真实」。只要攒了一段时间的数据，就可以让程序帮你把这类词找出来：

```bash
python -m bilibili_ranker stopword-analyze --data-dir output --output-dir analysis
```

1. **程序推荐候选词。** 被很多不同 UP 主用过、出现在很多分区、连续好多天都有的词，会进入候选表 `analysis/stopword_candidates.csv`。
2. **把握很大的词自动过滤。** 像「这种」「到底」这样本身没有实际意思的词，如果在各个分区都用得差不多、最近两周出现的次数也稳定，analysis会直接把它过滤掉。
3. **其他的你来决定。** 打开候选表检查，把决定写进 `analysis/stopword_decisions.csv`（第一次运行时会自动创建）：

   ```csv
   词,决定,备注
   可能,停用,
   挑战,保留,是内容不是套话
   ```

   「停用」的词以后一直被过滤；「保留」的词不再推荐，也不会被自动过滤。

   `stopword_decisions.csv` 就是你自己的 B 站停用词表，程序只会读它，不会改它。候选表每次运行都会重新生成，不要在候选表里写决定。用 Excel 编辑的话，保存时请选「CSV UTF-8」格式，否则中文会乱码。

分析完成后，`analysis` 文件夹里有：

| 文件 | 里面是什么 |
| --- | --- |
| `stopword_candidates.csv` | 等你审核的候选词，附带统计数字和几个标题例子 |
| `stopword_decisions.csv` | 你的决定 |
| `auto_stopwords.txt` | 这一次被自动过滤的词 |
| `stopword_summary.json` | 这次分析用的设置和结果概要 |
| `baseline/` | 只用项目自带词表时的词频和变化，用来对比 |
| `current/` | 再去掉你的停用词和自动过滤的词之后的结果，平时看这里就行 |

自动过滤的词每次都会重新判断，不再符合条件的会自动恢复。原始榜单文件不会被改动。

想看新规则下的词云，再运行一次：

```bash
python -m bilibili_ranker --output-dir analysis/current --no-fetch --aggregate --trend
```

<details>
<summary>什么样的词会被推荐、什么样的词会被自动过滤</summary>

**进入候选表**，要同时满足：

- 至少有 7 天出现过；
- 出现的天数占全部天数的 30% 以上；
- 至少有 5 个不同的 UP 主在标题里用过。

这三个数可以分别用 `--min-days`、`--min-day-ratio`、`--min-uploaders` 调整。

**被自动过滤**，还要再同时满足：

- 是「这种」「到底」「竟然」这类本身没有实际意思的词（代词、副词、量词之类）；
- 已经攒了至少 14 天的数据；
- 在各个分区里用得比较平均，大致相当于平均分布在 3 个以上的分区；
- 最近一周的出现次数在前一周的一半到两倍之间；
- 不在自带的保留词表里，你也没有把它标成「保留」。

程序只能从次数上判断一个词，看不懂它的意思。所以有实际意思的词只推荐、不会自动过滤，由你决定。

</details>

## 常用设置

| 写法 | 作用 |
| --- | --- |
| `--output-dir 文件夹` | 结果存到哪里，默认是 `output` |
| `--font-path 字体文件` | 指定画词云用的中文字体，支持 `.ttf`、`.ttc`、`.otf` |
| `--width 1920 --height 1080` | 词云图片的宽和高 |
| `--max-words 300` | 词云里最多放多少个词 |
| `--user-dict 文件` | 告诉程序哪些词是一个整体、不要拆开（比如新出的游戏名），每行写一个词 |
| `--languages zh,en,ja,ko` | 过滤哪几种语言里的常见虚词，默认是中、英、日、韩、法、德、西、俄 |
| `--resource-dir 文件夹` | 换成你自己的一套词表，文件夹里要有 `custom_stopwords.txt` 和 `allowlist.txt`，`title_phrases.txt` 可有可无 |
| `--timeout 15` | 访问 B 站时最多等多少秒 |

上面是抓榜时用的设置。停用词分析的设置通过 `--help` 查看：

```bash
python -m bilibili_ranker --help
python -m bilibili_ranker stopword-analyze --help
```

## 常见问题

**没有生成词云图？**

先看终端里的提示。最常见的原因是找不到中文字体，或者标题过滤完一个词都不剩。可以装一个中文字体（比如思源黑体、Noto Sans CJK），或者直接告诉程序字体在哪里：

```bash
python -m bilibili_ranker --output-dir output --font-path /path/to/font.ttf
```

也可以把字体路径写进环境变量 `BILIBILI_WORDCLOUD_FONT`。如果词云里出现方框或缺字，说明这个字体不包含那些字，换一个字体就好。

**抓取失败了？**

可能是网络问题，也可能是 B 站暂时拦住了请求。程序会自动重试几次，还是不行的话，过一会儿再运行。请不要频繁抓取。如果需要修改程序访问 B 站时自报的浏览器身份（User-Agent），可以设置环境变量 `BILIBILI_UA`。

**为什么词云里没有某个词？**

- 网址、BV 号、表情符号和纯数字不参与统计。
- 默认只统计两个字及以上的词，英文会统一转成小写。
- 这个词可能在停用词表里。想让它出现，把它加进 `allowlist.txt`。
- 如果是游戏名、人名被拆成了几段，用 `--user-dict` 告诉ranker它是一个整体。

数据来自 [B站排行榜](https://www.bilibili.com/v/popular/rank/all)。B 站随时可能调整排行榜的返回内容和访问限制，使用时请遵守 B 站的相关规定。

项目采用 [MIT License](LICENSE)。
