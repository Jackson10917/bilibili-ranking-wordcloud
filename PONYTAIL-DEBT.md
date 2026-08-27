# Ponytail 债务台账

源码中 `ponytail:` 注释的汇总，防止「以后再说」悄悄变成「永远不做」。
每行格式：位置、简化了什么、上限（ceiling）、重新审视的触发条件（upgrade）。

- src/bilibili_ranker/cleaner.py:70, 噪声剥离只匹配 B站自家域名（bilibili.com/b23.tv），不通配任意 TLD。ceiling: 通配任意 TLD 会误伤「3.5」「vs.」这类正常词元。upgrade: 词云里出现成规模的他站域名噪声时，再往名单里加域名。
- src/bilibili_ranker/cleaner.py:149, 日语汉字词用「jieba 全切成单字、且单字不全为停用词时整块保留」的启发式回退。ceiling: 中文虚词串与日语汉字词在形状上重叠，启发式判不出全部边界。upgrade: 需要精确切分日语时引入 mecab/UniDic 词典。

已清偿：

- client.py 传输层截断不重试 → `response.json()` 的 RequestException 现按「无效 JSON」走 200 重试路径，与截断垃圾 body 行为一致（test_fetch_retries_on_truncated_body 锁守）。
- fonts.py 只校验魔数 → `_validate_font_file` 末尾用 `ImageFont.truetype` 试载，与渲染同一条 PIL 加载路径，损坏字体在校验期拦截（test_corrupt_font_rejected_before_pil 锁守）。

2 markers, 0 with no trigger.
