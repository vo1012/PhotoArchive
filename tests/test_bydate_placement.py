"""build_bydate_dest_dir() -- pure string/path building, no filesystem I/O.

2026-09-15 (обсуждение с пользователем, реальный архив, поездка с заездом в несколько
городов за один месяц): место (город) раньше дописывалось в ИМЯ day/month-папки
("2013-06 Москва [PhotoArchive]") -- под годом появлялся десяток почти одинаковых
папок-соседей. Теперь place -- отдельный подуровень ВНУТРИ ровно одной day/month-папки на
период; файлы без места лежат прямо в ней, россыпью. См. build_bydate_dest_dir().

REVIEW-HANDOFF.md, Раунд 231-1: build_bydate_dest_dir() строит путь через os.path.join()
(`\\` на Windows, `/` на POSIX) -- ожидаемые строки здесь обязаны собираться тем же
os.path.join(), а не литералом с `\\`, иначе тесты красные на ubuntu-latest (там реально
гоняется unit-tests, см. .github/workflows/ci.yml)."""
import os
from datetime import datetime

import pytest

import photosort_win as m


@pytest.mark.parametrize("granularity,period_folder", [
    ("month", "2013-06 [PhotoArchive]"),
    ("day", "2013-06-18 [PhotoArchive]"),
])
def test_no_place_lands_directly_in_period_container(granularity, period_folder):
    date_value = datetime(2013, 6, 18, 13, 10, 40)
    dest = m.build_bydate_dest_dir("ByDate", date_value, "day", None, granularity)
    assert dest == os.path.join("ByDate", "2013", period_folder)


@pytest.mark.parametrize("granularity,period_folder", [
    ("month", "2013-06 [PhotoArchive]"),
    ("day", "2013-06-18 [PhotoArchive]"),
])
def test_place_becomes_subfolder_not_part_of_period_name(granularity, period_folder):
    date_value = datetime(2013, 6, 18, 13, 10, 40)
    dest = m.build_bydate_dest_dir("ByDate", date_value, "day", "Москва", granularity)
    assert dest == os.path.join("ByDate", "2013", period_folder, "Москва")


def test_multiple_places_same_month_share_one_container():
    # Живой случай: поездка с заездом в несколько городов за один месяц -- ровно ОДНА
    # "2013-06 [PhotoArchive]" на весь месяц, разные города -- разные подпапки внутри неё,
    # а не десяток "2013-06 <Город> [PhotoArchive]" папок-соседей под годом.
    d = datetime(2013, 6, 18)
    moscow = m.build_bydate_dest_dir("ByDate", d, "day", "Москва", "month")
    kazan = m.build_bydate_dest_dir("ByDate", d, "day", "Казань", "month")
    no_place = m.build_bydate_dest_dir("ByDate", d, "day", None, "month")
    common_container = os.path.join("ByDate", "2013", "2013-06 [PhotoArchive]")
    assert moscow == os.path.join(common_container, "Москва")
    assert kazan == os.path.join(common_container, "Казань")
    assert no_place == common_container


def test_place_is_sanitized_before_use_as_path_segment():
    # sanitize_windows_component() чистит Windows-запрещённые символы -- место идёт как
    # ОТДЕЛЬНЫЙ сегмент пути теперь, а не часть строки внутри имени папки, но чистка всё
    # равно обязана применяться (запрещённые символы одинаково недопустимы что в имени
    # файла/папки, что в отдельном сегменте).
    d = datetime(2020, 1, 1)
    dest = m.build_bydate_dest_dir("ByDate", d, "day", "Sankt-Peterburg:2", "month")
    place_segment = dest.rsplit(os.sep, 1)[-1]
    assert ":" not in place_segment


def test_year_precision_ignores_place_regardless_of_granularity():
    # precision=="year" всегда даёт month-unknown-корзину; место не сужает и не добавляется --
    # тот же выбор, что был у прежнего дизайна (RULES.md: "при year/flat место не пишется").
    d = datetime(2013, 1, 1)
    dest = m.build_bydate_dest_dir("ByDate", d, "year", "Москва", "month")
    assert dest == os.path.join("ByDate", "2013", "2013-00 month-unknown [PhotoArchive]")


@pytest.mark.parametrize("granularity", ["year", "flat"])
def test_year_and_flat_granularity_never_add_place_subfolder(granularity):
    d = datetime(2013, 6, 18)
    dest = m.build_bydate_dest_dir("ByDate", d, "day", "Москва", granularity)
    assert "Москва" not in dest
