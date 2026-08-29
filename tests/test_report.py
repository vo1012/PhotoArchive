"""build_model_from_rows() / build_model_from_analyze_stats() / _cluster_near_dup() /
_parse_bydate_segment() -- pure aggregation logic in report.py, no filesystem/HTML rendering.

REVIEW-HANDOFF.md, раунд 3 [ЗАМЕЧАНИЕ]: report.py не имел ни одного автотеста. Первый тест
ниже -- прямой regression на найденный тем же раундом [БЛОКЕР] (Tier D задваивался в Tier A)."""
import re
from collections import Counter
from datetime import datetime

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
    блок UNDATED). 2026-08-09 (задача 5): пункт объединён с Tier B/C -- заголовок и
    разбивка теперь общие на весь чек-лист, не отдельный "вообще без даты"."""
    data = {"undated_media": [
        {"timestamp": "2026-01-01 00:00:00", "source": "F:\\a.jpg",
         "dest": "D:\\Archive\\ByDate\\0000-undated\\Отпуск\\a.jpg"},
    ]}
    fields = r._build_checklist_fields(data)
    items = r._build_checklist_items(fields)
    joined = "".join(items)
    assert "1 файл — дата определена неточно или не определена вовсе" in joined
    assert "дата не определилась вовсе" in joined
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
    assert "дата определена неточно" not in joined


def test_undated_checklist_item_groups_and_previews_like_tier_bc():
    """Задача 5: тот же <details>-паттерн "превью 2 + Показать ещё N папок", что уже
    применён к Tier B/C -- живой репорт пользователя: 274 файла сплошным абзацем без
    группировки читались нечитаемо. 2026-08-09: заголовок группы теперь общий для B/C/D
    ("с неточной или отсутствующей датой"), не отдельный "вообще без даты"."""
    data = {"undated_media": [
        {"timestamp": "2026-01-01 00:00:00", "source": f"F:\\a{i}.jpg",
         "dest": rf"D:\Archive\ByDate\0000-undated\Папка{i % 3}\a{i}.jpg"}
        for i in range(9)
    ]}
    fields = r._build_checklist_fields(data)
    items = r._build_checklist_items(fields)
    joined = "".join(items)
    # Каждая из 3 папок -- своя строка "N файлов с неточной или отсутствующей датой" (тот же
    # паттерн, что у Tier B/C -- заголовок на группу, не один общий агрегат на всю категорию).
    assert joined.count("файла с неточной или отсутствующей датой") == 3
    assert "<details>" in joined
    assert "Показать ещё 1 папку" in joined  # 3 папки всего, превью 2, "ещё" -- 1


def test_date_issues_checklist_item_degrades_without_rows():
    """analyze-уровень не отслеживает undated_media/dates_review поштучно (только агрегат в
    model) -- fields.get() должен деградировать до сводки без списка файлов, не упасть с
    KeyError. Задача 5 (2026-08-09): date_issues_detail отсутствует на этом уровне
    (build_model_from_analyze_stats не добавляет этот ключ вовсе) -- del здесь имитирует
    именно это, не просто пустой список."""
    fields = r._build_checklist_fields({})
    fields["date_issues_d_total"] = 3
    del fields["date_issues_detail"]
    items = r._build_checklist_items(fields)
    joined = "".join(items)
    assert "3 файла — дата определена неточно или не определена вовсе" in joined
    assert "дата не определилась вовсе" in joined
    assert "Папка:" not in joined


def test_date_issues_checklist_wording_matches_analyze_vs_target_level():
    """Живая находка пользователя (2026-08-09): "файл всё равно сохранён" (ед. число,
    прошедшее время) было корректно только для TARGET-уровня (реальная сборка уже прошла) --
    на analyze-уровне (ничего ещё не записано на диск) та же фраза вводила в заблуждение.
    date_issues_detail -- уже существующий сигнал analyze/target (см. соседний тест выше и
    докстринг _build_checklist_items()). Заодно проверяет разбивку по тирам построчно
    (<br> между пунктами), не одной строкой через "; "."""
    # analyze-уровень: date_issues_detail отсутствует вовсе (см. соседний тест выше).
    analyze_fields = r._build_checklist_fields({})
    analyze_fields["date_issues_b_total"] = 2
    analyze_fields["date_issues_d_total"] = 1
    del analyze_fields["date_issues_detail"]
    analyze_joined = "".join(r._build_checklist_items(analyze_fields))
    assert "эти файлы всё равно будут сохранены в архиве корректно" in analyze_joined
    assert "уже сохранены" not in analyze_joined
    assert "они попадут" in analyze_joined  # мн. число, не "он попадёт"

    # TARGET-уровень: реальные строки логов -- date_issues_detail заполнен.
    target_data = {"undated_media": [
        {"timestamp": "2026-01-01 00:00:00", "source": "F:\\a.jpg",
         "dest": "D:\\Archive\\ByDate\\0000-undated\\Отпуск\\a.jpg"},
    ]}
    target_fields = r._build_checklist_fields(target_data)
    target_joined = "".join(r._build_checklist_items(target_fields))
    assert "эти файлы уже сохранены в архиве корректно" in target_joined
    assert "будут сохранены" not in target_joined

    # Разбивка -- построчно (<br>), не "; " одной строкой.
    assert "Разбивка:<br>" in analyze_joined
    assert "; " not in analyze_joined.split("Разбивка:")[1].split("</div>")[0]


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


def test_render_this_run_shows_meta_line_with_paths_and_date():
    """Речь пользователя, 2026-08-09: раньше "Пробный прогон"/"Пополнение архива" НЕ получали
    дату вовсе (прежнее решение задачи 10 явно исключало этот заголовок) -- пользователь прямо
    отменил это. Теперь source_paths/target_path/generated_at рендерятся тем же
    _render_report_meta(), что и у "Что нашлось в источнике"."""
    run_stats = {"appended_images": 5, "appended_videos": 0}
    html_out = r._render_this_run(run_stats, level="workdir", generated_at="2026-08-09 21:00",
                                   source_paths=["C:\\Pictures", "D:\\Photos"],
                                   target_path="E:\\__PhotoArchive__")
    assert "<h2>Пробный прогон</h2>" in html_out
    assert "Источники:" in html_out
    assert "C:\\Pictures<br>D:\\Photos" in html_out
    assert "Архив: E:\\__PhotoArchive__" in html_out
    assert 'report-meta-date">по состоянию на 2026-08-09 21:00' in html_out


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
    """Раунд 32, задача 4 (REVIEW-HANDOFF.md): "обработано в источнике" -- база для
    сверки, что отчёт ничего не потерял молча (та же цифра, что терминал печатает как
    "Обработано: N файлов" -- речь пользователя, 2026-08-16, отличает от "найдено"
    analyze-отчёта). Тот же тайл теперь ПЕРВЫЙ в ряду (2026-08-16), не предпоследний."""
    run_stats = {"appended_images": 5, "appended_videos": 0, "processed_count": 42}
    html_out = r._render_this_run(run_stats, level="target")
    assert "обработано в источнике" in html_out
    assert ">42<" in html_out


def test_render_this_run_processed_count_label_same_in_preview():
    """Речь пользователя, 2026-08-16: сканирование/хеширование/решение по каждому элементу в
    dry-run УЖЕ реально произошло -- preview не должен превращать лейбл в "было бы обработано"
    (в отличие от "добавлено"/"объём", где разница реальная -- физической записи в TARGET
    нет)."""
    run_stats = {"appended_images": 5, "appended_videos": 0, "processed_count": 42}
    html_out = r._render_this_run(run_stats, level="workdir")
    assert "обработано в источнике" in html_out
    assert "было бы обработано" not in html_out


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


def test_render_this_run_new_files_tile_excludes_near_dup_matches_chart_number():
    """Пакет B п.9 (SESSION-HANDOFF.txt, живая находка при чтении реального report.html):
    тайл "N новых файлов добавлено" раньше включал near-dup (appended_near_dup/better/crop),
    а диаграмма ниже ("Новые файлы — в архиве") их исключала -- два разных числа без
    объяснения разницы, хотя математически согласованы. Тайл теперь показывает СТРОГО новое
    число (без near-dup) -- совпадает с диаграммой; near-dup уходит в отдельную сноску."""
    run_stats = {
        "appended_images": 5, "appended_videos": 0,
        "appended_near_dup": 2, "near_dup_image": 2,
    }
    html_out = r._render_this_run(run_stats, level="target")
    # 5 - 2 = 3, не 5 -- тайл и диаграмма читают одно и то же число.
    assert '<div class="value">3</div>' in html_out
    assert '<div class="value">5</div>' not in html_out
    assert "Новые файлы — в архиве — 3 файла" in html_out
    assert "2 похожих кадра сохранены отдельно, см. диаграмму ниже" in html_out


def test_render_this_run_new_files_tile_no_footnote_without_near_dup():
    """Без near-dup вообще -- сноска не появляется (нечего пояснять)."""
    run_stats = {"appended_images": 5, "appended_videos": 0}
    html_out = r._render_this_run(run_stats, level="target")
    assert "сохранены отдельно, см. диаграмму ниже" not in html_out


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
    assert "Дубли — не копировались — 2 файла" in html_out
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
    assert "Дубли — не копировались бы" in html_out
    assert "Спорные — были бы сохранены отдельно, не в архиве (_Unsorted)" in html_out
    assert ("Итого: 6 файлов легло бы физически (новые + похожие + спорные), "
            "3 файла не было бы скопировано (дубли + не прочитано).") in html_out


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


def test_generate_workdir_report_wires_structure_recommendations(tmp_path):
    """2026-08-16, речь пользователя: карточка "Рекомендации" про облачную синхронизацию
    (снятая с рендера 2026-08-14 вместе с отдельной веткой level=="workdir", см. историю у
    _generate_from_model()) снова подключена -- но только в предпросмотре (dry-run/[2] до
    сборки), не после реальной сборки (см. следующий тест) -- совет "переименуйте и запустите
    снова" не имеет смысла постфактум (дедуп не переносит уже скопированные файлы)."""
    run_stats = {"appended_images": 1, "appended_videos": 0,
                 "album_profiles": {"src/Отпуск": _cloudlike_profile()}}
    out_path = tmp_path / "report.html"
    r.generate_report({}, str(out_path), level="workdir", run_stats=run_stats,
                       run_start="2026-01-01 00:00:00")
    html_out = out_path.read_text(encoding="utf-8")
    assert "похож на папку облачной синхронизации" in html_out


def test_generate_target_report_does_not_wire_structure_recommendations(tmp_path):
    """Тот же сценарий, что и тест выше, но level=="target" (реальная сборка уже случилась) --
    карточка не должна рендериться, см. её докстринг про постфактум-бессмысленный совет."""
    run_stats = {"appended_images": 1, "appended_videos": 0,
                 "album_profiles": {"src/Отпуск": _cloudlike_profile()}}
    out_path = tmp_path / "report.html"
    r.generate_report({}, str(out_path), level="target", run_stats=run_stats,
                       run_start="2026-01-01 00:00:00")
    html_out = out_path.read_text(encoding="utf-8")
    assert "похож на папку облачной синхронизации" not in html_out


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


def test_render_analyze_recommendations_shows_failed_archive_paths():
    """REVIEW-HANDOFF.md, Раунд 88, замечание 1 -- failed_archive_paths (read_error/
    bomb_suspected, Задача A) считался, но нигде не рендерился до этого фикса."""
    model = {"failed_archive_paths": [r"C:\S\corrupt.zip", r"C:\S\deep.zip"]}
    html_out = r._render_analyze_recommendations(model)
    assert "2 архива не открылись" in html_out
    assert '<a href="file:///C:/S/corrupt.zip" target="_blank" rel="noopener">C:\\S\\corrupt.zip</a>' in html_out


def test_render_analyze_recommendations_shows_listdir_failed_paths():
    """SESSION-HANDOFF.txt, 2026-08-11 (отложенная задача) -- listdir_failed_paths считался
    в SourceWalker, но нигде не пробрасывался в analyze-отчёт до этого фикса, тот же класс
    пропажи, что и failed_archive_paths (Раунд 88) выше."""
    model = {"listdir_failed_paths": [r"C:\S\blocked_dir"]}
    html_out = r._render_analyze_recommendations(model)
    assert "1 папку не удалось прочитать" in html_out
    assert '<a href="file:///C:/S/blocked_dir" target="_blank" rel="noopener">C:\\S\\blocked_dir</a>' in html_out


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


def test_render_sheet1_oldest_file_origin_display_has_no_folder_marker():
    """oldest иногда несёт origin_display-style путь (см. build_model_from_analyze_stats()) --
    ByDate/Albums там не бывает, _friendly_target_dir() не находит маркер -- рендер должен
    деградировать до одного имени файла, не падать и не показывать пустую "Папка: ". Раньше
    вызывался как _render_sheet1(model, "analyze") -- `level`-параметр убран 2026-08-16 (Раунд
    88 пп.2-3, мёртвая ветка is_scan, см. докстринг _render_sheet1()), сама проверка формата
    oldest-пути остаётся актуальной независимо от того, откуда пришла модель."""
    stats = _FakeAnalyzeStats()
    stats.oldest_date = __import__("datetime").datetime(2015, 7, 1)
    stats.oldest_display = "original_photo.jpg"
    model = r.build_model_from_analyze_stats(stats)
    html_out = r._render_sheet1(model)
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


def test_cta_block_workdir_shows_try_real_build_prompt():
    html_out = r._render_cta_block("workdir")
    assert "Нравится результат" in html_out
    assert "резервную копию" not in html_out
    assert "Открыть папку с архивом" not in html_out


def test_cta_block_analyze_renders_nothing():
    """Речь пользователя, 2026-08-11: последний оставшийся здесь для analyze текст ("Нравится
    результат?...") тоже убран -- эта карточка для analyze больше не рендерится вовсе (пустая
    строка), независимо от модели. "workdir" по-прежнему получает этот текст (см.
    test_cta_block_workdir_shows_try_real_build_prompt) -- решение касалось только analyze."""
    assert r._render_cta_block("analyze") == ""
    model = {"years": Counter({2020: 1}), "archives_with_media": 3, "total_bytes": 1000}
    assert r._render_cta_block("analyze", model=model) == ""


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
    # Речь пользователя, 2026-08-11: closing CTA для analyze больше не рендерится вовсе (см.
    # test_cta_block_analyze_renders_nothing ниже для прямого теста самой ветки).
    assert "Нравится результат" not in html_out


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
    # #dedup-near -- Пакет A п.4 (SESSION-HANDOFF.txt): без фрагмента ссылка открывала верх
    # страницы (секцию точных дублей, если она есть), не секцию похожих серий, к которой
    # относится эта подпись.
    assert (f'<a href="{r.DEDUP_VERIFICATION_FILENAME}#dedup-near" target="_blank" rel="noopener">'
            "полная сверка похожих серий →</a>") in detail


def test_cluster_checklist_item_folder_is_a_clickable_file_link():
    """Пункт B.9 ("большой разбор report.html", SESSION-HANDOFF.txt): "Папка: ..." в разборе
    похожих серий -- file://-ссылка на реальную папку в TARGET, не только текст. Однопапочная
    ветка не меняется задачей 6 -- verify_link здесь не используется вообще."""
    cluster = [r"C:\T\dst\Albums\Отпуск\a.jpg", r"C:\T\dst\Albums\Отпуск\b.jpg"]
    title, detail = r._cluster_checklist_item(cluster, verify_link=r.DEDUP_VERIFICATION_FILENAME)
    assert '<a href="file:///C:/T/dst/Albums/Отпуск" target="_blank" rel="noopener">Albums\\Отпуск</a>' in detail


def test_render_near_dup_verification_section_lists_full_cluster_without_truncation():
    # 2026-08-08 (альбомный редизайн, вёрстка): таблица вместо карточки-на-кластер --
    # "Кадров в серии" колонка, не заголовок "Похожая серия из N кадров".
    clusters = [[rf"C:\T\dst\Albums\A{i}\f.jpg" for i in range(7)]]  # 7 разных папок, >5
    html_out = r._render_near_dup_verification_section(clusters)
    assert "<td>7</td>" in html_out
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
    assert "<td>2</td>" in html_out


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


def test_render_dedup_verification_page_both_sections_have_distinct_anchors(tmp_path):
    """Пакет A п.4 (SESSION-HANDOFF.txt, живая находка из реального report.html): когда на
    странице есть И точные дубли, И похожие серии в разных папках, "Полная сверка дублей"
    всегда идёт ПЕРВОЙ секцией (_render_dedup_verification_page()), "Полная сверка похожих
    серий" -- ниже. Без #id/#fragment обе ссылки-подписи из основного отчёта вели бы на верх
    страницы (секцию дублей), даже подпись "полная сверка похожих серий →"."""
    data = {
        "appended": [
            {"timestamp": "2026-01-01 00:00:01", "source": r"F:\orig\a.jpg",
             "dest": r"C:\T\dst\Albums\Отпуск\a.jpg", "reason": "appended_new", "flags": ""},
            {"timestamp": "2026-01-01 00:00:01", "source": "s0",
             "dest": r"C:\T\dst\ByDate\2024\2024-06\c.jpg", "reason": "appended_new", "flags": ""},
            {"timestamp": "2026-01-01 00:00:01", "source": "s1",
             "dest": r"C:\T\dst\ByDate\2024\2024-07\d.jpg", "reason": "appended_near_dup", "flags": ""},
        ],
        "skipped": [
            {"timestamp": "2026-01-01 00:00:01", "source": r"F:\dup\a_copy.jpg",
             "matched_with": r"C:\T\dst\Albums\Отпуск\a.jpg", "reason": "already_present"},
        ],
        "near_dup_edges": [
            {"timestamp": "2026-01-01 00:00:01", "dest": r"C:\T\dst\ByDate\2024\2024-07\d.jpg",
             "matched_dest": r"C:\T\dst\ByDate\2024\2024-06\c.jpg"},
        ],
    }
    html_out = r._render_dedup_verification_page(data)
    assert '<h1 id="dedup-exact">Полная сверка дублей</h1>' in html_out
    assert '<h1 id="dedup-near">Полная сверка похожих серий</h1>' in html_out
    # Порядок секций -- дубли первой, похожие серии второй -- ровно тот случай, где отсутствие
    # якорей раньше молча ломало ссылку "похожие серии".
    assert html_out.index('id="dedup-exact"') < html_out.index('id="dedup-near"')
    # PROMPT_report_run_redesign.md (2026-08-14), прямое решение пользователя: для
    # run_start-ветки (checklist_new is not None -- реальный путь ЛЮБОГО production-вызова
    # с level=="target") generate_dedup_verification_page() больше не вызывается вовсе --
    # единственные потребители ссылки (_render_recommendations()/_render_exact_dup_examples())
    # убраны из этой ветки. Сквозная проверка через generate_report() ЗДЕСЬ больше не
    # применима -- сама функция _render_dedup_verification_page() выше по-прежнему рабочая
    # и корректно расставляет якоря, просто на неё сейчас никто не ссылается.


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
    assert "1 дубль" in html_out


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


def test_render_dedup_verification_page_sorted_by_dup_count_descending():
    """2026-08-08 (альбомный редизайн, вёрстка): сортировка по убыванию числа дублей внутри
    карточки папки -- "b.jpg" (3 дубля) должен идти раньше "a.jpg" (2 дубля) в основной
    таблице, хотя по алфавиту порядок обратный. Оба >1, чтобы не попасть под сворачивание
    "без повторов" (см. отдельный тест ниже) -- проверяем чистый сорт, не взаимодействие с ним."""
    data = {
        "appended": [
            {"timestamp": "t", "source": r"F:\orig\a.jpg",
             "dest": r"C:\T\dst\Albums\Отпуск\a.jpg", "reason": "appended_new", "flags": ""},
            {"timestamp": "t", "source": r"F:\orig\b.jpg",
             "dest": r"C:\T\dst\Albums\Отпуск\b.jpg", "reason": "appended_new", "flags": ""},
        ],
        "skipped": [
            {"source": r"F:\dup\a_copy1.jpg", "matched_with": r"C:\T\dst\Albums\Отпуск\a.jpg",
             "reason": "already_present"},
            {"source": r"F:\dup\a_copy2.jpg", "matched_with": r"C:\T\dst\Albums\Отпуск\a.jpg",
             "reason": "already_present"},
            {"source": r"F:\dup\b_copy1.jpg", "matched_with": r"C:\T\dst\Albums\Отпуск\b.jpg",
             "reason": "already_present"},
            {"source": r"F:\dup\b_copy2.jpg", "matched_with": r"C:\T\dst\Albums\Отпуск\b.jpg",
             "reason": "already_present"},
            {"source": r"F:\dup\b_copy3.jpg", "matched_with": r"C:\T\dst\Albums\Отпуск\b.jpg",
             "reason": "already_present"},
        ],
    }
    html_out = r._render_dedup_verification_page(data)
    main_table = html_out.split("<details>")[0]
    assert main_table.index(">b.jpg<") < main_table.index(">a.jpg<")


def test_render_dedup_verification_page_collapses_single_dup_rows_under_details():
    """Строки с ровно одним дублем (обычный случай, не путаница) сворачиваются под общий
    `<details>` в конце карточки папки, не показываются сразу вместе с "настоящими" находками."""
    data = {
        "appended": [
            {"timestamp": "t", "source": r"F:\orig\a.jpg",
             "dest": r"C:\T\dst\Albums\Отпуск\a.jpg", "reason": "appended_new", "flags": ""},
            {"timestamp": "t", "source": r"F:\orig\b.jpg",
             "dest": r"C:\T\dst\Albums\Отпуск\b.jpg", "reason": "appended_new", "flags": ""},
        ],
        "skipped": [
            {"source": r"F:\dup\a_copy.jpg", "matched_with": r"C:\T\dst\Albums\Отпуск\a.jpg",
             "reason": "already_present"},
            {"source": r"F:\dup\b_copy1.jpg", "matched_with": r"C:\T\dst\Albums\Отпуск\b.jpg",
             "reason": "already_present"},
            {"source": r"F:\dup\b_copy2.jpg", "matched_with": r"C:\T\dst\Albums\Отпуск\b.jpg",
             "reason": "already_present"},
        ],
    }
    html_out = r._render_dedup_verification_page(data)
    main_table, _, rest = html_out.partition("<details>")
    assert ">b.jpg<" in main_table
    assert ">a.jpg<" not in main_table  # только 1 дубль -- свёрнут
    assert "ещё 1 файл без повторов" in rest
    assert ">a.jpg<" in rest


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
        "quality_flags": Counter(), "unreadable": [],
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


def test_build_checklist_items_shows_disputed_paths_on_analyze_level():
    """Задача 4 (SESSION-HANDOFF.txt, 2026-08-09): analyze-уровень теперь показывает реальные
    пути (AnalyzeStats.disputed_paths), не голый агрегат по папкам -- заголовок без
    "однозначно" (прямая инструкция пользователя), file://-ссылка рабочая (путь абсолютный)."""
    fields = {
        "near_dup_clusters": [], "exact_dup_groups": [],
        "disputes_total": 2, "disputed_paths": [r"C:\S\Отпуск\icon.svg", r"C:\S\Отпуск\tiny.jpg"],
        "quality_flags": Counter(), "unreadable": [],
    }
    items = r._build_checklist_items(fields)
    joined = "".join(items)
    assert "2 файла не удалось распознать" in joined
    assert "однозначно" not in joined
    assert "icon.svg" in joined and "tiny.jpg" in joined
    assert 'file:///C:/S/' in joined


def test_build_checklist_items_falls_back_to_folder_counts_without_dispute_detail():
    """Ни disputes_detail (TARGET), ни disputed_paths (analyze) не переданы -- страховочный
    старый агрегат по папкам должен сохраниться, не падать с KeyError."""
    fields = {
        "near_dup_clusters": [], "exact_dup_groups": [],
        "disputes_total": 4, "disputes_by_folder": Counter({r"C:\S\Отпуск": 4}),
        "quality_flags": Counter(), "unreadable": [],
    }
    items = r._build_checklist_items(fields)
    joined = "".join(items)
    assert "4 файла не удалось распознать" in joined
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
        "quality_flags": Counter(), "unreadable": [],
    }
    joined = "".join(r._build_checklist_items(fields, target_path=r"D:\__PhotoArchive__"))
    assert '<a href="file:///D:/__PhotoArchive__/_Unsorted" target="_blank" rel="noopener">_Unsorted</a>' in joined


def test_generate_report_disputed_count_shown_via_section_three(tmp_path):
    """Пункт B.5 (2026-07-26): "сохранены ВСЕ N файлов, включая M спорных" -- изначальная
    вводная фраза _render_recommendations(). PROMPT_report_run_redesign.md (2026-08-14,
    прямое решение пользователя): эта карточка убрана -- то же самое ("спорные СКОПИРОВАНЫ,
    просто отдельно, не потеряны") теперь сообщает Раздел 3 (_render_run_auto_decisions())."""
    data = {
        "appended": [_appended_row(f"D:\\T\\ByDate\\2026\\2026-01\\a{i}.jpg") for i in range(5)],
        "disputes": [{"timestamp": "2026-01-01 09:00:00", "source": "s.gif",
                       "dest": r"D:\T\_Unsorted\s.gif", "reason": "animated_gif", "was_hidden": ""}],
    }
    out_path = tmp_path / "report.html"
    r.generate_report(data, str(out_path), level="target", run_start="2026-01-01 00:00:00")
    html_out = out_path.read_text(encoding="utf-8")
    assert "<b>1</b> файл сохранён в папке" in html_out
    assert "_Unsorted" in html_out
    assert "не потеряла" in html_out


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
    friendly-текст для показа строится отдельно на стороне рендера (_date_issues_checklist_item(),
    см. соответствующий тест -- объединённая версия прежней _dates_review_checklist_item(),
    задача 5, 2026-08-09)."""
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

    title, detail = r._date_issues_checklist_item(groups[0])
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
    assert "дата определена неточно" not in joined


def test_build_checklist_items_shows_dates_review_file_and_tier_when_detail_available():
    fields = {
        "near_dup_clusters": [], "exact_dup_groups": [],
        "disputes_total": 0, "disputes_by_folder": Counter(),
        "date_issues_b_total": 1, "date_issues_c_total": 0, "date_issues_d_total": 0,
        "date_issues_detail": [(r"Albums\Отпуск", [("a.jpg", "B")])],
        "quality_flags": Counter(), "unreadable": [],
    }
    items = r._build_checklist_items(fields)
    joined = "".join(items)
    assert "a.jpg" in joined
    assert "высокая уверенность" in joined
    assert "Albums\\Отпуск" in joined


def test_build_checklist_items_date_issues_summary_only_without_detail():
    """analyze-уровень: date_issues_detail отсутствует (AnalyzeStats не отслеживает source/dest
    на файл построчно) -- сводный пункт (заголовок + разбивка по тирам) должен всё равно
    показаться, без списка файлов/папок, не падать."""
    fields = {
        "near_dup_clusters": [], "exact_dup_groups": [],
        "disputes_total": 0, "disputes_by_folder": Counter(),
        "date_issues_b_total": 0, "date_issues_c_total": 3, "date_issues_d_total": 0,
        "quality_flags": Counter(), "unreadable": [],
    }
    items = r._build_checklist_items(fields)
    joined = "".join(items)
    assert "3 файла — дата определена неточно или не определена вовсе" in joined
    assert "оценочно, по соседним файлам в папке" in joined
    assert "Папка:" not in joined


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
    # 2026-08-15: "без RAW" теперь мелкой пометкой рядом с заголовком (_h2_title()), не частью
    # текста самого заголовка -- см. решение пользователя про единый вид всех "без RAW"-диаграмм.
    assert "Надёжность дат — фото и видео" in html_out
    assert '<span class="h2-note">(без RAW)</span>' in html_out


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


def test_cta_block_analyze_no_model_renders_nothing():
    """Раунд 33 (историческая проверка "не должен падать") + речь пользователя, 2026-08-11
    (сама карточка для analyze больше не рендерится вовсе, см. test_cta_block_analyze_renders_
    nothing) -- старые вызовы без model не должны падать."""
    assert r._render_cta_block("analyze") == ""


def test_cta_block_analyze_never_shows_album_date_grouping_prediction():
    """2026-08-09 (SESSION-HANDOFF.txt, прямое решение пользователя): абзац-предсказание
    "Найдено XX папок-альбомов ... которые разложатся по дате ..." убран целиком -- КАК файлы
    реально разложатся, показывает dry-run, analyze -- только то, что нашли. Модель по-прежнему
    может нести n_albums_detected/n_regular_folders (см. build_model_from_analyze_stats()), но
    _render_cta_block() их больше не читает -- регресс на случай, если абзац вернётся молча."""
    model = {
        "years": Counter({2020: 1}), "archives_with_media": 0, "total_bytes": 0,
        "n_albums_detected": 2, "n_media_in_albums": 30,
        "n_regular_folders": 3, "n_media_by_date": 7,
    }
    html_out = r._render_cta_block("analyze", model=model)
    assert "папок-альбомов" not in html_out
    assert "разложатся по дате" not in html_out
    assert "обычн" not in html_out


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
        # Задачи 4/6 (SESSION-HANDOFF.txt, 2026-08-09): disputed_paths ("не удалось распознать")/
        # unreadable_paths ("не прочитано") -- раздельные списки реальных путей, заменяют
        # общий n_broken_or_zero для рендера чек-листа/decisions-пирога (сам n_broken_or_zero
        # выше остаётся -- читается только Паспортом, _render_passport_integrity()).
        self.disputed_paths = []
        self.unreadable_paths = []
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
        # Задача 5 (SESSION-HANDOFF.txt, 2026-08-09): album-исключающие тир-счётчики B/C/D
        # раздельно -- та же семантика "не в альбоме", что n_tier_cd_bydate выше, тоньше на
        # тир. Значения по умолчанию согласованы с tier_counts={"C": 2, "D": 1} выше (n_tier_cd_
        # bydate=3 -- та же сумма C+D), конкретные тесты переопределяют при необходимости.
        self.n_tier_b_bydate = 0
        self.n_tier_c_bydate = 2
        self.n_tier_d_bydate = 1
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
        self.dump_item_paths = []
        self.n_albums_detected = 1
        # SESSION-HANDOFF.txt, 2026-08-07 (группировка альбом/дата) -- YY/ZZ/QQ, см.
        # AnalyzeStats.n_media_in_albums/bydate_media_by_folder/n_media_by_date.
        self.n_media_in_albums = 0
        self.bydate_media_by_folder = Counter()
        self.n_media_by_date = 0
        self.cities = Counter()
        # Пункт E -- "Топ камер/устройств съёмки".
        self.cameras = Counter()
        # _deep_nested_albums() в _render_passport_integrity() -- единственный оставшийся
        # потребитель (дерево само теперь читает source_tree_counts_image/video/raw ниже).
        self.tree_folder_counts = Counter()
        # Пункт B.2 -- полные пути запароленных архивов.
        self.n_archives_encrypted = 0
        self.encrypted_archive_paths = []
        # SESSION-HANDOFF.txt, 2026-08-11 ("большой разбор report.html", Задача A) -- новые
        # поля AnalyzeStats под редизайн Разделов 1-3 analyze-отчёта, см. build_model_from_
        # analyze_stats(). Значения по умолчанию согласованы с found-полями выше (available ==
        # found -- ни один тест этого фейка не готовит битые/unreadable файлы отдельно).
        self.max_depth = 0
        self.n_folders_with_media = 0
        self.n_dvd_units = 0
        self.n_images_available = self.n_images
        self.n_raw_available = self.n_raw
        self.n_videos_available = self.n_videos
        self.bytes_by_kind_available = Counter()
        self.tier_counts_no_raw = Counter(self.tier_counts)
        self.dates_by_year_photo = Counter()
        self.dates_by_year_video = Counter()
        self.format_counts_image = Counter()
        self.format_counts_raw = Counter()
        self.format_counts_video = Counter()
        self.dvd_vob_count = 0
        self.n_archives_failed = 0
        self.failed_archive_paths = []
        # SESSION-HANDOFF.txt, 2026-08-11 (отложенная задача) -- непрочитанные папки.
        self.listdir_failed_paths = []
        self.disputed_records = []
        self.unreadable_records = []
        # 2026-08-14 -- дерево реальной структуры SOURCE, см. build_model_from_analyze_stats().
        self.source_tree_counts_image = Counter()
        self.source_tree_counts_video = Counter()
        self.source_tree_counts_raw = Counter()


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


def test_render_analyze_recommendations_shows_found_archive_and_near_dup_series():
    """Речь пользователя, 2026-08-11: пункт "За {год} сохранилось заметно меньше снимков..."
    (_find_year_gap()) убран целиком из рендера -- эта проверка (была test_render_analyze_
    recommendations_shows_year_gap_found_archive_and_near_dup_series) больше не проверяет "2018"
    в выдаче, только found_archive/near_dup, которые не менялись. _find_year_gap() сама функция
    не удалена (см. test_find_year_gap_* выше), просто нигде не вызывается."""
    model = {
        "total_bytes": 0,
        "years": Counter({2016: 40, 2017: 40, 2018: 1, 2019: 40, 2020: 40}),
        "found_archive_paths": [r"D:\Old\PhotoArchive"],
        "near_dup_clusters": [["a", "b"], ["c", "d", "e"]],
        "tier_counts": Counter(),
    }
    html_out = r._render_analyze_recommendations(model)
    assert "сохранилось заметно меньше снимков" not in html_out
    assert "уже есть собранный архив" in html_out
    assert "2 серии похожих кадров" in html_out


def test_generate_report_from_analyze_stats_includes_recommendations_section(tmp_path):
    # Речь пользователя, 2026-08-11: "Рекомендации" объединены с "Что стоит проверить" в одну
    # карточку "Что стоит проверить и рекомендации" -- отдельного заголовка "Рекомендации"
    # (с большой буквы, как раньше) в тексте больше нет, только внутри объединённого заголовка.
    stats = _FakeAnalyzeStats()
    stats.found_archive_top_level = [r"D:\Old\PhotoArchive"]
    stats.near_dup_edges = [{"dest": "a", "matched_dest": "b"}]
    out_path = tmp_path / "report.html"
    r.generate_report_from_analyze_stats(stats, str(out_path))
    text = out_path.read_text(encoding="utf-8")
    assert "<h2>Что стоит проверить и рекомендации</h2>" in text
    assert "уже есть собранный архив" in text


def test_year_hbar_chart_single_year_bar_spans_full_width():
    """Горизонтальная форма (в отличие от прежней вертикальной, см. 2026-07-21 finding выше в
    истории) не имеет отдельного gap-based расчёта ширины столбца -- при n==1 бар естественно
    занимает всю доступную ширину (value == max_v), это ожидаемо, не визуальный баг."""
    svg = r._svg_year_hbar_chart(Counter({2026: 10}))
    m = re.search(r'<rect[^>]*width="([\d.]+)"', svg)
    assert m is not None
    assert float(m.group(1)) > 400  # почти вся plot_w (680 - 54 - 68 = 558), не крошечный бар


def test_render_sheet1_embeds_generated_at_in_heading_when_given():
    """Задача 10 (SESSION-HANDOFF.txt, 2026-08-09) + речь пользователя, 2026-08-09: "по
    состоянию на ГГГГ-ММ-ДД ЧЧ:ММ" раньше дописывалось прямо в <h1> -- теперь отдельной
    строкой ПОД заголовком (_render_report_meta(), не раздувает текст заголовка). Заодно h1
    уменьшен до h2 -- тот же уровень, что у "Пробный прогон"/"Пополнение архива"."""
    model = r.build_model_from_rows({"appended": [_appended_row(r"D:\T\ByDate\2026\2026-01-01 [PhotoArchive]\a.jpg")]})
    html_out = r._render_sheet1(model, generated_at="2026-08-09 13:21")
    assert "<h2>Ваш архив</h2>" in html_out
    assert 'class="report-meta-date">по состоянию на 2026-08-09 13:21' in html_out


def test_render_sheet1_omits_generated_at_when_not_given():
    """generated_at=None (по умолчанию) -- заголовок БЕЗ мета-строки вовсе (ни даты, ни путей
    -- см. _render_report_meta()'s "пустой результат"). Это и есть путь, которым
    _render_found_archive_block() зовёт _render_sheet1() как ВНУТРЕННЮЮ карточку на чужой
    странице (найденный архив внутри SOURCE) -- та страница не должна получить дату задним
    числом просто потому, что _render_sheet1() её теперь умеет показывать."""
    model = r.build_model_from_rows({"appended": []})
    html_out = r._render_sheet1(model)
    assert "<h2>Ваш архив</h2>" in html_out
    assert "по состоянию на" not in html_out
    assert "report-meta" not in html_out


class TestRenderReportMeta:
    """Речь пользователя, 2026-08-09: общая подпись-подзаголовок под каждым головным
    заголовком отчёта -- путь(и) SOURCE/TARGET слева, "по состоянию на ДАТА" справа."""

    def test_single_source_one_line(self):
        html_out = r._render_report_meta(["C:\\Users\\x\\Pictures"], None, "2026-08-09 21:00")
        assert "Источник: C:\\Users\\x\\Pictures" in html_out
        assert 'report-meta-date">по состоянию на 2026-08-09 21:00' in html_out
        assert "Источники" not in html_out  # ед. число для одного источника

    def test_multiple_sources_each_on_own_line(self):
        """Прямая формулировка пользователя: "несколько источников показывать каждый с новой
        строки" -- <br> между путями, не через запятую/точку с запятой одной строкой."""
        html_out = r._render_report_meta(
            ["C:\\Pictures", "D:\\Backup\\Photos", "E:\\"], None, "2026-08-09 21:00")
        assert "Источники:" in html_out
        assert "C:\\Pictures<br>D:\\Backup\\Photos<br>E:\\" in html_out

    def test_target_only_no_sources(self):
        """[4] Паспорт архива: self-scan, SOURCE==TARGET -- показываем только "Архив", без
        отдельной строки "Источник"."""
        html_out = r._render_report_meta(None, "D:\\__PhotoArchive__", "2026-08-09 21:00")
        assert "Архив: D:\\__PhotoArchive__" in html_out
        assert "Источник" not in html_out

    def test_both_source_and_target(self):
        """[2]/[3]: SOURCE (что сканируем) И TARGET (куда собираем/собрали) -- обе строки."""
        html_out = r._render_report_meta(["C:\\Pictures"], "D:\\__PhotoArchive__", "2026-08-09 21:00")
        assert "Источник: C:\\Pictures" in html_out
        assert "Архив: D:\\__PhotoArchive__" in html_out

    def test_empty_result_when_nothing_to_show(self):
        assert r._render_report_meta(None, None, None) == ""

    def test_paths_are_html_escaped(self):
        html_out = r._render_report_meta(["C:\\<script>"], None, None)
        assert "<script>" not in html_out
        assert "&lt;script&gt;" in html_out


def test_generate_report_from_analyze_stats_heading_and_footer_share_one_timestamp(tmp_path, monkeypatch):
    """Задача 10, п.2: ОДИН вызов strftime() на страницу, не два независимых -- если бы
    _render_sheet1()/_page_shell() каждый считали своё время сами, на границе минуты они могли
    бы разойтись. Здесь time.strftime() нарочно возвращает РАЗНОЕ значение при каждом вызове --
    если заголовок и футер совпадают, значит оба читают ОДНО и то же уже посчитанное значение."""
    calls = []

    def _fake_strftime(fmt):
        calls.append(fmt)
        return f"2026-08-09 13:{20 + len(calls)}"
    monkeypatch.setattr(r.time, "strftime", _fake_strftime)

    stats = _FakeAnalyzeStats()
    out_path = tmp_path / "report.html"
    r.generate_report_from_analyze_stats(stats, str(out_path))
    html_out = out_path.read_text(encoding="utf-8")

    assert "<h2>Что нашлось в источнике. Общая информация</h2>" in html_out
    heading_m = re.search(r'report-meta-date">по состоянию на ([\d\-]+ [\d:]+)</div>', html_out)
    footer_m = re.search(r"Сформировано PhotoArchive[^·]*· ([\d\-]+ [\d:]+)</div>", html_out)
    assert heading_m and footer_m, html_out
    assert heading_m.group(1) == footer_m.group(1)
    assert len(calls) == 1  # ровно один вызов strftime() на всю страницу


def test_render_passport_summary_embeds_generated_at_in_heading():
    """2026-08-09, речь пользователя: дата теперь в отдельной строке _render_report_meta()
    ПОД заголовком, не приклеена в текст <h1> -- заодно h1->h2."""
    stats = _FakeAnalyzeStats()
    html_out = r._render_passport_summary(stats, generated_at="2026-08-09 13:21")
    assert "<h2>Архив сейчас</h2>" in html_out
    assert 'report-meta-date">по состоянию на 2026-08-09 13:21' in html_out


def test_generate_passport_report_heading_and_footer_share_one_timestamp(tmp_path, monkeypatch):
    calls = []

    def _fake_strftime(fmt):
        calls.append(fmt)
        return f"2026-08-09 13:{20 + len(calls)}"
    monkeypatch.setattr(r.time, "strftime", _fake_strftime)

    stats = _FakeAnalyzeStats()
    out_path = tmp_path / "passport.html"
    r.generate_passport_report(stats, str(out_path))
    html_out = out_path.read_text(encoding="utf-8")

    assert "<h2>Архив сейчас</h2>" in html_out
    heading_m = re.search(r'report-meta-date">по состоянию на ([\d\-]+ [\d:]+)</div>', html_out)
    footer_m = re.search(r"Сформировано PhotoArchive[^·]*· ([\d\-]+ [\d:]+)</div>", html_out)
    assert heading_m and footer_m, html_out
    assert heading_m.group(1) == footer_m.group(1)


def test_render_analyze_sheet1_shows_location():
    """SESSION-HANDOFF.txt, 2026-08-11 ("большой разбор report.html", Задача B) -- новый
    _render_analyze_sheet1() -- 1.1 "Расположение" (папки/архивы/макс. глубина). "Доступно для
    архива" (1.2) переехала в свою отдельную карточку, _render_analyze_available_card() --
    речь пользователя, 2026-08-11, см. тесты ниже.

    Речь пользователя, 2026-08-11 (живой боевой прогон по C:\\ целиком): папки/архивы/глубина
    теперь считаются ТОЛЬКО по тому, что реально ведёт к медиафайлу (n_folders_with_media/
    archives_with_media/max_depth -- см. AnalyzeStats), не по общему числу папок-объектов/
    архивов/глубине обхода -- n_objects_total/n_archives_found (сырой подсчёт) на этот рендер
    больше не влияют вовсе.

    Речь пользователя, 2026-08-11 (тем же заходом): отдельный заголовок "Расположение" и три
    плитки под ним убраны -- одно предложение прозой без промежуточного заголовка, продолжающее
    headline-плитки той же карточки. Формулировка уточнена тем же заходом ("Источник содержит
    N папок" звучало как утверждение про источник целиком, хотя числа -- только про то, где
    расположены НАЙДЕННЫЕ медиафайлы) -- "Медиафайлы расположены в N папках и M архивах"
    (предложный падеж не различает 2-4 отдельной формой, в отличие от именительного -- только
    "1" даёт особую форму единственного числа). Уточнена ЕЩЁ раз тем же заходом: "с K уровнями
    вложенности" читалось двусмысленно (суммарно? у каждой папки?) -- отдельное предложение
    "Максимальный уровень вложенности — K." однозначно называет это максимумом одного самого
    глубокого пути. Уточнена ТРЕТИЙ раз тем же заходом: перенесена ПОСЛЕ "Из них ..." (была в
    том же <p>, что и "Медиафайлы расположены..."), тем же приглушённым начертанием
    (class="muted"), что и "Из них ..." рядом -- см. test_render_analyze_sheet1_depth_sentence_
    comes_after_location_breakdown_with_muted_style ниже."""
    stats = _FakeAnalyzeStats()
    stats.n_folders_with_media = 10
    stats.n_archives_with_media = 2
    stats.max_depth = 4
    model = r.build_model_from_analyze_stats(stats)
    html_out = r._render_analyze_sheet1(model)

    assert "<h2>Что нашлось в источнике. Общая информация</h2>" in html_out
    assert "<h3>Расположение</h3>" not in html_out
    assert "Медиафайлы расположены в 10 папках и 2 архивах." in html_out
    assert "Максимальный уровень вложенности — 4." in html_out
    assert "<h3>Доступно для архива</h3>" not in html_out


def test_render_analyze_sheet1_depth_sentence_comes_after_location_breakdown_with_muted_style():
    """Речь пользователя, 2026-08-11: "Максимальный уровень вложенности — K." перенесена ПОСЛЕ
    "Из них ..." (не в том же <p>, что "Медиафайлы расположены...", сразу за ним) и тем же
    приглушённым начертанием (class="muted"), что у "Из них ..." рядом."""
    stats = _FakeAnalyzeStats()
    stats.n_folders_with_media = 10
    stats.n_archives_with_media = 2
    stats.max_depth = 9
    stats.files_by_location = Counter({"folder": 244, "archive": 400})
    model = r.build_model_from_analyze_stats(stats)
    html_out = r._render_analyze_sheet1(model)

    assert '<p class="muted">Максимальный уровень вложенности — 9.</p>' in html_out
    location_pos = html_out.index("Из них")
    depth_pos = html_out.index("Максимальный уровень вложенности")
    assert location_pos < depth_pos
    # Не в одном <p> с "Медиафайлы расположены..." -- то предложение закрывается ДО этой фразы.
    assert "Медиафайлы расположены в 10 папках и 2 архивах.</p>" in html_out


def test_render_analyze_sheet1_location_sentence_uses_singular_forms_for_count_one():
    """N==1 даёт особую форму единственного числа ("в 1 папке", не "в 1 папках"), любое другое
    количество -- одна и та же форма множественного (предложный падеж, в отличие от
    именительного, не различает 2-4 отдельно)."""
    stats = _FakeAnalyzeStats()
    stats.n_folders_with_media = 1
    stats.n_archives_with_media = 1
    stats.max_depth = 1
    model = r.build_model_from_analyze_stats(stats)
    html_out = r._render_analyze_sheet1(model)
    assert "Медиафайлы расположены в 1 папке и 1 архиве." in html_out
    assert "Максимальный уровень вложенности — 1." in html_out


def test_render_analyze_available_card_shows_availability():
    """Речь пользователя, 2026-08-11: "Доступно для архива" -- отдельное "окно" (карточка), не
    подраздел карточки "Общая информация" -- см. _render_analyze_available_card()."""
    stats = _FakeAnalyzeStats()
    stats.n_images_available = 1  # n_images=3 в фейке -- 2 файла "не доступны"
    model = r.build_model_from_analyze_stats(stats)
    html_out = r._render_analyze_available_card(model)

    assert "<h2>Доступно для архива</h2>" in html_out
    assert "1 файл из 3 файла готовы к архивации" in html_out
    assert "_Unsorted" in html_out


def test_render_analyze_available_card_all_available_has_no_unsorted_mention():
    stats = _FakeAnalyzeStats()  # n_images_available == n_images по умолчанию фейка
    model = r.build_model_from_analyze_stats(stats)
    html_out = r._render_analyze_available_card(model)
    assert "Все 3 файла готовы к архивации" in html_out
    assert "_Unsorted" not in html_out


def test_render_analyze_available_card_keeps_oldest_file_and_busiest_month():
    """Перенесены в 1.2 ТЗ пользователя -- те же тексты, что были в старой шапке
    _render_sheet1(), теперь в своей отдельной карточке "Доступно для архива"."""
    stats = _FakeAnalyzeStats()
    stats.oldest_date = datetime(2015, 6, 1)
    stats.oldest_display = "Отпуск/img.jpg"
    model = r.build_model_from_analyze_stats(stats)
    html_out = r._render_analyze_available_card(model)
    assert "Самый старый файл" in html_out
    assert "Самый насыщенный месяц" in html_out


def test_render_analyze_sheet1_no_longer_has_bridge_phrase():
    """Речь пользователя, 2026-08-11: "Дальше — ваш архив в цифрах." убрана целиком, не
    перенесена в _render_analyze_available_card()."""
    stats = _FakeAnalyzeStats()
    model = r.build_model_from_analyze_stats(stats)
    assert "Дальше — ваш архив в цифрах." not in r._render_analyze_sheet1(model)
    assert "Дальше — ваш архив в цифрах." not in r._render_analyze_available_card(model)


def test_render_analyze_sheet2_top_formats_ignores_dvd_vob_count_alone():
    """Задача C, п.4 -- топ-5 форматов раздельно по категории. Речь пользователя, 2026-08-11:
    DVD (структура VIDEO_TS) не подмешивается в топ-5/сортировку видеоформатов ("DVD и vob --
    разные объекты и считаются по-разному, их нельзя смешивать") -- ранжированный список видео
    остаётся чистым от DVD. dvd_vob_count (счётчик .vob-ФРАГМЕНТОВ, другая единица) сам по себе
    ничего не рендерит -- только n_dvd_units (число РАЗЛИЧНЫХ дисков, см. тест ниже)."""
    stats = _FakeAnalyzeStats()
    stats.format_counts_image = Counter({".jpg": 5, ".png": 1})
    stats.format_counts_video = Counter({".mp4": 2})
    stats.dvd_vob_count = 7
    model = r.build_model_from_analyze_stats(stats)
    html_out = r._render_analyze_sheet2(model)
    assert "Топ форматов — фото" in html_out
    assert "JPG" in html_out
    assert "Топ форматов — видео" in html_out
    assert "MP4" in html_out
    assert "DVD" not in html_out
    assert "vob" not in html_out


def test_render_analyze_sheet2_shows_dvd_as_bar_inside_video_chart():
    """Речь пользователя, 2026-08-11 (несколько итераций того же вопроса): сначала DVD (.vob)
    ранжированным баром по числу .vob-файлов -- убрано ("нельзя смешивать" файлы с фрагментами);
    затем отдельная текстовая сноска под диаграммой -- тоже убрана ("это сообщение выводить не
    нужно"); "своей строкой/баром внутри той же диаграммы" (ответ на уточняющий вопрос) --
    но ЗНАЧЕНИЕ бара не число дисков (n_dvd_units), а РЕАЛЬНОЕ число файлов DVD-юнита
    (video_available_total - video_format_total) -- иначе сумма баров разошлась бы с "Тип
    медиа" (речь пользователя, "общее количество видео... должны совпадать, если нет группы
    'прочие'"). Число дисков остаётся в ПОДПИСИ ("N файлов (M дисков)"), не в значении/длине
    бара."""
    stats = _FakeAnalyzeStats()
    stats.format_counts_video = Counter({".mp4": 83})
    stats.n_videos = 89  # 83 обычных + 6 файлов одного DVD-юнита (vob+ifo+bup)
    stats.n_videos_available = 89
    stats.dvd_vob_count = 2
    stats.n_dvd_units = 1
    model = r.build_model_from_analyze_stats(stats)
    html_out = r._render_analyze_sheet2(model)
    assert "MP4" in html_out
    assert "83" in html_out
    assert '<p class="muted">Отдельно' not in html_out  # сноска-текст больше не рендерится
    # DVD -- реальный бар ВНУТРИ той же диаграммы, не отдельный текст: <text>DVD</text> есть,
    # подпись -- "6 файлов (1 диск)" (реальное число файлов + число дисков в скобках).
    assert re.search(r'<text[^>]*>DVD</text>', html_out)
    assert re.search(r'<text[^>]*>6 файлов \(1 диск\)</text>', html_out)
    # Речь пользователя, 2026-08-11: "общее количество видео в 'Тип медиа' и 'Топ форматов —
    # видео' должны совпадать, если нет группы 'прочие'" -- прямая проверка инварианта: MP4
    # (83) + DVD-бар (6, реальное число файлов, не 1 диск) == available-счётчик видео (89),
    # который показывает "Тип медиа". Нет "остальные" -- всего 2 категории, меньше топ-5.
    assert "остальные" not in html_out
    assert 83 + 6 == model["counts_available"]["video"] == 89


def test_render_passport_charts_dvd_bar_excludes_broken_non_dvd_video():
    """REVIEW-HANDOFF.md, Раунд 94 [ЗАМЕЧАНИЕ]: _render_passport_charts() брал разницу от
    counts["video"] (ДО broken/unreadable-фильтра) вместо counts_available["video"] (ПОСЛЕ),
    как уже верно делает _render_analyze_sheet2() выше -- битые НЕ-DVD видео молча приписывались
    к DVD-счётчику. Сценарий ревизора: 5 обычных видео + 2 битых НЕ-DVD видео (учтены в n_videos,
    но не в n_videos_available и не в format_counts_video) + DVD-юнит из 3 файлов."""
    stats = _FakeAnalyzeStats()
    stats.format_counts_video = Counter({".mp4": 5})
    stats.n_videos = 10  # 5 обычных + 2 битых НЕ-DVD + 3 файла DVD-юнита
    stats.n_videos_available = 8  # битые 2 исключены, DVD-юнит остаётся (5 + 3)
    stats.n_dvd_units = 1
    model = r.build_model_from_analyze_stats(stats)
    html_out = r._render_passport_charts(stats)
    assert re.search(r'<text[^>]*>DVD</text>', html_out)
    # Диск реально содержит 3 файла -- НЕ 5 (10-5), в которые ошибочно попадали 2 битых видео.
    assert re.search(r'<text[^>]*>3 файла \(1 диск\)</text>', html_out)
    assert "5 файлов (1 диск)" not in html_out
    assert 5 + 3 == model["counts_available"]["video"] == 8


def test_render_analyze_sheet2_top_formats_order_is_photo_video_raw():
    """Речь пользователя, 2026-08-11: "по умолчанию порядок такой: фото, видео, RAW" --
    раньше было фото/RAW/видео."""
    stats = _FakeAnalyzeStats()
    stats.format_counts_image = Counter({".jpg": 5})
    stats.format_counts_video = Counter({".mp4": 2})
    stats.format_counts_raw = Counter({".cr2": 1})
    model = r.build_model_from_analyze_stats(stats)
    html_out = r._render_analyze_sheet2(model)
    photo_pos = html_out.index("Топ форматов — фото")
    video_pos = html_out.index("Топ форматов — видео")
    raw_pos = html_out.index("Топ форматов — RAW")
    assert photo_pos < video_pos < raw_pos


def test_render_analyze_sheet2_top_formats_uses_fixed_two_column_grid():
    """Речь пользователя, 2026-08-11: "плашки Топ форматов 2 шт на всю ширину окна. Если
    появляется третья — с новой строки" -- фиксированные 2 колонки (не auto-fit)."""
    stats = _FakeAnalyzeStats()
    stats.format_counts_image = Counter({".jpg": 5})
    stats.format_counts_video = Counter({".mp4": 2})
    stats.format_counts_raw = Counter({".cr2": 1})
    model = r.build_model_from_analyze_stats(stats)
    html_out = r._render_analyze_sheet2(model)
    assert '<div class="grid-3-formats">' in html_out


def test_top_formats_hbar_extra_entries_use_their_own_display_text():
    """Речь пользователя, 2026-08-11: extra_entries -- готовые (метка, число, ПОДПИСЬ) тройки,
    не (метка, число) пары -- в отличие от обычных расширений (подпись всегда "N файлов" через
    _n_files()), у extra_entries подпись задаётся вызывающим кодом напрямую (DVD -- "N дисков",
    не "N файлов", см. _render_analyze_sheet2())."""
    chart = r._top_formats_hbar(Counter({".mp4": 5}), extra_entries=[("DVD", 2, "2 диска")])
    assert re.search(r'<text[^>]*>DVD</text>', chart)
    assert re.search(r'<text[^>]*>2 диска</text>', chart)
    assert "2 файла" not in chart


def test_render_analyze_sheet2_shows_years_split_by_photo_and_video():
    stats = _FakeAnalyzeStats()
    stats.dates_by_year_photo = Counter({2020: 3})
    stats.dates_by_year_video = Counter({2020: 1})
    model = r.build_model_from_analyze_stats(stats)
    html_out = r._render_analyze_sheet2(model)
    assert "Медиафайлы по годам — фото и видео" in html_out


def test_year_hbar_chart_dual_always_uses_two_row_layout():
    """Речь пользователя, 2026-08-11: две предыдущие итерации экспериментировали с переменной
    высотой строки для года только с одним типом (сначала растянутый бар, потом более короткая
    строка) -- по прямой просьбе пользователя ("верни двух-строчную организацию (всегда)")
    откачено назад: КАЖДЫЙ год -- полная bar_h+gap строка с двумя фиксированными позициями
    (фото сверху, видео снизу), пустая половина просто не рисуется, но место остаётся --
    причина отката: "при малом количестве цвет не виден" на одиночном тонком баре без второй
    половины для контраста."""
    only_photo = r._svg_year_hbar_chart_dual(Counter({2019: 10}), Counter())
    both = r._svg_year_hbar_chart_dual(Counter({2019: 10}), Counter({2019: 3}))
    rects_only = re.findall(r'<rect[^>]*height="([\d.]+)"', only_photo)
    rects_both = re.findall(r'<rect[^>]*height="([\d.]+)"', both)
    assert len(rects_only) == 1  # только фото -- один рисуемый bar, но строка полной высоты
    assert len(rects_both) == 2
    assert rects_only[0] == rects_both[0] == rects_both[1]  # та же sub_bar_h высота бара
    # Высота строки ОДИНАКОВАЯ независимо от того, один тип в году или оба -- не резервируется
    # переменно, всегда полная bar_h+gap.
    h_only = float(re.search(r'viewBox="0 0 [\d.]+ ([\d.]+)"', only_photo).group(1))
    h_both = float(re.search(r'viewBox="0 0 [\d.]+ ([\d.]+)"', both).group(1))
    assert h_only == h_both


def test_year_hbar_chart_dual_omits_zero_label_for_single_type_year():
    svg = r._svg_year_hbar_chart_dual(Counter({2020: 10}), Counter())
    assert ">0 файлов<" not in svg
    assert ">10 файлов<" in svg


def test_render_analyze_sheet2_uses_available_counts_not_found_for_type_pie():
    """2.1/2.2 -- на available-счётчиках, не found (Задача A/C) -- битый файл не должен
    попадать в диаграмму "доступного" типа/объёма."""
    stats = _FakeAnalyzeStats()
    stats.n_images = 5
    stats.n_images_available = 3
    stats.bytes_by_kind_available = Counter({"image": 999})
    model = r.build_model_from_analyze_stats(stats)
    assert model["counts_available"]["image"] == 3
    assert model["bytes_by_kind_available"]["image"] == 999


def test_render_analyze_sheet2_date_reliability_excludes_raw():
    stats = _FakeAnalyzeStats()
    stats.tier_counts = Counter({"A": 5, "C": 2, "D": 1})  # включает RAW
    stats.tier_counts_no_raw = Counter({"A": 4, "C": 1})  # без RAW
    model = r.build_model_from_analyze_stats(stats)
    html_out = r._render_analyze_sheet2(model)
    # Диаграмма строится из tier_counts_no_raw, не tier_counts -- "Точная (EXIF)" должна
    # показать 4, не 5 (число из RAW-инклюзивного счётчика).
    assert "4" in html_out


def test_disputed_records_render_archive_file_as_readable_text_not_dead_link(tmp_path):
    """Живая находка (SESSION-HANDOFF.txt, 2026-08-11), сквозной прогон run_analyze() ->
    generate_report_from_analyze_stats(): файл ИЗНУТРИ архива (size==0, disputed) раньше
    рендерился как file://-ссылка на несуществующий путь (tmp_extract уже вычищен) -- теперь
    disputed_records (Задача A) + _path_or_archive_list_checklist_item() (Задача D) рендерят
    его текстом "внутри архива Album.zip", без ссылки."""
    import zipfile
    import photosort_win as m
    source = tmp_path / "NewBatch"
    source.mkdir()
    target = tmp_path / "MyArchive"
    target.mkdir()
    workdir = tmp_path / "appdir"
    workdir.mkdir()
    with zipfile.ZipFile(source / "Album.zip", "w") as zf:
        zf.writestr("broken.jpg", b"")
    cfg = m.Config(source=str(source), target=str(target), sample_limit=0, workdir=str(workdir))

    stats = m.run_analyze(cfg, "analyze-quick", log=lambda *a, **k: None)
    out_path = tmp_path / "report.html"
    r.generate_report_from_analyze_stats(stats, str(out_path))
    html_out = out_path.read_text(encoding="utf-8")

    assert "не удалось распознать" in html_out
    assert "внутри архива Album.zip" in html_out
    # Нерабочая file://-ссылка на вычищенный tmp_extract-путь -- ровно то, что было багом.
    assert "file://" not in html_out or "tmp_extract" not in html_out.lower()


def test_path_or_archive_list_checklist_item_shows_full_folder_path():
    """Речь пользователя, 2026-08-11: заголовок группы файлов -- полный путь папки, не короткое
    базовое имя (_folder_label() -- раньше "Downloads" вместо "C:\\Users\\x\\Downloads")."""
    records = [
        {"in_archive": False, "abs_path": r"C:\Users\x\Downloads\k.bmp", "display": "k.bmp"},
        {"in_archive": False, "abs_path": r"C:\Users\x\Desktop\Telegram Desktop\a.png",
         "display": "a.png"},
    ]
    _, detail = r._path_or_archive_list_checklist_item("не удалось распознать", "hint ", records)
    assert r"C:\Users\x\Downloads (1" in detail
    assert r"C:\Users\x\Desktop\Telegram Desktop (1" in detail


def test_path_list_checklist_item_shows_full_folder_path():
    paths = [r"C:\Users\x\Downloads\k.bmp", r"C:\Users\x\Desktop\Telegram Desktop\a.png"]
    _, detail = r._path_list_checklist_item("не прочитано", "hint ", paths)
    assert r"C:\Users\x\Downloads" in detail
    assert r"C:\Users\x\Desktop\Telegram Desktop" in detail


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
    assert "<h2>Что скопировано</h2>" in html_out
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


def test_exact_dup_note_explains_undated_promotion_when_applicable(tmp_path):
    """Живая находка пользователя, 2026-08-24: текст дублей молча наводил на мысль о ручном
    вмешательстве -- реальная (частая) законная причина другая: недатированный файл
    (0000-undated) на более позднем прогоне "повысился" до датированного места, старая копия
    осталась (append-only). Пояснение показывается, ТОЛЬКО когда хотя бы один файл найденного
    кластера физически лежит в 0000-undated."""
    stats = _FakeAnalyzeStats()
    stats.exact_dup_edges = [
        {"dest": "ByDate/0000-undated/Users/a/hero.png", "matched_dest": "ByDate/2026/2026-07/hero.png"},
    ]
    stats.tier_counts = Counter()
    stats.n_tier_cd_bydate = 0
    html_out = r._render_passport_integrity(stats)
    assert "недатированный файл" in html_out
    assert "0000-undated" in html_out


def test_exact_dup_note_omits_undated_explanation_when_not_applicable():
    """Регрессия по значению: обычный дубль, не связанный с 0000-undated (например, ручная
    копия внутри одного альбома) -- пояснение про "повышение" даты НЕ должно появляться, чтобы
    не наводить на ложный след там, где он неприменим."""
    stats = _FakeAnalyzeStats()
    stats.exact_dup_edges = [
        {"dest": "Albums/A/orig.jpg", "matched_dest": "Albums/A/copy.jpg"},
    ]
    stats.tier_counts = Counter()
    stats.n_tier_cd_bydate = 0
    html_out = r._render_passport_integrity(stats)
    assert "недатированный файл" not in html_out
    assert "0000-undated" not in html_out


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

    def test_heading_and_footer_share_one_timestamp(self, tmp_path, monkeypatch):
        """Задача 10 (SESSION-HANDOFF.txt, 2026-08-09): эта страница -- ОТДЕЛЬНЫЙ файл, свой
        собственный _page_shell()/футер -- тоже получает "по состоянию на" в заголовке, тот же
        принцип "один strftime() на страницу", что и у report.html/passport.html."""
        calls = []

        def _fake_strftime(fmt):
            calls.append(fmt)
            return f"2026-08-09 13:{20 + len(calls)}"
        monkeypatch.setattr(r.time, "strftime", _fake_strftime)

        stats = _FakeAnalyzeStats()
        stats.exact_dup_edges = [{"dest": "Albums/A/orig.jpg", "matched_dest": "Albums/B/copy.jpg"}]
        out_path = tmp_path / "passport.html"
        r.generate_passport_verification_page(stats, str(out_path))
        html_out = (tmp_path / r.PASSPORT_VERIFICATION_FILENAME).read_text(encoding="utf-8")

        assert "<h2>Полная сверка — Паспорт архива</h2>" in html_out
        heading_m = re.search(r'report-meta-date">по состоянию на ([\d\-]+ [\d:]+)</div>', html_out)
        footer_m = re.search(r"Сформировано PhotoArchive[^·]*· ([\d\-]+ [\d:]+)</div>", html_out)
        assert heading_m and footer_m, html_out
        assert heading_m.group(1) == footer_m.group(1)

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
    """2026-08-15: график теперь строится из RAW-свободного сплита dates_by_year_photo/
    _video (та же логика, что и "Медиафайлы по годам" в остальном отчёте), не из
    dates_by_year (тот включает RAW) -- см. решение пользователя про единый вид "без RAW"."""
    stats = _FakeAnalyzeStats()
    stats.dates_by_year_photo = Counter({2019: 2, 2024: 3})
    stats.dates_by_year_video = Counter({2024: 2})
    out_path = tmp_path / "passport.html"
    r.generate_passport_report(stats, str(out_path))
    html_out = out_path.read_text(encoding="utf-8")
    assert "Медиафайлы по годам" in html_out
    assert "без учёта RAW" in html_out
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


def test_svg_hbar_chart_truncates_long_uppercase_label_conservatively():
    """2026-08-19, найдено при сверке лендинга с реальным кодом: старый лимит "не трогаем
    короче 26 символов, иначе обрезаем до 23+…" не учитывал ни font_size/margin_left, ни то,
    что подписи камер часто сплошь заглавные (шире средней буквы шрифта) -- на реальной EXIF-
    комбинации Make+Model вида "NIKON CORPORATION"+"NIKON D750" обрезанная строка (24 символа)
    всё равно физически вылезала за левый край SVG (подтверждено headless Chromium: x < 0),
    т.к. text-anchor="end" растит текст влево от фиксированной точки. Новый бюджет символов
    считается от реальной доступной ширины (margin_left-8)/font_size -- при дефолтных
    margin_left=170/font_size=12 не должен превышать 21 символ (20 + "…")."""
    long_label = "NIKON CORPORATION NIKON D750"  # 28 символов, реальная EXIF-комбинация
    html_out = r._svg_hbar_chart([(long_label, 10, "10 файлов")], aria_label="Тест")
    m = re.search(r'text-anchor="end"[^>]*>([^<]*)</text>', html_out)
    assert m, "не нашли подпись в SVG"
    shown = m.group(1)
    assert len(shown) <= 21, f"подпись слишком длинная для margin_left=170/font_size=12: {shown!r}"
    assert shown.endswith("…")


def test_svg_hbar_chart_keeps_short_label_untouched():
    html_out = r._svg_hbar_chart([("Canon EOS 80D", 10, "10 файлов")], aria_label="Тест")
    assert "Canon EOS 80D" in html_out
    assert "…" not in html_out


def test_svg_hbar_chart_truncates_cyrillic_uppercase_more_aggressively_than_latin():
    """Раунд 103 ревизора (2026-08-19): единый коэффициент 0.62 (калиброван на Latin
    uppercase) не покрывал Cyrillic uppercase -- заглавные кириллические буквы у большинства
    sans-serif шрифтов заметно шире латинских при том же font_size, и подтверждённый ревизором
    headless-Chromium кейс ("НОВЫЙ ГОД МОСКВА 2015", 21 символ -- короче старого лимита в 21
    символ, поэтому вообще НЕ обрезался) физически вылезал за левый край SVG. Не-ASCII подписи
    теперь обрезаются короче (бюджет символов ниже), чем такая же по длине ASCII-подпись."""
    cyrillic_label = "НОВЫЙ ГОД МОСКВА 2015"  # 21 символ -- ровно на старой границе необрезки
    latin_label = "N" * len(cyrillic_label)  # тот же символьный размер, чисто ASCII
    html_cyr = r._svg_hbar_chart([(cyrillic_label, 10, "10 файлов")], aria_label="Тест")
    html_lat = r._svg_hbar_chart([(latin_label, 10, "10 файлов")], aria_label="Тест")
    m_cyr = re.search(r'text-anchor="end"[^>]*>([^<]*)</text>', html_cyr)
    m_lat = re.search(r'text-anchor="end"[^>]*>([^<]*)</text>', html_lat)
    assert cyrillic_label not in html_cyr, "кириллическая ЗАГЛАВНАЯ подпись должна обрезаться"
    assert latin_label in html_lat, "тот же по длине ASCII (даже сплошь заглавный) не должен"
    assert len(m_cyr.group(1)) < len(m_lat.group(1))


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


# 2026-08-14, прямая просьба пользователя (тем же диалогом, что и "Структура источника"
# analyze): паспорт больше не строит собственное предсказанное дерево (_build_archive_tree()/
# _render_archive_tree_card()/_TREE_TOP_ORDER, старая пара функций, удалена целиком) -- живой
# тест поймал, что оно реально ОШИБАЛОСЬ на DVD-юните (VIDEO_TS внутри настоящего альбома
# показывался в ByDate-бакете, не там, где физически лежит), заменено на ту же
# _render_source_tree_card(), что уже питает "Структуру источника" analyze, с заголовком
# "Структура архива" и source_path=target_path (см. generate_passport_report()).


def test_generate_passport_report_includes_tree_when_data_present(tmp_path):
    stats = _FakeAnalyzeStats()
    stats.source_tree_counts_image = Counter({"Albums/Свадьба": 3})
    stats.source_tree_counts_video = Counter({"ByDate/2024/2024-07 [PhotoArchive]": 2})
    out_path = tmp_path / "passport.html"
    r.generate_passport_report(stats, str(out_path), target_path=r"D:\MyArchive")
    html_out = out_path.read_text(encoding="utf-8")
    assert "Структура архива" in html_out
    assert "Свадьба" in html_out
    assert "2024-07 [PhotoArchive]" in html_out
    assert "MyArchive" in html_out  # голова дерева -- путь TARGET


def test_generate_passport_report_omits_tree_when_empty(tmp_path):
    stats = _FakeAnalyzeStats()
    assert stats.source_tree_counts_image == Counter()
    assert stats.source_tree_counts_video == Counter()
    assert stats.source_tree_counts_raw == Counter()
    out_path = tmp_path / "passport.html"
    r.generate_passport_report(stats, str(out_path))
    html_out = out_path.read_text(encoding="utf-8")
    assert "Структура архива" not in html_out


def test_generate_passport_report_tree_shows_dvd_unit_inside_real_album_not_bydate(tmp_path):
    """Живая находка, поймавшая необходимость замены (2026-08-14): старое дерево
    (tree_folder_counts) клало DVD-юнит внутри настоящего альбома в ByDate-бакет вместо
    Albums-папки, где он физически лежит -- новое дерево строится из реального пути, ошибка
    физически невозможна (source_tree_counts_video уже несёт правильный путь узла)."""
    stats = _FakeAnalyzeStats()
    stats.source_tree_counts_video = Counter({"Albums/RealAlbum/VIDEO_TS": 2})
    out_path = tmp_path / "passport.html"
    r.generate_passport_report(stats, str(out_path))
    html_out = out_path.read_text(encoding="utf-8")
    assert "RealAlbum" in html_out
    assert "VIDEO_TS" in html_out
    # "ByDate" не должно появиться в самом ДЕРЕВЕ (<ul class="tree">) -- не во всей карточке
    # (её собственная подпись законно упоминает "Albums/ByDate" как противопоставление -- "не
    # предсказанные бакеты", см. generate_passport_report()) и не во всей странице (другие
    # карточки паспорта могут упоминать "по годам" законно).
    tree_start = html_out.index('<ul class="tree">')
    tree_end = html_out.index("</ul>", tree_start) + len("</ul>")
    assert "ByDate" not in html_out[tree_start:tree_end]


# 2026-08-14, прямая просьба пользователя: дерево реальной структуры SOURCE для analyze-отчёта
# (в отличие от _build_archive_tree() выше -- ПРЕДСКАЗАННАЯ раскладка Albums/ByDate/RAW/
# _Unsorted, здесь -- как файлы реально лежат сейчас, архивы отдельными ветками "так же, как и
# папки", ограничение глубины отображения с "*"-сворачиванием, только количество, без байт).
# Дополнение тем же днём: ОДИН видимый корень (имя/путь источника), не разные соседние ветки
# без общего родителя; счётчик на узел -- разбивка "x/y/z файлов" (фото/видео/RAW), не общая
# сумма; расшифровка формата -- один раз, в начале карточки, не на каждый узел.
_EMPTY = Counter()


def test_build_source_tree_own_count_is_counter_by_kind_not_int_or_bytes_pair():
    tree = r._build_source_tree(Counter({"Album": 2}), _EMPTY, _EMPTY)
    assert tree["children"]["Album"]["own"] == Counter({"image": 2})  # не голый int/bytes-пара


def test_build_source_tree_nests_by_path_segments():
    tree = r._build_source_tree(Counter({"A": 1, "A/B": 2}), _EMPTY, _EMPTY)
    a = tree["children"]["A"]
    assert a["own"] == Counter({"image": 1})
    assert a["children"]["B"]["own"] == Counter({"image": 2})


def test_build_source_tree_combines_all_three_kinds_on_same_node():
    tree = r._build_source_tree(Counter({"Album": 1}), Counter({"Album": 3}), Counter({"Album": 0}))
    assert tree["children"]["Album"]["own"] == Counter({"image": 1, "video": 3})


def test_build_source_tree_root_level_files_go_directly_into_root_own():
    """Ключ "" (файл прямо в корне SOURCE, см. _source_tree_parent_key()) больше не заводит
    отдельный синтетический узел-лист (2026-08-14, прямая просьба пользователя: у дерева
    теперь есть видимая шапка с именем источника, см. _render_source_tree_card()) -- идёт
    прямо в own самого root."""
    tree = r._build_source_tree(Counter({"": 3}), _EMPTY, _EMPTY)
    assert tree["own"] == Counter({"image": 3})
    assert tree["children"] == {}


def test_render_source_tree_card_empty_counters_render_nothing():
    assert r._render_source_tree_card(_EMPTY, _EMPTY, _EMPTY) == ""


def test_render_source_tree_card_single_root_with_source_name_wraps_everything():
    """Прямая просьба пользователя, 2026-08-14: "дерево должно быть одним для одного
    источника, а не разные ветки. В голове должно быть имя(путь) источника" -- ровно один
    верхнеуровневый <li> (корень с именем источника), найденные папки -- ЕГО дети внутри
    вложенного <ul>, не соседи корня без общего родителя."""
    html_out = r._render_source_tree_card(Counter({"Album": 1, "Other": 1}), _EMPTY, _EMPTY,
                                            source_path="D:\\Фото")
    assert html_out.count('<ul class="tree">') == 1
    tree_body = html_out.split('<ul class="tree">', 1)[1]
    assert tree_body.count("<li>") >= 3  # корень + Album + Other
    assert ">D:\\Фото<" in html_out
    # Корень -- единственный ПРЯМОЙ ребёнок <ul class="tree">, Album/Other -- внутри его <ul>
    root_start = tree_body.index("<li>")
    first_nested_ul = tree_body.index("<ul>", root_start)
    assert tree_body.index(">Album<") > first_nested_ul
    assert tree_body.index(">Other<") > first_nested_ul


def test_render_source_tree_card_falls_back_to_generic_label_without_source_path():
    html_out = r._render_source_tree_card(Counter({"Album": 1}), _EMPTY, _EMPTY)
    assert ">Источник<" in html_out


def test_render_source_tree_card_root_stat_is_own_root_files_not_grand_total():
    """Уточнение пользователя, 2026-08-14: "медиафайлы, которые в корне пути, должны
    показываться напротив имени источника (головы дерева)" -- ТОЛЬКО файлы прямо в корне
    SOURCE (own), не сумма по всему дереву (та же семантика own, что и у любого другого узла)."""
    html_out = r._render_source_tree_card(Counter({"": 1, "Album": 2}), _EMPTY, _EMPTY,
                                            source_path="D:\\Фото")
    root_line = html_out.split(">D:\\Фото<", 1)[1].split("</span>", 1)[0]
    assert "1/0/0" in root_line  # только файл прямо в корне -- 1, не 1+2=3


def test_render_source_tree_card_root_shows_no_stat_when_nothing_directly_in_root():
    """Частый случай -- у корня нет собственных файлов, всё лежит в подпапках (та же
    семантика, что уже подтверждена для промежуточных узлов вроде "Домашнее видео")."""
    html_out = r._render_source_tree_card(Counter({"Album": 2}), _EMPTY, _EMPTY,
                                            source_path="D:\\Фото")
    root_line = html_out.split(">D:\\Фото<", 1)[1].split("<ul>", 1)[0]
    assert "tree-stat" not in root_line


def test_render_source_tree_card_folders_without_media_are_never_created():
    """Не отдельный фильтр -- прямое следствие того, что узлы строятся ТОЛЬКО из путей файлов,
    реально найденных как медиа (см. AnalyzeStats.source_tree_counts_image/video/raw) -- папка
    без единого медиафайла нигде в поддереве никогда не попадает в counts вообще."""
    html_out = r._render_source_tree_card(Counter({"Album": 1}), _EMPTY, _EMPTY)
    assert "Album" in html_out
    assert "EmptyFolder" not in html_out


def test_render_source_tree_card_shows_archive_branch_same_as_folder():
    html_out = r._render_source_tree_card(Counter({"Incoming/Album": 2}), _EMPTY, _EMPTY)
    assert ">Incoming<" in html_out
    assert ">Album<" in html_out


def test_render_source_tree_card_within_depth_limit_shows_full_nesting_no_star():
    counts = Counter({"A/B/C/D": 5})  # ровно 4 уровня -- граница, ещё не свёрнуто
    html_out = r._render_source_tree_card(counts, _EMPTY, _EMPTY)
    assert ">D<" in html_out  # последний уровень показан листом, без "*"
    assert "D*" not in html_out
    assert "более глубокая вложенность" not in html_out  # легенда "*" не нужна -- нечего скрывать


def test_render_source_tree_card_beyond_depth_limit_collapses_and_marks_with_star():
    counts = Counter({"A/B/C/D/E": 5})  # 5 уровней -- глубже лимита (4), сворачивается на D
    html_out = r._render_source_tree_card(counts, _EMPTY, _EMPTY)
    assert ">D*<" in html_out  # "*" -- прямая просьба пользователя, не "E" отдельным узлом
    assert ">E<" not in html_out
    assert "5/0/0" in html_out  # весь счёт E ушёл в own D (роллап), не потерян
    assert "более глубокая вложенность" in html_out  # легенда "*"


def test_render_source_tree_card_collapsed_node_sums_entire_hidden_subtree():
    """Роллап суммирует ВСЁ поддерево за границей глубины, не только один непосредственный
    уровень -- две ветки за пределами лимита из одного и того же узла D."""
    counts = Counter({"A/B/C/D/E": 2, "A/B/C/D/F/G": 3})
    html_out = r._render_source_tree_card(counts, _EMPTY, _EMPTY)
    assert ">D*<" in html_out
    assert "5/0/0" in html_out  # 2 + 3, обе ветки свёрнуты в один узел D


def test_render_source_tree_card_no_bytes_shown_only_file_count():
    html_out = r._render_source_tree_card(Counter({"Album": 7}), _EMPTY, _EMPTY)
    assert "tree-stat" in html_out
    assert "Б" not in html_out.split("<h2>")[1].split("</div>")[0]  # ни КБ/МБ/ГБ рядом с узлами


def test_source_tree_stat_text_order_is_photo_video_raw():
    """Прямая просьба пользователя, 2026-08-14: "Количество файлов показывать фото/видео/raw
    (1/3/0 файлов)" -- ровно этот порядок, не алфавитный и не порядок объявления полей
    AnalyzeStats (image/raw/video)."""
    text = r._source_tree_stat_text(Counter({"image": 1, "video": 3, "raw": 0}))
    assert "1/3/0" in text


def test_source_tree_stat_text_empty_when_all_three_zero():
    assert r._source_tree_stat_text(Counter()) == ""


def test_render_source_tree_card_intermediate_node_without_own_files_shows_no_stat():
    """Живой пример пользователя (превью, 2026-08-14): "Домашнее видео"/"Свадьба.zip" не имеют
    медиафайлов в СВОЁМ корне (только в подпапках/VIDEO_TS/sub) -- у таких узлов вообще нет
    строки статистики, не "0/0/0"."""
    html_out = r._render_source_tree_card(Counter({"Домашнее видео/VIDEO_TS": 3}), _EMPTY, _EMPTY)
    parent_li = html_out.split(">Домашнее видео<", 1)[1].split("<ul>", 1)[0]
    assert "tree-stat" not in parent_li  # у "Домашнее видео" own пуст -- дети есть, стата нет
    assert "0/0/0" not in html_out


def test_render_source_tree_card_format_legend_appears_before_the_tree():
    """Прямая просьба пользователя, 2026-08-14: "Расшифровка x/y/z - должно быть в начале
    дерева как справка" -- легенда формата ДО <ul class="tree">, не после. Дополнение тем же
    днём: сам буквенный код "x/y/z" убран из текста легенды (пользователь счёл его лишним --
    "и без неё всё понятно"), но пояснение "фото/видео/RAW" в этой же строке остаётся."""
    html_out = r._render_source_tree_card(Counter({"Album": 1}), _EMPTY, _EMPTY)
    legend_pos = html_out.index("фото/видео/RAW")
    tree_pos = html_out.index('<ul class="tree">')
    assert legend_pos < tree_pos
    assert "x/y/z" not in html_out


def test_generate_report_from_analyze_stats_includes_source_tree_card(tmp_path):
    stats = _FakeAnalyzeStats()
    stats.source_tree_counts_image = Counter({"Album": 3})
    out_path = tmp_path / "report.html"
    r.generate_report_from_analyze_stats(stats, str(out_path), level="analyze")
    html_out = out_path.read_text(encoding="utf-8")
    assert "Структура источника" in html_out
    assert "Album" in html_out


def test_generate_report_from_analyze_stats_omits_source_tree_card_when_empty(tmp_path):
    stats = _FakeAnalyzeStats()
    assert stats.source_tree_counts_image == Counter()
    assert stats.source_tree_counts_video == Counter()
    assert stats.source_tree_counts_raw == Counter()
    out_path = tmp_path / "report.html"
    r.generate_report_from_analyze_stats(stats, str(out_path), level="analyze")
    html_out = out_path.read_text(encoding="utf-8")
    assert "Структура источника" not in html_out


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
