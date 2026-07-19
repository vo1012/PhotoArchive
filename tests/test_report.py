"""build_model_from_rows() / build_model_from_analyze_stats() / _cluster_near_dup() /
_parse_bydate_segment() -- pure aggregation logic in report.py, no filesystem/HTML rendering.

REVIEW-HANDOFF.md, раунд 3 [ЗАМЕЧАНИЕ]: report.py не имел ни одного автотеста. Первый тест
ниже -- прямой regression на найденный тем же раундом [БЛОКЕР] (Tier D задваивался в Tier A)."""
from collections import Counter

import pytest

import report as r


def _appended_row(dest, source=None):
    return {"timestamp": "2026-01-01 00:00:00", "source": source or dest, "dest": dest,
            "reason": "appended_new", "flags": ""}


def test_tier_d_not_folded_into_tier_a():
    """REVIEW-HANDOFF.md, раунд 3 [БЛОКЕР]: Tier D (undated_media.csv) не в dates_review.csv
    (date_value=None там гейтится отдельно) -- без undated_media.csv report.py не мог отличить
    "нет сигнала о дате вообще" от "точная EXIF-дата", оба одинаково отсутствовали в
    dates_review.csv. 3 датированных (B/B/C) + 7 недатированных -> раньше давало A=7 (все
    undated посчитаны как EXIF-точные), правильно -- A=0."""
    dated = [_appended_row(rf"C:\T\dst\ByDate\2026\2026-07 [PhotoArchive]\b{i}.jpg")
             for i in range(3)]
    undated = [_appended_row(rf"C:\T\dst\ByDate\0000-undated\u{i}.jpg") for i in range(7)]
    data = {
        "appended": dated + undated,
        "dates_review": [{"tier": "B", "dest": "x", "source": "x"},
                          {"tier": "B", "dest": "x", "source": "x"},
                          {"tier": "C", "dest": "x", "source": "x"}],
        "undated_media": [{"source": row["source"], "dest": row["dest"]} for row in undated],
    }
    model = r.build_model_from_rows(data)
    assert model["tier_counts"] == Counter({"B": 2, "C": 1, "D": 7, "A": 0})


def test_tier_d_missing_undated_media_degrades_to_old_buggy_behavior():
    """Same fixture, but WITHOUT undated_media.csv (e.g. an older archive whose logs predate
    this log) -- documents the known, accepted degradation (report.py has no other signal),
    not a silent new bug: falls back to the pre-fix counting, undated ends up in "A"."""
    dated = [_appended_row(rf"C:\T\dst\ByDate\2026\2026-07 [PhotoArchive]\b{i}.jpg")
             for i in range(3)]
    undated = [_appended_row(rf"C:\T\dst\ByDate\0000-undated\u{i}.jpg") for i in range(7)]
    data = {
        "appended": dated + undated,
        "dates_review": [{"tier": "B", "dest": "x", "source": "x"}],
    }
    model = r.build_model_from_rows(data)
    assert model["tier_counts"]["D"] == 0
    assert model["tier_counts"]["A"] == 9  # 10 total image rows - 1 already-counted "B"


def test_build_model_from_rows_empty_data_has_no_crash_and_hides_categories():
    model = r.build_model_from_rows({})
    assert model["total_media"] == 0
    assert model["oldest"] is None
    assert model["near_dup_clusters"] == []
    assert model["tier_counts"]["A"] == 0


@pytest.mark.parametrize("dest,expected", [
    (r"C:\T\dst\ByDate\2026\2026-07-18 Москва [PhotoArchive]\file.jpg", (2026, 7, 18, "Москва")),
    (r"C:\T\dst\ByDate\2026\2026-07 [PhotoArchive]\file.jpg", (2026, 7, None, None)),
    (r"C:\T\dst\ByDate\2026\file.jpg", (2026, None, None, None)),  # granularity=year
    (r"C:\T\dst\ByDate\file.jpg", None),  # granularity=flat -- год не восстановить
    (r"C:\T\dst\ByDate\0000-undated\sub\file.jpg", None),
    (r"C:\T\dst\ByDate\2026\2026-00 month-unknown [PhotoArchive]\file.jpg", (2026, None, None, None)),
    (r"C:\T\dst\RAW\ByDate\2026\2026-07-18 Москва [PhotoArchive]\file.cr2", (2026, 7, 18, "Москва")),
    (r"C:\T\dst\Albums\Отпуск 2019\file.jpg", None),  # не под ByDate вовсе
])
def test_parse_bydate_segment(dest, expected):
    assert r._parse_bydate_segment(dest) == expected


def test_cluster_near_dup_groups_transitively():
    # a-b and b-c share "b" -> one cluster of 3, not two separate pairs (union-find transitivity)
    rows = [
        {"dest": "a", "matched_dest": "b"},
        {"dest": "c", "matched_dest": "b"},
        {"dest": "x", "matched_dest": "y"},  # unrelated second cluster
    ]
    clusters = r._cluster_near_dup(rows)
    sizes = sorted(len(c) for c in clusters)
    assert sizes == [2, 3]


def test_cluster_near_dup_empty():
    assert r._cluster_near_dup([]) == []


class _FakeAnalyzeStats:
    """Минимальная замена AnalyzeStats -- только поля, которые реально читает
    build_model_from_analyze_stats(), без зависимости от photosort_win.py."""
    def __init__(self):
        self.n_images = 3
        self.n_raw = 0
        self.n_videos = 0
        self.oldest_date = None
        self.oldest_display = None
        self.n_near_dupes = 1
        self.predicted_unique_count = 3
        self.n_exact_dupes = 0
        self.n_broken_or_zero = 0
        self.predicted_unique_bytes = 12345
        self.dates_by_year = Counter({2026: 3})
        self.dates_by_year_month = Counter({"2026-07": 3})
        self.tier_counts = Counter({"C": 2, "D": 1})
        self.near_dup_edges = []


def test_build_model_from_analyze_stats_tier_counts_not_affected_by_the_target_level_bug():
    """analyze/analyze-full/analyze-quick incrémente stats.tier_counts безусловно
    (photosort_win.py:run_analyze) -- в отличие от TARGET/WORKDIR-уровня (build_model_from_rows),
    здесь Tier D никогда не терялся, регрессии тем же багом быть не может (REVIEW-HANDOFF.md,
    раунд 3 явно проверил и подтвердил асимметрию)."""
    model = r.build_model_from_analyze_stats(_FakeAnalyzeStats())
    assert model["tier_counts"] == Counter({"C": 2, "D": 1})
    assert model["counts"] == Counter({"image": 3, "raw": 0, "video": 0})
    assert model["decisions"]["near_dup"] == 1
    assert model["decisions"]["appended"] == 2
