"""_finalize_analyze_report() (Паспорт архива / analyze) must print the same [X/Y] generation
stages as _finalize_target_report() -- REVIEW-HANDOFF.md Раунд 151, замечание 2: both share the
same report-building code path (generate_report_from_analyze_stats() -> _write_flat_xlsx()),
but only the archive-run finalizer printed progress; on a large passport the analyze finalizer
went silent for seconds-to-tens-of-seconds after the 100% bar, reading as a freeze."""
import photosort_win as m


def _patch_report(monkeypatch):
    monkeypatch.setattr(m.report, "generate_placeholder_report", lambda *a, **k: None)
    monkeypatch.setattr(m.report, "generate_report_from_analyze_stats", lambda *a, **k: None)


def test_progress_lines_printed_during_generation(monkeypatch):
    _patch_report(monkeypatch)
    stats = m.AnalyzeStats(mode="analyze", total_files=42)
    logs = []
    m._finalize_analyze_report(stats, open_browser=False, log=logs.append)
    assert any(s.startswith("Формирую итоговый отчёт") for s in logs), logs
    assert any("[1/1] собираю страницу" in s for s in logs), logs
    assert any(s.startswith("Отчёт:") for s in logs), logs


def test_no_progress_lines_for_empty_source(monkeypatch):
    """total_files == 0 -> placeholder report, no generation stage to announce."""
    _patch_report(monkeypatch)
    stats = m.AnalyzeStats(mode="analyze", total_files=0)
    logs = []
    m._finalize_analyze_report(stats, open_browser=False, log=logs.append)
    assert not any("собираю страницу" in s for s in logs), logs


def test_generation_progress_shows_record_count_on_large_passport(monkeypatch):
    _patch_report(monkeypatch)
    stats = m.AnalyzeStats(mode="analyze", total_files=5000)
    stats.exact_dup_edges = [{}] * 2500
    stats.near_dup_edges = [{}] * 1000
    stats.dump_item_paths = ["x"] * 600
    logs = []
    m._finalize_analyze_report(stats, open_browser=False, log=logs.append)
    assert any("4100 записей" in s for s in logs), logs
