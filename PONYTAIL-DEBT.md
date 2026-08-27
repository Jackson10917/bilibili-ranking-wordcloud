# Ponytail 债务台账

源码中 `ponytail:` 注释的汇总，防止「以后再说」悄悄变成「永远不做」。
每行格式：位置、简化了什么、上限（ceiling）、重新审视的触发条件（upgrade）。
约定与 [ponytail](https://github.com/) 技能一致：注释里必须写明上限和升级路径。

- src/bilibili_ranker/cleaner.py:70, 噪声剥离只匹配 B站自家域名（bilibili.com/b23.tv），不通配任意 TLD。ceiling: 通配任意 TLD 会误伤「3.5」「vs.」这类正常词元。upgrade: 词云里出现成规模的他站域名噪声时，再往名单里加域名。
- src/bilibili_ranker/cleaner.py:149, 日语汉字词用「jieba 全切成单字、且单字不全为停用词时整块保留」的启发式回退。ceiling: 中文虚词串与日语汉字词在形状上重叠，启发式判不出全部边界。upgrade: 需要精确切分日语时引入 mecab/UniDic 词典。
- src/bilibili_ranker/client.py:153, 传输层截断（IncompleteRead）不在应用层重试。ceiling: 重试它会与 urllib3 Retry(total=2) 叠乘放大请求次数。upgrade: 线上日志频繁出现「请求失败：截断读取」时，再为它单开低次数重试。
- src/bilibili_ranker/fonts.py:74, 字体只校验 4 字节容器魔数，不解析字形表。ceiling: 字形表损坏要拖到 PIL 渲染期才报错（错误信息已含字体路径）。upgrade: 用户频繁遇到渲染期字体报错时，引入 fontTools 做深度校验。

4 markers, 0 with no trigger.
