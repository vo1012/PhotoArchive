"""date_from_filename() / date_from_folder_name() / resolve_date() / _valid() /
mtime_is_copy_artifact() / folder_cluster_median() -- pure date-inference logic, no filesystem
or EXIF I/O (resolve_date takes exif_dt already extracted, doesn't read files itself)."""
from datetime import datetime

import pytest

import photosort_win as m


@pytest.mark.parametrize("y,mo,d,expected", [
    (2020, 1, 1, True),
    (1899, 1, 1, False),  # below _MIN_YEAR
    (1900, 1, 1, True),  # exactly at _MIN_YEAR
    (datetime.now().year + 1, 1, 1, False),  # future year rejected
    (2021, 2, 30, False),  # invalid day for the month
])
def test_valid(y, mo, d, expected):
    assert m._valid(y, mo, d) is expected


@pytest.mark.parametrize("name,expected_date", [
    ("20220315_120000.jpg", datetime(2022, 3, 15)),
    ("IMG_20220315.jpg", datetime(2022, 3, 15)),
    ("IMG-20220315-WA0001.jpg", datetime(2022, 3, 15)),
    ("Screenshot_2022-03-15.png", datetime(2022, 3, 15)),
    ("PXL_20220315.jpg", datetime(2022, 3, 15)),
    ("VID_20220315.mp4", datetime(2022, 3, 15)),
    ("2022-03-15 party.jpg", datetime(2022, 3, 15)),
    ("2022_03.jpg", datetime(2022, 3, 1)),  # year_month only -> day defaults to 1
    ("IMG_1234.jpg", None),  # a plain counter must NOT be mistaken for a date
    ("random_photo.jpg", None),
    ("20221345_120000.jpg", None),  # month=13 is invalid -> no match, not a crash
])
def test_date_from_filename(name, expected_date):
    dt, ev = m.date_from_filename(name)
    if expected_date is None:
        assert dt is None
        assert ev is None
    else:
        assert dt == expected_date
        assert ev == "filename_pattern"


@pytest.mark.parametrize("rel_path,expected_date", [
    ("Отпуск 2015/photo.jpg", datetime(2015, 1, 1)),
    ("2015-08-20/Отпуск/photo.jpg", datetime(2015, 1, 1)),  # nearest ancestor wins (reversed scan)
    ("no_year_here/photo.jpg", None),
    ("year 1899 too old/photo.jpg", None),  # below _MIN_YEAR, rejected by _valid()
])
def test_date_from_folder_name(rel_path, expected_date):
    dt, ev = m.date_from_folder_name(rel_path)
    if expected_date is None:
        assert dt is None
        assert ev is None
    else:
        assert dt == expected_date
        assert ev == "folder_name_year"


def test_mtime_is_copy_artifact_needs_at_least_three_samples():
    assert m.mtime_is_copy_artifact([100.0, 100.1]) is False


def test_mtime_is_copy_artifact_narrow_window_flagged():
    assert m.mtime_is_copy_artifact([100.0, 101.0, 102.0], window_seconds=5) is True


def test_mtime_is_copy_artifact_wide_window_not_flagged():
    assert m.mtime_is_copy_artifact([100.0, 500.0, 900.0], window_seconds=5) is False


def test_folder_cluster_median_empty():
    assert m.folder_cluster_median([]) is None


def test_folder_cluster_median_odd_count():
    dates = [datetime(2020, 1, 1), datetime(2020, 1, 3), datetime(2020, 1, 5)]
    assert m.folder_cluster_median(dates) == datetime(2020, 1, 3)


class TestResolveDate:
    def test_exif_wins_as_tier_a(self):
        ctx = m.DateContext()
        exif_dt = datetime(2022, 5, 1, 10, 0, 0)
        dt, tier, confidence, evidence, precision = m.resolve_date(
            ctx, "Альбом/photo.jpg", mtime=1000.0, exif_dt=exif_dt, exif_source="exif_datetimeoriginal")
        assert (dt, tier, confidence, evidence, precision) == (
            exif_dt, "A", "high", "exif_datetimeoriginal", "day")

    def test_filename_pattern_is_tier_b_day_precision(self):
        ctx = m.DateContext()
        dt, tier, confidence, evidence, precision = m.resolve_date(
            ctx, "Альбом/IMG_20220315.jpg", mtime=1000.0)
        assert dt == datetime(2022, 3, 15)
        assert (tier, confidence, evidence, precision) == ("B", "medium", "filename_pattern", "day")

    def test_folder_year_is_tier_b_year_precision(self):
        # precision='year' (only the year is reliable) routes to the month-unknown bucket --
        # distinct from the day-precision filename-pattern case above.
        ctx = m.DateContext()
        dt, tier, confidence, evidence, precision = m.resolve_date(
            ctx, "Поездка 2019/no_date_in_name.jpg", mtime=1000.0)
        assert dt == datetime(2019, 1, 1)
        assert (tier, confidence, evidence, precision) == ("B", "medium", "folder_name_year", "year")

    def test_folder_cluster_inference_from_earlier_sibling(self):
        ctx = m.DateContext()
        exif_dt = datetime(2022, 5, 1, 10, 0, 0)
        m.resolve_date(ctx, "Альбом/a.jpg", mtime=1000.0, exif_dt=exif_dt, exif_source="exif")
        # Second file in the same folder has no reliable signal of its own -- borrows the
        # tier A/B neighbor's date via folder-cluster median.
        dt, tier, confidence, evidence, precision = m.resolve_date(
            ctx, "Альбом/no_signal.jpg", mtime=1001.0)
        assert dt == exif_dt
        assert (tier, confidence, evidence, precision) == (
            "C", "low", "inferred_from_folder_cluster", "day")

    def test_mtime_fallback_when_not_a_copy_artifact(self):
        ctx = m.DateContext()
        mtime = datetime(2018, 6, 1).timestamp()
        dt, tier, confidence, evidence, precision = m.resolve_date(
            ctx, "Random/no_signal.jpg", mtime=mtime)
        assert dt == datetime.fromtimestamp(mtime)
        assert (tier, confidence, evidence, precision) == ("C", "low", "mtime", "day")

    def test_no_signal_at_all_when_mtime_is_a_copy_artifact(self):
        ctx = m.DateContext()
        base = 5_000_000.0
        # Three siblings copied in the same instant -- mtime_is_copy_artifact() flags the
        # narrow window as unreliable, so the third file gets no date at all (tier D).
        m.resolve_date(ctx, "Дамп/a.jpg", mtime=base)
        m.resolve_date(ctx, "Дамп/b.jpg", mtime=base + 1)
        dt, tier, confidence, evidence, precision = m.resolve_date(ctx, "Дамп/c.jpg", mtime=base + 2)
        assert (dt, tier, confidence, evidence, precision) == (None, "D", "none", "no_signal", None)
