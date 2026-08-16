"""PROMPT_report_run_redesign.md, Фаза 0 (2026-08-14): два сквозных фикса, нужные для
редизайна прогонного отчёта, но независимые от report.py сами по себе.

1. _sum_stats() раньше молча ронял любой ключ со значением-списком (не число, не
   album_profiles) -- encrypted_archives/dvd_units_copied/dvd_units_skipped_duplicate
   никогда не доживали до report.py, даже при одном источнике (подтверждено исполнением
   до фикса -- см. историю сессии).
2. CLI archive/--dry-run путь (_main()) не пробрасывал RunResult.stopped_for_space в
   run_stats вообще (только интерактивное меню [2]/[3] это делало) -- отчёт после реальной
   остановки по нехватке места через CLI не показывал предупреждение."""
import photosort_win as m


class TestSumStatsMergesLists:
    def test_single_source_list_values_survive(self):
        merged = m._sum_stats([{
            "appended_images": 3,
            "encrypted_archives": ["C:\\a\\secret.zip"],
            "dvd_units_copied": [{"name": "DVD1"}],
        }])
        assert merged["appended_images"] == 3
        assert merged["encrypted_archives"] == ["C:\\a\\secret.zip"]
        assert merged["dvd_units_copied"] == [{"name": "DVD1"}]

    def test_two_sources_list_values_concatenate(self):
        merged = m._sum_stats([
            {"encrypted_archives": ["a.zip"]},
            {"encrypted_archives": ["b.zip", "c.zip"]},
        ])
        assert merged["encrypted_archives"] == ["a.zip", "b.zip", "c.zip"]

    def test_missing_list_key_in_one_source_does_not_error(self):
        merged = m._sum_stats([
            {"encrypted_archives": ["a.zip"]},
            {"appended_images": 1},
        ])
        assert merged["encrypted_archives"] == ["a.zip"]
        assert merged["appended_images"] == 1

    def test_numeric_and_album_profiles_merge_unaffected(self):
        """Регресс-проверка: новая elif-ветка не должна перехватывать числа/album_profiles."""
        merged = m._sum_stats([
            {"bytes_appended": 100, "album_profiles": {"p": {"name": "Alb", "n": 1,
                                                              "years": {2020}, "cameras": set(),
                                                              "date_subdirs": set()}}},
            {"bytes_appended": 50},
        ])
        assert merged["bytes_appended"] == 150
        assert merged["album_profiles"]["p"]["n"] == 1


class TestCliArchivePropagatesStoppedForSpace:
    def _fake_run_result(self, **overrides):
        kwargs = dict(failed=False, exit_code=m.EXIT_INSUFFICIENT_SPACE,
                       stats={"appended_images": 1, "bytes_appended": 100},
                       processed_count=1, stopped_for_space=True, pool=None, interrupted=False)
        kwargs.update(overrides)
        return m.RunResult(**kwargs)

    def _run_cli_archive(self, tmp_path, monkeypatch, run_result):
        source = tmp_path / "source"
        source.mkdir()
        target = tmp_path / "target"
        target.mkdir()

        captured = {}

        def fake_generate_report(data, out_path, **kwargs):
            captured["run_stats"] = kwargs.get("run_stats")

        monkeypatch.setattr(m.report, "generate_report", fake_generate_report)
        monkeypatch.setattr(m.report, "parse_target_logs", lambda *a, **k: {})
        monkeypatch.setattr(m, "run_for_source",
                             lambda *a, **k: run_result)
        monkeypatch.setattr(m.sys, "argv",
                             ["photosort_win.py", "archive", "--source", str(source),
                              "--target", str(target)])

        exit_code = m._main()
        return exit_code, captured.get("run_stats")

    def test_stopped_for_space_reaches_run_stats(self, tmp_path, monkeypatch):
        exit_code, run_stats = self._run_cli_archive(
            tmp_path, monkeypatch, self._fake_run_result())
        assert exit_code == m.EXIT_INSUFFICIENT_SPACE
        assert run_stats is not None
        assert run_stats["stopped_for_space"] is True

    def test_not_stopped_for_space_stays_false(self, tmp_path, monkeypatch):
        exit_code, run_stats = self._run_cli_archive(
            tmp_path, monkeypatch,
            self._fake_run_result(exit_code=0, stopped_for_space=False))
        assert exit_code == 0
        assert run_stats is not None
        assert run_stats["stopped_for_space"] is False
