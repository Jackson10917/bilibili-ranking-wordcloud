import csv

from bilibili_ranker.storage import RANKING_CSV_COLUMNS, write_records_csv


def test_write_records_csv_uses_documented_headers(tmp_path, make_record) -> None:
    destination = tmp_path / "ranking.csv"
    record = make_record("BV1CSV", "CSV 测试")

    write_records_csv(destination, [record])

    with destination.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        rows = list(reader)
        fieldnames = reader.fieldnames

    assert fieldnames == [header for _, header in RANKING_CSV_COLUMNS]
    assert rows[0]["BV号"] == "BV1CSV"
    assert rows[0]["视频标题"] == "CSV 测试"
    assert rows[0]["UP主"] == "测试UP"
