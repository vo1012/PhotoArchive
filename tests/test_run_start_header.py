"""Живая просьба пользователя, 2026-08-24 ("раньше такое было"): в рабочей консоли между
разными прогонами одной сессии GUI-мастера должен быть визуальный разделитель, и в начале
каждого прогона -- параметры запуска (режим/SOURCE/TARGET/TMP_EXTRACT_DIR/время старта).
_log_run_start_header() -- общий хелпер для _run_impl() (сборка/пробный прогон) и
run_analyze() (анализ/паспорт), единственное место, где такой заголовок формируется."""
import photosort_win as m


def _cfg(tmp_path, dry_run=False):
    source = tmp_path / "source"
    target = tmp_path / "target"
    workdir = tmp_path / "appdir"
    source.mkdir()
    target.mkdir()
    workdir.mkdir()
    return m.Config(source=str(source), target=str(target), sample_limit=0,
                     workdir=str(workdir), dry_run=dry_run)


class TestLogRunStartHeader:
    def test_prints_separator_mode_and_paths(self, tmp_path):
        cfg = _cfg(tmp_path)
        lines = []
        m._log_run_start_header("Сборка архива", cfg, log=lines.append)
        text = "\n".join(lines)
        assert "Сборка архива" in text
        assert f"SOURCE: {cfg.source}" in text
        assert f"TARGET: {cfg.target}" in text
        assert f"WORKDIR: {cfg.workdir}" in text
        assert f"TMP_EXTRACT_DIR: {cfg.tmp_extract}" in text
        assert text.count("=" * 70) == 2  # разделитель сверху и снизу шапки

    def test_includes_a_timestamp(self, tmp_path):
        cfg = _cfg(tmp_path)
        lines = []
        m._log_run_start_header("Паспорт архива", cfg, log=lines.append)
        import re
        assert any(re.search(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", line) for line in lines)

    def test_starts_with_a_blank_line_for_visual_separation_between_runs(self, tmp_path):
        """Пустая строка ПЕРЕД разделителем -- визуально отделяет от хвоста вывода предыдущего
        прогона в той же рабочей консоли (см. CLAUDE.md, "Рабочая консоль GUI-мастера" -- окно
        между прогонами сворачивается, не пересоздаётся, старый вывод остаётся в буфере)."""
        lines = []
        m._log_run_start_header("Сборка архива", _cfg(tmp_path), log=lines.append)
        assert lines[0] == ""


class _StopAfterHeader(Exception):
    """Сигнальное исключение -- обрывает _run_impl() сразу после того, как он дошёл до вызова
    заголовка, не запуская реальный конвейер обхода/копирования (не нужен для этого теста,
    интересен только сам факт и аргументы вызова _log_run_start_header())."""


class TestRunImplCallsHeaderWithCorrectLabel:
    """_run_impl() -- реальная сборка (dry_run=False) или пробный прогон (dry_run=True),
    ярлык должен совпадать с _BARE_LAUNCH_MODE_LABELS["build"/"dry_run"]."""

    def _run_and_capture_header_calls(self, monkeypatch, tmp_path, dry_run):
        calls = []
        monkeypatch.setattr(m, "_log_run_start_header",
                              lambda label, cfg, log=print: calls.append(label))

        def _raise(*a, **k):
            raise _StopAfterHeader

        monkeypatch.setattr(m, "_cleanup_own_tmp_extract_entries", _raise)
        cfg = _cfg(tmp_path, dry_run=dry_run)
        try:
            m._run_impl(cfg, log=lambda *a, **k: None, print_summary=False)
        except _StopAfterHeader:
            pass
        return calls

    def test_build_mode_uses_build_label(self, monkeypatch, tmp_path):
        calls = self._run_and_capture_header_calls(monkeypatch, tmp_path, dry_run=False)
        assert calls == [m._BARE_LAUNCH_MODE_LABELS["build"]]

    def test_dry_run_mode_uses_dry_run_label(self, monkeypatch, tmp_path):
        calls = self._run_and_capture_header_calls(monkeypatch, tmp_path, dry_run=True)
        assert calls == [m._BARE_LAUNCH_MODE_LABELS["dry_run"]]


class TestRunAnalyzeCallsHeaderWithCorrectLabel:
    """run_analyze() -- ярлык зависит от mode/self_scan, не был виден пользователю вовсе до
    этой находки (Анализ/Паспорт не печатали заголовок запуска никогда)."""

    def _run_and_capture_header_calls(self, monkeypatch, tmp_path, mode, self_scan):
        calls = []
        monkeypatch.setattr(m, "_log_run_start_header",
                              lambda label, cfg, log=print: calls.append(label))
        monkeypatch.setattr(m, "exiftool_batch", lambda paths, **kw: {})
        cfg = _cfg(tmp_path)
        m.run_analyze(cfg, mode, log=lambda *a, **k: None, self_scan=self_scan)
        return calls

    def test_passport_self_scan_uses_passport_label(self, monkeypatch, tmp_path):
        calls = self._run_and_capture_header_calls(monkeypatch, tmp_path, "analyze", True)
        assert calls == [m._BARE_LAUNCH_MODE_LABELS["passport"]]

    def test_analyze_quick_uses_view_label(self, monkeypatch, tmp_path):
        calls = self._run_and_capture_header_calls(monkeypatch, tmp_path, "analyze-quick", False)
        assert calls == [m._BARE_LAUNCH_MODE_LABELS["view"]]

    def test_full_analyze_without_self_scan_uses_its_own_label(self, monkeypatch, tmp_path):
        """mode=="analyze" без self_scan -- только полный CLI ("analyze --source ..." без
        --target), не пункт голого меню -- собственный ярлык, не переиспользует чужой."""
        calls = self._run_and_capture_header_calls(monkeypatch, tmp_path, "analyze", False)
        assert calls == ["Анализ источника"]
