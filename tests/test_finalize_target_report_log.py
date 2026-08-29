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


def test_progress_lines_printed_during_generation(monkeypatch, tmp_path):
    """Живой боевой прогон 2026-08-28: прогресс-бар застыл на 100%, а report.py ещё считает --
    без строк консоль читается как зависание. _finalize_target_report() печатает нумерованные
    этапы [X/Y] по мере прохождения (появление строк = сигнал "идёт работа", номер = "сколько
    ещё")."""
    _patch_report(monkeypatch)
    logs = []
    m._finalize_target_report(str(tmp_path), "target", any_succeeded=True, total_processed=42,
                               open_browser=False, log=logs.append, interrupted=False)
    assert any(s.startswith("Формирую итоговый отчёт") for s in logs), logs
    assert any("[1/2] читаю логи прогона" in s for s in logs), logs
    assert any("[2/2] собираю страницу" in s for s in logs), logs


def test_progress_single_step_when_data_passed_in(monkeypatch, tmp_path):
    """level=="workdir" (CLI --dry-run): логи уже собраны вызывающим кодом и переданы через
    data= -- шаг чтения логов не выполняется, счётчик Y = 1, не 2."""
    _patch_report(monkeypatch)
    logs = []
    m._finalize_target_report(str(tmp_path), "workdir", any_succeeded=True, total_processed=42,
                               open_browser=False, log=logs.append, interrupted=False,
                               data={"appended": [{}]})
    assert any("[1/1] собираю страницу" in s for s in logs), logs
    assert not any("читаю логи прогона" in s for s in logs), logs


def test_generation_progress_shows_event_count_on_large_run(monkeypatch, tmp_path):
    monkeypatch.setattr(m.report, "generate_placeholder_report", lambda *a, **k: None)
    monkeypatch.setattr(m.report, "generate_report", lambda *a, **k: None)
    monkeypatch.setattr(m.report, "parse_target_logs",
                         lambda *a, **k: {"skipped": [{}] * 4000, "unreadable": [{}] * 100})
    logs = []
    m._finalize_target_report(str(tmp_path), "target", any_succeeded=True, total_processed=4100,
                               open_browser=False, log=logs.append, interrupted=False)
    assert any("4100 записей" in s for s in logs), logs
