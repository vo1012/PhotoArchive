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
    (RunLogs.appended(), новая с этого фикса). full date -> (year, month, day); place -- сам
    альбом (geo-lookup для Albums-файлов не делается вообще, place всегда None из колонки)."""
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
    assert model["oldest"] == ((2015, 0, 0), "s2", "Свадьба")


def test_albums_file_without_date_column_still_invisible_old_archive_behavior():
    """Старый архив (appended.csv без колонки "date", собран версией до этого фикса) --
    поведение не меняется, файл по-прежнему не участвует в датах (ничего не восстановить)."""
    rows = [{"timestamp": "t", "source": "s1", "dest": r"C:\T\dst\Albums\Отпуск 2019\a.jpg",
             "reason": "appended_new", "flags": ""}]
    model = r.build_model_from_rows({"appended": rows})
    assert model["years"] == Counter()
    assert model["oldest"] is None


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
    assert "Нравится результат" in html_out


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
        self.total_bytes = 54321
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
    # Пакет п.2: total_bytes -- объём просканированного SOURCE (stats.total_bytes), не
    # predicted_unique_bytes ("что добавилось бы после дедупа", 0 для analyze-quick/[1]).
    assert model["total_bytes"] == 54321


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
