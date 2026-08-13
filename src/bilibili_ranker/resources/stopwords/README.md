# 停用词策略

基础停用词在运行时从 MIT 许可的 `stopwordsiso` 加载。默认语言为：

```
zh, en, ja, ko, fr, de, es, ru
```

来源：

- https://pypi.org/project/stopwordsiso/
- https://github.com/bact/stopwords-iso
- https://github.com/stopwords-iso/stopwords-zh

项目仅自行维护两份小型补充资源：

- `custom_stopwords.txt`：B站场景的结构词和宣传性噪声词；
- `allowlist.txt`：AI、MV、Vlog、4K 等必须保留的短词。

保留词优先级高于基础词表和自定义停用词。英文字母统一使用 Unicode
NFKC 和 `casefold()` 进行大小写无关匹配。

## 迭代规则

1. 修改前先查看多次抓取产生的排行榜 CSV 和词云。
2. 只有跨多个榜单快照持续高频、主题价值较低的词才能加入停用词。
3. 误删的短词优先加入保留词，而不是放宽全部过滤规则。
4. Emoji、标点和其他符号不属于词元，不单独统计，也不进入词云。
5. 每次增删都应在版本控制提交说明中写明词频证据。
