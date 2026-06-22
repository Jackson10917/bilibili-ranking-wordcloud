from bilibili_ranker.cleaner import TitleAnalyzer, deduplicate_records
from bilibili_ranker.stopwords import StopwordPolicy


def test_deduplicate_records_keeps_highest_ranked_item(make_record) -> None:
    first = make_record("BV1ABC", "第一条", 1)
    duplicate = make_record("bv1abc", "重复条目", 2)
    other = make_record("BV1XYZ", "另一条", 3)

    accepted, issues = deduplicate_records([first, duplicate, other])

    assert accepted == [first, other]
    assert len(issues) == 1
    assert issues[0].rank == 2
    assert "排名 1" in issues[0].reason


def test_title_analyzer_applies_allowlist_stopwords_and_symbol_filter(make_record) -> None:
    policy = StopwordPolicy(
        languages=(),
        stopwords=frozenset({"python"}),
        allowlist=frozenset({"ai"}),
    )
    analyzer = TitleAnalyzer(policy, minimum_token_length=2)
    record = make_record("BV1TOKENS", "AI Python 数据 数据 🚀")

    result = analyzer.analyze([record])

    assert result.processed_title_count == 1
    assert result.word_frequencies["ai"] == 1
    assert result.word_frequencies["数据"] == 2
    assert "python" not in result.word_frequencies
