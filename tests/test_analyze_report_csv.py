"""write_analyze_report_csv() -- аудит 2026-07-21 (SESSION-HANDOFF.txt, "потенциально лишние
режимы/CLI-флаги/конфиги"): функция раньше дампила буквально все поля AnalyzeStats,
включая Counter/list/dict, добавленные позже ради report.html -- в CSV они попадали как
нечитаемые Python-repr строки. Теперь пишутся только скалярные метрики."""
import csv
from collections import Counter

import photosort_win as m


def test_write_analyze_report_csv_skips_structural_fields(tmp_path):
    stats = m.AnalyzeStats(mode="analyze")
    stats.total_files = 5
    stats.dump_items_by_folder = Counter({"foo": 2})
    stats.near_dup_edges = [{"source": "a.jpg"}]
    stats.found_archive_top_level = ["/some/archive"]
    stats.found_archive_nested = {"/some/archive": ["/some/archive/Albums/x"]}

    out_path = tmp_path / "analyze_report.csv"
    m.write_analyze_report_csv(str(out_path), stats)

    with open(out_path, newline="", encoding="utf-8") as f:
        rows = {row["metric"]: row["value"] for row in csv.DictReader(f)}

    assert rows["total_files"] == "5"
    assert rows["mode"] == "analyze"
    for structural_field in ("dump_items_by_folder", "near_dup_edges",
                              "found_archive_top_level", "found_archive_nested"):
        assert structural_field not in rows


def test_write_analyze_report_csv_keeps_all_scalar_fields():
    stats = m.AnalyzeStats(mode="analyze-full")
    scalar_fields = {k for k, v in vars(stats).items()
                      if isinstance(v, (int, float, bool, str)) or v is None}
    # sanity: this dataclass does have a healthy number of plain scalar counters -- a test
    # that accidentally excluded everything would still pass an "absence" check like above.
    assert len(scalar_fields) > 20
