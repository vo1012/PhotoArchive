"""build_model_from_rows() / build_model_from_analyze_stats() / _cluster_near_dup() /
_parse_bydate_segment() -- pure aggregation logic in report.py, no filesystem/HTML rendering.

REVIEW-HANDOFF.md, раунд 3 [ЗАМЕЧАНИЕ]: report.py не имел ни одного автотеста. Первый тест
ниже -- прямой regression на найденный тем же раундом [БЛОКЕР] (Tier D задваивался в Tier A)."""
import re
from collections import Counter

import pytest

import report as r


def _appended_row(dest, source=None):
    return {"timestamp": "2026-01-01 00:00:00", "source": source or dest, "dest": dest,
            "reason": "appended_new", "flags": ""}


def test_svg_pie_legend_shows_less_than_one_percent_instead_of_zero():
    """report.py:646 (до фикса) -- f"{frac*100:.0f}%" округлял малые ненулевые доли (напр.
    29/19371 ~= 0.15%) до "0%", выглядящего как "файлов нет", хотя счётчик ненулевой -- живая
    находка пользователя на реальном отчёте (Точная 98%/Высокая 0%/Оценочная 1%/Низкая 0%,
    хотя Высокая и Низкая -- 29 и 4 файла соответственно)."""
    _, legend = r._svg_pie([
        ("Точная", 19076, "#000"), ("Высокая", 29, "#000"),
        ("Оценочная", 262, "#000"), ("Низкая", 4, "#000"),
    ])
    assert "Точная — 19076 файлов (98%)" in legend
    assert "Высокая — 29 файлов (<1%)" in legend
    assert "Оценочная — 262 файла (1%)" in legend
    assert "Низкая — 4 файла (<1%)" in legend


def test_svg_pie_legend_normal_rounding_unaffected():
    _, legend = r._svg_pie([("A", 1, "#000"), ("B", 1, "#000")])
    assert "A — 1 файл (50%)" in legend
    assert "B — 1 файл (50%)" in legend


def test_type_breakdown_caption_only_nonzero_categories():
    caption = r._type_breakdown_caption(Counter({"image": 10, "raw": 0, "video": 3}), "Дубли")
    assert caption == "Дубли, в т.ч.: фото — 10 файлов, видео — 3 файла"


def test_type_breakdown_caption_empty_when_all_zero():
    assert r._type_breakdown_caption(Counter(), "Дубли") == ""
    assert r._type_breakdown_caption(Counter({"image": 0}), "Дубли") == ""


def test_sheet2_shows_exact_dup_type_breakdown_caption():
    """2026-07-26, по просьбе пользователя: "Дубли" на диаграмме "Итог решений
    программы" -- caption под диаграммой, classification matched_with по расширению."""
    data = {
        "appended": [_appended_row(r"C:\T\dst\Albums\A\a.jpg")],
        "skipped": [
            {"source": "s1", "matched_with": r"C:\T\dst\Albums\A\a.jpg", "reason": "already_present"},
            {"source": "s2", "matched_with": r"C:\T\dst\Albums\A\b.cr2", "reason": "already_present"},
            {"source": "s3", "matched_with": r"C:\T\dst\Albums\A\c.mp4", "reason": "already_present"},
        ],
    }
    model = r.build_model_from_rows(data)
    assert model["skipped_present_by_type"] == Counter({"image": 1, "raw": 1, "video": 1})
    html_out = r._render_sheet2(model)
    assert "Дубли, в т.ч.: фото — 1 файл, RAW — 1 файл, видео — 1 файл" in html_out


def test_year_hbar_chart_collapses_gap_years_into_one_row():
    """SESSION-HANDOFF.txt п.3 (2026-08-05, боевой прогон): раньше каждый год пробела получал
    отдельную нулевую строку (2026-07-31, пункт B.7) -- пользователь решил, что раз у каждого
    построенного года и так есть подпись, сама последовательность ("1973" -> "2003") уже
    показывает разрыв. Диапазон 1973-2019 = 47 лет, меньше _YEAR_HBAR_MAX_SPAN -- непрерывный
    пробег 1974-2002 схлопывается в ОДНУ строку-разрыв, не в 29 нулевых строк."""
    years = Counter({1973: 1, **{y: 10 for y in range(2003, 2020)}})
    svg = r._svg_year_hbar_chart(years)
    assert ">1973<" in svg
    for y in range(2003, 2020):
        assert f">{y}<" in svg
    for y in range(1974, 2003):
        assert f">{y}<" not in svg  # больше не подписан индивидуально
    assert svg.count(">0 файлов<") == 0  # ни одной построчной нулевой строки
    assert "1974–2002: нет фото" in svg
    assert svg.count("нет фото") == 1  # ровно одна строка-разрыв, не 29


def test_year_hbar_chart_shows_every_year_when_no_gaps():
    years = Counter({2020: 5, 2021: 3, 2022: 9})
    svg = r._svg_year_hbar_chart(years)
    assert svg.count("<rect") == 3  # ни одного нулевого года -- ни одна строка без бара
    for y in (2020, 2021, 2022):
        assert f">{y}<" in svg


def test_year_hbar_chart_fifty_years_still_renders_every_row():
    """Проверка на масштаб: 50 подряд идущих лет (span=50, меньше _YEAR_HBAR_MAX_SPAN) --
    ни один год не пропадает, каждый получает свою строку (в отличие от вертикальной формы,
    горизонтальной не нужно прореживать подписи)."""
    years = Counter({y: 1 for y in range(1970, 2020)})  # 50 разных лет
    svg = r._svg_year_hbar_chart(years)
    assert svg.count("<rect") == 50
    assert ">1970<" in svg
    assert ">2019<" in svg


def test_year_hbar_chart_extreme_span_does_not_zero_fill():
    """_YEAR_HBAR_MAX_SPAN: одна битая EXIF-дата (1902) среди прочих 2020-х не должна раздуть
    график на сотни пустых строк ради одного выброса -- выше порога заполнение пропусков
    отключается целиком, показываются только годы, у которых реально есть файлы."""
    years = Counter({1902: 1, 2024: 50, 2025: 80})
    svg = r._svg_year_hbar_chart(years)
    assert svg.count("<rect") == 3  # только три реальных года, не 124-строчный график
    assert ">1902<" in svg and ">2024<" in svg and ">2025<" in svg
    assert ">1950<" not in svg  # пропуски НЕ заполнены выше порога


def test_year_hbar_chart_single_year_gap_uses_singular_wording():
    years = Counter({2020: 5, 2022: 3})  # 2021 -- пробел ровно из одного года
    svg = r._svg_year_hbar_chart(years)
    assert "2021: нет фото" in svg
    assert "2021–2021" not in svg  # не диапазон из одного и того же года


def test_year_hbar_chart_multiple_separate_gaps_each_get_own_row():
    years = Counter({2000: 1, 2010: 1, 2020: 1})  # два отдельных пробега: 2001-2009, 2011-2019
    svg = r._svg_year_hbar_chart(years)
    assert "2001–2009: нет фото" in svg
    assert "2011–2019: нет фото" in svg
    assert svg.count("нет фото") == 2


def test_year_gap_ranges_finds_all_contiguous_zero_runs():
    # SESSION-HANDOFF.txt п.3: в отличие от _find_year_gap() (только САМЫЙ заметный провал),
    # этот хелпер находит ВСЕ непрерывные нулевые пробеги, для строк-разрывов графика.
    years = Counter({2000: 1, 2010: 1, 2020: 1})
    assert r._year_gap_ranges(2000, 2020, years) == [(2001, 2009), (2011, 2019)]


def test_year_gap_ranges_empty_when_no_gaps():
    years = Counter({2020: 5, 2021: 3, 2022: 9})
    assert r._year_gap_ranges(2020, 2022, years) == []


def test_covered_year_span_excludes_empty_years():
    # SESSION-HANDOFF.txt п.5 (2026-08-05, боевой прогон): живой пример пользователя --
    # диапазон 2015-2026 (12 календарных лет), но снимки только в 2015/2024/2025/2026 --
    # плитка "N лет истории" раньше показывала 12 (буквальный max-min+1), должна показывать 4
    # (только реально покрытые годы).
    years = Counter({2015: 1, 2024: 40, 2025: 113, 2026: 3})
    assert r._covered_year_span(years) == 4


def test_covered_year_span_equals_calendar_span_without_gaps():
    years = Counter({2020: 5, 2021: 3, 2022: 9})
    assert r._covered_year_span(years) == 3


def test_covered_year_span_single_year():
    assert r._covered_year_span(Counter({2020: 5})) == 1


def test_parse_target_logs_reads_rotated_csv_files_too(tmp_path):
    """report.py:parse_target_logs() (до фикса) читал только текущий "name.csv" -- после
    ротации (photosort_win.py:_rotate_log_if_needed, 20 МБ, переименовывает старый файл в
    "name-YYYYMMDD-HHMMSS.csv") история до ротации молча выпадала из отчёта. Три файла:
    две ротации + текущий, порядок в результате должен быть хронологический (старые первыми)."""
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    header = "timestamp,source,dest,reason,flags,date,duration,place\n"
    (logs_dir / "appended-20260101-000000.csv").write_text(
        header + "2026-01-01 00:00:00,s1,d1,appended_new,,,,\n", encoding="utf-8")
    (logs_dir / "appended-20260201-000000.csv").write_text(
        header + "2026-02-01 00:00:00,s2,d2,appended_new,,,,\n", encoding="utf-8")
    (logs_dir / "appended.csv").write_text(
        header + "2026-03-01 00:00:00,s3,d3,appended_new,,,,\n", encoding="utf-8")
    data = r.parse_target_logs(str(logs_dir))
    assert [row["source"] for row in data["appended"]] == ["s1", "s2", "s3"]


def test_addition_date_range_from_appended_csv(tmp_path):
    """Пункт B.10 ("большой разбор report.html", SESSION-HANDOFF.txt): дата первого и
    последнего автоматического пополнения -- по timestamp в appended.csv."""
    target = tmp_path / "Archive"
    logs_dir = target / "__служебные_файлы" / "logs"
    logs_dir.mkdir(parents=True)
    header = "timestamp,source,dest,reason,flags,date,duration,place\n"
    (logs_dir / "appended.csv").write_text(
        header
        + "2026-01-15 10:00:00,s1,d1,appended_new,,,,\n"
        + "2026-03-20 18:30:00,s2,d2,appended_new,,,,\n",
        encoding="utf-8",
    )
    first, last = r._addition_date_range(str(target))
    assert (first, last) == ("2026-01-15", "2026-03-20")


def test_addition_date_range_none_when_no_logs(tmp_path):
    assert r._addition_date_range(str(tmp_path / "Empty")) == (None, None)


def test_generate_passport_report_shows_addition_date_range(tmp_path):
    target = tmp_path / "Archive"
    logs_dir = target / "__служебные_файлы" / "logs"
    logs_dir.mkdir(parents=True)
    header = "timestamp,source,dest,reason,flags,date,duration,place\n"
    (logs_dir / "appended.csv").write_text(
        header
        + "2026-01-15 10:00:00,s1,d1,appended_new,,,,\n"
        + "2026-03-20 18:30:00,s2,d2,appended_new,,,,\n",
        encoding="utf-8",
    )
    stats = _FakeAnalyzeStats()
    out_path = target / "__служебные_файлы" / "passport.html"
    r.generate_passport_report(stats, str(out_path), target_path=str(target))
    html_out = out_path.read_text(encoding="utf-8")
    assert "с 2026-01-15 по 2026-03-20" in html_out
    assert "нельзя установить повторным сканированием" in html_out


def test_generate_passport_report_no_addition_dates_without_logs(tmp_path):
    stats = _FakeAnalyzeStats()
    out_path = tmp_path / "passport.html"
    r.generate_passport_report(stats, str(out_path), target_path=str(tmp_path / "NoLogsHere"))
    html_out = out_path.read_text(encoding="utf-8")
    assert "Автоматических пополнений" not in html_out


def test_undated_checklist_item_shows_folder_and_name():
    """2026-07-26, обсуждение с пользователем: "N файлов вообще без даты" раньше был
    безадресным (только счётчик) -- пользователю нечем было найти сами файлы без похода в
    undated_media.csv. Задача 5 (2026-08-02): показываются только файлы под
    ByDate/0000-undated/ -- только там дата реально определяет место файла (см. RULES.md,
    блок UNDATED)."""
    data = {"undated_media": [
        {"timestamp": "2026-01-01 00:00:00", "source": "F:\\a.jpg",
         "dest": "D:\\Archive\\ByDate\\0000-undated\\Отпуск\\a.jpg"},
    ]}
    fields = r._build_checklist_fields(data)
    items = r._build_checklist_items(fields)
    joined = "".join(items)
    assert "1 файл вообще без даты" in joined
    assert "a.jpg" in joined
    assert "ByDate\\0000-undated\\Отпуск" in joined


def test_undated_checklist_item_hides_albums_files_entirely():
    """Задача 5 (SESSION-HANDOFF.txt, пакет "боевой прогон D:\\"): по RULES.md (блок UNDATED)
    Albums/ раскладывается по структуре исходных папок независимо от даты -- отсутствие даты
    там ни на что не влияет, совет "стоит проставить дату" был бы бесполезен. Живая проверка
    на боевом прогоне: все 274 файла Tier D лежали в Albums/, ни одного в ByDate/0000-undated/
    -- по решению пользователя такие файлы не показываются вообще, ни в каком виде."""
    data = {"undated_media": [
        {"timestamp": "2026-01-01 00:00:00", "source": "F:\\a.jpg",
         "dest": "D:\\Archive\\Albums\\Отпуск\\a.jpg"},
    ]}
    fields = r._build_checklist_fields(data)
    items = r._build_checklist_items(fields)
    joined = "".join(items)
    assert "вообще без даты" not in joined


def test_undated_checklist_item_groups_and_previews_like_tier_bc():
    """Задача 5: тот же <details>-паттерн "превью 2 + Показать ещё N папок", что уже
    применён к Tier B/C -- живой репорт пользователя: 274 файла сплошным абзацем без
    группировки читались нечитаемо."""
    data = {"undated_media": [
        {"timestamp": "2026-01-01 00:00:00", "source": f"F:\\a{i}.jpg",
         "dest": rf"D:\Archive\ByDate\0000-undated\Папка{i % 3}\a{i}.jpg"}
        for i in range(9)
    ]}
    fields = r._build_checklist_fields(data)
    items = r._build_checklist_items(fields)
    joined = "".join(items)
    # Каждая из 3 папок -- своя строка "N файлов вообще без даты" (тот же паттерн, что у
    # Tier B/C -- заголовок на группу, не один общий агрегат на всю категорию).
    assert joined.count("файла вообще без даты") == 3
    assert "<details>" in joined
    assert "Показать ещё 1 папку" in joined  # 3 папки всего, превью 2, "ещё" -- 1


def test_undated_checklist_item_degrades_without_rows():
    """analyze-уровень не отслеживает undated_media поштучно (только агрегат в model) --
    fields.get() должен деградировать до старого текста без списка, не упасть с KeyError.
    Задача 5: undated_detail тоже отсутствует на этом уровне (build_model_from_analyze_stats
    не добавляет этот ключ вовсе) -- del здесь имитирует именно это, не просто пустой список."""
    fields = r._build_checklist_fields({})
    fields["undated_total"] = 3
    del fields["undated_media"]
    del fields["undated_detail"]
    items = r._build_checklist_items(fields)
    joined = "".join(items)
    assert "3 файла вообще без даты" in joined
    assert "Где искать" not in joined


def test_parse_target_logs_skips_corrupted_rotated_file_without_crashing(tmp_path):
    """ci/windows_ci_test.py::test_log_rotation -- живая регрессия от фикса ротации выше:
    ротированный файл -- переименованный "как есть" файл на момент ротации, не гарантированно
    валидный CSV (тест форсирует ротацию файлом без единого "\\n" -- csv.DictReader падает с
    _csv.Error "field larger than field limit", не OSError, раньше ронял весь прогон целиком).
    Должно тихо пропуститься, не бросить исключение наружу; валидный текущий файл всё равно
    читается."""
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    (logs_dir / "appended-20260101-000000.csv").write_bytes(b"x" * (21 * 1024 * 1024))
    (logs_dir / "appended.csv").write_text(
        "timestamp,source,dest,reason,flags,date,duration,place\n"
        "2026-03-01 00:00:00,s3,d3,appended_new,,,,\n", encoding="utf-8")
    data = r.parse_target_logs(str(logs_dir))
    assert [row["source"] for row in data["appended"]] == ["s3"]


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


def test_raw_without_jpeg_counted_in_dates_raw_with_jpeg_excluded():
    """SESSION-HANDOFF.txt, баг 8: raw_without_jpeg -- своя, не дублирующая ничей другой файл
    дата, должен участвовать в years/cities. raw_with_jpeg дублирует уже учтённый JPEG --
    осознанно исключён, участие задвоило бы цифры."""
    rows = [
        {"timestamp": "t", "source": "s1",
         "dest": r"C:\T\dst\RAW\ByDate\2020\2020-05-01 Сочи [PhotoArchive]\a.cr2",
         "reason": "raw_without_jpeg", "flags": ""},
        {"timestamp": "t", "source": "s2",
         "dest": r"C:\T\dst\RAW\ByDate\2021\2021-06-01 Сочи [PhotoArchive]\b.cr2",
         "reason": "raw_with_jpeg", "flags": ""},
    ]
    model = r.build_model_from_rows({"appended": rows})
    assert model["years"] == Counter({2020: 1})
    assert model["cities"] == Counter({"Сочи": 1})


def test_raw_without_jpeg_in_albums_visible_for_dates_via_date_column():
    """REVIEW-HANDOFF.md, раунд 29 [БЛОКЕР]: пересечение багов 8 и 9 -- одинокий RAW
    (raw_without_jpeg) внутри Albums\\... не имеет ни сегмента ByDate в пути (баг 9), ни
    исключающего reason=raw_with_jpeg (баг 8), поэтому должен участвовать в датах через
    колонку "date", как и обычный image/video appended_new. До фикса photosort_win.py
    (raw_mirrored-ветка не передавала date= в run_logs.appended()) колонка была всегда пустой
    и файл выпадал из years/cities несмотря на резерв, добавленный багом 9."""
    rows = [
        {"timestamp": "t", "source": "s1", "dest": r"C:\T\dst\RAW\Albums\Отпуск\a.cr2",
         "reason": "raw_without_jpeg", "flags": "", "date": "2019-07-15"},
    ]
    model = r.build_model_from_rows({"appended": rows})
    assert model["years"] == Counter({2019: 1})
    assert model["year_months"] == Counter({"2019-07": 1})


def test_albums_file_visible_for_dates_via_date_column():
    """SESSION-HANDOFF.txt, баг 9: файлы в Albums\\... не имеют сегмента ByDate в пути вообще
    -- _parse_bydate_segment() для них всегда None. Резерв -- колонка "date" в appended.csv
    (RunLogs.appended(), новая с этого фикса). full date -> (year, month, day); эти строки не
    несут колонку "place" (живая находка 2026-07-25, отдельный фикс -- см.
    test_albums_file_place_comes_from_place_column ниже), поэтому oldest падает на старый
    резерв -- имя самого альбома."""
    rows = [
        {"timestamp": "t", "source": "s1", "dest": r"C:\T\dst\Albums\Отпуск 2019\a.jpg",
         "reason": "appended_new", "flags": "", "date": "2019-07-15"},
        # precision=="year" -- только год достоверен, писать месяц/день было бы ложной точностью
        {"timestamp": "t", "source": "s2", "dest": r"C:\T\dst\Albums\Свадьба\b.jpg",
         "reason": "appended_new", "flags": "", "date": "2015"},
    ]
    model = r.build_model_from_rows({"appended": rows})
    assert model["years"] == Counter({2019: 1, 2015: 1})
    assert model["year_months"] == Counter({"2019-07": 1})
    assert model["oldest"] == ((2015, 0, 0), r"C:\T\dst\Albums\Свадьба\b.jpg", "Свадьба")


def test_albums_file_without_date_column_still_invisible_old_archive_behavior():
    """Старый архив (appended.csv без колонки "date", собран версией до этого фикса) --
    поведение не меняется, файл по-прежнему не участвует в датах (ничего не восстановить)."""
    rows = [{"timestamp": "t", "source": "s1", "dest": r"C:\T\dst\Albums\Отпуск 2019\a.jpg",
             "reason": "appended_new", "flags": ""}]
    model = r.build_model_from_rows({"appended": rows})
    assert model["years"] == Counter()
    assert model["oldest"] is None


def test_albums_file_place_comes_from_place_column():
    """Живая находка 2026-07-25 (боевой прогон F:\\, весь архив ушёл в Albums\\..., ни одного
    города в отчёте): _process_record() раньше вызывал place_for_gps() только в ветке ByDate
    (нужен для имени папки) -- Albums-файлы никогда не получали geo-lookup вообще, "География"
    оставалась пустой для любого архива, целиком состоящего из альбомов. Теперь place считается
    независимо от маршрутизации и пишется отдельной колонкой "place" в appended.csv, тем же
    способом, что и "date" (баг 9) -- report.py читает её как резерв, когда путь (dest) сам по
    себе места не несёт (Albums\\... не имеет сегмента ByDate)."""
    rows = [
        {"timestamp": "t", "source": "s1", "dest": r"C:\T\dst\Albums\Отпуск 2019\a.jpg",
         "reason": "appended_new", "flags": "", "date": "2019-07-15", "place": "Сочи"},
        {"timestamp": "t", "source": "s2", "dest": r"C:\T\dst\Albums\Свадьба\b.jpg",
         "reason": "appended_new", "flags": "", "date": "2015", "place": "Казань"},
    ]
    model = r.build_model_from_rows({"appended": rows})
    assert model["cities"] == Counter({"Сочи": 1, "Казань": 1})
    # oldest берёт настоящее место из колонки, а не запасное имя альбома.
    assert model["oldest"] == ((2015, 0, 0), r"C:\T\dst\Albums\Свадьба\b.jpg", "Казань")


def test_bydate_place_from_path_wins_over_place_column():
    """Место, восстановленное из имени папки ByDate (_parse_bydate_segment), и колонка "place"
    для ByDate-файлов должны совпадать (оба считаются от одного and того же place_for_gps() при
    сборке) -- но если они почему-то разойдутся (например, старая строка archives.log до этого
    фикса), путь остаётся приоритетным источником -- та же логика "path сначала, колонка как
    резерв", что уже применяется для даты."""
    rows = [
        {"timestamp": "t", "source": "s1",
         "dest": r"C:\T\dst\ByDate\2020\2020-05 Сочи [PhotoArchive]\a.jpg",
         "reason": "appended_new", "flags": "", "place": "Казань"},
    ]
    model = r.build_model_from_rows({"appended": rows})
    assert model["cities"] == Counter({"Сочи": 1})


def test_raw_without_jpeg_in_album_place_comes_from_place_column():
    """Тот же живой фикс, что и для image/video выше, распространён и на одинокий RAW
    (raw_without_jpeg) внутри Albums\\... -- у него тоже может быть собственный GPS-тег."""
    rows = [
        {"timestamp": "t", "source": "s1", "dest": r"C:\T\dst\RAW\Albums\Поход\a.cr2",
         "reason": "raw_without_jpeg", "flags": "", "date": "2021-08-01", "place": "Алтай"},
    ]
    model = r.build_model_from_rows({"appended": rows})
    assert model["cities"] == Counter({"Алтай": 1})


def test_render_this_run_shows_bytes_appended_free_disk_and_undated():
    """Пакет п.2 (SESSION-HANDOFF.txt): bytes_appended/free_disk_bytes/undated есть в
    run_stats с самого появления секции "Пополнение архива"/"Пробный прогон", но не
    рендерились -- теперь должны появиться как число-плашки."""
    run_stats = {
        "appended_images": 5, "appended_videos": 0,
        "bytes_appended": 10 * 1024**2, "free_disk_bytes": 2 * 1024**3, "undated": 3,
    }
    html_out = r._render_this_run(run_stats, level="workdir")
    assert "10 МБ" in html_out
    assert "2.0 ГБ" in html_out
    assert "свободно на диске сейчас" in html_out
    assert ">3<" in html_out
    assert "не удалось бы распознать дату" in html_out


def test_render_this_run_hides_free_disk_when_absent():
    """[3]/CLI archive не кладёт free_disk_bytes в run_stats -- плашка не должна появляться."""
    run_stats = {"appended_images": 5, "appended_videos": 0}
    html_out = r._render_this_run(run_stats, level="target")
    assert "свободно на диске сейчас" not in html_out


def test_render_this_run_shows_encrypted_archive_even_when_source_otherwise_empty():
    """2026-08-07, найдено в разговоре с пользователем: если SOURCE -- это ТОЛЬКО запароленный
    архив и больше ничего (appended/skipped/disputed/unreadable все по нулям), ранний
    `return ""` срабатывал раньше, чем функция вообще доходила до блока encrypted_archives --
    секция пропадала целиком, отчёт выглядел неотличимо от "SOURCE был пуст", хотя на самом
    деле важное предупреждение было потеряно."""
    run_stats = {
        "appended_images": 0, "appended_videos": 0,
        "encrypted_archives": ["D:/Фото/секретный.zip"],
    }
    html_out = r._render_this_run(run_stats, level="target")
    assert html_out != ""
    assert "защищены паролем" in html_out or "защищённые паролем" in html_out


def test_render_this_run_shows_processed_count_as_comparison_base():
    """Раунд 32, задача 4 (REVIEW-HANDOFF.md): "всего найдено на источнике" -- база для
    сверки, что отчёт ничего не потерял молча."""
    run_stats = {"appended_images": 5, "appended_videos": 0, "processed_count": 42}
    html_out = r._render_this_run(run_stats, level="target")
    assert "найдено на источнике" in html_out
    assert ">42<" in html_out


def test_render_this_run_shows_duration():
    # Живой репорт пользователя (2026-08-01): секция "Пополнение архива" не показывала,
    # сколько времени заняла сборка -- duration_seconds появился в run_stats
    # (photosort_win.py:_run_impl()) специально для этого тайла.
    run_stats = {"appended_images": 5, "appended_videos": 0, "duration_seconds": 3725}  # 1ч 2м 5с
    html_out = r._render_this_run(run_stats, level="target")
    assert "1 час 2 минуты" in html_out
    assert "заняла сборка" in html_out


def test_render_this_run_hides_duration_when_absent():
    run_stats = {"appended_images": 5, "appended_videos": 0}
    html_out = r._render_this_run(run_stats, level="target")
    assert "заняла сборка" not in html_out
    assert "заняла проверка" not in html_out


def test_render_this_run_duration_preview_wording():
    # preview (level != "target", напр. [2]/--dry-run): сканирование РЕАЛЬНО заняло это
    # время (в отличие от копирования/дедупа, которые в dry-run гипотетические) -- меняется
    # только существительное, не модальность ("было бы").
    run_stats = {"appended_images": 5, "appended_videos": 0, "duration_seconds": 90}
    html_out = r._render_this_run(run_stats, level="workdir")
    assert "заняла проверка" in html_out
    assert "заняла сборка" not in html_out


def test_render_this_run_warns_about_listdir_failures():
    """Раунд 32, задача 4: если хотя бы одна папка не прочиталась, это прямой сигнал в
    отчёте, не просто число для ручного сложения."""
    run_stats = {"appended_images": 5, "appended_videos": 0, "listdir_failed_count": 2}
    html_out = r._render_this_run(run_stats, level="target")
    assert "2 папки" in html_out
    assert "не удалось прочитать" in html_out


def test_render_this_run_no_listdir_warning_when_zero():
    run_stats = {"appended_images": 5, "appended_videos": 0, "listdir_failed_count": 0}
    html_out = r._render_this_run(run_stats, level="target")
    assert "не удалось прочитать при обходе" not in html_out


def test_render_this_run_raw_included_in_new_files_total_with_breakdown():
    """2026-07-26, по прямому решению пользователя: RAW теперь входит в "итого новых файлов"
    (raw_mirrored раньше нигде не показывался в этой секции), с подписью "в т.ч." в тайле."""
    run_stats = {"appended_images": 5, "appended_videos": 2, "raw_mirrored": 3}
    html_out = r._render_this_run(run_stats, level="target")
    assert '<div class="value">10</div>' in html_out  # 5+2+3, не 7
    assert "новых файлов добавлено" in html_out
    assert "в т.ч.: фото — 5 файлов, RAW — 3 файла, видео — 2 файла" in html_out


def test_render_this_run_new_files_breakdown_hidden_when_single_type():
    """Если весь прирост -- одного типа (например, только фото), разбивка не добавляет
    ничего нового к уже показанному числу -- но всё равно рендерится (простое и
    предсказуемое правило, не скрывать в этом случае отдельно)."""
    run_stats = {"appended_images": 5, "appended_videos": 0}
    html_out = r._render_this_run(run_stats, level="target")
    assert "в т.ч.: фото — 5 файлов" in html_out


def test_render_this_run_shows_breakdown_for_skipped_disputed_unreadable_near_dup():
    """2026-07-26, по прямой просьбе пользователя: не только "новые файлы", вся статистика
    секции -- итого + в т.ч. фото/RAW/видео. near_dup -- только фото/видео (near-dup никогда
    не бывает raw, см. photosort_win.py)."""
    run_stats = {
        "appended_images": 1, "appended_videos": 0,
        "skipped_present": 6, "skipped_present_image": 3, "skipped_present_raw": 2,
        "skipped_present_video": 1,
        "appended_near_dup": 2, "near_dup_image": 1, "near_dup_video": 1,
        "unreadable_count": 4, "unreadable_count_image": 2, "unreadable_count_other": 2,
        "disputed": 3, "disputed_other": 3,
    }
    html_out = r._render_this_run(run_stats, level="target")
    assert "Дубли, в т.ч.: фото — 3 файла, RAW — 2 файла, видео — 1 файл" in html_out
    assert "Похожие кадры, в т.ч.: фото — 1 файл, видео — 1 файл" in html_out
    assert "Не прочитано, в т.ч.: фото — 2 файла, прочее — 2 файла" in html_out
    assert "Спорные, в т.ч.: прочее — 3 файла" in html_out


def test_render_this_run_legend_shows_status_and_landed_summary_line():
    """SESSION-HANDOFF.txt, пакет "боевой прогон D:\\", задача 2: подпись легенды должна
    прямо называть, физически ли категория легла в архив, плюс итоговая двоичная строка
    landed (новые, включая near-dup + спорные) vs not_copied (дубли + не прочитано)."""
    run_stats = {
        "appended_images": 5, "appended_videos": 0,
        "skipped_present": 2, "appended_near_dup": 1,
        "unreadable_count": 1, "disputed": 1,
    }
    html_out = r._render_this_run(run_stats, level="target")
    assert "Новые файлы — в архиве — 4 файла" in html_out
    assert "Дубли — не копировались, уже есть в архиве — 2 файла" in html_out
    assert "Похожие кадры — в архиве (сохранены рядом) — 1 файл" in html_out
    assert "Не прочитано — не скопировано (ошибка чтения) — 1 файл" in html_out
    assert "Спорные — сохранены отдельно, не в архиве (_Unsorted) — 1 файл" in html_out
    assert ("Итого: 6 файлов легло физически (новые + похожие + спорные), "
            "3 файла не скопировано (дубли + не прочитано).") in html_out


def test_render_this_run_legend_and_summary_hypothetical_in_preview():
    """level != "target" ([2]/--dry-run) -- ничего физически не записано, легенда и итоговая
    строка должны говорить "было бы", не факт, тот же принцип, что уже применяется к
    added_label/saved_label выше по функции."""
    run_stats = {
        "appended_images": 5, "appended_videos": 0,
        "skipped_present": 2, "appended_near_dup": 1,
        "unreadable_count": 1, "disputed": 1,
    }
    html_out = r._render_this_run(run_stats, level="workdir")
    assert "Новые файлы — были бы в архиве" in html_out
    assert "Дубли — не копировались бы, уже есть в архиве" in html_out
    assert "Спорные — были бы сохранены отдельно, не в архиве (_Unsorted)" in html_out
    assert ("Итого: 6 файлов легло бы физически (новые + похожие + спорные), "
            "3 файла не было бы скопировано (дубли + не прочитано).") in html_out


def test_render_this_run_album_merge_destination_is_a_path_from_archive_root():
    """Пункт B.4 ("большой разбор report.html", SESSION-HANDOFF.txt): "куда" -- путь от корня
    архива ("Albums\\дедушка"), не голое имя альбома -- иначе неотличимо от "откуда"."""
    run_stats = {
        "appended_images": 1, "appended_videos": 0,
        "album_merge_events": [("дедушка", r"Users\HTPC\Desktop\старые фото", False)],
    }
    html_out = r._render_this_run(run_stats, level="target")
    assert "«Albums\\дедушка» ←" in html_out
    assert "«дедушка» ←" not in html_out


def test_render_this_run_album_merge_advice_shown_for_two_unique_sources_in_preview():
    """Задача C1 (SESSION-HANDOFF.txt): совет "возможно, это разные альбомы" -- только когда
    >=2 источника дали РЕАЛЬНО разное (is_dup=False) содержимое, и только в [2] Пробный прогон
    (level=="workdir"), где структуру ещё дёшево пересобрать по-другому."""
    run_stats = {
        "appended_images": 2, "appended_videos": 0,
        "album_merge_events": [
            ("дедушка", r"Люди\дедушка", False),
            ("дедушка", r"Природа\дедушка", False),
        ],
    }
    html_out = r._render_this_run(run_stats, level="workdir")
    assert "возможно, это на самом деле разные альбомы" in html_out


def test_render_this_run_album_merge_advice_hidden_when_second_source_is_only_a_dup():
    """Второй источник, давший ТОЛЬКО повторный дубль (is_dup=True) -- не в счёт "разных
    источников" для совета C1, объединение в этом случае безобидно."""
    run_stats = {
        "appended_images": 1, "appended_videos": 0,
        "album_merge_events": [
            ("дедушка", r"Люди\дедушка", False),
            ("дедушка", r"Природа\дедушка", True),
        ],
    }
    html_out = r._render_this_run(run_stats, level="workdir")
    assert "«Albums\\дедушка» ←" in html_out
    assert "возможно, это на самом деле разные альбомы" not in html_out


def test_render_this_run_album_merge_advice_hidden_outside_preview():
    """level=="target" (настоящая сборка) -- совет C1 не показывается, реальная переделка уже
    дороже, чем во время [2] Пробный прогон (см. докстринг _render_this_run())."""
    run_stats = {
        "appended_images": 2, "appended_videos": 0,
        "album_merge_events": [
            ("дедушка", r"Люди\дедушка", False),
            ("дедушка", r"Природа\дедушка", False),
        ],
    }
    html_out = r._render_this_run(run_stats, level="target")
    assert "«Albums\\дедушка» ←" in html_out
    assert "возможно, это на самом деле разные альбомы" not in html_out


def _cloudlike_profile(n=40, years=4, cameras=3, date_subdirs=0, name="Отпуск"):
    return {
        "name": name, "n": n,
        "years": set(range(2015, 2015 + years)),
        "cameras": {f"Camera{i}" for i in range(cameras)},
        "date_subdirs": {f"2020-0{i + 1}" for i in range(date_subdirs)},
    }


def test_dryrun_structure_recommendations_flags_album_with_two_of_four_signals():
    """Задача B (SESSION-HANDOFF.txt): >=2 из 4 структурных признаков + n >= REC_MIN_FILES --
    здесь years>=4 и cameras>=3 (2 признака), n=40 >= REC_MIN_FILES=30."""
    run_stats = {"album_profiles": {"src/Отпуск": _cloudlike_profile()}}
    html_out = r._render_dryrun_structure_recommendations(run_stats)
    assert "«Отпуск» похож на папку облачной синхронизации" in html_out
    assert "~Отпуск" in html_out


def test_dryrun_structure_recommendations_hides_album_with_only_one_signal():
    profile = _cloudlike_profile(years=4, cameras=1, date_subdirs=0)
    run_stats = {"album_profiles": {"src/Отпуск": profile}}
    html_out = r._render_dryrun_structure_recommendations(run_stats)
    assert html_out == ""


def test_dryrun_structure_recommendations_hides_small_album_below_min_files():
    """n < REC_MIN_FILES -- не показывать, даже если структурные признаки набрались."""
    profile = _cloudlike_profile(n=5, years=4, cameras=3)
    run_stats = {"album_profiles": {"src/Отпуск": profile}}
    html_out = r._render_dryrun_structure_recommendations(run_stats)
    assert html_out == ""


def test_dryrun_structure_recommendations_name_hint_counts_as_one_signal():
    """Имя-намёк (CLOUDLIKE_ALBUM_HINTS) + всего 1 числовой признак -- тоже >=2 сигнала."""
    profile = _cloudlike_profile(years=4, cameras=1, date_subdirs=0, name="Google Photos")
    run_stats = {"album_profiles": {"src/Google Photos": profile}}
    html_out = r._render_dryrun_structure_recommendations(run_stats)
    assert "«Google Photos» похож на папку облачной синхронизации" in html_out


def test_dryrun_structure_recommendations_caps_at_rec_struct_max():
    profiles = {
        f"src/Альбом{i}": _cloudlike_profile(n=100 - i, name=f"Альбом{i}")
        for i in range(r.REC_STRUCT_MAX + 3)
    }
    run_stats = {"album_profiles": profiles}
    html_out = r._render_dryrun_structure_recommendations(run_stats)
    assert html_out.count("<li>") == r.REC_STRUCT_MAX


def test_dryrun_structure_recommendations_empty_when_no_profiles():
    assert r._render_dryrun_structure_recommendations({}) == ""
    assert r._render_dryrun_structure_recommendations({"album_profiles": {}}) == ""


def test_generate_workdir_report_includes_structure_recommendations(tmp_path):
    """Проводка через _generate_from_model(): level=="workdir" (не full_workdir) должен
    показывать карточку, level=="target" -- не должен (тот же принцип, что у C1)."""
    run_stats = {"appended_images": 1, "appended_videos": 0,
                 "album_profiles": {"src/Отпуск": _cloudlike_profile()}}
    out_path = tmp_path / "report.html"
    r.generate_report({}, str(out_path), level="workdir", run_stats=run_stats)
    html_out = out_path.read_text(encoding="utf-8")
    assert "похож на папку облачной синхронизации" in html_out


def test_render_this_run_shows_encrypted_archive_paths():
    """Пункт B.2 ("большой разбор report.html", SESSION-HANDOFF.txt): раньше был только
    счётчик запароленных архивов -- теперь список с полным (кликабельным) путём."""
    run_stats = {
        "appended_images": 1, "appended_videos": 0,
        "encrypted_archives": [r"D:\Source\family.zip"],
    }
    html_out = r._render_this_run(run_stats, level="target")
    assert "защищённые паролем" in html_out
    assert '<a href="file:///D:/Source/family.zip" target="_blank" rel="noopener">D:\\Source\\family.zip</a>' in html_out


def test_render_this_run_no_encrypted_archives_section_when_empty():
    run_stats = {"appended_images": 5, "appended_videos": 0}
    html_out = r._render_this_run(run_stats, level="target")
    assert "паролем" not in html_out


def test_render_this_run_nested_encrypted_archive_shows_readable_text_not_orphaned_separator():
    """REVIEW-HANDOFF.md, Раунд 47, замечание 1: первая версия фикса передавала "" для
    вложенного запароленного архива -- html.escape("") тоже "", ни ссылки, ни текста не
    оставалось, только "осиротевший" "; "-разделитель перед следующим элементом списка.
    Теперь photosort_win.py передаёт относительный display ("outer.zip → secret.zip") вместо
    пустой строки -- архив читаем в списке, просто без ссылки (не абсолютный путь)."""
    run_stats = {
        "appended_images": 1, "appended_videos": 0,
        "encrypted_archives": ["outer.zip → secret.zip", r"C:\Source\family.zip"],
    }
    html_out = r._render_this_run(run_stats, level="target")
    assert "outer.zip → secret.zip" in html_out
    assert "; <a" not in html_out  # осиротевший разделитель перед первой ссылкой -- ушёл
    assert '<a href="file:///C:/Source/family.zip" target="_blank" rel="noopener">C:\\Source\\family.zip</a>' in html_out


def test_render_this_run_shows_dvd_units_copied():
    # 2026-08-07: DVD-Video (VIDEO_TS) теперь копируется целиком как один юнит -- см.
    # SourceWalker._handle_dvd_unit()/_process_dvd_item() в photosort_win.py.
    run_stats = {
        "appended_images": 1, "appended_videos": 3,
        "dvd_units_copied": [{"name": "Some_Movie_DVD5",
                               "dest_path": r"D:\Target\Albums\Some_Movie_DVD5\VIDEO_TS",
                               "n_files": 3, "total_bytes": 900, "fingerprint": "abc"}],
    }
    html_out = r._render_this_run(run_stats, level="target")
    assert "DVD-видео" in html_out
    assert "скопировано целиком" in html_out
    assert ('<a href="file:///D:/Target/Albums/Some_Movie_DVD5/VIDEO_TS" '
            'target="_blank" rel="noopener">Some_Movie_DVD5</a>') in html_out


def test_render_this_run_no_dvd_section_when_empty():
    run_stats = {"appended_images": 5, "appended_videos": 0}
    html_out = r._render_this_run(run_stats, level="target")
    assert "DVD-видео" not in html_out


def test_render_this_run_shows_dvd_units_skipped_duplicate():
    # "Объединение DVD-папок недопустимо" (требование пользователя) -- уже архивированный
    # диск пропускается ЦЕЛИКОМ, отчёт должен явно сказать, что это не потерянные файлы, а
    # намеренный пропуск дубля.
    run_stats = {
        "appended_images": 1, "appended_videos": 0,
        "dvd_units_skipped_duplicate": [{"name": "Disc1",
                                          "dest_path": r"D:\Target\Albums\Disc1\VIDEO_TS"}],
    }
    html_out = r._render_this_run(run_stats, level="target")
    assert "уже в архиве, пропущено" in html_out
    assert "Disc1" in html_out


def test_render_analyze_recommendations_shows_encrypted_archive_paths():
    model = {"encrypted_archive_paths": [r"C:\S\old.zip", r"C:\S\older.zip"]}
    html_out = r._render_analyze_recommendations(model)
    assert "2 архива защищены паролем" in html_out
    assert '<a href="file:///C:/S/old.zip" target="_blank" rel="noopener">C:\\S\\old.zip</a>' in html_out


def test_render_analyze_recommendations_nested_encrypted_archive_readable_not_orphaned():
    """REVIEW-HANDOFF.md, Раунд 47, замечание 1 -- тот же репро, что для _render_this_run()
    выше, но для второй точки потребления encrypted_archive_paths (analyze-уровень)."""
    model = {"encrypted_archive_paths": ["outer.zip → secret.zip", r"C:\S\family.zip"]}
    html_out = r._render_analyze_recommendations(model)
    assert "outer.zip → secret.zip" in html_out
    assert "; <a" not in html_out


def test_render_this_run_no_breakdown_captions_without_typed_stats():
    """Старый вызывающий код (до этой фичи) не передаёт typed-ключи для skipped/disputed/
    unreadable/near_dup вообще -- .get(...,0) везде даёт пустые Counter, ни одна из ЭТИХ
    четырёх строк "в т.ч." не должна появиться, отчёт не падает (новый файловый тайл наверху
    всё равно покажет свою разбивку -- она строится из appended_images/videos/raw_mirrored,
    которые существуют независимо от этой фичи)."""
    run_stats = {"appended_images": 5, "appended_videos": 0, "skipped_present": 6, "disputed": 2}
    html_out = r._render_this_run(run_stats, level="target")
    assert "Дубли, в т.ч." not in html_out
    assert "Спорные, в т.ч." not in html_out
    assert "Не прочитано, в т.ч." not in html_out
    assert "Похожие кадры, в т.ч." not in html_out


def test_page_shell_checkmark_css_not_corrupted_by_escape_parsing():
    """Регрессия: "\\2713" в обычной Python-строке -- НЕ валидный unicode-escape (\\u2713
    нужен для этого), парсится как octal "\\271" ("¹") + буквальная "3" -- CSS content
    для .trust-list li::before рендерился как "¹3", не как чекмарк "✓". Найдено этой же
    сессией при ручной генерации отчёта, до появления этого теста ловилось только глазами."""
    html_out = r._page_shell("t", "")
    assert "content: \"✓" in html_out
    assert "¹3" not in html_out


def test_trust_block_banner_and_checklist():
    """4.1/4.3 (PROMPT_report_marketing.md): баннер доверия + компактный чек-лист, самая
    частая рекомендация всех источников маркетингового ТЗ -- отсутствовала в HTML полностью.

    2026-08-06, живой репорт пользователя: чек-лист больше НЕ повторяет дословно то, что уже
    сказано в баннере строкой выше ("Файлы не удалялись."/"Оригиналы сохранены на своих
    местах." убраны) -- для чистого target без нечитаемых файлов добавить нечего, чек-лист
    (<ul>) не рендерится вовсе, остаётся только баннер."""
    target_html = r._render_trust_block("target")
    assert "Оригиналы не изменены и не удалены" in target_html
    assert "<ul" not in target_html
    assert "Реальных изменений на диске нет" not in target_html

    workdir_html = r._render_trust_block("workdir")
    assert "Реальных изменений на диске нет — это предпросмотр." in workdir_html


def test_trust_block_hides_unreadable_line_when_zero():
    """Пункт B.1 ("большой разбор report.html", SESSION-HANDOFF.txt): "Ошибки чтения показаны
    отдельно" раньше выводился безусловно -- на чистом архиве это ответ на незаданный вопрос."""
    assert "Ошибки чтения" not in r._render_trust_block("target", 0)
    assert "Ошибки чтения" not in r._render_trust_block("target")  # default
    assert "Ошибки чтения показаны отдельно, не смешаны с остальным." in r._render_trust_block("target", 3)


def test_generate_report_includes_trust_banner(tmp_path):
    """Смоук на весь пайплайн generate_report() -- баннер должен попасть в реальный файл,
    не только в изолированный вызов _render_trust_block()."""
    out_path = tmp_path / "report.html"
    r.generate_report({}, str(out_path), level="target")
    html_out = out_path.read_text(encoding="utf-8")
    assert "Оригиналы не изменены и не удалены" in html_out


def test_generate_placeholder_report_includes_trust_banner(tmp_path):
    out_path = tmp_path / "report.html"
    r.generate_placeholder_report("Источник пуст.", str(out_path))
    html_out = out_path.read_text(encoding="utf-8")
    assert "Оригиналы не изменены и не удалены" in html_out


def test_generate_placeholder_report_default_stays_dry(tmp_path):
    """Раунд 34 (REVIEW-HANDOFF.md): без suggest_other_location=True -- старое поведение
    (сухая отсылка к консоли), ничего не меняется для вызывающих мест, которые не в курсе
    новой подсказки."""
    out_path = tmp_path / "report.html"
    r.generate_placeholder_report("Источник пуст.", str(out_path))
    html_out = out_path.read_text(encoding="utf-8")
    assert "Подробности — в консоли программы" in html_out
    assert "на другом месте" not in html_out


def test_generate_placeholder_report_suggests_other_location(tmp_path):
    """Раунд 34: пустой/недоступный источник -- активная подсказка вместо сухой заглушки."""
    out_path = tmp_path / "report.html"
    r.generate_placeholder_report("Источник оказался недоступен или пуст.", str(out_path),
                                   suggest_other_location=True)
    html_out = out_path.read_text(encoding="utf-8")
    assert "стоит проверить" in html_out
    assert "на другом месте" in html_out
    assert "Подробности — в консоли программы" not in html_out


def test_generate_placeholder_report_interrupted_never_suggests_other_location(tmp_path):
    """Раунд 34: interrupted (Ctrl+C) -- причина и так ясна пользователю, активная подсказка
    "проверьте другой диск" была бы неуместна и сбивала бы с толку -- даже если вызывающий
    код по ошибке передаст suggest_other_location=True вместе с interrupted=True."""
    out_path = tmp_path / "report.html"
    r.generate_placeholder_report("Прервано.", str(out_path), interrupted=True,
                                   suggest_other_location=True)
    html_out = out_path.read_text(encoding="utf-8")
    assert "на другом месте" not in html_out
    assert "Подробности — в консоли программы" in html_out


def test_render_this_run_shows_stopped_for_space_notice():
    """4.2 (PROMPT_report_marketing.md): триада исхода -- отчёт после остановки по нехватке
    места раньше выглядел неотличимо от отчёта после полного успеха."""
    run_stats = {"appended_images": 3, "appended_videos": 0, "stopped_for_space": True}
    html_out = r._render_this_run(run_stats, level="target")
    assert "Почти всё разложено" in html_out
    assert "Не хватило места" in html_out


def test_render_this_run_stopped_for_space_survives_all_zero_counts():
    """Остановка может случиться до того, как хоть один файл дописался -- секция не должна
    молча стать пустой (тот же ранний return "", что для пустого SOURCE, был бы неотличим)."""
    run_stats = {"stopped_for_space": True}
    html_out = r._render_this_run(run_stats, level="target")
    assert html_out != ""
    assert "Почти всё разложено" in html_out


def test_render_this_run_no_notice_without_stopped_for_space():
    run_stats = {"appended_images": 3, "appended_videos": 0}
    html_out = r._render_this_run(run_stats, level="target")
    assert "Почти всё разложено" not in html_out


def test_render_sheet2_explains_album_vs_bydate_placement():
    """4.4 (PROMPT_report_marketing.md): фраза, объясняющая логику раскладки, должна появиться
    рядом с "Топ альбомов по размеру", когда в архиве вообще есть альбомы."""
    rows = [_appended_row(r"C:\T\dst\Albums\Отпуск 2019\a.jpg")]
    model = r.build_model_from_rows({"appended": rows})
    assert model["top_albums"]  # sanity: fixture действительно даёт непустой список
    html_out = r._render_sheet2(model)
    assert "сохранены как альбомы" in html_out


def test_find_year_gap_detects_clear_dip():
    """4.5: 2003/2005 насыщены (50 файлов), 2004 -- почти пусто (2 файла), явный провал на
    порядок -- должен быть найден."""
    years = Counter({2001: 40, 2002: 45, 2003: 50, 2004: 2, 2005: 50, 2006: 48})
    assert r._find_year_gap(years) == 2004


def test_find_year_gap_none_for_short_history():
    """Меньше нескольких лет истории -- провал неотличим от шума, не показываем вовсе."""
    years = Counter({2020: 50, 2021: 2, 2022: 50})
    assert r._find_year_gap(years) is None


def test_find_year_gap_none_when_neighbors_are_themselves_small():
    """Соседи сами малочисленны (< порога) -- честно не с чем сравнивать, не выдумываем провал."""
    years = Counter({2018: 3, 2019: 4, 2020: 1, 2021: 3, 2022: 4, 2023: 3})
    assert r._find_year_gap(years) is None


def test_find_year_gap_none_for_smooth_history():
    years = Counter({2018: 40, 2019: 38, 2020: 42, 2021: 41, 2022: 39, 2023: 40})
    assert r._find_year_gap(years) is None


def test_video_duration_summed_cumulatively_from_appended_rows():
    """4.6 (PROMPT_report_marketing.md): длительность видео -- персистентная колонка "duration"
    в appended.csv, кумулятивная сумма по всему архиву (не только этот прогон)."""
    rows = [
        {"timestamp": "t", "source": "a", "dest": r"C:\T\dst\ByDate\2020\a.mp4",
         "reason": "appended_new", "flags": "", "duration": "3600"},
        {"timestamp": "t", "source": "b", "dest": r"C:\T\dst\ByDate\2020\b.mp4",
         "reason": "appended_new", "flags": "", "duration": "1800"},
        # фото рядом -- не должно попасть в сумму, даже если бы там случайно было значение
        {"timestamp": "t", "source": "c", "dest": r"C:\T\dst\ByDate\2020\c.jpg",
         "reason": "appended_new", "flags": ""},
    ]
    model = r.build_model_from_rows({"appended": rows})
    assert model["video_duration_seconds"] == 5400.0


def test_video_duration_missing_column_defaults_to_zero():
    """Старый архив без колонки duration -- 0, не ошибка."""
    rows = [{"timestamp": "t", "source": "a", "dest": r"C:\T\dst\ByDate\2020\a.mp4",
             "reason": "appended_new", "flags": ""}]
    model = r.build_model_from_rows({"appended": rows})
    assert model["video_duration_seconds"] == 0.0


def test_fmt_video_duration_hours_and_minutes():
    assert r._fmt_video_duration(7200) == "2 часа"
    assert r._fmt_video_duration(90) == "2 минуты"
    assert r._fmt_video_duration(0) == "1 минута"  # 0 -- min(1) не должен пропасть как "0 минут"


def test_fmt_run_duration_hours_and_minutes():
    assert r._fmt_run_duration(3725) == "1 час 2 минуты"  # 1ч 2м 5с -- секунды отбрасываются


def test_fmt_run_duration_exact_hour_no_minutes():
    assert r._fmt_run_duration(7200) == "2 часа"  # ровно 2ч -- без "0 минут" на хвосте


def test_fmt_run_duration_minutes_only():
    assert r._fmt_run_duration(125) == "2 минуты"


def test_fmt_run_duration_less_than_a_minute():
    assert r._fmt_run_duration(30) == "меньше минуты"


def test_render_sheet1_shows_video_duration_only_when_present():
    rows = [{"timestamp": "t", "source": "a", "dest": r"C:\T\dst\ByDate\2020\a.mp4",
             "reason": "appended_new", "flags": "", "duration": "7200"}]
    model = r.build_model_from_rows({"appended": rows})
    html_out = r._render_sheet1(model)
    assert "Видео в архиве" in html_out
    assert "2 часа" in html_out

    model_no_video = r.build_model_from_rows({})
    assert "Видео в архиве" not in r._render_sheet1(model_no_video)


def test_render_sheet1_oldest_file_shows_folder_and_name():
    """REVIEW-HANDOFF.md, Раунд 40: путь самого старого файла уже вычислен
    (build_model_from_rows()), но раньше никогда не рендерился. dest -- реальный путь в
    АРХИВЕ (не row["source"], см. комментарий у places, где oldest собирается) -- тот же
    способ отображения (папка + имя), что уже принят для near-dup/точных повторов."""
    rows = [{"timestamp": "t", "source": "s1",
             "dest": r"C:\T\dst\Albums\Отпуск 2015\Море\photo.jpg",
             "reason": "appended_new", "flags": "", "date": "2015-07-01"}]
    model = r.build_model_from_rows({"appended": rows})
    html_out = r._render_sheet1(model)
    assert "photo.jpg" in html_out
    assert r"Albums\Отпуск 2015\Море" in html_out
    assert "s1" not in html_out  # старый origin_display больше не подставляется


def test_render_sheet1_oldest_file_is_a_clickable_file_link():
    """Пункт B.8 ("большой разбор report.html", SESSION-HANDOFF.txt): путь самого старого
    файла -- file://-ссылка, открывающая ИМЕННО ФАЙЛ (не только папку)."""
    rows = [{"timestamp": "t", "source": "s1",
             "dest": r"C:\T\dst\Albums\Отпуск 2015\Море\photo.jpg",
             "reason": "appended_new", "flags": "", "date": "2015-07-01"}]
    model = r.build_model_from_rows({"appended": rows})
    html_out = r._render_sheet1(model)
    assert '<a href="file:///C:/T/dst/Albums/Отпуск 2015/Море/photo.jpg" target="_blank" rel="noopener">' in html_out


def test_render_sheet1_oldest_file_analyze_level_has_no_folder_marker():
    """level=="analyze" кладёт в oldest origin_display (путь в ИСТОЧНИКЕ, см.
    build_model_from_analyze_stats()) -- ByDate/Albums там не бывает, _friendly_target_dir()
    не находит маркер -- рендер должен деградировать до одного имени файла, не падать и не
    показывать пустую "Папка: "."""
    stats = _FakeAnalyzeStats()
    stats.oldest_date = __import__("datetime").datetime(2015, 7, 1)
    stats.oldest_display = "original_photo.jpg"
    model = r.build_model_from_analyze_stats(stats)
    html_out = r._render_sheet1(model, "analyze")
    assert "original_photo.jpg" in html_out
    assert "Папка: " not in html_out


def test_cta_block_target_shows_folder_link_and_backup_advice():
    """4.7/4.8 (PROMPT_report_marketing.md): успешная сборка -- ссылка на TARGET-папку,
    призыв проверить ещё один источник, и единственный совет про резервную копию."""
    html_out = r._render_cta_block("target", target_path=r"D:\PhotoArchive")
    assert 'href="file:///D:/PhotoArchive"' in html_out
    assert "Открыть папку с архивом" in html_out
    assert "ещё один диск или флешку" in html_out
    assert "резервную копию" in html_out


def test_cta_block_target_without_path_skips_link():
    html_out = r._render_cta_block("target", target_path=None)
    assert "Открыть папку с архивом" not in html_out
    assert "резервную копию" in html_out  # совет про бэкап не зависит от наличия ссылки


def test_cta_block_workdir_and_analyze_show_try_real_build_prompt():
    for level in ("workdir", "analyze"):
        html_out = r._render_cta_block(level)
        assert "Нравится результат" in html_out
        assert "резервную копию" not in html_out
        assert "Открыть папку с архивом" not in html_out


def test_generate_report_target_includes_cta_and_backup(tmp_path):
    out_path = tmp_path / "report.html"
    r.generate_report({}, str(out_path), level="target", target_path=r"D:\PhotoArchive")
    html_out = out_path.read_text(encoding="utf-8")
    assert "Открыть папку с архивом" in html_out
    assert "резервную копию" in html_out


def test_generate_report_workdir_includes_trust_banner_and_cta(tmp_path):
    """Раздел 7, п.8 приёмочного чек-листа: баннер доверия должен быть одинаково соблюдён во
    всех трёх формах отчёта -- здесь level="workdir" ([2]/--dry-run), отдельно от target."""
    out_path = tmp_path / "report.html"
    run_stats = {"appended_images": 1, "appended_videos": 0}
    r.generate_report({}, str(out_path), level="workdir", run_stats=run_stats)
    html_out = out_path.read_text(encoding="utf-8")
    assert "Оригиналы не изменены и не удалены" in html_out
    assert "Реальных изменений на диске нет" in html_out
    assert "Нравится результат" in html_out


def test_generate_report_from_analyze_stats_includes_trust_banner(tmp_path):
    out_path = tmp_path / "report.html"
    r.generate_report_from_analyze_stats(_FakeAnalyzeStats(), str(out_path))
    html_out = out_path.read_text(encoding="utf-8")
    assert "Оригиналы не изменены и не удалены" in html_out
    # Раунд 33 (REVIEW-HANDOFF.md): _FakeAnalyzeStats имеет реальные годы/байты -- closing
    # CTA теперь новая "уязвимо/защищено" формулировка, не старое нейтральное "Нравится
    # результат?" (см. test_cta_block_analyze_* ниже для прямых тестов самой ветки).
    assert "хранятся на одном источнике" in html_out


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


def test_cluster_checklist_item_multi_folder_links_each_file_individually():
    """Живое обсуждение с пользователем (2026-08-01): вместо миниатюр (пункт C, отложен) --
    ссылка на файл/папку, где эти файлы лежат. Разные папки -- нет одной общей ссылки, но
    каждый файл в списке кликабелен по отдельности. verify_link=None -- fallback-ветка (нет
    "Полной сверки", level!="target"), старое построчное поведение сохраняется."""
    cluster = [r"C:\T\dst\ByDate\2024\2024-06\a.jpg", r"C:\T\dst\ByDate\2024\2024-07\b.jpg"]
    title, detail = r._cluster_checklist_item(cluster)
    assert '<a href="file:///C:/T/dst/ByDate/2024/2024-06/a.jpg" target="_blank" rel="noopener">' in detail
    assert '<a href="file:///C:/T/dst/ByDate/2024/2024-07/b.jpg" target="_blank" rel="noopener">' in detail


def test_cluster_checklist_item_multi_folder_with_verify_link_uses_summary_not_per_file_list():
    """Задача 6 (SESSION-HANDOFF.txt, пакет "боевой прогон D:\\"): живые примеры на боевом
    прогоне (32/19/13/8-кадровые кластеры) показали, что построчный список путей для случая
    "разные папки" читается непоследовательно рядом с компактной однопапочной веткой ниже --
    с verify_link построчный список убирается вообще, остаётся ссылка на полную сверку."""
    cluster = [r"C:\T\dst\ByDate\2024\2024-06\a.jpg", r"C:\T\dst\ByDate\2024\2024-07\b.jpg"]
    title, detail = r._cluster_checklist_item(cluster, verify_link=r.DEDUP_VERIFICATION_FILENAME)
    assert title == "Похожая серия из 2 кадров"
    assert "a.jpg" not in detail  # построчного списка путей больше нет
    assert "b.jpg" not in detail
    assert (f'<a href="{r.DEDUP_VERIFICATION_FILENAME}" target="_blank" rel="noopener">'
            "полная сверка похожих серий →</a>") in detail


def test_cluster_checklist_item_folder_is_a_clickable_file_link():
    """Пункт B.9 ("большой разбор report.html", SESSION-HANDOFF.txt): "Папка: ..." в разборе
    похожих серий -- file://-ссылка на реальную папку в TARGET, не только текст. Однопапочная
    ветка не меняется задачей 6 -- verify_link здесь не используется вообще."""
    cluster = [r"C:\T\dst\Albums\Отпуск\a.jpg", r"C:\T\dst\Albums\Отпуск\b.jpg"]
    title, detail = r._cluster_checklist_item(cluster, verify_link=r.DEDUP_VERIFICATION_FILENAME)
    assert '<a href="file:///C:/T/dst/Albums/Отпуск" target="_blank" rel="noopener">Albums\\Отпуск</a>' in detail


def test_render_near_dup_verification_section_lists_full_cluster_without_truncation():
    clusters = [[rf"C:\T\dst\Albums\A{i}\f.jpg" for i in range(7)]]  # 7 разных папок, >5
    html_out = r._render_near_dup_verification_section(clusters)
    assert "Похожая серия из 7 кадров" in html_out
    for i in range(7):
        assert f"A{i}\\f.jpg" in html_out  # ни один файл не обрезан "и ещё N"


def test_render_near_dup_verification_section_empty_returns_nothing():
    assert r._render_near_dup_verification_section([]) == ""


def test_render_near_dup_verification_section_excludes_single_folder_clusters():
    """REVIEW-HANDOFF.md, Раунд 52 (придирка 2): страница обещает "разные папки" (CHANGELOG.md,
    задача 6) -- однопапочный кластер уже показан полностью прямо в основном отчёте
    (компактная ветка _cluster_checklist_item()), дублировать его здесь не нужно."""
    single_folder = [[r"C:\T\dst\Albums\A\f1.jpg", r"C:\T\dst\Albums\A\f2.jpg"]]
    assert r._render_near_dup_verification_section(single_folder) == ""

    multi_folder = [[r"C:\T\dst\Albums\A\f1.jpg", r"C:\T\dst\Albums\B\f2.jpg"]]
    html_out = r._render_near_dup_verification_section(multi_folder)
    assert "Похожая серия из 2 кадров" in html_out


def test_render_dedup_verification_page_empty_when_only_single_folder_near_dup():
    """Тот же принцип, что и у секции выше, применённый к гейту всей страницы -- если из
    похожих серий есть только однопапочные (а точных дублей нет вовсе), страница не должна
    материализоваться как почти пустая (только "назад к отчёту")."""
    data = {
        "appended": [], "skipped": [],
        "near_dup_edges": [
            {"dest": r"C:\T\dst\Albums\A\f1.jpg", "matched_dest": r"C:\T\dst\Albums\A\f2.jpg"},
        ],
    }
    assert r._render_dedup_verification_page(data) == ""


def test_render_dedup_verification_page_includes_near_dup_clusters_without_exact_dups():
    """Страница должна строиться и без единого точного дубля, если есть хотя бы одна
    многопапочная похожая серия -- задача 6 полагается на то, что страница существует и в
    этом случае (иначе ссылка из _cluster_checklist_item() вела бы в никуда)."""
    data = {
        "appended": [], "skipped": [],
        "near_dup_edges": [
            {"dest": r"C:\T\dst\ByDate\2024\a.jpg", "matched_dest": r"C:\T\dst\Albums\B\b.jpg"},
        ],
    }
    html_out = r._render_dedup_verification_page(data)
    assert "Полная сверка похожих серий" in html_out
    assert "Полная сверка дублей" not in html_out
    assert "a.jpg" in html_out and "b.jpg" in html_out


def test_generate_report_target_level_near_dup_multi_folder_links_to_verification_page(tmp_path):
    """Сквозная проверка: похожая серия в разных папках на level="target" должна и ссылаться
    на "Полную сверку", и сама страница должна реально содержать её полный список."""
    data = {
        "appended": [
            {"timestamp": "2026-01-01 00:00:01", "source": "s0",
             "dest": r"C:\T\dst\ByDate\2024\2024-06\a.jpg", "reason": "appended_new", "flags": ""},
            {"timestamp": "2026-01-01 00:00:01", "source": "s1",
             "dest": r"C:\T\dst\ByDate\2024\2024-07\b.jpg", "reason": "appended_near_dup", "flags": ""},
        ],
        "skipped": [],
        "near_dup_edges": [
            {"timestamp": "2026-01-01 00:00:01", "dest": r"C:\T\dst\ByDate\2024\2024-07\b.jpg",
             "matched_dest": r"C:\T\dst\ByDate\2024\2024-06\a.jpg"},
        ],
    }
    out_path = tmp_path / "report.html"
    r.generate_report(data, str(out_path), level="target", run_start="2026-01-01 00:00:00")
    html_out = out_path.read_text(encoding="utf-8")
    assert "Похожая серия из 2 кадров" in html_out
    assert "полная сверка похожих серий →" in html_out
    assert "Кадры лежат в разных папках" in html_out
    verify_out = (tmp_path / r.DEDUP_VERIFICATION_FILENAME).read_text(encoding="utf-8")
    assert "Похожая серия из 2 кадров" in verify_out
    assert "2024-06\\a.jpg" in verify_out and "2024-07\\b.jpg" in verify_out


def test_render_exact_dup_examples_empty_renders_nothing():
    assert r._render_exact_dup_examples({"exact_dup_groups": []}, "Дубли") == ""
    assert r._render_exact_dup_examples(None, "Дубли") == ""


def test_render_exact_dup_examples_progressive_disclosure():
    """EXACT_DUP_PREVIEW_N=2 -- третья и далее группы уходят под "Показать ещё N", тот же
    паттерн, что near-dup уже использует в Листе 3."""
    data = {"appended": [], "skipped": []}
    for i in range(5):
        data["skipped"].append({"source": f"s{i}", "matched_with": rf"C:\T\dst\Albums\Альбом{i}\a.jpg",
                                 "reason": "already_present"})
    fields = {"exact_dup_groups": r._cluster_exact_dup_full(data)}
    html_out = r._render_exact_dup_examples(fields, "Дубли")
    assert html_out.count("<details>") == 1  # ровно один сворачиваемый блок на "остальное"
    assert "Показать ещё 3 группы" in html_out
    assert all(f"Альбом{i}" in html_out for i in range(5))  # ничего не потеряно, только скрыто


def test_render_exact_dup_examples_shows_origin_pattern_b3():
    """Пункт B.3 ("большой разбор report.html", SESSION-HANDOFF.txt): превью-карточка раньше
    не показывала origin ("скопировано из ‹origin›") вообще -- только имя+счётчик, теперь
    переиспользует _cluster_exact_dup_full() (та же форма, что полная "Сверка дублей")."""
    data = {
        "appended": [{"timestamp": "t", "source": r"F:\orig\a.jpg",
                      "dest": r"C:\T\dst\Albums\Отпуск\a.jpg", "reason": "appended_new", "flags": ""}],
        "skipped": [
            {"source": "s1", "matched_with": r"C:\T\dst\Albums\Отпуск\a.jpg", "reason": "already_present"},
            {"source": "s2", "matched_with": r"C:\T\dst\Albums\Отпуск\a.jpg", "reason": "already_present"},
        ],
    }
    fields = {"exact_dup_groups": r._cluster_exact_dup_full(data)}
    html_out = r._render_exact_dup_examples(fields, "Дубли — примеры", intro=r.EXACT_DUP_INTRO)
    assert "Отпуск" in html_out
    assert "a.jpg (×2) — скопировано из F:\\orig\\a.jpg" in html_out


def test_build_model_from_rows_includes_exact_dup_groups_via_generate_report(tmp_path):
    """Сквозная проверка через generate_report() (не только build_model_from_rows() напрямую)
    -- exact_dup_groups должен реально дойти до HTML, не только до промежуточного dict."""
    data = {
        "appended": [{"timestamp": "2026-01-01 00:00:01", "source": "s0",
                      "dest": r"C:\T\dst\Albums\Отпуск\z.jpg", "reason": "appended_new", "flags": ""}],
        "skipped": [
            {"timestamp": "2026-01-01 00:00:01", "source": "s1",
             "matched_with": r"C:\T\dst\Albums\Отпуск\a.jpg", "reason": "already_present"},
            {"timestamp": "2026-01-01 00:00:01", "source": "s2",
             "matched_with": r"C:\T\dst\Albums\Отпуск\a.jpg", "reason": "already_present"},
        ],
    }
    out_path = tmp_path / "report.html"
    r.generate_report(data, str(out_path), level="target")
    html_out = out_path.read_text(encoding="utf-8")
    assert "Дубли" in html_out
    assert "a.jpg (×2)" in html_out


def test_cluster_exact_dup_full_groups_by_folder_with_origin_and_full_source_list():
    """2026-07-26, обсуждение с пользователем: недоверчивый пользователь хочет путь+имя
    каждого файла, не число -- _cluster_exact_dup_full() против _cluster_exact_dup() выше
    (которая по-прежнему только считает) должна отдавать полный список source-путей и
    origin (откуда сам заархивированный файл был скопирован)."""
    data = {
        "appended": [
            {"timestamp": "t", "source": r"F:\orig\a.jpg",
             "dest": r"C:\T\dst\Albums\Отпуск\a.jpg", "reason": "appended_new", "flags": ""},
        ],
        "skipped": [
            {"source": "s1", "matched_with": r"C:\T\dst\Albums\Отпуск\a.jpg", "reason": "already_present"},
            {"source": "s2", "matched_with": r"C:\T\dst\Albums\Отпуск\a.jpg", "reason": "already_present"},
            {"source": "s3", "matched_with": r"C:\T\dst\ByDate\2020\2020-05\c.jpg", "reason": "already_present"},
            {"source": "s4", "matched_with": r"C:\T\dst\Albums\Отпуск\a.jpg", "reason": "identical_at_destination"},
        ],
    }
    groups = r._cluster_exact_dup_full(data)
    assert len(groups) == 2
    folder, items = groups[0]  # отсортировано по убыванию числа дублей -- Отпуск (2) первый
    assert folder == r"Albums\Отпуск"
    matched, origin, sources = items[0]
    assert matched == r"C:\T\dst\Albums\Отпуск\a.jpg"
    assert origin == r"F:\orig\a.jpg"
    assert sources == ["s1", "s2"]  # identical_at_destination (s4) исключён


def test_cluster_exact_dup_full_missing_origin_degrades_to_empty_string():
    """matched_with может не иметь строки в appended.csv (архив старше этой фичи логов, либо
    файл вне текущего окна ротации) -- origin тогда "", не KeyError/исключение."""
    data = {"appended": [], "skipped": [
        {"source": "s1", "matched_with": r"C:\T\dst\Albums\A\a.jpg", "reason": "already_present"},
    ]}
    groups = r._cluster_exact_dup_full(data)
    _, items = groups[0]
    assert items[0][1] == ""


def test_render_dedup_verification_page_groups_visually_by_folder():
    """По прямой просьбе пользователя -- не сплошной поток, а разделение по папкам альбома
    (отдельная карточка на папку)."""
    data = {
        "appended": [{"timestamp": "t", "source": r"F:\orig\a.jpg",
                      "dest": r"C:\T\dst\Albums\Отпуск\a.jpg", "reason": "appended_new", "flags": ""}],
        "skipped": [
            {"source": r"F:\dup\a_copy.jpg", "matched_with": r"C:\T\dst\Albums\Отпуск\a.jpg",
             "reason": "already_present"},
        ],
    }
    html_out = r._render_dedup_verification_page(data)
    assert html_out.count('<div class="card">') >= 2  # заголовочная карточка + минимум одна папка
    assert "Albums\\Отпуск" in html_out
    assert "скопировано из F:\\orig\\a.jpg" in html_out
    assert "F:\\dup\\a_copy.jpg" in html_out
    assert "отклонён" in html_out


def test_render_dedup_verification_page_lists_each_duplicate_on_its_own_line():
    """Пункт B.3 ("большой разбор report.html", SESSION-HANDOFF.txt): каждый путь-дубль с
    новой строки, визуально отделены -- не один сплошной comma-separated список."""
    data = {
        "appended": [{"timestamp": "t", "source": r"F:\orig\a.jpg",
                      "dest": r"C:\T\dst\Albums\Отпуск\a.jpg", "reason": "appended_new", "flags": ""}],
        "skipped": [
            {"source": r"F:\dup1\a.jpg", "matched_with": r"C:\T\dst\Albums\Отпуск\a.jpg",
             "reason": "already_present"},
            {"source": r"F:\dup2\a.jpg", "matched_with": r"C:\T\dst\Albums\Отпуск\a.jpg",
             "reason": "already_present"},
        ],
    }
    html_out = r._render_dedup_verification_page(data)
    assert "F:\\dup1\\a.jpg<br>F:\\dup2\\a.jpg" in html_out
    assert "F:\\dup1\\a.jpg, F:\\dup2\\a.jpg" not in html_out


def test_render_dedup_verification_page_empty_returns_nothing():
    assert r._render_dedup_verification_page({"appended": [], "skipped": []}) == ""


def test_generate_dedup_verification_page_writes_sibling_file_and_links_back(tmp_path):
    data = {
        "appended": [{"timestamp": "t", "source": r"F:\orig\a.jpg",
                      "dest": r"C:\T\dst\Albums\Отпуск\a.jpg", "reason": "appended_new", "flags": ""}],
        "skipped": [
            {"source": r"F:\dup\a_copy.jpg", "matched_with": r"C:\T\dst\Albums\Отпуск\a.jpg",
             "reason": "already_present"},
        ],
    }
    report_path = tmp_path / "report.html"
    link = r.generate_dedup_verification_page(data, str(report_path))
    assert link == r.DEDUP_VERIFICATION_FILENAME
    verify_path = tmp_path / r.DEDUP_VERIFICATION_FILENAME
    assert verify_path.exists()
    assert "Отпуск" in verify_path.read_text(encoding="utf-8")


def test_generate_dedup_verification_page_returns_none_without_exact_dups(tmp_path):
    report_path = tmp_path / "report.html"
    link = r.generate_dedup_verification_page({"appended": [], "skipped": []}, str(report_path))
    assert link is None
    assert not (tmp_path / r.DEDUP_VERIFICATION_FILENAME).exists()


def test_generate_report_target_level_links_to_dedup_verification_page(tmp_path):
    """Сквозная проверка -- generate_report(level="target") должен и написать соседний файл,
    и дать на него ссылку из основного отчёта; level="workdir" -- ни того, ни другого (файлы
    ещё не скопированы, сверять нечего). 2026-07-26: ссылка должна быть частью самой карточки
    "Дубли — примеры" (сразу под ней), не в хвосте всей страницы -- живая находка
    пользователя, что оторванная от карточки ссылка была необъяснима ("почему это примеры,
    если рядом полная информация")."""
    data = {
        "appended": [{"timestamp": "2026-01-01 00:00:01", "source": r"F:\orig\a.jpg",
                      "dest": r"C:\T\dst\Albums\Отпуск\a.jpg", "reason": "appended_new", "flags": ""}],
        "skipped": [
            {"timestamp": "2026-01-01 00:00:01", "source": r"F:\dup\a_copy.jpg",
             "matched_with": r"C:\T\dst\Albums\Отпуск\a.jpg", "reason": "already_present"},
        ],
    }
    out_path = tmp_path / "report.html"
    r.generate_report(data, str(out_path), level="target")
    html_out = out_path.read_text(encoding="utf-8")
    assert r.DEDUP_VERIFICATION_FILENAME in html_out
    assert "полная сверка построчно" in html_out
    assert (tmp_path / r.DEDUP_VERIFICATION_FILENAME).exists()
    # Ссылка должна идти СРАЗУ после карточки "Дубли", не в хвосте документа --
    # проверяем расстояние в тексте, не через следующую секцию (Лист 3 здесь пустой -- нет
    # near-dup/disputes/unreadable в этих минимальных данных, карточка вообще не рендерится).
    card_pos = html_out.index("Дубли — примеры")
    link_pos = html_out.index("полная сверка построчно")
    assert card_pos < link_pos < card_pos + 600

    out_path2 = tmp_path / "workdir_report.html"
    r.generate_report(data, str(out_path2), level="workdir")
    assert r.DEDUP_VERIFICATION_FILENAME not in out_path2.read_text(encoding="utf-8")


def test_cluster_disputes_groups_by_folder_with_reason():
    rows = [
        {"source": r"C:\S\Отпуск\icon.svg", "reason": "icon_or_svg"},
        {"source": r"C:\S\Отпуск\tiny.jpg", "reason": "tiny_image"},
        {"source": r"C:\S\Свадьба\bad.mp4", "reason": "unreadable_video"},
    ]
    groups = r._cluster_disputes(rows)
    assert len(groups) == 2
    folder, items = groups[0]  # Отпуск (2 файла) первый -- отсортировано по убыванию размера
    assert folder == r"C:\S\Отпуск"
    assert items == [("icon.svg", "icon_or_svg"), ("tiny.jpg", "tiny_image")]


def test_dispute_reason_label_translates_known_codes_and_passes_through_unknown():
    assert r._dispute_reason_label("tiny_image") == "слишком маленькое изображение"
    assert r._dispute_reason_label("some_future_code") == "some_future_code"


def test_build_checklist_items_shows_dispute_file_and_reason_when_detail_available():
    """Раунд 32, задача 2 (REVIEW-HANDOFF.md): "Спорные" должны показывать имя файла и причину,
    не только число на папку -- тот же паттерн, что уже есть у near-dup/exact-dup."""
    fields = {
        "near_dup_clusters": [], "exact_dup_groups": [],
        "disputes_total": 1, "disputes_by_folder": Counter({r"C:\S\Отпуск": 1}),
        "disputes_detail": [(r"C:\S\Отпуск", [("icon.svg", "icon_or_svg")])],
        "dates_review_total": 0, "dates_review_by_folder": Counter(), "dates_review_bc_total": 0,
        "undated_total": 0, "quality_flags": Counter(), "unreadable": [],
    }
    items = r._build_checklist_items(fields)
    joined = "".join(items)
    assert "icon.svg" in joined
    assert "похоже на иконку/SVG" in joined
    # Пункт B.9: папка-источник -- file://-ссылка (folder тут уже абсолютный source-путь,
    # см. _win_dirname(row["source"]) в _cluster_disputes()).
    assert '<a href="file:///C:/S/Отпуск" target="_blank" rel="noopener">Отпуск</a>' in joined


def test_file_link_or_text_only_links_absolute_windows_paths():
    """_file_link_or_text() -- относительный путь (level=="analyze", origin_display без
    корня SOURCE) остаётся обычным текстом, ссылка вела бы в никуда."""
    assert r._file_link_or_text("X", r"D:\Archive\Album") == \
        '<a href="file:///D:/Archive/Album" target="_blank" rel="noopener">X</a>'
    assert r._file_link_or_text("X", "Users/HTPC/Desktop") == "X"
    assert r._file_link_or_text("X", None) == "X"
    assert r._file_link_or_text("X", "") == "X"


def test_build_checklist_items_falls_back_to_folder_counts_without_dispute_detail():
    """analyze-уровень: disputes_detail отсутствует (AnalyzeStats не отслеживает source/reason
    на файл) -- старое поведение (только числа по папкам) должно сохраниться, не падать."""
    fields = {
        "near_dup_clusters": [], "exact_dup_groups": [],
        "disputes_total": 4, "disputes_by_folder": Counter({r"C:\S\Отпуск": 4}),
        "dates_review_total": 0, "dates_review_by_folder": Counter(), "dates_review_bc_total": 0,
        "undated_total": 0, "quality_flags": Counter(), "unreadable": [],
    }
    items = r._build_checklist_items(fields)
    joined = "".join(items)
    assert "4 файла не удалось однозначно распознать" in joined
    assert "Сгруппированы по исходной папке" in joined


def test_unsorted_link_is_a_file_url_when_target_path_given():
    """Пункт B.5 ("большой разбор report.html", SESSION-HANDOFF.txt): адрес спорных всегда
    один и тот же (_Unsorted) -- безусловная file://-ссылка, тот же паттерн, что CTA-блок."""
    link = r._unsorted_link(r"D:\__PhotoArchive__")
    assert '<a href="file:///D:/__PhotoArchive__/_Unsorted" target="_blank" rel="noopener">_Unsorted</a>' == link
    assert r._unsorted_link(None) == "_Unsorted"


def test_build_checklist_items_dispute_detail_links_unsorted_with_target_path():
    fields = {
        "near_dup_clusters": [], "exact_dup_groups": [],
        "disputes_total": 1, "disputes_by_folder": Counter({r"C:\S\Отпуск": 1}),
        "disputes_detail": [(r"C:\S\Отпуск", [("icon.svg", "icon_or_svg")])],
        "dates_review_total": 0, "dates_review_by_folder": Counter(), "dates_review_bc_total": 0,
        "undated_total": 0, "quality_flags": Counter(), "unreadable": [],
    }
    joined = "".join(r._build_checklist_items(fields, target_path=r"D:\__PhotoArchive__"))
    assert '<a href="file:///D:/__PhotoArchive__/_Unsorted" target="_blank" rel="noopener">_Unsorted</a>' in joined


def test_generate_report_intro_says_all_files_saved_including_disputed(tmp_path):
    """Пункт B.5: "сохранены ВСЕ N файлов, включая M спорных" -- не намекает, что со спорными
    что-то не так/потеряно, в отличие от голого счётчика без контекста."""
    data = {
        "appended": [_appended_row(f"D:\\T\\ByDate\\2026\\2026-01\\a{i}.jpg") for i in range(5)],
        "disputes": [{"timestamp": "2026-01-01 09:00:00", "source": "s.gif",
                       "dest": r"D:\T\_Unsorted\s.gif", "reason": "animated_gif", "was_hidden": ""}],
    }
    out_path = tmp_path / "report.html"
    r.generate_report(data, str(out_path), level="target", run_start="2026-01-01 00:00:00")
    html_out = out_path.read_text(encoding="utf-8")
    assert "Сохранены ВСЕ 6 файлов, включая 1 спорный" in html_out


def test_render_this_run_explains_disputed_reasons_when_present():
    """Пункт B.6: "Спорные — N файлов" раньше оставалось голым числом без объяснения ПОЧЕМУ."""
    run_stats = {"appended_images": 1, "appended_videos": 0, "disputed": 2}
    html_out = r._render_this_run(run_stats, level="target")
    assert "похожие на иконку" in html_out
    assert "не потеряны" in html_out


def test_render_this_run_no_dispute_explanation_when_none():
    run_stats = {"appended_images": 5, "appended_videos": 0}
    html_out = r._render_this_run(run_stats, level="target")
    assert "не потеряны" not in html_out


def test_cluster_dates_review_groups_by_archive_folder_with_tier():
    """2026-07-26, по просьбе пользователя (общий аудит "путь для проверки"): группировка по
    dest (папка АРХИВА), не source -- в отличие от disputes (_Unsorted зеркалирует source).

    2026-08-02, прямое замечание пользователя: точность даты определяет место файла только
    внутри ByDate (см. RULES.md, блок UNDATED) -- Albums/ файлы отфильтрованы (тот же принцип,
    что уже применён к Tier D, см. test_undated_checklist_item_hides_albums_files_entirely).

    Пункт B.9 ("большой разбор report.html"): folder теперь АБСОЛЮТНЫЙ путь (не friendly-
    усечённый) -- нужен, чтобы построить рабочую file://-ссылку на реальную папку в TARGET,
    friendly-текст для показа строится отдельно на стороне рендера
    (_dates_review_checklist_item(), см. соответствующий тест)."""
    rows = [
        {"source": "s1", "dest": r"C:\T\dst\Albums\Отпуск\a.jpg", "tier": "B"},
        {"source": "s2", "dest": r"C:\T\dst\Albums\Отпуск\b.jpg", "tier": "C"},
        {"source": "s3", "dest": r"C:\T\dst\ByDate\2020\2020-05\c.jpg", "tier": "B"},
        # tier A/D исключены -- не "приблизительная", разные категории:
        {"source": "s4", "dest": r"C:\T\dst\Albums\Отпуск\d.jpg", "tier": "A"},
    ]
    groups = r._cluster_dates_review(rows)
    assert len(groups) == 1  # Albums-группа отфильтрована, осталась только ByDate
    folder, items = groups[0]
    assert folder == r"C:\T\dst\ByDate\2020\2020-05"
    assert items == [("c.jpg", "B")]

    title, detail = r._dates_review_checklist_item(groups[0])
    assert "ByDate\\2020\\2020-05" in detail


def test_cluster_dates_review_hides_albums_files_entirely():
    """2026-08-02, прямое замечание пользователя: если файл уже лёг в альбом, точность его
    даты не влияет на место -- "стоит перепроверить" для такого файла бесполезно, тот же
    довод, что уже привёл к test_undated_checklist_item_hides_albums_files_entirely у Tier D."""
    data = {"dates_review": [
        {"source": "s1", "dest": r"C:\T\dst\Albums\Отпуск\a.jpg", "tier": "C"},
    ]}
    fields = r._build_checklist_fields(data)
    items = r._build_checklist_items(fields)
    joined = "".join(items)
    assert "получили дату приблизительно" not in joined


def test_build_checklist_items_shows_dates_review_file_and_tier_when_detail_available():
    fields = {
        "near_dup_clusters": [], "exact_dup_groups": [],
        "disputes_total": 0, "disputes_by_folder": Counter(),
        "dates_review_by_folder": Counter({r"Albums\Отпуск": 1}), "dates_review_bc_total": 1,
        "dates_review_detail": [(r"Albums\Отпуск", [("a.jpg", "B")])],
        "undated_total": 0, "quality_flags": Counter(), "unreadable": [],
    }
    items = r._build_checklist_items(fields)
    joined = "".join(items)
    assert "a.jpg" in joined
    assert "высокая уверенность" in joined
    assert "Albums\\Отпуск" in joined


def test_build_checklist_items_falls_back_to_folder_counts_without_dates_review_detail():
    """analyze-уровень: dates_review_detail отсутствует -- старое поведение (числа по папкам)
    должно сохраниться, не падать."""
    fields = {
        "near_dup_clusters": [], "exact_dup_groups": [],
        "disputes_total": 0, "disputes_by_folder": Counter(),
        "dates_review_by_folder": Counter({r"C:\S\Отпуск": 3}), "dates_review_bc_total": 3,
        "undated_total": 0, "quality_flags": Counter(), "unreadable": [],
    }
    items = r._build_checklist_items(fields)
    joined = "".join(items)
    assert "3 файла получили дату приблизительно" in joined
    assert "Папки-источники" in joined


def test_sheet2_tier_chart_caption_excludes_raw():
    """Раунд 32, задача 1 (REVIEW-HANDOFF.md): RAW не участвует в tier-расчёте
    (dated_media_count = counts["image"]+counts["video"]) -- заголовок диаграммы должен
    честно называть, что она не про весь архив."""
    rows = [
        {"timestamp": "t", "source": "s1", "dest": r"C:\T\dst\ByDate\2020\2020-05\a.jpg",
         "reason": "appended_new", "flags": ""},
    ]
    model = r.build_model_from_rows({"appended": rows})
    html_out = r._render_sheet2(model)
    assert "Надёжность дат — фото и видео, без RAW" in html_out


def test_near_dup_checklist_has_optional_disclaimer():
    """Раунд 32, задача 5: одна фраза-разграничитель на весь блок near-dup, не сказано, что
    выбор необязателен -- есть только когда реально есть кластеры."""
    fields_with = {"near_dup_clusters": [["a", "b"]], "exact_dup_groups": [],
                    "disputes_total": 0, "disputes_by_folder": Counter(),
                    "dates_review_by_folder": Counter(), "dates_review_bc_total": 0,
                    "undated_total": 0, "quality_flags": Counter(), "unreadable": []}
    joined = "".join(r._build_checklist_items(fields_with))
    assert "выбор необязателен" in joined.lower()

    fields_without = dict(fields_with, near_dup_clusters=[])
    joined_without = "".join(r._build_checklist_items(fields_without))
    assert "необязателен" not in joined_without.lower()


def test_cta_block_mentions_old_media_only_at_target_level():
    """Раунд 32, задача 6: совет "не спешите избавляться от старых носителей" -- только после
    реальной сборки (level=="target"), не в workdir/analyze-превью, где архива ещё нет."""
    target_html = r._render_cta_block("target")
    assert "торопиться" in target_html or "не спешите" in target_html.lower()

    workdir_html = r._render_cta_block("workdir")
    assert "торопиться" not in workdir_html and "не спешите" not in workdir_html.lower()


def test_cta_block_analyze_no_model_falls_back_to_generic_text():
    """Раунд 33: старые вызовы без model (level=="analyze") не должны падать -- откат на
    прежний нейтральный текст."""
    html_out = r._render_cta_block("analyze")
    assert "Нравится результат" in html_out


def test_cta_block_analyze_with_archives_uses_vulnerable_protected_framing():
    """Раунд 33 (REVIEW-HANDOFF.md): при найденных внутри источника архивах (zip/rar) --
    рамка "уязвимо/защищено", не нейтральная "можно запустить сборку". Уменьшенная версия
    (только внутри одного источника, не "M мест" по нескольким прогонам)."""
    model = {"years": Counter({2015: 2, 2020: 1}), "archives_with_media": 3, "total_bytes": 1000}
    html_out = r._render_cta_block("analyze", model=model)
    assert "3 отдельных архивах" in html_out
    assert "испортиться независимо" in html_out
    assert "Нравится результат" not in html_out


def test_cta_block_analyze_with_archives_and_no_loose_files_omits_scatter_claim():
    # SESSION-HANDOFF.txt п.4 (2026-08-05, боевой прогон): раньше "не только россыпью"
    # утверждалось безусловно при archives_found>0 -- источник может состоять ЦЕЛИКОМ из
    # архивов (files_by_location без root/folder), тогда "россыпи" вообще нет.
    model = {
        "years": Counter({2020: 1}), "archives_with_media": 2, "total_bytes": 1000,
        "files_by_location": Counter({"archive": 50}),
    }
    html_out = r._render_cta_block("analyze", model=model)
    assert "россыпью" not in html_out
    assert "2 отдельных архивах" in html_out


def test_cta_block_analyze_with_archives_and_loose_files_keeps_scatter_claim():
    model = {
        "years": Counter({2020: 1}), "archives_with_media": 2, "total_bytes": 1000,
        "files_by_location": Counter({"root": 5, "archive": 50}),
    }
    html_out = r._render_cta_block("analyze", model=model)
    assert "не только россыпью" in html_out


def test_cta_block_analyze_without_archives_uses_single_source_framing():
    model = {"years": Counter({2020: 1}), "archives_with_media": 0, "total_bytes": 500 * 1024**2}
    html_out = r._render_cta_block("analyze", model=model)
    assert "хранятся на одном источнике" in html_out
    assert "500 МБ" in html_out
    assert "отдельных архивах" not in html_out


def test_cta_block_analyze_archives_found_without_media_omits_archive_claim():
    """Живой боевой прогон 2026-08-06: источник (папка веб-проекта) содержал 108 архивов
    (archives_found, RAW-счётчик, считает любой статус archive_*, в т.ч. archive_no_media/
    battered/encrypted), но НИ ОДИН не дал ни одного медиафайла (все 237 файлов -- в папках,
    files_by_location без "archive"). CTA раньше писал "108 отдельных архивов ... сборка
    соберёт всё это", хотя собирать было нечего -- вводило в заблуждение. archives_found
    (raw) в модели намеренно большой, archives_with_media=0 -- функция должна ориентироваться
    именно на последнее."""
    model = {
        "years": Counter({2020: 1}), "archives_found": 108, "archives_with_media": 0,
        "total_bytes": 500 * 1024**2, "files_by_location": Counter({"folder": 237}),
    }
    html_out = r._render_cta_block("analyze", model=model)
    assert "отдельных архивах" not in html_out
    assert "испортиться независимо" not in html_out
    assert "хранятся на одном источнике" in html_out


def test_cta_block_analyze_empty_model_falls_back_to_generic_text():
    html_out = r._render_cta_block(
        "analyze", model={"years": Counter(), "archives_with_media": 0, "total_bytes": 0})
    assert "Нравится результат" in html_out


def test_cta_block_analyze_shows_album_date_grouping_line():
    """SESSION-HANDOFF.txt, 2026-08-07 (группировка альбом/дата в analyze-отчёте): агрегат
    "найдено XX папок-альбомов с YY медиафайлами и ZZ обычных папок... с QQ файлами"."""
    model = {
        "years": Counter({2020: 1}), "archives_with_media": 0, "total_bytes": 0,
        "n_albums_detected": 2, "n_media_in_albums": 30,
        "n_regular_folders": 3, "n_media_by_date": 7,
    }
    html_out = r._render_cta_block("analyze", model=model)
    assert "Найдено 2 папки-альбома с 30 медиафайлами" in html_out
    assert "3 обычные папки, которые разложатся по дате, с 7 файлами" in html_out


def test_cta_block_analyze_album_date_grouping_singular_folder_agreement():
    """Ровно 1 обычная папка -- согласование "которая разложится", не "которые разложатся"."""
    model = {
        "years": Counter(), "archives_with_media": 0, "total_bytes": 0,
        "n_albums_detected": 0, "n_media_in_albums": 0,
        "n_regular_folders": 1, "n_media_by_date": 4,
    }
    html_out = r._render_cta_block("analyze", model=model)
    assert "1 обычная папка, которая разложится по дате" in html_out
    assert "которые разложатся" not in html_out


def test_cta_block_analyze_omits_album_date_grouping_line_when_both_zero():
    model = {"years": Counter(), "archives_with_media": 0, "total_bytes": 0}
    html_out = r._render_cta_block("analyze", model=model)
    assert "папок-альбомов" not in html_out
    assert "разложатся по дате" not in html_out


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
        self.total_bytes = 54321
        self.predicted_unique_bytes = 12345
        # SESSION-HANDOFF.txt п.4 -- общее число объектов + разбивка файлов по месту.
        self.n_objects_total = 0
        self.files_by_location = Counter()
        self.bytes_by_location = Counter()
        self.dates_by_year = Counter({2026: 3})
        self.dates_by_year_month = Counter({"2026-07": 3})
        self.tier_counts = Counter({"C": 2, "D": 1})
        self.near_dup_edges = []
        # Задачи 3/4, речь пользователя 2026-08-02: exact_dup_edges (тот же union-find граф,
        # что near_dup_edges, для точных дублей) + n_tier_cd_bydate (подмножество tier C/D,
        # НЕ лежащее в Albums -- см. _render_passport_integrity()).
        self.exact_dup_edges = []
        self.n_tier_cd_bydate = 3
        self.n_archives_found = 0
        self.n_archives_with_media = 0
        self.found_archive_top_level = []
        # SESSION-HANDOFF.txt п.9: "Объём по категориям" -- пусто здесь (n_images/n_raw/
        # n_videos выше тоже не имеют байтовых партнёров в этом фейке).
        self.bytes_by_kind = Counter()
        # "analyze" (не "analyze-quick") -- near_dup/exact_dup/predicted_unique_count выше уже
        # заполнены как настоящий полный проход (Паспорт архива), см. п.8: is_analyze_quick
        # должен остаться False, иначе "Итог решений программы" скрылась бы, ломая существующие
        # тесты на decisions ниже.
        self.mode = "analyze"
        # generate_passport_report()/_render_passport_integrity() -- не читаются
        # build_model_from_analyze_stats(), нужны только паспорту (см. test_passport ниже).
        self.total_files = self.n_images + self.n_raw + self.n_videos
        self.n_signature_mismatch = 0
        self.n_dump_items = 0
        self.n_albums_detected = 1
        # SESSION-HANDOFF.txt, 2026-08-07 (группировка альбом/дата) -- YY/ZZ/QQ, см.
        # AnalyzeStats.n_media_in_albums/bydate_media_by_folder/n_media_by_date.
        self.n_media_in_albums = 0
        self.bydate_media_by_folder = Counter()
        self.n_media_by_date = 0
        self.cities = Counter()
        # Пункт E -- "Топ камер/устройств съёмки".
        self.cameras = Counter()
        # generate_passport_report() -- дерево структуры архива (_render_archive_tree_card()).
        self.tree_folder_counts = Counter()
        self.tree_folder_bytes = Counter()
        # Пункт B.2 -- полные пути запароленных архивов.
        self.encrypted_archive_paths = []


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
    # Пакет п.2: total_bytes -- объём просканированного SOURCE (stats.total_bytes), не
    # predicted_unique_bytes ("что добавилось бы после дедупа", 0 для analyze-quick/[1]).
    assert model["total_bytes"] == 54321


def test_build_model_from_analyze_stats_uses_stats_bytes_by_kind():
    # SESSION-HANDOFF.txt п.9 (2026-08-05, боевой прогон): раньше bytes_by_kind в модели всегда
    # был пустым Counter() -- "Объём по категориям" никогда не рендерилась для analyze-отчётов.
    stats = _FakeAnalyzeStats()
    stats.bytes_by_kind = Counter({"image": 1000, "raw": 2000, "video": 3000})
    model = r.build_model_from_analyze_stats(stats)
    assert model["bytes_by_kind"] == Counter({"image": 1000, "raw": 2000, "video": 3000})


def test_build_model_from_analyze_stats_wires_album_date_grouping_fields():
    """SESSION-HANDOFF.txt, 2026-08-07 (группировка альбом/дата): n_regular_folders в модели --
    len() Counter'а bydate_media_by_folder, не сам Counter -- ZZ считает РАЗНЫЕ папки."""
    stats = _FakeAnalyzeStats()
    stats.n_albums_detected = 4
    stats.n_media_in_albums = 40
    stats.bydate_media_by_folder = Counter({"a": 2, "b": 5})
    stats.n_media_by_date = 7
    model = r.build_model_from_analyze_stats(stats)
    assert model["n_albums_detected"] == 4
    assert model["n_media_in_albums"] == 40
    assert model["n_regular_folders"] == 2
    assert model["n_media_by_date"] == 7


def test_render_sheet2_hides_decisions_pie_for_analyze_quick_mode():
    # SESSION-HANDOFF.txt п.8: CLI `analyze` (внутренний mode=="analyze-quick") никогда не
    # делает дедуп-проход -- decisions всегда вырождена (всё 0, кроме "Не прочитано"),
    # показывать её как честную диаграмму означало бы соврать про "100% дублей/новых файлов".
    stats = _FakeAnalyzeStats()
    stats.mode = "analyze-quick"
    stats.n_near_dupes = 0
    stats.predicted_unique_count = 0
    stats.n_exact_dupes = 0
    stats.n_broken_or_zero = 2  # unreadable ненулевой -- общий механизм "пустая скрывается сама" не сработал бы
    model = r.build_model_from_analyze_stats(stats)
    html_out = r._render_sheet2(model)
    assert "Итог решений программы" not in html_out


def test_render_sheet2_shows_decisions_pie_for_full_analyze_mode():
    # Паспорт архива (self-scan, внутренний mode=="analyze" буквально) -- дедуп-проход реально
    # идёт, диаграмма остаётся осмысленной и должна рендериться как раньше.
    stats = _FakeAnalyzeStats()
    stats.mode = "analyze"
    model = r.build_model_from_analyze_stats(stats)
    html_out = r._render_sheet2(model)
    assert "Итог решений программы" in html_out


def test_render_analyze_recommendations_empty_model_returns_nothing():
    """Все источники данных пусты/нулевые (например analyze-quick с совсем короткой
    историей) -- секция не должна рендериться вообще, не пустая карточка."""
    html_out = r._render_analyze_recommendations({
        "total_bytes": 0, "years": Counter(), "found_archive_paths": [],
        "near_dup_clusters": [], "tier_counts": Counter(),
    })
    assert html_out == ""


def test_render_analyze_recommendations_no_longer_duplicates_approx_dates():
    """Живой боевой прогон 2026-08-06: "Что стоит проверить" (152, Tier B+C) и "Рекомендации"
    (73, Tier C) показывали два разных числа под почти одинаковой формулировкой "приблизительно"
    -- не баг подсчёта (79 Tier B + 73 Tier C = 152), но вводит в заблуждение при чтении рядом.
    По решению пользователя -- убрать дублирующий пункт из "Рекомендаций" целиком (tier_counts
    уже полностью покрыт "Что стоит проверить"/_build_checklist_items), а не просто
    переформулировать. tier_counts=Counter({"C": 7}) с пустыми остальными полями -- секция не
    должна рендериться вообще (тот же случай, что test_render_analyze_recommendations_empty_
    model_returns_nothing, но явно проверяет именно tier_counts, не просто "все поля нулевые")."""
    html_out = r._render_analyze_recommendations({
        "total_bytes": 5 * 1024**3, "years": Counter(), "found_archive_paths": [],
        "near_dup_clusters": [], "tier_counts": Counter({"C": 7}),
    })
    assert html_out == ""


def test_render_analyze_recommendations_shows_year_gap_found_archive_and_near_dup_series():
    model = {
        "total_bytes": 0,
        "years": Counter({2016: 40, 2017: 40, 2018: 1, 2019: 40, 2020: 40}),
        "found_archive_paths": [r"D:\Old\PhotoArchive"],
        "near_dup_clusters": [["a", "b"], ["c", "d", "e"]],
        "tier_counts": Counter(),
    }
    html_out = r._render_analyze_recommendations(model)
    assert "2018" in html_out
    assert "уже есть собранный архив" in html_out
    assert "2 серии похожих кадров" in html_out


def test_generate_report_from_analyze_stats_includes_recommendations_section(tmp_path):
    stats = _FakeAnalyzeStats()
    stats.found_archive_top_level = [r"D:\Old\PhotoArchive"]
    stats.near_dup_edges = [{"dest": "a", "matched_dest": "b"}]
    out_path = tmp_path / "report.html"
    r.generate_report_from_analyze_stats(stats, str(out_path))
    text = out_path.read_text(encoding="utf-8")
    assert "Рекомендации" in text
    assert "уже есть собранный архив" in text


def test_year_hbar_chart_single_year_bar_spans_full_width():
    """Горизонтальная форма (в отличие от прежней вертикальной, см. 2026-07-21 finding выше в
    истории) не имеет отдельного gap-based расчёта ширины столбца -- при n==1 бар естественно
    занимает всю доступную ширину (value == max_v), это ожидаемо, не визуальный баг."""
    svg = r._svg_year_hbar_chart(Counter({2026: 10}))
    m = re.search(r'<rect[^>]*width="([\d.]+)"', svg)
    assert m is not None
    assert float(m.group(1)) > 400  # почти вся plot_w (680 - 54 - 68 = 558), не крошечный бар


def test_render_sheet1_analyze_level_does_not_claim_an_archive_exists():
    """2026-07-21 finding, confirmed still present 2026-07-24: _render_sheet1() always said
    "Ваш архив"/"с учётом только что добавленного в этом пополнении" even for analyze/
    analyze-quick/analyze-full, where nothing has been archived yet -- just a SOURCE scan.
    level="analyze" must use scan wording instead."""
    model = r.build_model_from_rows({"appended": [_appended_row(r"D:\T\ByDate\2026\2026-01-01 [PhotoArchive]\a.jpg")],
                                      "skipped": [], "disputes": [], "dates_review": [],
                                      "unreadable": [], "near_dup_edges": []})
    target_html = r._render_sheet1(model, "target")
    analyze_html = r._render_sheet1(model, "analyze")
    assert "Ваш архив" in target_html and "пополнении" in target_html
    assert "Ваш архив" not in analyze_html and "пополнении" not in analyze_html
    assert "Что нашлось на этом диске" in analyze_html


def test_render_sheet1_analyze_shows_object_count_tile():
    # SESSION-HANDOFF.txt п.4 (2026-08-05, боевой прогон): плитка "объектов (папок и архивов)"
    # -- только для analyze (build_model_from_rows() для реальной сборки её не считает вовсе).
    stats = _FakeAnalyzeStats()
    stats.n_objects_total = 42
    model = r.build_model_from_analyze_stats(stats)
    html_out = r._render_sheet1(model, "analyze")
    assert "42" in html_out
    assert "объектов" in html_out


def test_render_sheet1_target_level_omits_object_count_tile():
    model = r.build_model_from_rows({"appended": [_appended_row(r"C:\T\dst\Albums\A\a.jpg")]})
    html_out = r._render_sheet1(model, "target")
    assert "объектов (папок и архивов)" not in html_out


def test_render_sheet1_analyze_shows_files_by_location_breakdown():
    stats = _FakeAnalyzeStats()
    stats.files_by_location = Counter({"root": 5, "folder": 10, "archive": 3})
    # Разбор накопления п.3а (2026-08-05, боевой прогон): объём рядом со штуками.
    stats.bytes_by_location = Counter({"root": 1 * 1024**3, "folder": 2 * 1024**3,
                                        "archive": 512 * 1024**2})
    model = r.build_model_from_analyze_stats(stats)
    html_out = r._render_sheet1(model, "analyze")
    assert "5 файлов в корне источника (1.0 ГБ)" in html_out
    assert "10 файлов в папках (2.0 ГБ)" in html_out
    assert "3 файла внутри архивов (512 МБ)" in html_out


def test_render_sheet1_analyze_omits_breakdown_when_single_location():
    # Один-единственный непустой бакет -- разбивка была бы бесполезным повтором общего числа.
    stats = _FakeAnalyzeStats()
    stats.files_by_location = Counter({"folder": 10})
    model = r.build_model_from_analyze_stats(stats)
    html_out = r._render_sheet1(model, "analyze")
    assert "Из них" not in html_out


def test_render_sheet3_single_analyze_has_no_stale_stub():
    """The unconditional "Эта часть рекомендаций дорабатывается" stub for level=="analyze"
    predated the "analyze as 2 parts" redesign and stayed stale after it -- _build_checklist_items
    already returns real items for analyze today, so the stub should be gone entirely."""
    model = r.build_model_from_rows({"appended": [], "skipped": [], "disputes": [],
                                      "dates_review": [], "unreadable": [{"source": "x.jpg"}],
                                      "near_dup_edges": []})
    html_out = r._render_sheet3_single(model, "analyze")
    assert "дорабатывается" not in html_out


def _dated_appended_row(dest, timestamp):
    return {"timestamp": timestamp, "source": dest, "dest": dest, "reason": "appended_new", "flags": ""}


def test_generate_report_workdir_default_stays_minimal(tmp_path):
    """REVIEW-HANDOFF.md, Раунд 38: CLI --dry-run (full_workdir по умолчанию False) должен
    остаться на старом урезанном рендере, даже если данные технически содержат и историю, и
    run_start -- та самая защита от фантомных повторных --dry-run записей, ради которой
    full_workdir существует как отдельный явный флаг."""
    data = {
        "appended": [
            _dated_appended_row(r"C:\T\ByDate\2019\2019-05 [PhotoArchive]\old.jpg", "2019-05-01 00:00:00"),
            _dated_appended_row(r"C:\T\ByDate\2026\2026-01 [PhotoArchive]\new.jpg", "2026-01-02 00:00:00"),
        ],
        "skipped": [], "disputes": [], "dates_review": [], "unreadable": [], "near_dup_edges": [],
    }
    out_path = tmp_path / "report.html"
    r.generate_report(data, str(out_path), level="workdir", run_start="2026-01-01 00:00:00")
    html_out = out_path.read_text(encoding="utf-8")
    assert "Ваш архив" not in html_out
    assert "Что стоит проверить" in html_out or html_out  # старый рендер не падает


def test_generate_report_workdir_full_workdir_differs_from_default_minimal(tmp_path):
    """full_workdir=True (интерактивный [2] на непустом Target, REVIEW-HANDOFF.md, Раунд 38) --
    2026-07-31, по прямой просьбе пользователя: кумулятивная "Ваш архив" здесь БОЛЬШЕ НЕ
    рендерится (убрана вместе с тем же разделом у обычного level=="target", см.
    _generate_from_model()) -- но full_workdir всё ещё отличается от обычного --dry-run:
    вместо одного нераздельного "Что стоит проверить" (_render_sheet3_single) идёт та же
    "Новое в этом пополнении"-подача, что у настоящей сборки (_render_recommendations)."""
    data = {
        "appended": [
            _dated_appended_row(r"C:\T\ByDate\2019\2019-05 [PhotoArchive]\old.jpg", "2019-05-01 00:00:00"),
            _dated_appended_row(r"C:\T\ByDate\2026\2026-01 [PhotoArchive]\new.jpg", "2026-01-02 00:00:00"),
        ],
        "skipped": [], "disputes": [], "dates_review": [],
        # Лист 3/"Новое в этом пополнении" -- чек-лист ПРОБЛЕМ (near-dup/disputed/unreadable),
        # не просто фактов -- пустой список "unreadable" дал бы пустую карточку (_render_
        # checklist_card() возвращает "" без items), нужна хотя бы одна находка после run_start.
        "unreadable": [{"source": r"C:\S\bad.jpg", "timestamp": "2026-01-02 00:00:00"}],
        "near_dup_edges": [],
    }
    out_path = tmp_path / "report.html"
    r.generate_report(data, str(out_path), level="workdir", run_start="2026-01-01 00:00:00",
                       full_workdir=True)
    html_out = out_path.read_text(encoding="utf-8")
    assert "Ваш архив" not in html_out
    assert "Новое в этом пополнении" in html_out


def test_generate_report_target_omits_cumulative_archive_history(tmp_path):
    """SESSION-HANDOFF.txt, 2026-07-31, по прямой просьбе пользователя: обычный отчёт после
    [3]/CLI archive (level="target", run_start передан -- реальный путь продакшн-кода, см.
    photosort_win.py:_finalize_target_report()) больше не показывает кумулятивную "Ваш
    архив"/диаграммы/"накопилось до этого пополнения" -- только результат ЭТОГО прогона, с
    отсылкой к отдельному «Паспорту архива» за полной картиной (см. _render_cta_block())."""
    data = {
        "appended": [
            _dated_appended_row(r"C:\T\ByDate\2019\2019-05 [PhotoArchive]\old.jpg", "2019-05-01 00:00:00"),
            _dated_appended_row(r"C:\T\ByDate\2026\2026-01 [PhotoArchive]\new.jpg", "2026-01-02 00:00:00"),
        ],
        "skipped": [], "disputes": [], "dates_review": [],
        "unreadable": [{"source": r"C:\S\bad.jpg", "timestamp": "2026-01-02 00:00:00"}],
        "near_dup_edges": [],
    }
    out_path = tmp_path / "report.html"
    r.generate_report(data, str(out_path), level="target", run_start="2026-01-01 00:00:00",
                       target_path=r"D:\PhotoArchive")
    html_out = out_path.read_text(encoding="utf-8")
    assert "Ваш архив" not in html_out
    assert "Накопилось до этого пополнения" not in html_out
    assert "2019" not in html_out  # старая история архива больше не рендерится вообще
    assert "Новое в этом пополнении" in html_out
    assert "Паспорт архива" in html_out  # совет запустить его отдельно, в CTA-блоке


def test_generate_passport_report_clean_archive_shows_no_problems_explicitly(tmp_path):
    """SESSION-HANDOFF.txt, design-сессия 2026-07-31: паспорт показывает "проблем нет" так же
    явно, как "проблема есть" -- в отличие от остального report.html (пустая категория
    скрывается целиком), здесь ВСЕГДА восемь пунктов (пункт 8 -- глубокая вложенность
    альбомов, живое обсуждение с пользователем 2026-08-01), независимо от того, нашлось
    что-то или нет."""
    stats = _FakeAnalyzeStats()
    stats.n_near_dupes = 0
    stats.tier_counts = Counter()
    stats.n_tier_cd_bydate = 0  # держим согласованным с tier_counts (см. задачу 3, report.py)
    out_path = tmp_path / "passport.html"
    r.generate_passport_report(stats, str(out_path), target_path=r"D:\__PhotoArchive__")
    html_out = out_path.read_text(encoding="utf-8")
    assert "Дублей внутри архива нет." in html_out
    assert "Похожих кадров/возможных кропов не найдено." in html_out
    assert "Повреждённых или пустых файлов нет." in html_out
    assert "У всех файлов расширение совпадает с содержимым." in html_out
    assert "Посторонних архивов (zip/rar) внутри не осталось." in html_out
    assert "Все файлы лежат внутри признанных альбомов/дат." in html_out
    assert "У всех файлов есть точная или приблизительная дата съёмки." in html_out
    assert "Глубоко вложенных альбомов нет." in html_out
    assert 'class="attn"' not in html_out
    assert html_out.count('class="ok"') == 8
    assert r"D:\__PhotoArchive__" in html_out


def test_generate_passport_report_flags_problems_explicitly(tmp_path):
    """Задачи 4/5, речь пользователя 2026-08-02: "Дублей внутри архива нет"/"Похожих кадров не
    найдено" теперь строятся из exact_dup_edges/near_dup_edges (union-find кластеры), не из
    голых счётчиков n_exact_dupes/n_near_dupes (те остаются в AnalyzeStats, но
    _render_passport_integrity() их больше не читает напрямую).

    REVIEW-HANDOFF.md, Раунд 57 [ЗАМЕЧАНИЕ], закрыто 2026-08-03: 1 ребро в exact_dup_edges/
    near_dup_edges -- это ОДНА группа из 2 файлов (оригинал + 1 лишняя копия), значит РЕАЛЬНО
    лишних копий -- 1, не 2. Числа ниже раньше (до фикса) были "2"/"2" -- эта же формулировка
    буквально закрепляла завышенный счёт как ожидаемое поведение, не как баг (см. докстринг
    _render_passport_dup_li() в report.py)."""
    stats = _FakeAnalyzeStats()
    stats.exact_dup_edges = [{"dest": "Albums/A/1.jpg", "matched_dest": "Albums/A/2.jpg"}]
    stats.near_dup_edges = [{"dest": "ByDate/2024/2024-01/x.jpg",
                              "matched_dest": "ByDate/2024/2024-01/y.jpg"}]
    stats.n_broken_or_zero = 1
    stats.n_signature_mismatch = 1
    stats.n_archives_found = 1
    stats.n_dump_items = 4
    stats.tier_counts = Counter()
    stats.n_tier_cd_bydate = 0  # держим согласованным с tier_counts (см. задачу 3, report.py)
    out_path = tmp_path / "passport.html"
    r.generate_passport_report(stats, str(out_path))
    html_out = out_path.read_text(encoding="utf-8")
    assert "1 дубль внутри архива" in html_out
    assert "не удаляйте лишние копии вручную" in html_out.lower()
    assert "1 похожий кадр сохранено рядом с оригиналом" in html_out
    assert "1 файл повреждён или пуст (0 байт)." in html_out
    assert "У 1 файла расширение не совпадает" in html_out
    assert "найден 1 архив" in html_out
    assert "4 файла лежат не внутри" in html_out
    assert html_out.count('class="attn"') == 6


def test_passport_dup_count_subtracts_group_count_not_total_files(tmp_path):
    """REVIEW-HANDOFF.md, Раунд 57 [ЗАМЕЧАНИЕ]: второй пример ревизора -- ДВЕ лишних ручных
    копии одного и того же файла (3 экземпляра итого) должны дать "2 дубля", не "3". Три файла
    в одном union-find кластере (A-B, A-C -- обе копии смэтчены против одного оригинала) --
    T=3, G=1 группа -> T-G=2, ровно stats.n_exact_dupes в этом сценарии."""
    stats = _FakeAnalyzeStats()
    stats.exact_dup_edges = [
        {"dest": "Albums/A/orig.jpg", "matched_dest": "Albums/A/copy1.jpg"},
        {"dest": "Albums/A/orig.jpg", "matched_dest": "Albums/A/copy2.jpg"},
    ]
    stats.tier_counts = Counter()
    stats.n_tier_cd_bydate = 0
    html_out = r._render_passport_integrity(stats)
    assert "2 дубля внутри архива" in html_out
    assert "3 дубля" not in html_out


def test_deep_nested_albums_finds_max_depth_per_album_above_threshold():
    """Живое обсуждение с пользователем (2026-08-01): "вложенность больше 2" -- число
    ПОДпапок внутри альбома (Albums/Альбом/A/B -- 2, не триггерит; .../A/B/C -- 3,
    триггерит), максимум по всем бакетам одного альбома."""
    counts = Counter({
        "Albums/Свадьба": 5,
        "Albums/Свадьба/Фотограф1/День1/Утро": 2,  # глубина 3 -- триггерит
        "Albums/Отпуск/Пляж": 1,  # глубина 1 -- не триггерит
        "ByDate/2024/2024-07 [PhotoArchive]": 3,  # не Albums -- игнорируется
    })
    deep = r._deep_nested_albums(counts)
    assert deep == [("Свадьба", 3)]


def test_deep_nested_albums_empty_when_nothing_deep():
    assert r._deep_nested_albums(Counter({"Albums/Отпуск": 5, "Albums/Отпуск/Пляж": 1})) == []
    assert r._deep_nested_albums(Counter()) == []


class TestPassportVerificationPage:
    """REVIEW-HANDOFF.md, Раунд 57: generate_passport_verification_page()/
    passport_verification.html не имели вообще ни одного регресс-теста, несмотря на заявленные
    в af50df1 "+4 новых теста" (`grep -c "def test_" tests/test_report.py` не менялся до/после
    того коммита)."""

    def test_writes_page_and_returns_link_for_multi_folder_exact_dup_group(self, tmp_path):
        stats = _FakeAnalyzeStats()
        stats.exact_dup_edges = [{"dest": "Albums/A/orig.jpg", "matched_dest": "Albums/B/copy.jpg"}]
        out_path = tmp_path / "passport.html"
        link = r.generate_passport_verification_page(stats, str(out_path))
        assert link == r.PASSPORT_VERIFICATION_FILENAME
        verify_path = tmp_path / r.PASSPORT_VERIFICATION_FILENAME
        assert verify_path.exists()
        html_out = verify_path.read_text(encoding="utf-8")
        assert "Полная сверка" in html_out
        assert "Точные дубли из 2 файлов" in html_out
        assert "orig.jpg" in html_out and "copy.jpg" in html_out
        assert "← назад к паспорту" in html_out

    def test_returns_none_and_writes_nothing_for_single_folder_group(self, tmp_path):
        # Both files in the SAME folder -- already shown in full by the "Целостность архива"
        # preview itself (no truncation for a single-folder group), the separate page would be
        # pure noise -- same filter as the equivalent regular-run verification page.
        stats = _FakeAnalyzeStats()
        stats.exact_dup_edges = [{"dest": "Albums/A/orig.jpg", "matched_dest": "Albums/A/copy.jpg"}]
        out_path = tmp_path / "passport.html"
        link = r.generate_passport_verification_page(stats, str(out_path))
        assert link is None
        assert not (tmp_path / r.PASSPORT_VERIFICATION_FILENAME).exists()

    def test_returns_none_and_writes_nothing_when_no_duplicates_at_all(self, tmp_path):
        stats = _FakeAnalyzeStats()
        out_path = tmp_path / "passport.html"
        link = r.generate_passport_verification_page(stats, str(out_path))
        assert link is None
        assert not (tmp_path / r.PASSPORT_VERIFICATION_FILENAME).exists()

    def test_multi_folder_near_dup_group_also_gets_its_own_section(self, tmp_path):
        stats = _FakeAnalyzeStats()
        stats.near_dup_edges = [{"dest": "ByDate/2024/2024-01/a.jpg",
                                  "matched_dest": "Albums/Отпуск/b.jpg"}]
        out_path = tmp_path / "passport.html"
        link = r.generate_passport_verification_page(stats, str(out_path))
        assert link == r.PASSPORT_VERIFICATION_FILENAME
        html_out = (tmp_path / r.PASSPORT_VERIFICATION_FILENAME).read_text(encoding="utf-8")
        assert "Похожая серия из 2 файлов" in html_out


def test_generate_passport_report_flags_deep_nested_album(tmp_path):
    stats = _FakeAnalyzeStats()
    stats.tier_counts = Counter()
    stats.n_tier_cd_bydate = 0  # держим согласованным с tier_counts (см. задачу 3, report.py)
    stats.tree_folder_counts = Counter({"Albums/Свадьба/Фотограф1/День1/Утро": 2})
    out_path = tmp_path / "passport.html"
    r.generate_passport_report(stats, str(out_path))
    html_out = out_path.read_text(encoding="utf-8")
    assert "«Свадьба» (3 подпапки)" in html_out
    assert "стоит перенести глубокие подпапки на верхний уровень" in html_out
    # 7-й пункт (дата) остался "ok" -- tier_counts обнулён отдельно от остальных находок
    assert '<li class="ok">У всех файлов есть точная или приблизительная дата' in html_out


def test_deep_nested_albums_list_truncates_with_and_more_count():
    """REVIEW-HANDOFF.md, Раунд 46, замечание 2: при >TOP_N число расходилось со списком без
    оговорки -- тот же паттерн "и ещё N", что B.2 (запароленные архивы)."""
    counts = Counter({f"Albums/Альбом{i:02d}/A/B/C": 1 for i in range(15)})
    stats = _FakeAnalyzeStats()
    stats.tier_counts = Counter()
    stats.n_tier_cd_bydate = 0  # держим согласованным с tier_counts (см. задачу 3, report.py)
    stats.tree_folder_counts = counts
    html_out = r._render_passport_integrity(stats)
    assert "15 альбомов имеют глубокую вложенность" in html_out
    assert " и ещё 5" in html_out
    # ровно TOP_N (10) имён перечислены явно, не все 15:
    assert html_out.count("подпапки)") == 10


def test_generate_passport_report_includes_years_chart(tmp_path):
    stats = _FakeAnalyzeStats()
    stats.dates_by_year = Counter({2019: 2, 2024: 5})
    out_path = tmp_path / "passport.html"
    r.generate_passport_report(stats, str(out_path))
    html_out = out_path.read_text(encoding="utf-8")
    assert "Медиафайлы по годам" in html_out
    assert "<svg" in html_out


def test_generate_passport_report_oldest_file_is_a_clickable_link(tmp_path):
    """Пункт B.8: в паспорте oldest_display -- путь ОТНОСИТЕЛЬНО TARGET (self_scan), нужен
    target_path, чтобы собрать реальный абсолютный путь для file://-ссылки."""
    import datetime
    stats = _FakeAnalyzeStats()
    stats.oldest_date = datetime.datetime(2015, 7, 1)
    stats.oldest_display = "Albums/Отпуск/photo.jpg"
    out_path = tmp_path / "passport.html"
    r.generate_passport_report(stats, str(out_path), target_path=r"D:\__PhotoArchive__")
    html_out = out_path.read_text(encoding="utf-8")
    assert '<a href="file:///D:/__PhotoArchive__/Albums/Отпуск/photo.jpg" target="_blank" rel="noopener">' in html_out


def test_generate_passport_report_oldest_file_plain_text_without_target_path(tmp_path):
    import datetime
    stats = _FakeAnalyzeStats()
    stats.oldest_date = datetime.datetime(2015, 7, 1)
    stats.oldest_display = "Albums/Отпуск/photo.jpg"
    out_path = tmp_path / "passport.html"
    r.generate_passport_report(stats, str(out_path))
    html_out = out_path.read_text(encoding="utf-8")
    assert "photo.jpg" in html_out
    assert "<a href=" not in html_out.split("Самый старый файл")[1][:200]


def test_split_home_and_foreign_parses_city_cc_format():
    """Пункт D ("большой разбор report.html", SESSION-HANDOFF.txt): place_for_gps() отдаёт
    "Город" для домашней страны, "Город, CC" для остального мира -- разбор по наличию ", "."""
    cities = Counter({"Москва": 3, "Казань": 1, "Paris, FR": 2, "Tokyo, JP": 1, "Nice, FR": 1})
    home, foreign = r._split_home_and_foreign(cities)
    assert home == Counter({"Москва": 3, "Казань": 1})
    # Paris+Nice -- обе Франция, суммируются в одну запись по стране:
    assert foreign == Counter({"Франция": 3, "Япония": 1})


def test_split_home_and_foreign_empty():
    assert r._split_home_and_foreign(Counter()) == (Counter(), Counter())


def test_country_name_ru_falls_back_to_code_for_unknown():
    assert r._country_name_ru("FR") == "Франция"
    assert r._country_name_ru("XX") == "XX"  # неизвестный/нестандартный код -- как есть


def test_geo_hbar_shows_both_groups_with_labels():
    cities = Counter({"Москва": 3, "Paris, FR": 2})
    html_out = r._geo_hbar(cities)
    assert "По вашим местам" in html_out
    assert "Остальной мир" in html_out
    assert "Москва" in html_out
    assert "Франция" in html_out


def test_geo_hbar_omits_missing_group():
    html_out = r._geo_hbar(Counter({"Москва": 3}))
    assert "По вашим местам" in html_out
    assert "Остальной мир" not in html_out


def test_geo_hbar_empty_returns_nothing():
    assert r._geo_hbar(Counter()) == ""


def test_render_sheet1_no_longer_shows_geography_block():
    """Пункт D: блок географии убран из шапки (Sheet1) -- переехал на Лист 2."""
    rows = [{"timestamp": "t", "source": "s1", "dest": r"C:\T\dst\Albums\A\a.jpg",
             "reason": "appended_new", "flags": "", "date": "2026-01-01", "place": "Москва"}]
    model = r.build_model_from_rows({"appended": rows})
    assert model["cities"] == Counter({"Москва": 1})
    html_out = r._render_sheet1(model)
    assert "География" not in html_out
    assert "city-list" not in html_out


def test_render_sheet2_shows_geography_as_horizontal_bars_not_pie():
    """Пункт D: круговая диаграмма географии на Листе 2 заменена на горизонтальные столбики,
    без общего "прочие"-сектора."""
    model = r.build_model_from_rows({"appended": [
        {"timestamp": "t", "source": "s1", "dest": r"C:\T\dst\Albums\A\a.jpg",
         "reason": "appended_new", "flags": "", "date": "2026-01-01", "place": "Москва"},
        {"timestamp": "t", "source": "s2", "dest": r"C:\T\dst\Albums\A\b.jpg",
         "reason": "appended_new", "flags": "", "date": "2026-01-01", "place": "Paris, FR"},
    ]})
    html_out = r._render_sheet2(model)
    assert "География" in html_out
    assert "Москва" in html_out
    assert "Франция" in html_out
    assert "Остальные места" not in html_out  # старая круговая "прочие"-подпись


def test_build_model_from_rows_aggregates_cameras_excludes_unknown():
    """Пункт E ("большой разбор report.html", SESSION-HANDOFF.txt): "camera" -- новая колонка
    appended.csv, файлы без камеры (пустая строка) не попадают в агрегат вообще."""
    rows = [
        {"timestamp": "t", "source": "s1", "dest": r"C:\T\dst\Albums\A\a.jpg",
         "reason": "appended_new", "flags": "", "camera": "Canon EOS 80D"},
        {"timestamp": "t", "source": "s2", "dest": r"C:\T\dst\Albums\A\b.jpg",
         "reason": "appended_new", "flags": "", "camera": "Canon EOS 80D"},
        {"timestamp": "t", "source": "s3", "dest": r"C:\T\dst\Albums\A\c.jpg",
         "reason": "appended_new", "flags": "", "camera": "iPhone 14"},
        {"timestamp": "t", "source": "s4", "dest": r"C:\T\dst\Albums\A\d.jpg",
         "reason": "appended_new", "flags": "", "camera": ""},
    ]
    model = r.build_model_from_rows({"appended": rows})
    assert model["cameras"] == Counter({"Canon EOS 80D": 2, "iPhone 14": 1})


def test_top_cameras_chart_hidden_below_minimum_distinct_cameras():
    """Пункт E: "одного-двух пунктов" -- меньше _MIN_DISTINCT_CAMERAS разных камер, диаграмма
    не рендерится совсем, даже если файлов с этими камерами много."""
    assert r._top_cameras_chart(Counter({"Canon EOS 80D": 500})) == ""
    assert r._top_cameras_chart(Counter({"Canon EOS 80D": 500, "iPhone 14": 3})) == ""
    assert r._top_cameras_chart(Counter()) == ""


def test_top_cameras_chart_shows_top_n_when_enough_distinct_cameras():
    cameras = Counter({"Canon EOS 80D": 50, "iPhone 14": 20, "Nikon D850": 5})
    html_out = r._top_cameras_chart(cameras)
    assert "Canon EOS 80D" in html_out
    assert "iPhone 14" in html_out
    assert "Nikon D850" in html_out
    assert "<svg" in html_out


def test_render_sheet2_shows_top_cameras_card_when_enough_data():
    model = r.build_model_from_rows({"appended": [
        {"timestamp": "t", "source": f"s{i}", "dest": rf"C:\T\dst\Albums\A\{i}.jpg",
         "reason": "appended_new", "flags": "", "camera": cam}
        for i, cam in enumerate(["Canon EOS 80D", "iPhone 14", "Nikon D850"])
    ]})
    html_out = r._render_sheet2(model)
    assert "Топ камер/устройств съёмки" in html_out
    assert "Canon EOS 80D" in html_out


def test_render_sheet2_omits_top_cameras_card_without_enough_data():
    model = r.build_model_from_rows({"appended": [
        {"timestamp": "t", "source": "s1", "dest": r"C:\T\dst\Albums\A\a.jpg",
         "reason": "appended_new", "flags": "", "camera": ""},
    ]})
    html_out = r._render_sheet2(model)
    assert "Топ камер/устройств съёмки" not in html_out


def test_generate_passport_report_includes_geography_when_cities_present(tmp_path):
    """2026-07-31: "География" исчезла из обычного report.html вместе с убранным Sheet2
    (кумулятивное "Ваш архив") -- place_for_gps() данные считались, но нигде не показывались.
    run_analyze() (и Паспорт архива через него) теперь сам резолвит GPS -> место
    (AnalyzeStats.cities), _render_geo_card() -- отдельная карточка, не через _render_sheet2()."""
    stats = _FakeAnalyzeStats()
    stats.cities = Counter({"Москва": 3, "Санкт-Петербург": 1})
    out_path = tmp_path / "passport.html"
    r.generate_passport_report(stats, str(out_path))
    html_out = out_path.read_text(encoding="utf-8")
    assert "География" in html_out
    assert "Москва" in html_out


def test_generate_passport_report_omits_geography_when_no_cities():
    stats = _FakeAnalyzeStats()
    assert stats.cities == Counter()
    assert r._render_geo_card(stats.cities) == ""


def test_build_archive_tree_nests_by_slash_and_keeps_own_stats_unsummed():
    """SESSION-HANDOFF.txt, "большой разбор report.html", пункт A -- "объём каждой папки БЕЗ
    вложенных" дословно из ТЗ: узел показывает только свои own-файлы, промежуточные узлы
    (здесь "ByDate", "2024") получают own=(0, 0), даже когда у них есть содержимое глубже."""
    counts = Counter({"Albums/Свадьба": 2, "ByDate/2024/2024-07 [PhotoArchive]": 5})
    byte_counts = Counter({"Albums/Свадьба": 2000, "ByDate/2024/2024-07 [PhotoArchive]": 5000})
    tree = r._build_archive_tree(counts, byte_counts)
    albums = tree["children"]["Albums"]
    assert albums["own"] == (0, 0)  # "Albums" сам по себе -- не бакет, только контейнер
    assert albums["children"]["Свадьба"]["own"] == (2, 2000)
    bydate_2024 = tree["children"]["ByDate"]["children"]["2024"]
    assert bydate_2024["own"] == (0, 0)
    assert bydate_2024["children"]["2024-07 [PhotoArchive]"]["own"] == (5, 5000)


def test_render_archive_tree_card_empty_counter_renders_nothing():
    assert r._render_archive_tree_card(Counter(), Counter()) == ""


def test_render_archive_tree_card_orders_top_level_albums_bydate_raw_unsorted():
    """_TREE_TOP_ORDER -- фиксированный порядок верхнего уровня (структура архива всегда одна
    и та же), не алфавитный (иначе "RAW" оказался бы перед "ByDate")."""
    counts = Counter({"_Unsorted": 1, "RAW": 1, "ByDate/2024/2024-07 [PhotoArchive]": 1, "Albums/A": 1})
    byte_counts = Counter({k: 100 for k in counts})
    html_out = r._render_archive_tree_card(counts, byte_counts)
    assert "Структура архива" in html_out
    positions = [html_out.index(f'>{name}<') for name in ("Albums", "ByDate", "RAW", "_Unsorted")]
    assert positions == sorted(positions)


def test_generate_passport_report_includes_tree_when_data_present(tmp_path):
    stats = _FakeAnalyzeStats()
    stats.tree_folder_counts = Counter({"Albums/Свадьба": 3, "ByDate/2024/2024-07 [PhotoArchive]": 2})
    stats.tree_folder_bytes = Counter({"Albums/Свадьба": 3000, "ByDate/2024/2024-07 [PhotoArchive]": 2000})
    out_path = tmp_path / "passport.html"
    r.generate_passport_report(stats, str(out_path))
    html_out = out_path.read_text(encoding="utf-8")
    assert "Структура архива" in html_out
    assert "Свадьба" in html_out
    assert "2024-07 [PhotoArchive]" in html_out


def test_generate_passport_report_omits_tree_when_empty(tmp_path):
    stats = _FakeAnalyzeStats()
    assert stats.tree_folder_counts == Counter()
    out_path = tmp_path / "passport.html"
    r.generate_passport_report(stats, str(out_path))
    html_out = out_path.read_text(encoding="utf-8")
    assert "Структура архива" not in html_out


def test_split_rows_by_time_returns_only_new_rows():
    """REVIEW-HANDOFF.md, Раунд 44: раньше возвращала (new, before) -- "before" (всё старше
    run_start) не читалась ни одним вызывающим кодом с 2026-07-31 (729a2de, убрано
    кумулятивное "Ваш архив"), вычислялась впустую на каждом обычном отчёте. Теперь строит и
    возвращает ТОЛЬКО отобранное "новое" -- прямой dict, не пара."""
    data = {
        "appended": [
            {"timestamp": "2026-01-01 09:00:00", "dest": "old.jpg"},
            {"timestamp": "2026-01-01 11:00:00", "dest": "new.jpg"},
        ],
    }
    result = r._split_rows_by_time(data, run_start="2026-01-01 10:00:00")
    assert isinstance(result, dict)
    assert [row["dest"] for row in result["appended"]] == ["new.jpg"]
    # категории, отсутствующие в data вовсе -- пустой список, не KeyError.
    assert result["disputes"] == []


def test_page_shell_includes_brand_header_with_logo_and_landing_link():
    """2026-08-06, согласовано с пользователем: логотип PhotoArchive + ссылка на лендинг в
    шапке каждой страницы отчёта (_page_shell(), общая для report.html/passport.html/сверок
    дублей) -- регрессионный тест, чтобы случайная правка _page_shell() не потеряла шапку."""
    doc = r._page_shell("Тест", "<div class=\"card\"><h1>Тест</h1></div>")
    assert 'class="report-brand"' in doc
    assert '<svg role="img" aria-label="PhotoArchive"' in doc
    assert f'href="{r._LANDING_URL}"' in doc
    assert r._LANDING_URL_DISPLAY in doc
    # ссылка на лендинг встречается дважды -- на самом лого (клик) и на видимом тексте URL
    # (клик/копирование выделением, без JS -- см. докстринг _render_brand_header()).
    assert doc.count(f'href="{r._LANDING_URL}"') == 2
