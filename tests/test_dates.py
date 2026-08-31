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


def test_folder_cluster_median_prewar_film_scan_no_crash():
    # Реальный краш 2026-08-31 (сканирование сетевого диска): _MIN_YEAR=1900 допускает
    # довоенные плёночные сканы, и одна такая Tier A/B-дата в кластере роняла ВЕСЬ прогон --
    # прежний код звал d.timestamp(), а на Windows mktime() бросает OSError [Errno 22] для
    # наивных дат до 1970. На Linux .timestamp() отдаёт отрицательное число, поэтому тесты
    # на VPS этого не ловили. Фикс: медиана через фиксированную эпоху, без .timestamp().
    dates = [datetime(1932, 1, 1), datetime(1932, 3, 1), datetime(1932, 6, 1)]
    assert m.folder_cluster_median(dates) == datetime(1932, 3, 1)


def test_folder_cluster_median_extreme_year_arithmetic_robust():
    # Крайняя проверка арифметики медианы: datetime(1, 1, 1) в кластере. Прежний код падал
    # ЗДЕСЬ и на Linux: datetime(1,1,1).timestamp() -> ValueError "year 0 is out of range".
    # Кросс-платформенный red-before-green. (С гейтом года в parse_exif_date такой год больше
    # не приходит с EXIF-пути, но folder_cluster_median обязана быть устойчивой сама по себе.)
    dates = [datetime(1, 1, 1), datetime(2020, 1, 1), datetime(2020, 1, 1)]
    assert m.folder_cluster_median(dates) == datetime(2020, 1, 1)


@pytest.mark.parametrize("s", [
    "0001:01:01 00:00:00",   # севшая батарейка -> заводская дата
    "1850:06:15 12:00:00",   # год ниже _MIN_YEAR
    f"{datetime.now().year + 5}:01:01 00:00:00",  # год из будущего (сбитые часы вперёд)
])
def test_parse_exif_date_rejects_implausible_year(s):
    # REVIEW-HANDOFF.md Раунд 175-1: неправдоподобный EXIF-год не должен становиться Tier A.
    # Прежний код принимал любой год 1..9999 (datetime() конструируется) -> уверенная дата,
    # которая на Windows доходила до strftime() (< 1900 отклоняется CRT).
    assert m.parse_exif_date(s) is None


@pytest.mark.parametrize("s,expected", [
    ("2015:06:15 12:30:00", datetime(2015, 6, 15, 12, 30, 0)),
    ("1900:01:01 00:00:00", datetime(1900, 1, 1)),   # ровно на нижней границе
])
def test_parse_exif_date_accepts_plausible_year(s, expected):
    assert m.parse_exif_date(s) == expected


def test_best_exif_datetime_skips_implausible_key_falls_to_next():
    # DateTimeOriginal с невозможным годом -> берётся следующий валидный ключ, а не он.
    # (Гейт ловит только год < 1900 / из будущего -- "сбитые, но правдоподобные" часы,
    #  напр. сброс на 2000, ни один гейт без кросс-сверки EXIF не отличит.)
    tags = {"DateTimeOriginal": "1850:01:01 00:00:00", "CreateDate": "2015:06:15 12:00:00"}
    dt, key = m.best_exif_datetime(tags)
    assert (dt, key) == (datetime(2015, 6, 15, 12, 0, 0), "CreateDate")


def test_safe_dt_from_mtime_valid_value():
    assert m._safe_dt_from_mtime(1609459200.0) == datetime.fromtimestamp(1609459200.0)


def test_safe_dt_from_mtime_out_of_range_returns_none():
    # Абсурдный mtime (битая ФС / SMB-шара) -> None, а не OverflowError/OSError наружу.
    assert m._safe_dt_from_mtime(1e30) is None
    assert m._safe_dt_from_mtime(-1e30) is None
    assert m._safe_dt_from_mtime(float("nan")) is None


def test_safe_dt_from_mtime_prewar_recovered_via_epoch():
    # mtime до 1970 (~ середина 1932): на Windows fromtimestamp() бросает OSError (реальный
    # краш resolve_date:6287 у пользователя 2026-08-31) -> откат на фиксированную эпоху
    # восстанавливает дату НА ОБЕИХ платформах (файл не уходит зря в undated). Середина года
    # -- чтобы сдвиг локального TZ не перекинул год.
    result = m._safe_dt_from_mtime(-1184889600.0)
    assert result is not None and result.year == 1932


def test_safe_dt_from_mtime_pre_1900_returns_none():
    # Год < _MIN_YEAR: mtime почти наверняка сбой часов ФС, и Windows CRT strftime такие
    # годы отклоняет -> None (файл в Tier D), не дата.
    assert m._safe_dt_from_mtime(-2_500_000_000.0) is None  # ~1890


def test_safe_dt_from_mtime_far_future_returns_none():
    # Раунд 177-1: гейт года ДВУСТОРОННИЙ (_valid). Мусорный большой mtime (переполнение
    # FILETIME/наносекунд) -- эпоха-фолбэк строит из него дату года ~3000-9999; такой файл
    # должен уйти в Tier D, а не осесть в ByDate/9511/. (До фикса 177-1 -> datetime(3015,...).)
    assert m._safe_dt_from_mtime(33_000_000_000.0) is None   # ~год 3015
    assert m._safe_dt_from_mtime(238_000_000_000.0) is None  # ~год 9511


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

    def test_use_folder_name_date_false_skips_folder_year_falls_to_mtime(self):
        # Живой репорт пользователя (2026-08-01, "Паспорт архива"): use_folder_name_date=False
        # -- run_passport()'s self_scan=True использует его, потому что на TARGET сама папка
        # "2019 [PhotoArchive]" -- это разметка, которую программа сгенерировала на прошлом
        # прогоне (Tier C/D), а не независимое доказательство. Без сигнала из имени папки файл
        # должен упасть дальше по цепочке (mtime), а не остаться без даты вовсе.
        ctx = m.DateContext()
        mtime = datetime(2019, 6, 1).timestamp()
        dt, tier, confidence, evidence, precision = m.resolve_date(
            ctx, "Поездка 2019/no_date_in_name.jpg", mtime=mtime, use_folder_name_date=False)
        assert dt == datetime.fromtimestamp(mtime)
        assert (tier, confidence, evidence, precision) == ("C", "low", "mtime", "day")

    def test_use_folder_name_date_true_is_still_the_default(self):
        # Regression guard: existing call sites (real SOURCE-side build/analyze) must keep
        # reading the folder-year signal unless they opt out explicitly.
        ctx = m.DateContext()
        dt, tier, confidence, evidence, precision = m.resolve_date(
            ctx, "Поездка 2019/no_date_in_name.jpg", mtime=1000.0)
        assert (tier, evidence, precision) == ("B", "folder_name_year", "year")

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

    def test_bogus_mtime_falls_to_tier_d_instead_of_crashing(self):
        # Битый/мусорный mtime (SMB-шара, повреждённая ФС) без других сигналов даты: файл
        # уходит в Tier D ("дата не определена"), а не роняет весь прогон. Прежний код звал
        # datetime.fromtimestamp(1e30) -> OverflowError на всех платформах. Red-before-green.
        ctx = m.DateContext()
        dt, tier, confidence, evidence, precision = m.resolve_date(
            ctx, "Сетевой/no_signal.jpg", mtime=1e30)
        assert (dt, tier, confidence, evidence, precision) == (None, "D", "none", "no_signal", None)

    def test_folder_cluster_inference_with_prewar_neighbor(self):
        # Точный путь краша 2026-08-31: resolve_date() -> folder_cluster_median() на кластере
        # с довоенной Tier A-датой. На Windows прежний код падал здесь OSError [Errno 22].
        ctx = m.DateContext()
        exif_dt = datetime(1935, 7, 1, 12, 0, 0)
        m.resolve_date(ctx, "Плёнки/scan01.jpg", mtime=1000.0, exif_dt=exif_dt, exif_source="exif")
        dt, tier, confidence, evidence, precision = m.resolve_date(
            ctx, "Плёнки/scan02_no_exif.jpg", mtime=1001.0)
        assert dt == exif_dt
        assert (tier, evidence) == ("C", "inferred_from_folder_cluster")

    def test_implausible_exif_dt_from_stale_cache_falls_through(self):
        # Раунд 175-1, второй барьер: exif_dt может прийти из archive_cache, засеянного ДО
        # фикса parse_exif_date (неправдоподобная дата уже строкой в БД). resolve_date()
        # обязана отбросить такой год, а не отдать его как Tier A -> strftime() на Windows.
        ctx = m.DateContext()
        dt, tier, *_ = m.resolve_date(
            ctx, "Плёнки/IMG_20150615.jpg", mtime=1000.0,
            exif_dt=datetime(1850, 1, 1), exif_source="cached")
        assert dt == datetime(2015, 6, 15)          # упал на дату из имени файла (Tier B)
        assert tier == "B"

    def test_bogus_mtime_does_not_poison_copy_artifact_detector(self):
        # REVIEW-HANDOFF.md Раунд 175-2: один файл с мусорным mtime в папке-копи-событии
        # НЕ должен глушить mtime_is_copy_artifact() для остальных её файлов. Прежний код
        # аппендил сырой 1e30 в ctx.dir_mtimes -> span ~1e30 >> окна -> копи-событие не
        # детектится -> сиблинги ошибочно получают Tier C mtime вместо Tier D.
        ctx = m.DateContext()
        base = 9_000_000.0
        m.resolve_date(ctx, "Копия/bad.jpg", mtime=1e30)          # мусорный mtime
        m.resolve_date(ctx, "Копия/a.jpg", mtime=base)
        m.resolve_date(ctx, "Копия/b.jpg", mtime=base + 1)
        dt, tier, *_ = m.resolve_date(ctx, "Копия/c.jpg", mtime=base + 2)
        assert (dt, tier) == (None, "D")
        assert 1e30 not in ctx.dir_mtimes["Копия"]

    def test_valid_mtime_still_recorded_for_copy_artifact_detection(self):
        # Регресс-страж к 175-2: нормальный mtime по-прежнему копится в dir_mtimes.
        ctx = m.DateContext()
        base = 7_000_000.0
        m.resolve_date(ctx, "Событие/a.jpg", mtime=base, exif_dt=datetime(2015, 1, 1), exif_source="e")
        assert ctx.dir_mtimes["Событие"] == [base]
