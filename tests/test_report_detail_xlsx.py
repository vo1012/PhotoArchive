"""PROMPT_report_detail_xlsx.md, Фаза 0/1 (2026-08-16): построитель плоских строк
(_build_detail_rows) + запись .xlsx (generate_detail_xlsx) в report_detail_xlsx.py, плюс
проводка кнопки «Детализированный отчёт» через report.generate_report(). Плюс (тем же днём,
"Открыто на момент записи" спеки) -- Паспорт архива: _build_passport_detail_rows()/
generate_passport_detail_xlsx(), через report.generate_passport_report()."""
from types import SimpleNamespace

from openpyxl import load_workbook

import report as r
import report_detail_xlsx as rx
from test_report import _FakeAnalyzeStats


def _appended(source, dest, reason="appended_new", ts="2026-01-01 00:00:01"):
    return {"timestamp": ts, "source": source, "dest": dest, "reason": reason, "flags": "",
            "date": "", "duration": "", "place": "", "camera": ""}


def _skipped(source, matched_with, reason, ts="2026-01-01 00:00:01"):
    return {"timestamp": ts, "source": source, "matched_with": matched_with, "reason": reason}


def _disputed(source, dest, reason, ts="2026-01-01 00:00:01"):
    return {"timestamp": ts, "source": source, "reason": reason, "dest": dest, "was_hidden": 0}


def _unreadable(source, error, ts="2026-01-01 00:00:01"):
    return {"timestamp": ts, "source": source, "error": error}


class TestBuildDetailRows:
    def test_normal_appended_row_has_no_note_and_is_copied(self):
        rows = rx._build_detail_rows({"appended": [
            _appended(r"D:\SOURCE\Vacation\a.jpg", r"D:\TARGET\Albums\Vacation\a.jpg"),
        ]})
        assert len(rows) == 1
        row = rows[0]
        assert row["folder"] == r"D:\SOURCE\Vacation"
        assert row["name"] == "a.jpg"
        assert row["ext"] == "jpg"
        assert row["kind"] == "image"
        assert row["copied"] is True
        assert row["dest_or_dup"] == r"D:\TARGET\Albums\Vacation\a.jpg"
        assert row["final_name"] == ""
        assert row["series_id"] == 0
        assert row["note"] == ""
        assert row["color"] is None

    def test_renamed_dest_sets_final_name_and_note(self):
        rows = rx._build_detail_rows({"appended": [
            _appended(r"D:\SOURCE\a.jpg", r"D:\TARGET\ByDate\2020\a_1.jpg"),
        ]})
        row = rows[0]
        assert row["final_name"] == "a_1.jpg"
        assert "переименовано" in row["note"]

    def test_near_dup_cluster_sets_series_id_and_note_for_all_members(self):
        data = {
            "appended": [
                _appended(r"D:\SOURCE\a.jpg", r"D:\TARGET\a.jpg"),
                _appended(r"D:\SOURCE\b.jpg", r"D:\TARGET\b.jpg"),
            ],
            "near_dup_edges": [
                {"timestamp": "2026-01-01 00:00:01", "source": r"D:\SOURCE\b.jpg",
                 "dest": r"D:\TARGET\b.jpg", "matched_dest": r"D:\TARGET\a.jpg",
                 "category": "appended_near_dup", "hamming": "3"},
            ],
        }
        rows = rx._build_detail_rows(data)
        by_name = {row["name"]: row for row in rows}
        assert by_name["a.jpg"]["series_id"] == by_name["b.jpg"]["series_id"] == 1
        assert "похожая серия" in by_name["a.jpg"]["note"]
        assert "похожая серия" in by_name["b.jpg"]["note"]

    def test_tier_b_or_c_date_sets_approximate_date_note(self):
        """PROMPT_report_detail_xlsx.md, "Примечание": «дата приблизительная» -- дата ЕСТЬ, но
        не по EXIF (Tier B/C, dates_review.csv). Tier A (dest отсутствует в dates_review.csv)
        -- без пометки."""
        data = {
            "appended": [
                _appended(r"D:\SOURCE\a.jpg", r"D:\TARGET\a.jpg"),
                _appended(r"D:\SOURCE\b.jpg", r"D:\TARGET\b.jpg"),
                _appended(r"D:\SOURCE\c.jpg", r"D:\TARGET\c.jpg"),
            ],
            "dates_review": [
                {"timestamp": "2026-01-01 00:00:01", "dest": r"D:\TARGET\a.jpg",
                 "date": "2020-01-01", "tier": "B", "confidence": "", "evidence": "",
                 "source": r"D:\SOURCE\a.jpg"},
                {"timestamp": "2026-01-01 00:00:01", "dest": r"D:\TARGET\b.jpg",
                 "date": "2020-01-01", "tier": "C", "confidence": "", "evidence": "",
                 "source": r"D:\SOURCE\b.jpg"},
            ],
        }
        rows = rx._build_detail_rows(data)
        by_name = {row["name"]: row for row in rows}
        assert "дата приблизительная" in by_name["a.jpg"]["note"]
        assert "дата приблизительная" in by_name["b.jpg"]["note"]
        assert "дата приблизительная" not in by_name["c.jpg"]["note"]
        assert by_name["c.jpg"]["note"] == ""

    def test_dvd_unit_multi_file_appended_collapses_to_one_row(self):
        """REVIEW-HANDOFF.md, Раунд 95 [БЛОКЕР]: _process_dvd_item() (photosort_win.py) вызывает
        run_logs.appended() на КАЖДЫЙ файл юнита -- appended.csv реально содержит одну строку
        на .VOB/.IFO/.BUP, не одну на весь диск. Сценарий ревизора (4 файла одного юнита,
        общий reason/dest-папка) -- построитель обязан свернуть их в одну строку."""
        dvd_reason = "DVD-Video (VIDEO_TS), скопирован целиком"
        dest_dir = r"D:\TARGET\Albums\DVD5\VIDEO_TS"
        rows = rx._build_detail_rows({"appended": [
            _appended(r"D:\RIP\DVD5/VIDEO_TS.IFO", dest_dir + r"\VIDEO_TS.IFO", reason=dvd_reason),
            _appended(r"D:\RIP\DVD5/VTS_01_0.BUP", dest_dir + r"\VTS_01_0.BUP", reason=dvd_reason),
            _appended(r"D:\RIP\DVD5/VTS_01_0.VOB", dest_dir + r"\VTS_01_0.VOB", reason=dvd_reason),
            _appended(r"D:\RIP\DVD5/VTS_01_1.VOB", dest_dir + r"\VTS_01_1.VOB", reason=dvd_reason),
        ]})
        assert len(rows) == 1
        row = rows[0]
        assert row["name"] == "VIDEO_TS"
        assert row["kind"] == "video"
        assert row["copied"] is True
        assert row["dest_or_dup"] == dest_dir
        assert row["final_name"] == ""
        assert row["series_id"] == 0
        assert "DVD" in row["note"]
        assert "4 файла" in row["note"]

    def test_dvd_unit_dest_collision_suffix_shown_as_final_name(self):
        """_unique_dvd_dest_name() суффиксирует папку при коллизии двух физически разных
        дисков ("VIDEO_TS (2)") -- та же идея "переименовано", что и у обычного файла, но на
        уровне папки-юнита."""
        dvd_reason = "DVD-Video (VIDEO_TS), скопирован целиком"
        dest_dir = r"D:\TARGET\Albums\DVD5\VIDEO_TS (2)"
        rows = rx._build_detail_rows({"appended": [
            _appended(r"D:\RIP\DVD5/VIDEO_TS.IFO", dest_dir + r"\VIDEO_TS.IFO", reason=dvd_reason),
        ]})
        assert rows[0]["final_name"] == "VIDEO_TS (2)"

    def test_two_distinct_dvd_units_stay_separate_rows(self):
        dvd_reason = "DVD-Video (VIDEO_TS), скопирован целиком"
        rows = rx._build_detail_rows({"appended": [
            _appended(r"D:\RIP\DiskA/VIDEO_TS.IFO", r"D:\TARGET\A\VIDEO_TS\VIDEO_TS.IFO", reason=dvd_reason),
            _appended(r"D:\RIP\DiskA/VTS_01_0.VOB", r"D:\TARGET\A\VIDEO_TS\VTS_01_0.VOB", reason=dvd_reason),
            _appended(r"D:\RIP\DiskB/VIDEO_TS.IFO", r"D:\TARGET\B\VIDEO_TS\VIDEO_TS.IFO", reason=dvd_reason),
        ]})
        assert len(rows) == 2
        note_by_dest = {row["dest_or_dup"]: row["note"] for row in rows}
        assert "2 файла" in note_by_dest[r"D:\TARGET\A\VIDEO_TS"]
        assert "1 файл)" in note_by_dest[r"D:\TARGET\B\VIDEO_TS"]

    def test_dvd_unit_nested_subfolder_files_still_collapse_to_one_row(self):
        """REVIEW-HANDOFF.md, Раунд 96 (придирка): _dvd_unit_file_records()
        (photosort_win.py) рекурсивна "на случай нестандартного рипа" -- если рип нестандартный
        и часть файлов юнита лежит во вложенной подпапке ВНУТРИ VIDEO_TS (не только плоско в её
        корне), os.path.dirname(dest) для этих файлов раньше давал .../VIDEO_TS/Sub, а не
        .../VIDEO_TS -- юнит раскалывался на 2 строки вместо 1. Group-by теперь ищет ближайшего
        предка с именем VIDEO_TS (_dvd_unit_root()), не просто прямого родителя."""
        dvd_reason = "DVD-Video (VIDEO_TS), скопирован целиком"
        dest_dir = r"D:\TARGET\Albums\DVD5\VIDEO_TS"
        rows = rx._build_detail_rows({"appended": [
            _appended(r"D:\RIP\DVD5/VIDEO_TS.IFO", dest_dir + r"\VIDEO_TS.IFO", reason=dvd_reason),
            _appended(r"D:\RIP\DVD5/Sub/VTS_01_0.VOB", dest_dir + r"\Sub\VTS_01_0.VOB",
                      reason=dvd_reason),
        ]})
        assert len(rows) == 1
        assert rows[0]["dest_or_dup"] == dest_dir
        assert "2 файла" in rows[0]["note"]

    def test_skipped_present_is_duplicate_gray_not_copied(self):
        rows = rx._build_detail_rows({"skipped": [
            _skipped(r"D:\SOURCE\dup.jpg", r"D:\TARGET\a.jpg", "already_present"),
        ]})
        row = rows[0]
        assert row["copied"] is False
        assert row["dest_or_dup"] == r"D:\TARGET\a.jpg"
        assert row["note"] == "дубликат"
        assert row["color"] == rx._COLOR_DUPLICATE

    def test_identical_at_destination_is_also_duplicate(self):
        rows = rx._build_detail_rows({"skipped": [
            _skipped(r"D:\SOURCE\dup.jpg", r"D:\TARGET\a.jpg", "identical_at_destination"),
        ]})
        assert rows[0]["note"] == "дубликат"

    def test_raw_skipped_has_jpeg_own_color_and_wording(self):
        """PROMPT_report_detail_xlsx.md, колонка 6: matched_with уже несёт путь JPEG-партнёра
        В АРХИВЕ (photosort_win.py:_process_record(), decision.decision=="raw_skipped") --
        построитель читает готовое значение, не пересчитывает."""
        rows = rx._build_detail_rows({"skipped": [
            _skipped(r"D:\SOURCE\a.cr2", r"D:\TARGET\Albums\a.jpg", "raw_skipped_has_jpeg"),
        ]})
        row = rows[0]
        assert row["kind"] == "raw"
        assert row["copied"] is False
        assert row["dest_or_dup"] == r"D:\TARGET\Albums\a.jpg"
        assert "RAW не сохранён" in row["note"]
        assert row["color"] == rx._COLOR_RAW_SKIPPED
        assert row["color"] not in (rx._COLOR_DUPLICATE, rx._COLOR_PROBLEM)

    def test_disputed_row_is_copied_yes_with_unsorted_note(self):
        rows = rx._build_detail_rows({"disputes": [
            _disputed(r"D:\SOURCE\icon.svg", r"D:\TARGET\_Unsorted\icon.svg", "icon_or_svg"),
        ]})
        row = rows[0]
        assert row["copied"] is True
        assert row["dest_or_dup"] == r"D:\TARGET\_Unsorted\icon.svg"
        assert "спорный (_Unsorted)" in row["note"]
        assert "похоже на иконку" in row["note"]  # _dispute_reason_label(), переиспользован
        assert row["color"] is None  # без отдельного цвета -- решение пользователя 2026-08-15

    def test_disputed_row_without_recognized_extension_falls_back_to_other(self):
        rows = rx._build_detail_rows({"disputes": [
            _disputed(r"D:\SOURCE\weird.dat", r"D:\TARGET\_Unsorted\weird.dat", "not_media"),
        ]})
        assert rows[0]["kind"] == "other"

    def test_unreadable_row_not_copied_empty_dest_terracotta(self):
        rows = rx._build_detail_rows({"unreadable": [
            _unreadable(r"D:\SOURCE\broken.jpg", "unsupported_container"),
        ]})
        row = rows[0]
        assert row["copied"] is False
        assert row["dest_or_dup"] == ""
        assert row["note"] == "не прочитано: unsupported_container"
        assert row["color"] == rx._COLOR_PROBLEM

    def test_rows_sorted_by_folder_then_name(self):
        data = {"appended": [
            _appended(r"D:\SOURCE\Z\a.jpg", r"D:\TARGET\a.jpg"),
            _appended(r"D:\SOURCE\A\b.jpg", r"D:\TARGET\b.jpg"),
            _appended(r"D:\SOURCE\A\a.jpg", r"D:\TARGET\c.jpg"),
        ]}
        rows = rx._build_detail_rows(data)
        assert [(r_["folder"], r_["name"]) for r_ in rows] == [
            (r"D:\SOURCE\A", "a.jpg"), (r"D:\SOURCE\A", "b.jpg"), (r"D:\SOURCE\Z", "a.jpg"),
        ]

    def test_source_dirname_handles_archive_member_slash_path(self):
        """origin_display для файлов из архива использует "/" (report.py:_source_basename()
        докстринг) -- _source_dirname() должен понимать тот же формат."""
        assert rx._source_dirname("Foto.zip/подпапка/файл.jpg") == "Foto.zip/подпапка"
        assert rx._source_dirname(r"D:\SOURCE\a.jpg") == r"D:\SOURCE"


class TestGenerateDetailXlsx:
    def test_returns_none_and_writes_nothing_when_no_rows(self, tmp_path):
        out_path = tmp_path / "report.html"
        result = rx.generate_detail_xlsx({}, str(out_path))
        assert result is None
        assert not (tmp_path / rx.DETAIL_XLSX_FILENAME).exists()

    def test_writes_file_next_to_report_with_header_and_autofilter(self, tmp_path):
        out_path = tmp_path / "report.html"
        data = {"appended": [_appended(r"D:\SOURCE\a.jpg", r"D:\TARGET\a.jpg")]}
        result = rx.generate_detail_xlsx(data, str(out_path))
        assert result == rx.DETAIL_XLSX_FILENAME
        full_path = tmp_path / rx.DETAIL_XLSX_FILENAME
        assert full_path.exists()
        wb = load_workbook(str(full_path))
        ws = wb.active
        header = [c.value for c in ws[1]]
        assert header == rx._COLUMN_HEADERS
        assert ws[1][0].font.bold is True
        assert ws.auto_filter.ref == f"A1:I{ws.max_row}"
        assert ws.freeze_panes == "A2"

    def test_many_same_color_rows_all_get_the_color(self, tmp_path):
        """Живой боевой прогон 2026-08-28: _write_flat_xlsx() кэширует объекты Font по цвету и
        переиспределяет один инстанс на все ячейки одного цвета (было -- новый Font на каждую
        ячейку, сотни тысяч на большом архиве). Проверяем, что цвет от этого не теряется:
        несколько строк одного цвета -- все реально окрашены."""
        data = {"skipped": [
            _skipped(rf"D:\SOURCE\dup{i}.jpg", rf"D:\TARGET\a{i}.jpg", "already_present")
            for i in range(5)
        ]}
        rx.generate_detail_xlsx(data, str(tmp_path / "report.html"))
        wb = load_workbook(str(tmp_path / rx.DETAIL_XLSX_FILENAME))
        ws = wb.active
        expected = "FF" + rx._COLOR_DUPLICATE.lstrip("#")
        for row_i in range(2, 7):  # 5 строк данных
            assert ws.cell(row=row_i, column=1).font.color.rgb == expected

    def test_rows_grouped_by_folder_in_output_order(self, tmp_path):
        """2026-08-28 (write_only-режим, "вариант 1"): Excel-outline (сворачиваемые +/- по
        папке) убран ради ×40 скорости на большом архиве -- но строки по-прежнему
        отсортированы по папке (файлы одной папки идут подряд), навигация через автофильтр/
        сортировку. Проверяем именно порядок (не outline_level, которого больше нет)."""
        out_path = tmp_path / "report.html"
        data = {"appended": [
            _appended(r"D:\SOURCE\B\a.jpg", r"D:\TARGET\c.jpg"),
            _appended(r"D:\SOURCE\A\b.jpg", r"D:\TARGET\b.jpg"),
            _appended(r"D:\SOURCE\A\a.jpg", r"D:\TARGET\a.jpg"),
        ]}
        rx.generate_detail_xlsx(data, str(out_path))
        wb = load_workbook(str(tmp_path / rx.DETAIL_XLSX_FILENAME))
        ws = wb.active
        folders = [ws.cell(row=i, column=1).value for i in (2, 3, 4)]
        names = [ws.cell(row=i, column=2).value for i in (2, 3, 4)]
        assert folders == [r"D:\SOURCE\A", r"D:\SOURCE\A", r"D:\SOURCE\B"]  # папка A подряд
        assert names == ["a.jpg", "b.jpg", "a.jpg"]  # внутри папки -- по имени
        # write_only больше не пишет row_dimensions -- их нет/дефолтны, это ожидаемо
        assert ws.row_dimensions[3].outline_level == 0

    def test_duplicate_and_problem_rows_get_distinct_visible_font_colors(self, tmp_path):
        out_path = tmp_path / "report.html"
        data = {
            "skipped": [_skipped(r"D:\SOURCE\dup.jpg", r"D:\TARGET\a.jpg", "already_present")],
            "unreadable": [_unreadable(r"D:\SOURCE\broken.jpg", "err")],
        }
        rx.generate_detail_xlsx(data, str(out_path))
        wb = load_workbook(str(tmp_path / rx.DETAIL_XLSX_FILENAME))
        ws = wb.active
        # broken.jpg (unreadable) сортируется раньше dup.jpg (skipped) -- оба в D:\SOURCE.
        rgb_by_name = {ws.cell(row=i, column=2).value: ws.cell(row=i, column=1).font.color.rgb
                       for i in (2, 3)}
        assert rgb_by_name["broken.jpg"] == "FF" + rx._COLOR_PROBLEM.lstrip("#")
        assert rgb_by_name["dup.jpg"] == "FF" + rx._COLOR_DUPLICATE.lstrip("#")
        assert rgb_by_name["broken.jpg"] != rgb_by_name["dup.jpg"]


class TestGenerateReportWiresDetailXlsxButton:
    def test_target_level_with_new_files_gets_active_link_and_file(self, tmp_path):
        data = {"appended": [_appended(r"D:\SOURCE\a.jpg", r"D:\TARGET\a.jpg")]}
        out_path = tmp_path / "report.html"
        r.generate_report(data, str(out_path), level="target", run_start="2026-01-01 00:00:00")
        html_out = out_path.read_text(encoding="utf-8")
        assert f'href="{rx.DETAIL_XLSX_FILENAME}"' in html_out
        assert 'disabled aria-disabled="true">Детализированный отчёт' not in html_out
        assert (tmp_path / rx.DETAIL_XLSX_FILENAME).exists()

    def test_workdir_level_also_gets_active_link(self, tmp_path):
        """Спека, "Формат и охват": оба режима (level=="target" И level=="workdir") -- один
        код-путь после унификации dry-run 2026-08-14."""
        data = {"appended": [_appended(r"D:\SOURCE\a.jpg", r"D:\TARGET\a.jpg")]}
        out_path = tmp_path / "report.html"
        r.generate_report(data, str(out_path), level="workdir", run_start="2026-01-01 00:00:00")
        html_out = out_path.read_text(encoding="utf-8")
        assert f'href="{rx.DETAIL_XLSX_FILENAME}"' in html_out

    def test_stays_disabled_when_nothing_at_all_happened(self, tmp_path):
        out_path = tmp_path / "report.html"
        r.generate_report({}, str(out_path), level="target", run_start="2026-01-01 00:00:00")
        html_out = out_path.read_text(encoding="utf-8")
        assert "disabled" in html_out
        assert f'href="{rx.DETAIL_XLSX_FILENAME}"' not in html_out
        assert not (tmp_path / rx.DETAIL_XLSX_FILENAME).exists()

    def test_analyze_level_never_calls_detail_xlsx_at_all(self, tmp_path):
        """checklist_new остаётся None для level=="analyze" (run_start никогда не передаётся
        этой веткой в проде, см. generate_report_from_analyze_stats()) -- эта ветка не должна
        писать report_detail.xlsx вообще, не только не ссылаться на него."""
        data = {"appended": [_appended(r"D:\SOURCE\a.jpg", r"D:\TARGET\a.jpg")]}
        out_path = tmp_path / "report.html"
        r.generate_report(data, str(out_path), level="target")  # run_start не передан
        assert not (tmp_path / rx.DETAIL_XLSX_FILENAME).exists()

    def test_only_skipped_rows_still_activates_button_even_with_zero_new(self, tmp_path):
        """n_new_total==0 (ранний return в _render_run_copied()) не должен означать "нечего
        детализировать" -- в xlsx всё равно попадут дубли/спорные/непрочитанное этого прогона."""
        data = {"skipped": [_skipped(r"D:\SOURCE\dup.jpg", r"D:\TARGET\a.jpg", "already_present")]}
        out_path = tmp_path / "report.html"
        r.generate_report(data, str(out_path), level="target", run_start="2026-01-01 00:00:00",
                           run_stats={"skipped_present": 1})
        html_out = out_path.read_text(encoding="utf-8")
        assert f'href="{rx.DETAIL_XLSX_FILENAME}"' in html_out
        assert (tmp_path / rx.DETAIL_XLSX_FILENAME).exists()


def _dispute_record(abs_path=None, display="", in_archive=False):
    """Та же форма, что _analyze_dispute_record() в photosort_win.py."""
    return {"in_archive": in_archive, "abs_path": abs_path, "display": display}


def _passport_stats(**overrides):
    base = dict(
        encrypted_archive_paths=[], failed_archive_paths=[],
        disputed_records=[], unreadable_records=[],
        exact_dup_edges=[], near_dup_edges=[], dump_item_paths=[],
    )
    base.update(overrides)
    return SimpleNamespace(**base)


class TestBuildPassportDetailRows:
    def test_encrypted_archive_row(self):
        stats = _passport_stats(encrypted_archive_paths=[r"D:\TARGET\Foto.zip"])
        rows = rx._build_passport_detail_rows(stats)
        assert len(rows) == 1
        row = rows[0]
        assert row["folder"] == r"D:\TARGET"
        assert row["name"] == "Foto.zip"
        assert row["category"] == "архив"
        assert row["note"] == "запаролен"
        assert row["color"] == rx._COLOR_PROBLEM
        assert row["group_id"] == 0

    def test_failed_archive_row(self):
        stats = _passport_stats(failed_archive_paths=[r"D:\TARGET\Broken.rar"])
        row = rx._build_passport_detail_rows(stats)[0]
        assert row["category"] == "архив"
        assert row["note"] == "не открылся"
        assert row["color"] == rx._COLOR_PROBLEM

    def test_disputed_record_uses_abs_path_no_color(self):
        stats = _passport_stats(disputed_records=[_dispute_record(abs_path=r"D:\TARGET\weird.dat")])
        row = rx._build_passport_detail_rows(stats)[0]
        assert row["folder"] == r"D:\TARGET"
        assert row["name"] == "weird.dat"
        assert row["category"] == "повреждённый файл"
        assert row["note"] == "содержимое не распознано"
        assert row["color"] is None

    def test_disputed_record_in_archive_falls_back_to_display(self):
        """abs_path пуст для файлов ИЗНУТРИ архива (физический файл уже вычищен из
        tmp_extract к моменту чтения отчёта) -- display -- витринная замена."""
        stats = _passport_stats(disputed_records=[
            _dispute_record(abs_path=None, display="Foto.zip/DCIM/broken.jpg", in_archive=True),
        ])
        row = rx._build_passport_detail_rows(stats)[0]
        assert row["name"] == "broken.jpg"

    def test_unreadable_record_gets_problem_color(self):
        stats = _passport_stats(unreadable_records=[_dispute_record(abs_path=r"D:\TARGET\corrupt.jpg")])
        row = rx._build_passport_detail_rows(stats)[0]
        assert row["category"] == "повреждённый файл"
        assert row["note"] == "не читается (ошибка чтения)"
        assert row["color"] == rx._COLOR_PROBLEM

    def test_exact_dup_group_of_three_shares_one_group_id(self):
        """Речь пользователя, 2026-08-16: дублей может быть больше 1 -- группа не ограничена
        парой. Union-find в _cluster_passport_edges() уже поддерживает 3+ файлов нативно."""
        stats = _passport_stats(exact_dup_edges=[
            {"dest": "Albums/A/orig.jpg", "matched_dest": "Albums/A/copy1.jpg"},
            {"dest": "Albums/A/orig.jpg", "matched_dest": "Albums/A/copy2.jpg"},
        ])
        rows = rx._build_passport_detail_rows(stats)
        assert len(rows) == 3
        assert {row["name"] for row in rows} == {"orig.jpg", "copy1.jpg", "copy2.jpg"}
        group_ids = {row["group_id"] for row in rows}
        assert group_ids == {1}
        assert all(row["category"] == "дубликат" for row in rows)
        assert all(row["color"] == rx._COLOR_DUPLICATE for row in rows)

    def test_dup_paths_made_absolute_with_target_path(self):
        """Живая находка (боевой прогон на синтетическом архиве, 2026-08-16): без target_path
        "Папка" дублей/похожих серий оставалась относительной ("Albums\\A"), а у архивов/
        битых файлов -- уже абсолютной, непоследовательно в одной колонке одного листа."""
        stats = _passport_stats(exact_dup_edges=[
            {"dest": "Albums/A/orig.jpg", "matched_dest": "Albums/A/copy1.jpg"},
        ])
        rows = rx._build_passport_detail_rows(stats, target_path=r"D:\TARGET")
        by_name = {row["name"]: row for row in rows}
        assert by_name["orig.jpg"]["folder"] == r"D:\TARGET\Albums\A"

    def test_near_dup_group_no_color_own_category(self):
        stats = _passport_stats(near_dup_edges=[
            {"dest": "ByDate/2024/a.jpg", "matched_dest": "ByDate/2024/b.jpg"},
        ])
        rows = rx._build_passport_detail_rows(stats)
        assert all(row["category"] == "похожая серия" for row in rows)
        assert all(row["color"] is None for row in rows)
        assert all(row["group_id"] == 1 for row in rows)

    def test_two_distinct_exact_dup_groups_get_different_numbers(self):
        stats = _passport_stats(exact_dup_edges=[
            {"dest": "Albums/A/a1.jpg", "matched_dest": "Albums/A/a2.jpg"},
            {"dest": "Albums/B/b1.jpg", "matched_dest": "Albums/B/b2.jpg"},
        ])
        rows = rx._build_passport_detail_rows(stats)
        group_by_name = {row["name"]: row["group_id"] for row in rows}
        assert group_by_name["a1.jpg"] == group_by_name["a2.jpg"]
        assert group_by_name["b1.jpg"] == group_by_name["b2.jpg"]
        assert group_by_name["a1.jpg"] != group_by_name["b1.jpg"]

    def test_dump_item_paths_get_own_category_no_color_no_group(self):
        """Живая находка пользователя, 2026-08-24: "N файлов лежат не внутри альбома/даты"
        (_render_passport_integrity()) раньше был единственным пунктом карточки без путей в
        детализации вовсе -- stats.dump_item_paths теперь тоже попадает в xlsx, тем же
        принципом, что и архивы/битые файлы/дубли выше."""
        stats = _passport_stats(dump_item_paths=["SomeStray/photo.jpg"])
        rows = rx._build_passport_detail_rows(stats)
        assert len(rows) == 1
        row = rows[0]
        assert row["folder"] == "SomeStray"
        assert row["name"] == "photo.jpg"
        assert row["category"] == "вне альбома/даты"
        assert row["note"] == ""
        assert row["color"] is None
        assert row["group_id"] == 0

    def test_dump_item_paths_made_absolute_with_target_path(self):
        stats = _passport_stats(dump_item_paths=["SomeStray/photo.jpg"])
        rows = rx._build_passport_detail_rows(stats, target_path=r"D:\TARGET")
        assert rows[0]["folder"] == r"D:\TARGET\SomeStray"


class TestGeneratePassportDetailXlsx:
    def test_returns_none_when_nothing_found(self, tmp_path):
        out_path = tmp_path / "passport.html"
        assert rx.generate_passport_detail_xlsx(_passport_stats(), str(out_path)) is None
        assert not (tmp_path / rx.PASSPORT_DETAIL_XLSX_FILENAME).exists()

    def test_writes_file_with_expected_columns(self, tmp_path):
        stats = _passport_stats(encrypted_archive_paths=[r"D:\TARGET\Foto.zip"])
        out_path = tmp_path / "passport.html"
        result = rx.generate_passport_detail_xlsx(stats, str(out_path))
        assert result == rx.PASSPORT_DETAIL_XLSX_FILENAME
        wb = load_workbook(str(tmp_path / rx.PASSPORT_DETAIL_XLSX_FILENAME))
        ws = wb.active
        header = [c.value for c in ws[1]]
        assert header == rx._PASSPORT_COLUMN_HEADERS
        row = [c.value for c in ws[2]]
        assert row == [r"D:\TARGET", "Foto.zip", "архив", 0, "запаролен"]


class TestGeneratePassportReportWiresDetailXlsxButton:
    def test_active_link_when_findings_exist(self, tmp_path):
        stats = _FakeAnalyzeStats()
        stats.encrypted_archive_paths = [r"D:\TARGET\Foto.zip"]
        out_path = tmp_path / "passport.html"
        r.generate_passport_report(stats, str(out_path))
        html_out = out_path.read_text(encoding="utf-8")
        assert f'href="{rx.PASSPORT_DETAIL_XLSX_FILENAME}"' in html_out
        assert (tmp_path / rx.PASSPORT_DETAIL_XLSX_FILENAME).exists()

    def test_target_path_reaches_xlsx_for_absolute_dup_paths(self, tmp_path):
        stats = _FakeAnalyzeStats()
        stats.exact_dup_edges = [{"dest": "Albums/A/orig.jpg", "matched_dest": "Albums/A/copy.jpg"}]
        out_path = tmp_path / "passport.html"
        r.generate_passport_report(stats, str(out_path), target_path=r"D:\TARGET")
        wb = load_workbook(str(tmp_path / rx.PASSPORT_DETAIL_XLSX_FILENAME))
        ws = wb.active
        folders = [ws.cell(row=i, column=1).value for i in range(2, ws.max_row + 1)]
        assert all(f.startswith(r"D:\TARGET") for f in folders)

    def test_stays_disabled_when_archive_is_clean(self, tmp_path):
        stats = _FakeAnalyzeStats()
        out_path = tmp_path / "passport.html"
        r.generate_passport_report(stats, str(out_path))
        html_out = out_path.read_text(encoding="utf-8")
        assert 'disabled aria-disabled="true">Детализированный отчёт' in html_out
        assert f'href="{rx.PASSPORT_DETAIL_XLSX_FILENAME}"' not in html_out
        assert not (tmp_path / rx.PASSPORT_DETAIL_XLSX_FILENAME).exists()

    def test_dump_items_alone_activate_the_button(self, tmp_path):
        """Живая находка пользователя, 2026-08-24: "N файлов лежат не внутри альбома/даты" мог
        быть ЕДИНСТВЕННОЙ находкой паспорта -- до фикса dump_item_paths не попадал в xlsx вовсе,
        значит rows была бы пуста и кнопка осталась бы неактивной, хотя report.html уже
        показывает "N файлов..." -- несоответствие между "что видно" и "что можно раскрыть"."""
        stats = _FakeAnalyzeStats()
        stats.n_dump_items = 1
        stats.dump_item_paths = ["SomeStray/photo.jpg"]
        out_path = tmp_path / "passport.html"
        r.generate_passport_report(stats, str(out_path))
        html_out = out_path.read_text(encoding="utf-8")
        assert f'href="{rx.PASSPORT_DETAIL_XLSX_FILENAME}"' in html_out
        assert (tmp_path / rx.PASSPORT_DETAIL_XLSX_FILENAME).exists()

    def test_old_verification_page_no_longer_written(self, tmp_path):
        """PROMPT_report_detail_xlsx.md, решение 2026-08-16: "HTML Паспорта -- только числа,
        все пути -- в xlsx" -- generate_passport_verification_page() больше не вызывается из
        generate_passport_report(), тем же принципом, что и у обычного прогона."""
        stats = _FakeAnalyzeStats()
        stats.exact_dup_edges = [{"dest": "Albums/A/orig.jpg", "matched_dest": "Albums/B/copy.jpg"}]
        out_path = tmp_path / "passport.html"
        r.generate_passport_report(stats, str(out_path))
        assert not (tmp_path / r.PASSPORT_VERIFICATION_FILENAME).exists()


class TestPassportArchivesAndBrokenAttnText:
    def test_archives_breakdown_mentions_encrypted_and_failed(self):
        text = r._passport_archives_attn(3, 1, 1)
        assert "3 архива" in text
        assert "1 запаролен" in text
        assert "1 не открылся" in text

    def test_archives_no_breakdown_sentence_when_all_plain(self):
        text = r._passport_archives_attn(2, 0, 0)
        assert "Из них" not in text

    def test_broken_breakdown_mentions_unreadable_and_rest(self):
        text = r._passport_broken_attn(5, 2)
        assert "5 файлов" in text
        assert "2 не удалось физически прочитать" in text
        assert "остальные 3" in text

    def test_broken_no_breakdown_when_all_unreadable(self):
        text = r._passport_broken_attn(2, 2)
        assert "остальные" not in text

    def test_broken_no_breakdown_sentence_when_none_unreadable(self):
        text = r._passport_broken_attn(4, 0)
        assert "Из них" not in text
