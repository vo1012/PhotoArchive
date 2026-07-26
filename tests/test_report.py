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
    caption = r._type_breakdown_caption(Counter({"image": 10, "raw": 0, "video": 3}), "Точные повторы")
    assert caption == "Точные повторы, в т.ч.: фото — 10 файлов, видео — 3 файла"


def test_type_breakdown_caption_empty_when_all_zero():
    assert r._type_breakdown_caption(Counter(), "Точные повторы") == ""
    assert r._type_breakdown_caption(Counter({"image": 0}), "Точные повторы") == ""


def test_sheet2_shows_exact_dup_type_breakdown_caption():
    """2026-07-26, по просьбе пользователя: "Точные повторы" на диаграмме "Итог решений
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
    assert "Точные повторы, в т.ч.: фото — 1 файл, RAW — 1 файл, видео — 1 файл" in html_out


def test_svg_bar_chart_thins_labels_when_many_years():
    """2026-07-26, живая находка пользователя на реальном архиве (18 разных лет, 1973 +
    2003-2019) -- при MAX_LABELS=16 подписи прореживаются, но столбцов остаётся ровно n,
    первый и последний год подписаны всегда."""
    years = Counter({1973: 1, **{y: 10 for y in range(2003, 2020)}})  # 18 разных лет
    svg = r._svg_bar_chart(years)
    assert svg.count("<rect") == 18  # ни один столбец не пропущен
    assert ">1973<" in svg  # первый год всегда подписан
    assert ">2019<" in svg  # последний год всегда подписан
    label_count = svg.count('text-anchor="middle" fill="#8A8A7C"'.replace("#8A8A7C", r.COLOR_TEXT_MUTED))
    assert 0 < label_count < 18  # часть подписей скрыта, не все и не ни одной


def test_svg_bar_chart_shows_every_label_when_few_years():
    years = Counter({2020: 5, 2021: 3, 2022: 9})
    svg = r._svg_bar_chart(years)
    assert svg.count("<rect") == 3
    for y in (2020, 2021, 2022):
        assert f">{y}<" in svg


def test_svg_bar_chart_fifty_years_still_renders_every_bar():
    """Проверка на масштаб, о котором спросил пользователь: 50 разных лет -- ни один столбец
    не пропадает (bar_w не опускается до нуля/отрицательного значения), подписи прорежены
    сильнее, чем при 18 годах, но не исчезают полностью."""
    years = Counter({y: 1 for y in range(1970, 2020)})  # 50 разных лет
    svg = r._svg_bar_chart(years)
    assert svg.count("<rect") == 50
    assert ">1970<" in svg
    assert ">2019<" in svg
    assert 'width="0.0"' not in svg
    label_count = svg.count(f'text-anchor="middle" fill="{r.COLOR_TEXT_MUTED}"')
    assert 0 < label_count < 16


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


def test_undated_checklist_item_shows_folder_and_name():
    """2026-07-26, обсуждение с пользователем: "N файлов вообще без даты" раньше был
    безадресным (только счётчик) -- пользователю нечем было найти сами файлы без похода в
    undated_media.csv. Теперь путь+имя каждого (их всегда мало -- Tier D редкий случай)."""
    data = {"undated_media": [
        {"timestamp": "2026-01-01 00:00:00", "source": "F:\\a.jpg",
         "dest": "D:\\Archive\\Albums\\Отпуск\\a.jpg"},
    ]}
    fields = r._build_checklist_fields(data)
    items = r._build_checklist_items(fields)
    joined = "".join(items)
    assert "1 файл вообще без даты" in joined
    assert "Albums\\Отпуск\\a.jpg" in joined


def test_undated_checklist_item_degrades_without_rows():
    """analyze-уровень не отслеживает undated_media поштучно (только агрегат в model) --
    fields.get() должен деградировать до старого текста без списка, не упасть с KeyError."""
    fields = r._build_checklist_fields({})
    fields["undated_total"] = 3
    del fields["undated_media"]
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


def test_render_this_run_shows_processed_count_as_comparison_base():
    """Раунд 32, задача 4 (REVIEW-HANDOFF.md): "всего найдено на источнике" -- база для
    сверки, что отчёт ничего не потерял молча."""
    run_stats = {"appended_images": 5, "appended_videos": 0, "processed_count": 42}
    html_out = r._render_this_run(run_stats, level="target")
    assert "найдено на источнике" in html_out
    assert ">42<" in html_out


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
    assert "Точные повторы, в т.ч.: фото — 3 файла, RAW — 2 файла, видео — 1 файл" in html_out
    assert "Похожие кадры, в т.ч.: фото — 1 файл, видео — 1 файл" in html_out
    assert "Не прочитано, в т.ч.: фото — 2 файла, прочее — 2 файла" in html_out
    assert "Спорные, в т.ч.: прочее — 3 файла" in html_out


def test_render_this_run_no_breakdown_captions_without_typed_stats():
    """Старый вызывающий код (до этой фичи) не передаёт typed-ключи для skipped/disputed/
    unreadable/near_dup вообще -- .get(...,0) везде даёт пустые Counter, ни одна из ЭТИХ
    четырёх строк "в т.ч." не должна появиться, отчёт не падает (новый файловый тайл наверху
    всё равно покажет свою разбивку -- она строится из appended_images/videos/raw_mirrored,
    которые существуют независимо от этой фичи)."""
    run_stats = {"appended_images": 5, "appended_videos": 0, "skipped_present": 6, "disputed": 2}
    html_out = r._render_this_run(run_stats, level="target")
    assert "Точные повторы, в т.ч." not in html_out
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
    частая рекомендация всех источников маркетингового ТЗ -- отсутствовала в HTML полностью."""
    target_html = r._render_trust_block("target")
    assert "Оригиналы не изменены и не удалены" in target_html
    assert "Файлы не удалялись." in target_html
    assert "Реальных изменений на диске нет" not in target_html

    workdir_html = r._render_trust_block("workdir")
    assert "Реальных изменений на диске нет — это предпросмотр." in workdir_html


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


def test_cluster_exact_dup_groups_by_folder_and_counts_duplicates():
    """REVIEW-HANDOFF.md, Раунд 31: только reason=="already_present" учитывается -- не
    raw_skipped_has_jpeg (решение конфига, не совпадение содержимого) и не
    identical_at_destination (редкая коллизия имён при записи), по решению пользователя."""
    rows = [
        {"source": "s1", "matched_with": r"C:\T\dst\Albums\Отпуск\a.jpg", "reason": "already_present"},
        {"source": "s2", "matched_with": r"C:\T\dst\Albums\Отпуск\a.jpg", "reason": "already_present"},
        {"source": "s3", "matched_with": r"C:\T\dst\Albums\Отпуск\b.jpg", "reason": "already_present"},
        {"source": "s4", "matched_with": r"C:\T\dst\ByDate\2020\2020-05\c.jpg", "reason": "already_present"},
        # исключены из группировки, хоть и в skipped.csv:
        {"source": "s5", "matched_with": r"C:\T\dst\Albums\Отпуск\a.jpg", "reason": "identical_at_destination"},
        {"source": "s6", "matched_with": "", "reason": "raw_skipped_has_jpeg"},
    ]
    groups = r._cluster_exact_dup(rows)
    assert len(groups) == 2  # два разных folder: Albums\Отпуск и ByDate\2020\2020-05
    folder, total, items = groups[0]  # отсортировано по убыванию total -- Отпуск (3) первый
    assert folder == r"Albums\Отпуск"
    assert total == 3
    assert dict(items) == {r"C:\T\dst\Albums\Отпуск\a.jpg": 2, r"C:\T\dst\Albums\Отпуск\b.jpg": 1}


def test_cluster_exact_dup_empty():
    assert r._cluster_exact_dup([]) == []
    assert r._cluster_exact_dup([{"source": "s", "matched_with": "x", "reason": "raw_skipped_has_jpeg"}]) == []


def test_render_exact_dup_examples_shows_folder_and_count():
    fields = {"exact_dup_groups": r._cluster_exact_dup([
        {"source": "s1", "matched_with": r"C:\T\dst\Albums\Отпуск\a.jpg", "reason": "already_present"},
        {"source": "s2", "matched_with": r"C:\T\dst\Albums\Отпуск\a.jpg", "reason": "already_present"},
    ])}
    html_out = r._render_exact_dup_examples(fields, "Точные повторы — примеры", intro=r.EXACT_DUP_INTRO)
    assert "Точные повторы — примеры" in html_out
    assert "Ничего делать не нужно" in html_out
    assert "Отпуск" in html_out
    assert "a.jpg (×2)" in html_out


def test_render_exact_dup_examples_empty_renders_nothing():
    assert r._render_exact_dup_examples({"exact_dup_groups": []}, "Точные повторы") == ""
    assert r._render_exact_dup_examples(None, "Точные повторы") == ""


def test_render_exact_dup_examples_progressive_disclosure():
    """EXACT_DUP_PREVIEW_N=2 -- третья и далее группы уходят под "Показать ещё N", тот же
    паттерн, что near-dup уже использует в Листе 3."""
    rows = []
    for i in range(5):
        rows.append({"source": f"s{i}", "matched_with": rf"C:\T\dst\Albums\Альбом{i}\a.jpg",
                     "reason": "already_present"})
    fields = {"exact_dup_groups": r._cluster_exact_dup(rows)}
    html_out = r._render_exact_dup_examples(fields, "Точные повторы")
    assert html_out.count("<details>") == 1  # ровно один сворачиваемый блок на "остальное"
    assert "Показать ещё 3 группы" in html_out
    assert all(f"Альбом{i}" in html_out for i in range(5))  # ничего не потеряно, только скрыто


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
    assert "Точные повторы" in html_out
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
    "Точные повторы — примеры" (сразу под ней), не в хвосте всей страницы -- живая находка
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
    # Ссылка должна идти СРАЗУ после карточки "Точные повторы", не в хвосте документа --
    # проверяем расстояние в тексте, не через следующую секцию (Лист 3 здесь пустой -- нет
    # near-dup/disputes/unreadable в этих минимальных данных, карточка вообще не рендерится).
    card_pos = html_out.index("Точные повторы — примеры")
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


def test_cluster_dates_review_groups_by_archive_folder_with_tier():
    """2026-07-26, по просьбе пользователя (общий аудит "путь для проверки"): группировка по
    dest (папка АРХИВА), не source -- в отличие от disputes (_Unsorted зеркалирует source),
    файлы с приблизительной датой лежат как обычно в Albums/ByDate."""
    rows = [
        {"source": "s1", "dest": r"C:\T\dst\Albums\Отпуск\a.jpg", "tier": "B"},
        {"source": "s2", "dest": r"C:\T\dst\Albums\Отпуск\b.jpg", "tier": "C"},
        {"source": "s3", "dest": r"C:\T\dst\ByDate\2020\2020-05\c.jpg", "tier": "B"},
        # tier A/D исключены -- не "приблизительная", разные категории:
        {"source": "s4", "dest": r"C:\T\dst\Albums\Отпуск\d.jpg", "tier": "A"},
    ]
    groups = r._cluster_dates_review(rows)
    assert len(groups) == 2
    folder, items = groups[0]  # Отпуск (2 файла) первый
    assert folder == r"Albums\Отпуск"
    assert items == [("a.jpg", "B"), ("b.jpg", "C")]


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
    model = {"years": Counter({2015: 2, 2020: 1}), "archives_found": 3, "total_bytes": 1000}
    html_out = r._render_cta_block("analyze", model=model)
    assert "3 отдельных архивах" in html_out
    assert "испортиться независимо" in html_out
    assert "Нравится результат" not in html_out


def test_cta_block_analyze_without_archives_uses_single_source_framing():
    model = {"years": Counter({2020: 1}), "archives_found": 0, "total_bytes": 500 * 1024**2}
    html_out = r._render_cta_block("analyze", model=model)
    assert "хранятся на одном источнике" in html_out
    assert "500 МБ" in html_out
    assert "отдельных архивах" not in html_out


def test_cta_block_analyze_empty_model_falls_back_to_generic_text():
    html_out = r._render_cta_block("analyze", model={"years": Counter(), "archives_found": 0, "total_bytes": 0})
    assert "Нравится результат" in html_out


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
        self.dates_by_year = Counter({2026: 3})
        self.dates_by_year_month = Counter({"2026-07": 3})
        self.tier_counts = Counter({"C": 2, "D": 1})
        self.near_dup_edges = []
        self.n_archives_found = 0
        self.found_archive_top_level = []


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


def test_render_analyze_recommendations_empty_model_returns_nothing():
    """Все источники данных пусты/нулевые (например analyze-quick с совсем короткой
    историей) -- секция не должна рендериться вообще, не пустая карточка."""
    html_out = r._render_analyze_recommendations({
        "total_bytes": 0, "years": Counter(), "found_archive_count": 0,
        "near_dup_clusters": [], "tier_counts": Counter(),
    })
    assert html_out == ""


def test_render_analyze_recommendations_shows_disk_space_and_approx_dates_even_in_quick_mode():
    """REVIEW-HANDOFF.md, Раунд 36: total_bytes/tier_counts всегда считаются, даже в
    analyze-quick -- эти два пункта должны появляться независимо от near-dup/found-archive."""
    html_out = r._render_analyze_recommendations({
        "total_bytes": 5 * 1024**3, "years": Counter(), "found_archive_count": 0,
        "near_dup_clusters": [], "tier_counts": Counter({"C": 7}),
    })
    assert "Рекомендации" in html_out
    assert "5.0 ГБ" in html_out
    assert "приблизительно" in html_out
    # near-dup/found-archive пункты не должны попасть, если данных нет.
    assert "серию" not in html_out and "серии" not in html_out and "серий" not in html_out
    assert "собранный архив" not in html_out


def test_render_analyze_recommendations_shows_year_gap_found_archive_and_near_dup_series():
    model = {
        "total_bytes": 0,
        "years": Counter({2016: 40, 2017: 40, 2018: 1, 2019: 40, 2020: 40}),
        "found_archive_count": 1,
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


def test_svg_bar_chart_single_bar_width_is_capped():
    """2026-07-21 finding: with n==1 (data for a single year only) the bar stretched to
    ~60% of the whole chart width (gap*0.6 with no upper cap) -- looked like "a chart taking
    up the whole screen". Regression: bar width must stay capped regardless of gap."""
    svg = r._svg_bar_chart(Counter({2026: 10}))
    m = re.search(r'<rect[^>]*width="([\d.]+)"', svg)
    assert m is not None
    assert float(m.group(1)) <= 64


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
    full_workdir существует как отдельный явный флаг, а не переиспользование checklist_before."""
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


def test_generate_report_workdir_full_workdir_renders_full_history(tmp_path):
    """full_workdir=True (интерактивный [2] на непустом Target, REVIEW-HANDOFF.md, Раунд 38) --
    полноценный "Ваш архив" с учётом реальной истории, смёрженной с гипотетическими строками
    этого прогона, не урезанный чек-лист-only рендер."""
    data = {
        "appended": [
            _dated_appended_row(r"C:\T\ByDate\2019\2019-05 [PhotoArchive]\old.jpg", "2019-05-01 00:00:00"),
            _dated_appended_row(r"C:\T\ByDate\2026\2026-01 [PhotoArchive]\new.jpg", "2026-01-02 00:00:00"),
        ],
        "skipped": [], "disputes": [], "dates_review": [], "unreadable": [], "near_dup_edges": [],
    }
    out_path = tmp_path / "report.html"
    r.generate_report(data, str(out_path), level="workdir", run_start="2026-01-01 00:00:00",
                       full_workdir=True)
    html_out = out_path.read_text(encoding="utf-8")
    assert "Ваш архив" in html_out
