"""PROMPT_report_run_redesign.md, Промпт 1/3 (2026-08-14): Раздел 1 "Что скопировано" сводного
прогонного отчёта -- _render_run_copied() и его подключение через model_new (generate_report()/
_generate_from_model()), включая перевод трастового баннера на model_new вместо кумулятивной
model (решение пользователя, Фаза 0)."""
import report as r


def _appended_row(dest, ts="2026-01-01 00:00:01", **extra):
    row = {"timestamp": ts, "source": "s", "dest": dest, "reason": "appended_new", "flags": ""}
    row.update(extra)
    return row


class TestRenderRunCopiedHeadline:
    def test_basic_tile_and_type_breakdown(self):
        model_new = r.build_model_from_rows({"appended": [
            _appended_row(r"C:\T\dst\ByDate\2020\2020-05-Крым\a.jpg"),
            _appended_row(r"C:\T\dst\ByDate\2020\2020-05-Крым\b.jpg"),
            _appended_row(r"C:\T\dst\ByDate\2021\2021-01\c.mp4"),
        ]})
        html_out = r._render_run_copied(model_new, {}, "target")
        assert "<h2>Что скопировано</h2>" in html_out
        assert "<div class=\"value\">3</div>" in html_out
        assert "файлов добавлено" in html_out
        assert "2 файла фото" in html_out or "2 файла" in html_out  # _n_files("2")
        assert "и" in html_out  # ",""и" склейка между категориями
        assert "Детализированный отчёт" in html_out
        assert "disabled" in html_out

    def test_preview_uses_future_tense_label(self):
        model_new = r.build_model_from_rows({"appended": [_appended_row(r"C:\T\dst\ByDate\2020\a.jpg")]})
        html_out = r._render_run_copied(model_new, {}, "workdir")
        assert "файлов будет добавлено" in html_out
        assert "файлов добавлено<" not in html_out

    def test_single_type_breakdown_not_rendered(self):
        """Одна ненулевая категория -- плитка уже назвала число, доп. предложение "Из них..."
        избыточно (тот же принцип, что files_by_location в _render_analyze_sheet1)."""
        model_new = r.build_model_from_rows({"appended": [
            _appended_row(r"C:\T\dst\ByDate\2020\a.jpg"),
            _appended_row(r"C:\T\dst\ByDate\2020\b.jpg"),
        ]})
        html_out = r._render_run_copied(model_new, {}, "target")
        assert "Из них" not in html_out

    def test_volume_falls_back_to_run_stats_when_dest_missing_on_disk(self):
        """Предпросмотр: dest гипотетический, os.path.getsize() тихо даёт 0 (_row_size()) --
        объём должен взяться из run_stats["bytes_appended"] агрегатом, не остаться пустым."""
        model_new = r.build_model_from_rows({"appended": [
            _appended_row(r"C:\T\dst\ByDate\2020\a.jpg"),
        ]})
        assert model_new["total_bytes"] == 0  # файла реально нет на диске в этом тесте
        html_out = r._render_run_copied(model_new, {"bytes_appended": 12345}, "workdir")
        assert r._fmt_bytes(12345) in html_out

    def test_volume_by_category_pie_falls_back_to_run_stats_in_dry_run(self):
        """Речь пользователя, 2026-08-18: dry-run обязан выглядеть так же, как реальный
        прогон -- "Объём по категориям" не должна молча пропадать из-за того, что dest ещё не
        существует физически (см. _row_size()). run_stats["bytes_appended_image/_video/_raw"]
        (photosort_win.py) -- тот же агрегат, что и headline-плитка объёма чуть выше, просто
        разбитый по категории."""
        model_new = r.build_model_from_rows({"appended": [
            _appended_row(r"C:\T\dst\ByDate\2020\a.jpg"),
            _appended_row(r"C:\T\dst\ByDate\2020\b.mp4"),
        ]})
        assert sum(model_new["bytes_by_kind"].values()) == 0  # файлов реально нет на диске
        html_out = r._render_run_copied(
            model_new, {"bytes_appended_image": 1000, "bytes_appended_video": 2000}, "workdir")
        assert "Объём по категориям" in html_out
        assert r._fmt_bytes(1000) in html_out
        assert r._fmt_bytes(2000) in html_out

    def test_volume_by_category_pie_absent_without_any_byte_source(self):
        """Ни getsize(dest), ни run_stats-агрегата -- диаграмма честно не рендерится (не
        пустой пирог из нулей), тот же принцип, что и у остальных диаграмм этого раздела."""
        model_new = r.build_model_from_rows({"appended": [
            _appended_row(r"C:\T\dst\ByDate\2020\a.jpg"),
        ]})
        html_out = r._render_run_copied(model_new, {}, "workdir")
        assert "Объём по категориям" not in html_out

    def test_year_span_tile_singular_grammar(self):
        model_new = r.build_model_from_rows({"appended": [
            _appended_row(r"C:\T\dst\ByDate\2020\2020-05-Крым\a.jpg"),
        ]})
        html_out = r._render_run_copied(model_new, {}, "target")
        assert ">1<" in html_out
        assert "год истории</div>" in html_out
        assert "лет истории</div>" not in html_out


class TestRenderRunCopiedZeroStates:
    def test_source_empty_message(self):
        model_new = r.build_model_from_rows({"appended": []})
        html_out = r._render_run_copied(model_new, {}, "target")
        assert "не нашлось фото или видео для добавления" in html_out
        assert "Детализированный отчёт" in html_out

    def test_all_already_in_archive_message_differs_from_empty_source(self):
        model_new = r.build_model_from_rows({"appended": []})
        html_out = r._render_run_copied(model_new, {"skipped_present": 5}, "target")
        assert "уже в архиве" in html_out
        assert "не нашлось фото или видео" not in html_out

    def test_all_already_in_archive_preview_wording(self):
        model_new = r.build_model_from_rows({"appended": []})
        html_out = r._render_run_copied(model_new, {"skipped_present": 5}, "workdir")
        assert "Нового не будет добавлено" in html_out

    def test_stopped_for_space_overrides_other_zero_messages(self):
        model_new = r.build_model_from_rows({"appended": []})
        html_out = r._render_run_copied(
            model_new, {"skipped_present": 5, "stopped_for_space": True}, "target")
        assert "остановлена раньше" in html_out
        assert "уже в архиве" not in html_out

    def test_zero_conditional_tiles_not_rendered(self):
        """"сэкономлено 0"/другие условные плитки не показываются при нуле -- проверяем на
        непустом прогоне без экономии (bytes_saved отсутствует)."""
        model_new = r.build_model_from_rows({"appended": [
            _appended_row(r"C:\T\dst\ByDate\2020\a.jpg"),
        ]})
        html_out = r._render_run_copied(model_new, {}, "target")
        assert "сэкономлено" not in html_out


def _dvd_unit(name="Диск1", n_files=3, total_bytes=123456, dest_path=r"C:\T\dst\Albums\Диск1\VIDEO_TS"):
    return {"name": name, "dest_path": dest_path, "n_files": n_files,
            "total_bytes": total_bytes, "fingerprint": "abc"}


class TestRenderRunCopiedDvdUnit:
    """REVIEW-HANDOFF.md, Раунд 91 [ЗАМЕЧАНИЕ] -- model_new (build_model_from_rows())
    реклассифицирует appended.csv-строки по расширению (_media_kind()/report.py's VIDEO_EXTS),
    DVD-юнита (.VOB/.IFO/.BUP) там нет ни в одном из трёх наборов расширений -- все файлы
    уходили в "other", полностью пропадая из counts, headline расходился с _render_this_run()
    (тот считает DVD через живой run_stats["appended_videos"], не постфактум-реклассификацию).
    В DVD-only прогоне это было не просто расхождением числа, а ложным "не нашлось" при
    физически скопированных файлах. Фикс -- run_stats["dvd_units_copied"] (тот же авторитетный
    источник, что уже использует _render_this_run()) считается отдельно от counts/model_new."""

    def test_dvd_only_run_does_not_show_false_nothing_found_message(self):
        """Живой сценарий ревизора: appended.csv несёт только .VOB/.IFO/.BUP строки (все три
        уходят в "other" у model_new), но run_stats["dvd_units_copied"] подтверждает реальную
        копию -- Раздел 1 не должен утверждать "не нашлось"."""
        model_new = r.build_model_from_rows({"appended": [
            _appended_row(r"C:\T\dst\Albums\Диск1\VIDEO_TS\VTS_01_1.VOB"),
            _appended_row(r"C:\T\dst\Albums\Диск1\VIDEO_TS\VTS_01_0.IFO"),
            _appended_row(r"C:\T\dst\Albums\Диск1\VIDEO_TS\VTS_01_0.BUP"),
        ]})
        assert model_new["counts"].get("video", 0) == 0  # подтверждает механизм находки
        run_stats = {"dvd_units_copied": [_dvd_unit(n_files=3)]}
        html_out = r._render_run_copied(model_new, run_stats, "target")
        assert "не нашлось" not in html_out

    def test_dvd_only_run_headline_matches_dvd_file_count(self):
        model_new = r.build_model_from_rows({"appended": []})
        run_stats = {"dvd_units_copied": [_dvd_unit(n_files=3)]}
        html_out = r._render_run_copied(model_new, run_stats, "target")
        assert '<div class="value">3</div>' in html_out

    def test_dvd_only_run_names_the_disk(self):
        model_new = r.build_model_from_rows({"appended": []})
        run_stats = {"dvd_units_copied": [_dvd_unit(name="ОтпускДиск")]}
        html_out = r._render_run_copied(model_new, run_stats, "target")
        assert "DVD-видео (VIDEO_TS) — скопировано целиком" in html_out
        assert "ОтпускДиск" in html_out

    def test_mixed_run_headline_includes_both_regular_and_dvd_files(self):
        """Смешанный прогон (обычные фото + DVD) -- headline обязан включать оба, не только
        то, что попало в model_new."""
        model_new = r.build_model_from_rows({"appended": [
            _appended_row(r"C:\T\dst\ByDate\2020\a.jpg"),
            _appended_row(r"C:\T\dst\ByDate\2020\b.jpg"),
        ]})
        run_stats = {"dvd_units_copied": [_dvd_unit(n_files=3)]}
        html_out = r._render_run_copied(model_new, run_stats, "target")
        assert '<div class="value">5</div>' in html_out  # 2 фото + 3 DVD

    def test_mixed_run_type_breakdown_mentions_dvd_separately_from_video(self):
        model_new = r.build_model_from_rows({"appended": [
            _appended_row(r"C:\T\dst\ByDate\2020\a.jpg"),
        ]})
        run_stats = {"dvd_units_copied": [_dvd_unit(n_files=3)]}
        html_out = r._render_run_copied(model_new, run_stats, "target")
        assert "DVD (VIDEO_TS)" in html_out
        assert "Из них" in html_out

    def test_no_dvd_units_does_not_render_dvd_paragraph(self):
        model_new = r.build_model_from_rows({"appended": [_appended_row(r"C:\T\dst\ByDate\2020\a.jpg")]})
        html_out = r._render_run_copied(model_new, {}, "target")
        assert "DVD-видео" not in html_out

    def test_standalone_vob_now_counts_as_video(self):
        """2026-08-16, речь пользователя: отдельно стоящий .vob (реальный старый видеоформат
        камкордера, НЕ внутри VIDEO_TS) должен считаться обычным видео, тот же алгоритм, что уже
        применяется в photosort_win.py's VIDEO_EXTS (с 2026-08-07) -- _media_kind() теперь
        разбирает "vob" отдельным условием по ПУТИ (_is_inside_video_ts()), не только по
        расширению, закрывая прежний пробел без риска задвоения DVD-юнита (см. следующий тест)."""
        model_new = r.build_model_from_rows({"appended": [
            _appended_row(r"C:\T\dst\ByDate\2020\clip.vob"),
        ]})
        assert model_new["counts"].get("video", 0) == 1
        assert model_new["counts"].get("other", 0) == 0

    def test_vob_inside_video_ts_still_other_not_double_counted(self):
        """Тот же фикс не должен сломать класс выше -- .VOB ВНУТРИ VIDEO_TS остаётся "other"
        (уже посчитан через dvd_units_copied), не начинает задваиваться в counts["video"] теперь,
        когда "vob" в принципе умеет быть "video"."""
        model_new = r.build_model_from_rows({"appended": [
            _appended_row(r"C:\T\dst\Albums\Диск1\VIDEO_TS\VTS_01_1.VOB"),
        ]})
        assert model_new["counts"].get("video", 0) == 0
        assert model_new["counts"].get("other", 0) == 1

    def test_vob_inside_video_ts_collision_suffix_still_other(self):
        """"VIDEO_TS (2)" -- та же уникализация имени папки, что _unique_dvd_dest_name()
        (photosort_win.py) даёт при коллизии (два физически разных DVD-диска под одним
        альбомом/датой) -- проверка не должна требовать буквального "VIDEO_TS" без суффикса."""
        model_new = r.build_model_from_rows({"appended": [
            _appended_row(r"C:\T\dst\Albums\Диск1\VIDEO_TS (2)\VTS_01_1.VOB"),
        ]})
        assert model_new["counts"].get("video", 0) == 0
        assert model_new["counts"].get("other", 0) == 1

    def test_dvd_unit_missing_n_files_key_defaults_to_zero_not_crash(self):
        """dvd_units_copied в реальном коде всегда несёт n_files (см. _handle_dvd_unit()), но
        .get() с дефолтом -- защита от KeyError, если структура когда-нибудь изменится."""
        model_new = r.build_model_from_rows({"appended": []})
        run_stats = {"dvd_units_copied": [{"name": "X", "dest_path": "Y"}]}
        html_out = r._render_run_copied(model_new, run_stats, "target")
        assert "не нашлось" in html_out  # n_dvd_files==0 -- обычная нулевая ветка


class TestGenerateReportWiresSectionOneAndModelNew:
    def test_target_report_contains_section_one(self, tmp_path):
        data = {"appended": [_appended_row(r"C:\T\dst\ByDate\2020\a.jpg",
                                            ts="2026-02-01 00:00:01")]}
        out_path = tmp_path / "report.html"
        r.generate_report(data, str(out_path), level="target",
                           run_start="2026-02-01 00:00:00")
        html_out = out_path.read_text(encoding="utf-8")
        assert "<h2>Что скопировано</h2>" in html_out

    def test_cli_dry_run_without_full_workdir_still_has_section_one(self, tmp_path):
        """2026-08-14, прямая просьба пользователя ("вид отчёта по dry-run всегда должен быть
        одинаковым с реальным прогоном, независимо от существования архива") -- level=="workdir"
        на НОВОМ TARGET, ещё не существующем (CLI --dry-run без смёрженной истории Target)
        теперь ТОЖЕ показывает Раздел 1 -- раньше здесь была отдельная, более бедная ветка
        рендера, унифицирована тем же диалогом. Параметр `full_workdir` убран из report.py
        целиком в рамках той же унификации (REVIEW-HANDOFF.md, Раунд 92) -- различие между
        этим случаем и [2] на существующем Target теперь только в переданной `data`, не в
        отдельном флаге."""
        data = {"appended": [_appended_row(r"C:\T\dst\ByDate\2020\a.jpg",
                                            ts="2026-02-01 00:00:01")]}
        out_path = tmp_path / "report.html"
        r.generate_report(data, str(out_path), level="workdir",
                           run_start="2026-02-01 00:00:00")
        html_out = out_path.read_text(encoding="utf-8")
        assert "<h2>Что скопировано</h2>" in html_out

    def test_trust_banner_uses_model_new_not_cumulative_unreadable(self, tmp_path):
        """Решение пользователя (Фаза 0, 2026-08-14): трастовый баннер в target/workdir-ветке
        должен читать unreadable ТОЛЬКО этого прогона (model_new), не всю историю архива."""
        data = {
            "appended": [_appended_row(r"C:\T\dst\ByDate\2020\a.jpg", ts="2026-02-01 00:00:01")],
            # Старая, накопленная ДО этого прогона запись -- НЕ должна включаться в
            # трастовый баннер, если он честно смотрит только на этот прогон.
            "unreadable": [{"timestamp": "2020-01-01 00:00:01", "source": "old.jpg",
                             "error": "bad"}],
        }
        out_path = tmp_path / "report.html"
        r.generate_report(data, str(out_path), level="target",
                           run_start="2026-02-01 00:00:00")
        html_out = out_path.read_text(encoding="utf-8")
        assert "Ошибки чтения показаны отдельно" not in html_out

    def test_trust_banner_shows_when_unreadable_is_within_this_run(self, tmp_path):
        data = {
            "appended": [_appended_row(r"C:\T\dst\ByDate\2020\a.jpg", ts="2026-02-01 00:00:01")],
            "unreadable": [{"timestamp": "2026-02-01 00:00:02", "source": "new.jpg",
                             "error": "bad"}],
        }
        out_path = tmp_path / "report.html"
        r.generate_report(data, str(out_path), level="target",
                           run_start="2026-02-01 00:00:00")
        html_out = out_path.read_text(encoding="utf-8")
        assert "Ошибки чтения показаны отдельно" in html_out

    def test_analyze_level_untouched_by_model_new(self, tmp_path):
        """generate_report_from_analyze_stats() -- checklist_new/model_new всегда None там,
        трастовый баннер должен продолжать читать кумулятивную model, как раньше."""
        import photosort_win as m
        stats = m.AnalyzeStats(mode="analyze-quick")
        out_path = tmp_path / "report.html"
        r.generate_report_from_analyze_stats(stats, str(out_path))
        html_out = out_path.read_text(encoding="utf-8")
        assert "<h2>Что скопировано</h2>" not in html_out
