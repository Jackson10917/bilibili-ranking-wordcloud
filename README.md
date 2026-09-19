# B站排行榜与标题词云

每天看看 B 站榜上都在聊什么。

这个小工具会抓取排行榜，把视频信息保存成 CSV，再从标题里统计词频、生成词云。连续收集几天后，还能查看累计词频，以及哪些词最近出现得更多。

统计只来自视频标题，适合观察榜单话题，不能代表视频内容或整个 B 站的热度。

## 先跑一次

需要 Python 3.10 或更高版本，以及能正常访问 B 站的网络。生成中文词云还需要系统里有中文字体。

先下载代码：

```bash
git clone https://github.com/Jackson10917/bilibili-ranking-wordcloud.git
cd bilibili-ranking-wordcloud
```

Windows PowerShell：

```powershell
py -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -e .
```

macOS / Linux：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
```

然后运行：

```bash
python -m bilibili_ranker --output-dir output
```

结果会放在 `output/`：

| 文件 | 内容 |
| --- | --- |
| `ranking_时间戳.csv` | 排名、标题、UP 主、分区、播放量等榜单信息 |
| `word_frequency_时间戳.csv` | 每个词在本次榜单标题中出现的次数 |
| `wordcloud_时间戳.png` | 按词频生成的词云 |

CSV 可以直接用 Excel 打开。文件名使用 UTC 时间，重复运行会保存新快照，不覆盖之前的榜单。没有找到字体时，仍会保存 CSV，并提示词云未生成。

## 连续收集，看累计和变化

每次使用同一个输出目录，就能把历史数据攒起来：

```bash
python -m bilibili_ranker --output-dir output --aggregate --trend
```

除了当天的榜单和词频，还会更新三个文件：

- `word_frequency_aggregate.csv`：累计词频。
- `wordcloud_aggregate.png`：根据累计词频生成的词云，替代当天的单独词云。
- `word_frequency_trend.csv`：近期与前一期的词频、排名变化。

同一天运行多次，累计和趋势只取当天最后一份快照。一个视频如果连续三天在榜，它的标题也会参与三天的统计，所以累计词频包含了在榜时长的影响。

趋势默认比较最近 7 个快照日和之前的最多 7 个快照日。至少收集 8 天才会生成趋势表；满 14 天后，两边才都是完整的 7 天。中间漏抓某一天时，按已有快照顺延。

只想用已有数据重新出图，不想再请求 B 站：

```bash
python -m bilibili_ranker --output-dir output --no-fetch --aggregate --trend
```

不同分区请分开目录保存，避免混在一起统计。可以用 `--rid` 指定 B 站接口支持的分区 ID，默认 `0` 是全站榜。

## 让词云少一点套话

标题里常有“完整版”“全网首发”或活动标签。它们反复出现，却不一定能说明视频在讲什么。项目会过滤这些内容，同时尽量保留游戏名、歌曲名，以及 AI、MV 等有意义的短词。

词表放在 [resources/stopwords](src/bilibili_ranker/resources/stopwords/)：

| 文件 | 用途 |
| --- | --- |
| `custom_stopwords.txt` | 不需要参与统计的词 |
| `title_phrases.txt` | 在分词前去掉的完整短语，例如活动标签 |
| `allowlist.txt` | 即使被停用词规则命中，也要保留的词 |

例如，“全民制作人”可以作为活动短语过滤，但不能因此把所有标题里的“制作”都删掉；“全网首发”可以去掉，但手机发布里的“首发”仍有意义。

有了历史榜单，还可以运行停用词分析：

```bash
python -m bilibili_ranker stopword-analyze --data-dir output --output-dir analysis
```

它会从原始标题重新统计，生成候选报告，并把符合条件的互动套话加入独立的自动词表。普通高频词不会仅因为出现得多、排名下降或退出榜单就被删除。

| 输出位置 | 内容 |
| --- | --- |
| `analysis/stopword_candidates.csv` | 候选词、统计依据和标题示例 |
| `analysis/auto_stopwords.txt` | 本轮自动生效的停用词 |
| `analysis/stopword_summary.json` | 分析参数与结果摘要 |
| `analysis/baseline/` | 按当前内置词表重算的词频与趋势 |
| `analysis/current/` | 应用自动词表后的词频与趋势，日常查看这里即可 |

自动词表每次都会重新评估，不再符合条件的词会退出。原始榜单保持不变。分析完成后，可以再生成一张使用新规则的词云：

```bash
python -m bilibili_ranker --output-dir analysis/current --no-fetch --aggregate --trend
```

<details>
<summary>候选词和自动生效的条件</summary>

候选词默认至少出现 7 天、覆盖 60% 的快照日，累计词频不少于 20。可以用 `--min-days`、`--min-day-ratio` 和 `--min-total` 调整。

自动生效会更严格：必须属于预先列出的互动套话，历史至少有 14 个快照日，涉及至少 5 个独立视频、3 个分区，最近两期词频比在 0.5–2 之间，而且不在保留词表中。

这些条件用于减少误删，并不等于机器能完全理解词义。候选报告仍值得定期检查。

</details>

## 常用设置

| 参数 | 用途 |
| --- | --- |
| `--output-dir PATH` | 保存结果的目录，默认 `output` |
| `--font-path PATH` | 指定中文字体文件，支持 TTF、TTC、OTF |
| `--width 1920 --height 1080` | 词云尺寸 |
| `--max-words 300` | 词云最多显示多少个词 |
| `--user-dict PATH` | 补充 jieba 词典，帮助识别游戏名、番剧名等专有名词 |
| `--languages zh,en,ja,ko` | 选择要加载的停用词语言 |
| `--resource-dir PATH` | 使用自己的词表目录，需包含 `custom_stopwords.txt` 和 `allowlist.txt`，可选提供 `title_phrases.txt` |
| `--timeout 15` | 网络请求超时秒数 |

上表是抓榜命令的设置。停用词分析子命令的参数请查看它自己的帮助：

```bash
python -m bilibili_ranker --help
python -m bilibili_ranker stopword-analyze --help
```

## 常见问题

**没有生成词云，怎么办？**

先看终端里的提示。常见原因是缺少中文字体，或标题过滤后没有可用的词。可以安装 Noto Sans CJK、思源黑体等字体，也可以直接指定已有字体：

```bash
python -m bilibili_ranker --output-dir output --font-path /path/to/font.ttf
```

字体路径也可以通过环境变量 `BILIBILI_WORDCLOUD_FONT` 设置。词云出现方框或缺字时，请换成包含相应字符的字体。

**为什么抓取失败了？**

可能是网络问题，也可能是 B 站暂时限制了请求。程序会尝试重试；仍然失败时，稍后再运行。需要调整请求的 User-Agent 时，可以设置环境变量 `BILIBILI_UA`。请避免频繁抓取。

**为什么词云里没有某个词？**

链接、BV 号、Emoji 和纯数字不会参与统计。普通词默认至少两个字符，英文会统一成小写。也可以检查停用词表，或把需要保留的词加入 `allowlist.txt`；如果是名字被切碎，优先补充分词词典。

## 开发与数据来源

本地运行测试：

```bash
python -m pip install -e ".[test,lint]"
python -m pytest tests
python -m ruff check .
python -m ruff format --check .
python -m mypy
```

数据来自 [B站排行榜](https://www.bilibili.com/v/popular/rank/all)，抓取使用 `ranking/v2` 接口。接口返回内容和访问限制可能变化，使用时请遵守 B 站的相关规则。

项目采用 [MIT License](LICENSE)。
