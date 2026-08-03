"""_finalize_target_report() must not log "Отчёт: <path>" when interrupted=True -- the two
Ctrl+C call sites (_bare_launch_run_build(), CLI _main()) already log their own follow-up line
"Отчёт (данные на момент остановки): <path>" right after calling this function, so the plain
"Отчёт: <path>" line duplicated the same path (SESSION-HANDOFF.txt, 2026-07-31, пункт G --
regression from the _InterruptedRunReport feature, 2026-07-28, коммит 501f021: that fix didn't
notice this function already logs the path internally)."""
import photosort_win as m


def _patch_report(monkeypatch):
    monkeypatch.setattr(m.report, "generate_placeholder_report",
                         lambda *a, **k: None)
    monkeypatch.setattr(m.report, "parse_target_logs", lambda *a, **k: {})
    monkeypatch.setattr(m.report, "generate_report", lambda *a, **k: None)


def test_no_report_line_when_interrupted(monkeypatch, tmp_path):
    _patch_report(monkeypatch)
    logs = []
    m._finalize_target_report(str(tmp_path), "target", any_succeeded=True, total_processed=3,
                               open_browser=False, log=logs.append, interrupted=True)
    assert not any(s.startswith("Отчёт:") for s in logs), logs


def test_report_line_present_when_not_interrupted(monkeypatch, tmp_path):
    _patch_report(monkeypatch)
    logs = []
    m._finalize_target_report(str(tmp_path), "target", any_succeeded=True, total_processed=3,
                               open_browser=False, log=logs.append, interrupted=False)
    assert any(s.startswith("Отчёт:") for s in logs), logs


def test_no_report_line_when_interrupted_before_anything_processed(monkeypatch, tmp_path):
    """total_processed == 0 branch (placeholder report) goes through the same log() call."""
    _patch_report(monkeypatch)
    logs = []
    m._finalize_target_report(str(tmp_path), "target", any_succeeded=True, total_processed=0,
                               open_browser=False, log=logs.append, interrupted=True)
    assert not any(s.startswith("Отчёт:") for s in logs), logs
