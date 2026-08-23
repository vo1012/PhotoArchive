"""2026-08-23, живая находка пользователя: _open_report_in_browser() безусловно звала
_reclaim_console_focus() (код 2026-07-21, из эпохи текстового меню) -- та насильно возвращала
фокус КОНСОЛИ синхронным time.sleep()-циклом прямо перед тем, как GUI-цикл
(gui_menu.run_bare_launch()) сворачивает эту же консоль -- окно НОВОГО мастера (создаётся уже
после) оказывалось позади только что открывшегося браузера. Фикс: _reclaim_console_focus()
теперь звонится только когда _console_freed_for_gui==False (текстовое меню/не-Windows
dev-сессия, где консоль реально следующий экран) -- на GUI-пути показ окна отвечает
gui_menu._Wizard.build_shell()'s собственная логика."""
import photosort_win as m


def test_skips_reclaim_console_focus_on_gui_path(monkeypatch):
    calls = []
    monkeypatch.setattr(m, "_console_freed_for_gui", True)
    monkeypatch.setattr(m.webbrowser, "open", lambda path: True)
    monkeypatch.setattr(m, "_reclaim_console_focus", lambda: calls.append(True))
    m._open_report_in_browser("C:\\TEST_TEST\\report.html")
    assert calls == []


def test_calls_reclaim_console_focus_on_text_menu_path(monkeypatch):
    calls = []
    monkeypatch.setattr(m, "_console_freed_for_gui", False)
    monkeypatch.setattr(m.webbrowser, "open", lambda path: True)
    monkeypatch.setattr(m, "_reclaim_console_focus", lambda: calls.append(True))
    m._open_report_in_browser("C:\\TEST_TEST\\report.html")
    assert calls == [True]


def test_webbrowser_failure_does_not_prevent_focus_decision(monkeypatch):
    """webbrowser.open() падает (например, нет ни одного зарегистрированного браузера) --
    _reclaim_console_focus() всё равно должна быть вызвана/пропущена по тому же правилу, не
    заблокирована исключением из webbrowser.open()."""
    calls = []
    monkeypatch.setattr(m, "_console_freed_for_gui", False)

    def _boom(path):
        raise OSError("simulated: no browser registered")

    monkeypatch.setattr(m.webbrowser, "open", _boom)
    monkeypatch.setattr(m, "_reclaim_console_focus", lambda: calls.append(True))
    m._open_report_in_browser("C:\\TEST_TEST\\report.html")  # must not raise
    assert calls == [True]
