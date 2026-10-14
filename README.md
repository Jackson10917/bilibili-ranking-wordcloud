# B站排行榜词云

每天抓一份 B 站全站排行榜，把视频标题拆成词，生成累计词频、词云和最近两周的变化表。榜单每天保存一份，同一天重跑会覆盖当天文件。

统计只看标题。同一个视频连续多天在榜，标题也会被多次统计。

## 安装

需要 Python 3.12 或更新版本，以及中文字体。Windows 默认使用微软雅黑；Ubuntu 安装字体：

```bash
sudo apt-get install fonts-noto-cjk
```

下载代码并安装依赖：

```bash
git clone https://github.com/Jackson10917/bilibili-ranking-wordcloud.git
cd bilibili-ranking-wordcloud
python -m pip install -r requirements.txt
```

## 使用

抓取当天榜单：

```bash
python bili_wordcloud.py fetch
```

生成结果：

```bash
python bili_wordcloud.py report
```

原始榜单保存在 `data/ranking_YYYY-MM-DD.csv`，日期按北京时间算。结果保存在 `results/`：

| 文件 | 内容 |
| --- | --- |
| `word_frequency.csv` | 全部快照日累计的词和次数 |
| `wordcloud.png` | 累计词云，出现越多的词越大 |
| `trend.csv` | 最近 7 个快照日与之前 7 个的次数差 |
| `auto_stopwords.txt` | 当前自动过滤的词 |

两条命令都可以用 `--data 文件夹` 指定数据位置。`report` 还可以用 `--out 文件夹` 指定结果位置，用 `--font 字体路径` 指定中文字体。

趋势表按“本周次数减上周次数”排序，只收至少有一周出现 2 次的词。不足 14 个快照日时，两个窗口还不完整；缺少某天数据时，按已有快照日往前取。

## 维护词表

标题先统一全角字符和英文大小写，再用 jieba 分词。只统计至少两个字符、含有文字的词，并过滤中英文常见停用词。

| 文件 | 作用 |
| --- | --- |
| [words/stopwords.txt](words/stopwords.txt) | 手动过滤的词，包括互动套话 |
| [words/keep.txt](words/keep.txt) | 必须保留的词，覆盖手动和自动停用词 |
| [words/phrases.txt](words/phrases.txt) | 分词前整段移除的短语，比如活动标签 |
| [words/userdict.txt](words/userdict.txt) | 不要拆开的词，比如游戏名 |

### 自动停用词

从第 14 个快照日开始，程序按日期回放。每天看最近最多 28 个快照日：一个词属于代词、副词、量词等虚词，至少一半的日子出现过，而且被至少 5 个不同 UP 主用过，就自动过滤。

已经过滤的词，只要仍在至少四分之一的日子里出现，就继续过滤；低于四分之一才恢复。`keep.txt` 里的词始终保留。程序只看分词器的词性和统计次数，判断错的词可以加到保留表。

自动词表每次都从原始榜单重新算，不需要保存上一次的状态。

数据来自 [B站全站排行榜](https://www.bilibili.com/v/popular/rank/all)。原始榜单和生成结果存放在私有数据仓库，不随公开代码发布。

采用 [MIT 许可](LICENSE)。
