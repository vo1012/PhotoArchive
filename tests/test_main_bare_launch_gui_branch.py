"""Раунд 114 ревью (придирка, закрыта раунд 116-раунд ответа): "новая ветка голого запуска
_main() (10759-10787) и _fatal_messagebox() -- нет ни одного автотеста". Эта ветка (голый
запуск на Windows -- пробует gui_menu) физически недостижима на этой (Linux) машине без
monkeypatch os.name == "nt" (сам гейт `if os.name == "nt":` в _main()) -- тот же приём, что уже
использует ревизор для gui_menu.probe_display_available()/_configure_dpi_awareness() (см.
REVIEW-HANDOFF.md, Раунд 116). Реальный tkinter здесь не нужен -- gui_menu.probe_display_
available()/gui_menu.run_bare_launch() подменяются напрямую, чтобы проверить именно ВЕТВЛЕНИЕ
_main(), а не сам GUI (тот уже покрыт отдельными тестами/ручным кликом на Windows-сессии).

2026-08-22, по прямой просьбе пользователя ("не нужно текстовое дублирование GUI") -- текстовое
меню (m.run_bare_launch()) больше НЕ является фоллбэком для голого запуска на Windows: если GUI
недоступен (probe_display_available()==False или ImportError на `import gui_menu`),
_fatal_messagebox() показывается напрямую, m.run_bare_launch() (текстовая) НЕ зовётся вообще.
Тесты "..._falls_back_to_text_menu_..." (проверяли откат на текстовое меню) заменены на
"..._shows_fatal_messagebox_..." ниже; тесты "..._on_text_menu_crash"/"..._from_text_menu_..."
(проверяли поведение внутри текстового-меню-как-фоллбэка на Windows -- путь, которого больше не
существует) удалены как проверяющие недостижимый код."""
import os
import sys

import gui_menu
import photosort_win as m


def _set_bare_argv(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["photosort_win.py"])
    monkeypatch.setattr(os, "name", "nt")


def test_main_uses_gui_menu_when_display_available(monkeypatch):
    _set_bare_argv(monkeypatch)
    calls = []
    monkeypatch.setattr(gui_menu, "probe_display_available", lambda: True)
    monkeypatch.setattr(gui_menu, "run_bare_launch", lambda **kw: calls.append(("gui", kw)))

    def _text_fallback_should_not_run(**kw):
        raise AssertionError("text-mode run_bare_launch() must not run when GUI is available")

    monkeypatch.setattr(m, "run_bare_launch", _text_fallback_should_not_run)
    stdio_calls = []
    monkeypatch.setattr(m, "_configure_windows_stdio_at_startup",
                          lambda has_cli_args: stdio_calls.append(has_cli_args))

    assert m._main() == 0
    assert [c[0] for c in calls] == ["gui"]
    # 2026-08-22 (windowed-сборка): вызывается БЕЗУСЛОВНО первой строкой _main(), не только
    # когда GUI подтверждён -- has_cli_args=False, потому что argv здесь пустой (голый запуск).
    assert stdio_calls == [False]


def test_main_shows_fatal_messagebox_when_display_unavailable(monkeypatch):
    """2026-08-22: GUI недоступен -- _fatal_messagebox() напрямую, никакого отката на текстовое
    меню (m.run_bare_launch() не должна звать вообще -- в отличие от старого поведения, где она
    была фоллбэком). _configure_windows_stdio_at_startup() ВСЁ РАВНО вызывается -- она больше не
    гейтится на успех GUI-пробы (windowed-сборка, см. её докстринг -- ждать подтверждения GUI
    больше незачем, консоли изначально нет ни в одном исходе)."""
    _set_bare_argv(monkeypatch)
    monkeypatch.setattr(gui_menu, "probe_display_available", lambda: False)

    def _gui_should_not_run(**kw):
        raise AssertionError("gui_menu.run_bare_launch() must not run when the probe failed")

    monkeypatch.setattr(gui_menu, "run_bare_launch", _gui_should_not_run)

    def _text_should_not_run(**kw):
        raise AssertionError("text-mode run_bare_launch() must not run -- no longer a fallback")

    monkeypatch.setattr(m, "run_bare_launch", _text_should_not_run)
    stdio_calls = []
    monkeypatch.setattr(m, "_configure_windows_stdio_at_startup",
                          lambda has_cli_args: stdio_calls.append(has_cli_args))
    messagebox_calls = []
    monkeypatch.setattr(m, "_fatal_messagebox", lambda text: messagebox_calls.append(text))

    assert m._main() == 1
    assert len(messagebox_calls) == 1
    assert stdio_calls == [False]


def test_main_shows_fatal_messagebox_when_gui_menu_import_fails(monkeypatch):
    _set_bare_argv(monkeypatch)
    monkeypatch.setitem(sys.modules, "gui_menu", None)  # forces ImportError on `import gui_menu`
    monkeypatch.setattr(m, "_configure_windows_stdio_at_startup", lambda has_cli_args: None)

    def _text_should_not_run(**kw):
        raise AssertionError("text-mode run_bare_launch() must not run -- no longer a fallback")

    monkeypatch.setattr(m, "run_bare_launch", _text_should_not_run)
    messagebox_calls = []
    monkeypatch.setattr(m, "_fatal_messagebox", lambda text: messagebox_calls.append(text))

    assert m._main() == 1
    assert len(messagebox_calls) == 1


def test_main_propagates_exception_from_gui_session_uncaught(monkeypatch):
    """Round 114 (замечание 2)'s исходная находка -- сбой ПОСЕРЕДИНЕ GUI-сессии (не только сбой
    пробного tk.Tk()) не должен тонуть без crash.log под общим except. Эта ветка гейтится
    только на факт, что probe_display_available() уже сказал "да, GUI доступен" -- дальше
    run_bare_launch() зовётся БЕЗ try/except вокруг, исключение обязано долететь наружу как
    есть."""
    _set_bare_argv(monkeypatch)
    monkeypatch.setattr(gui_menu, "probe_display_available", lambda: True)
    monkeypatch.setattr(m, "_configure_windows_stdio_at_startup", lambda has_cli_args: None)

    def _boom(**kw):
        raise RuntimeError("crash mid GUI session, not a display-probe failure")

    monkeypatch.setattr(gui_menu, "run_bare_launch", _boom)

    import pytest
    with pytest.raises(RuntimeError, match="crash mid GUI session"):
        m._main()


def test_main_non_windows_bare_launch_still_uses_text_menu(monkeypatch):
    """m.run_bare_launch() (текстовая) остаётся живым internal dev-путём на НЕ-Windows -- это
    решение 2026-08-22 касается только реального конечного пользователя на Windows, не dev-
    сессий (см. модульный докстринг этого файла и комментарий в _main())."""
    monkeypatch.setattr(sys, "argv", ["photosort_win.py"])
    monkeypatch.setattr(os, "name", "posix")
    text_calls = []
    monkeypatch.setattr(m, "run_bare_launch", lambda **kw: text_calls.append(kw))

    assert m._main() == 0
    assert len(text_calls) == 1


def test_fatal_messagebox_never_raises_off_windows(monkeypatch, capsys):
    monkeypatch.setattr(os, "name", "posix")
    m._fatal_messagebox("test message, non-Windows path")
    assert "test message, non-Windows path" in capsys.readouterr().err


def test_fatal_messagebox_swallows_ctypes_failure_on_windows(monkeypatch, capsys):
    """Реальный клик-тест на HTPC (2026-08-21, SESSION-HANDOFF.txt) поймал: windll -- Windows-only
    attribute of the real ctypes module -- на НЕ-Windows машине его просто нет,
    `ctypes.windll.user32.MessageBoxW` сам кидал AttributeError -- удобный, но случайный способ
    попасть в except-ветку _fatal_messagebox(). На РЕАЛЬНОЙ Windows этот атрибут существует и
    работает взаправду -- тест вместо проверки "проглатывает сбой" открывал настоящий модальный
    MessageBoxW и зависал, ожидая живого клика (283с вместо мгновенного прохода на этой самой
    машине). Мокаем сам MessageBoxW так, чтобы он бросал исключение НЕЗАВИСИМО от реальной ОС --
    тест теперь проверяет заявленное поведение (except Exception: pass -> печать в stderr) на
    любой платформе, не полагаясь на побочный эффект отсутствующего атрибута."""
    monkeypatch.setattr(os, "name", "nt")
    import ctypes

    class _FailingUser32:
        def MessageBoxW(self, *args, **kwargs):
            raise OSError("simulated MessageBoxW failure")

    class _FailingWindll:
        user32 = _FailingUser32()

    monkeypatch.setattr(ctypes, "windll", _FailingWindll(), raising=False)
    m._fatal_messagebox("test message, simulated windows path")
    assert "test message, simulated windows path" in capsys.readouterr().err


class _FakeConsoleWindll:
    """Заглушка ctypes.windll.kernel32 для _configure_windows_stdio_at_startup() -- НИКОГДА не
    давать этим тестам дёрнуть настоящий AttachConsole() на РЕАЛЬНОЙ Windows-машине, где эта
    сессия сейчас и исполняется: настоящий AttachConsole() отсоединил/подменил бы консоль
    САМОГО процесса pytest, ломая вывод этого же прогона -- тот же класс риска, что уже описан в
    test_fatal_messagebox_swallows_ctypes_failure_on_windows() выше (там -- настоящий модальный
    MessageBoxW, здесь -- манипуляция консолью).

    2026-09 (PROMPT_run_screen.md): AllocConsole/GetConsoleWindow больше не в реальном коде --
    GUI-режим не создаёт рабочую консоль вовсе (см. RunEventBus/_BusTeeStream в
    photosort_win.py) -- заглушки этих двух методов убраны отсюда вместе с ними."""

    def __init__(self, raise_on=(), attach_result=1):
        # Настоящие ctypes-функции (windll.kernel32.Foo) поддерживают присваивание .restype/
        # .argtypes -- обычный bound method Python этого не умеет (нет __dict__ у wrapper-
        # объекта). Функции-замыкания, назначенные атрибутами экземпляра, -- умеют (обычные
        # function-объекты поддерживают произвольные атрибуты) -- реалистичнее имитируют то,
        # с чем работает реальный код.
        self.calls = []
        self._raise_on = raise_on
        self._attach_result = attach_result

        def AttachConsole(pid):
            self.calls.append(("AttachConsole", pid))
            if "AttachConsole" in self._raise_on:
                raise OSError("simulated AttachConsole failure")
            return self._attach_result
        self.AttachConsole = AttachConsole


def _fake_windll(kernel32, user32=None):
    attrs = {"kernel32": kernel32}
    if user32 is not None:
        attrs["user32"] = user32
    return type("W", (), attrs)()


def _fake_open_capturing(opened, content_by_path=None):
    """Подменяет builtins.open() -- реальный open("CONOUT$"/"CONIN$", ...) на не-Windows
    машине, где эта сессия сейчас исполняется, упал бы (устройство не существует)."""
    def _fake_open(path, mode, **kwargs):
        opened.append(path)
        import io
        return io.StringIO()
    return _fake_open


def test_configure_windows_stdio_is_noop_off_windows(monkeypatch):
    monkeypatch.setattr(os, "name", "posix")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(m, "_console_freed_for_gui", False)
    orig_stdout, orig_stderr = sys.stdout, sys.stderr
    m._configure_windows_stdio_at_startup(has_cli_args=False)
    assert m._console_freed_for_gui is False
    assert sys.stdout is orig_stdout
    assert sys.stderr is orig_stderr


def test_configure_windows_stdio_is_noop_when_not_frozen(monkeypatch):
    """КРИТИЧНО (см. функции докстринг): обычный dev-запуск (`python photosort_win.py ...`,
    тот же путь, которым ci/windows_ci_test.py's subprocess.run()-тесты реально гоняются на
    Windows CI-раннере) -- НЕ должен трогать sys.stdout/stderr вообще, даже на os.name=="nt".
    Без этой проверки функция подменила бы уже рабочий sys.stdout (например, пайп
    `capture_output=True` вызывающего subprocess.run()) на os.devnull, тихо ломая перехват
    вывода в тестах, которые ни разу не бегали в windowed-сборке."""
    monkeypatch.setattr(os, "name", "nt")
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    monkeypatch.setattr(m, "_console_freed_for_gui", False)
    orig_stdout, orig_stderr = sys.stdout, sys.stderr
    m._configure_windows_stdio_at_startup(has_cli_args=True)
    assert m._console_freed_for_gui is False
    assert sys.stdout is orig_stdout
    assert sys.stderr is orig_stderr


def test_configure_windows_stdio_leaves_already_redirected_output_untouched(monkeypatch):
    """Живой баг, пойман реальным прогоном собранного .exe (см. функции докстринг): вызывающий
    (subprocess.run(capture_output=True)/Start-Process -RedirectStandardOutput -- именно так
    работает ci/smoke_test_exe.py на УЖЕ СОБРАННОМ frozen .exe) уже дал windowed-бутлоадеру
    рабочий sys.stdout (пайп) -- функция не должна трогать его вообще, ни AttachConsole, ни
    devnull, независимо от has_cli_args."""
    monkeypatch.setattr(os, "name", "nt")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    fake_kernel32 = _FakeConsoleWindll()
    import ctypes
    monkeypatch.setattr(ctypes, "windll", _fake_windll(fake_kernel32), raising=False)
    monkeypatch.setattr(m, "_console_freed_for_gui", False)
    # sys.stdout НЕ None -- имитирует уже переданный вызывающим рабочий пайп.
    assert sys.stdout is not None
    orig_stdout, orig_stderr = sys.stdout, sys.stderr
    m._configure_windows_stdio_at_startup(has_cli_args=True)
    assert fake_kernel32.calls == []  # AttachConsole вообще не пробовался
    assert sys.stdout is orig_stdout
    assert sys.stderr is orig_stderr


def test_configure_windows_stdio_bare_launch_redirects_to_devnull(monkeypatch):
    """has_cli_args=False (голый запуск), sys.stdout уже None (bootloader не получил ни одного
    валидного хендла -- настоящий голый запуск без какого-либо перенаправления) -- AttachConsole
    НИКОГДА не пробуется (windowed-сборка и так никогда не создаёт консоль для этого пути),
    sys.stdout/stderr сразу на os.devnull."""
    monkeypatch.setattr(os, "name", "nt")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    fake_kernel32 = _FakeConsoleWindll()
    import ctypes
    monkeypatch.setattr(ctypes, "windll", _fake_windll(fake_kernel32), raising=False)
    monkeypatch.setattr(m, "_console_freed_for_gui", False)
    m._configure_windows_stdio_at_startup(has_cli_args=False)
    assert m._console_freed_for_gui is True
    assert fake_kernel32.calls == []  # AttachConsole не вызывался вовсе
    assert sys.stdout is not None
    assert sys.stderr is not None


def test_configure_windows_stdio_cli_attaches_to_parent_console(monkeypatch):
    """has_cli_args=True, sys.stdout уже None (реальный CLI-запуск без перенаправления, из
    существующего терминала) -- AttachConsole (ATTACH_PARENT_PROCESS=0xFFFFFFFF) пробуется, при
    успехе sys.stdout/stderr/stdin переоткрываются на CONOUT$/CONIN$, НЕ на devnull."""
    monkeypatch.setattr(os, "name", "nt")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    fake_kernel32 = _FakeConsoleWindll(attach_result=1)
    import ctypes
    monkeypatch.setattr(ctypes, "windll", _fake_windll(fake_kernel32), raising=False)
    monkeypatch.setattr(m, "_console_freed_for_gui", False)
    opened = []
    monkeypatch.setattr("builtins.open", _fake_open_capturing(opened))
    m._configure_windows_stdio_at_startup(has_cli_args=True)
    assert fake_kernel32.calls == [("AttachConsole", 0xFFFFFFFF)]
    assert opened == ["CONOUT$", "CONOUT$", "CONIN$"]
    assert sys.stdout is not None


def test_configure_windows_stdio_cli_falls_back_to_devnull_when_no_parent_console(monkeypatch):
    """AttachConsole возвращает 0 (нет консоли-родителя -- например, .exe с аргументами
    запущен не из терминала) -- падать нельзя, sys.stdout/stderr должны остаться безопасно
    записываемыми (devnull), не None/невалидными."""
    monkeypatch.setattr(os, "name", "nt")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    fake_kernel32 = _FakeConsoleWindll(attach_result=0)
    import ctypes
    monkeypatch.setattr(ctypes, "windll", _fake_windll(fake_kernel32), raising=False)
    monkeypatch.setattr(m, "_console_freed_for_gui", False)
    m._configure_windows_stdio_at_startup(has_cli_args=True)  # must not raise
    assert sys.stdout is not None
    sys.stdout.write("safe write, must not raise")  # devnull -- writable, not None


def test_configure_windows_stdio_cli_swallows_attach_console_failure(monkeypatch):
    monkeypatch.setattr(os, "name", "nt")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    fake_kernel32 = _FakeConsoleWindll(raise_on=("AttachConsole",))
    import ctypes
    monkeypatch.setattr(ctypes, "windll", _fake_windll(fake_kernel32), raising=False)
    monkeypatch.setattr(m, "_console_freed_for_gui", False)
    m._configure_windows_stdio_at_startup(has_cli_args=True)  # must not raise
    assert m._console_freed_for_gui is True
    assert sys.stdout is not None


class TestShouldPauseBeforeExit:
    """2026-08-23, переписано вместе с _should_pause_before_exit() -- по прямой просьбе
    пользователя ("рабочая консоль GUI-мастера не предусматривает работу в ней с клавиатуры")
    функция больше не различает состояния GUI-консоли (_work_console_allocated не читается
    вовсе) -- единственная развилка теперь "текстовое меню/не-Windows dev-сессия" (пауза нужна,
    поведение как раньше) vs "любой реальный Windows bare launch" (пауза никогда не нужна,
    краш/явный выход/Ctrl-C идут другими путями -- см. TestMainNeverPausesOnWindowsGuiBareLaunch
    и TestMainCrashNotice ниже)."""

    def test_not_bare_launch_never_pauses(self, monkeypatch):
        monkeypatch.setattr(m, "_console_freed_for_gui", False)
        assert m._should_pause_before_exit(False) is False

    def test_text_menu_or_non_windows_pauses_as_before(self, monkeypatch):
        # Консоль никогда не отсоединялась -- старое поведение, пауза нужна.
        monkeypatch.setattr(m, "_console_freed_for_gui", False)
        assert m._should_pause_before_exit(True) is True

    def test_windows_bare_launch_never_pauses(self, monkeypatch):
        # Любой реальный Windows bare launch (GUI или fatal_messagebox-фоллбэк) -- пауза не
        # нужна независимо от того, дошла ли GUI-сессия до реальной обработки.
        monkeypatch.setattr(m, "_console_freed_for_gui", True)
        assert m._should_pause_before_exit(True) is False


class TestMainNeverPausesOnWindowsGuiBareLaunch:
    """2026-08-23, по прямой просьбе пользователя ("Ctrl-C -- только способ убить программу, без
    дополнительных подтверждений"; "терминал не предусматривает работу в нем с клавиатуры").
    Раньше (Раунд 123/128 ревью) main() различал явный GUI-выход (m._GuiExplicitExit,
    пропускал паузу) от настоящего Ctrl-C (голый KeyboardInterrupt, паузил, если консоль
    когда-либо создавалась) -- теперь оба случая ведут себя одинаково: ни один не паузит на
    Windows GUI bare launch, см. _should_pause_before_exit()'s новый упрощённый докстринг."""

    def test_gui_explicit_exit_skips_pause(self, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["photosort_win.py"])
        pause_calls = []
        monkeypatch.setattr(m, "_pause_before_exit", lambda *a, **kw: pause_calls.append(True))
        monkeypatch.setattr(m, "console_log", lambda msg: None)
        monkeypatch.setattr(m, "_console_freed_for_gui", True)

        def _boom():
            raise m._GuiExplicitExit

        monkeypatch.setattr(m, "_main", _boom)
        import pytest
        with pytest.raises(SystemExit) as exc_info:
            m.main()
        # 2026-08-24, живая просьба пользователя: код 0 (не 130) для голого запуска -- см.
        # main()'s except KeyboardInterrupt докстринг-комментарий (Windows Terminal не
        # закрывает вкладку сама на ненулевом коде выхода, что выглядело как "не полный выход").
        assert exc_info.value.code == 0
        assert pause_calls == []

    def test_real_keyboard_interrupt_also_skips_pause(self, monkeypatch):
        """2026-08-23: инверсия прежнего "контрольного теста на регрессию" -- раньше настоящий
        Ctrl-C ДОЛЖЕН был паузить, когда консоль видна; теперь, по прямой просьбе пользователя,
        Ctrl-C -- это способ мгновенно убить программу, без единого дополнительного
        подтверждения, независимо от состояния консоли."""
        monkeypatch.setattr(sys, "argv", ["photosort_win.py"])
        pause_calls = []
        monkeypatch.setattr(m, "_pause_before_exit", lambda *a, **kw: pause_calls.append(True))
        monkeypatch.setattr(m, "console_log", lambda msg: None)
        monkeypatch.setattr(m, "_console_freed_for_gui", True)

        def _boom():
            raise KeyboardInterrupt

        monkeypatch.setattr(m, "_main", _boom)
        import pytest
        with pytest.raises(SystemExit) as exc_info:
            m.main()
        # 2026-08-24: код 0 для голого запуска -- см. предыдущий тест/main()'s докстринг-комментарий.
        assert exc_info.value.code == 0
        assert pause_calls == []

    def test_eof_error_skips_pause_too(self, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["photosort_win.py"])
        pause_calls = []
        monkeypatch.setattr(m, "_pause_before_exit", lambda *a, **kw: pause_calls.append(True))
        monkeypatch.setattr(m, "console_log", lambda msg: None)
        monkeypatch.setattr(m, "_console_freed_for_gui", True)

        def _boom():
            raise EOFError

        monkeypatch.setattr(m, "_main", _boom)
        import pytest
        with pytest.raises(SystemExit) as exc_info:
            m.main()
        assert exc_info.value.code == 130
        assert pause_calls == []


class TestMainCrashNotice:
    """2026-08-23, по прямой просьбе пользователя -- заменяет прежний
    TestMainReshowsHiddenConsoleBeforePause (Раунд 128 ревью). Тот класс чинил симптом
    (консоль могла быть свёрнута в момент паузы) костылём поверх консольного input(); эта
    версия убирает саму причину -- настоящий краш посреди GUI bare launch больше не паузит на
    консоли вообще, а показывает отдельное GUI-окно (gui_menu._show_crash_notice()),
    независимо от того, свёрнута рабочая консоль или нет."""

    def _patch_common(self, monkeypatch, console_freed_for_gui):
        pause_calls = []
        monkeypatch.setattr(sys, "argv", ["photosort_win.py"])
        monkeypatch.setattr(m, "_pause_before_exit", lambda *a, **kw: pause_calls.append(True))
        monkeypatch.setattr(m, "console_log", lambda msg: None)
        monkeypatch.setattr(m, "_log_unexpected_crash", lambda log: None)
        monkeypatch.setattr(m, "_console_freed_for_gui", console_freed_for_gui)
        return pause_calls

    def test_exception_shows_gui_crash_notice_on_windows_bare_launch(self, monkeypatch):
        pause_calls = self._patch_common(monkeypatch, console_freed_for_gui=True)
        notice_calls = []
        import gui_menu
        monkeypatch.setattr(gui_menu, "_show_crash_notice",
                              lambda message: notice_calls.append(message))

        def _boom():
            raise RuntimeError("simulated crash inside _run_wizard()")

        monkeypatch.setattr(m, "_main", _boom)
        import pytest
        with pytest.raises(SystemExit) as exc_info:
            m.main()
        assert exc_info.value.code == 1
        # Консольная пауза НЕ используется на этом пути вовсе -- GUI-окно её полностью
        # заменяет, независимо от того, свёрнута рабочая консоль в этот момент или нет.
        assert pause_calls == []
        assert len(notice_calls) == 1
        assert "ОШИБКА" in notice_calls[0]
        assert "crash.log" in notice_calls[0]

    def test_exception_falls_back_to_console_pause_on_text_menu(self, monkeypatch):
        """Не-Windows dev-сессия/текстовое меню -- GUI недоступен по определению
        (_console_freed_for_gui только когда-либо становится True на os.name=="nt"), краш
        по-прежнему паузит на консоли, как раньше."""
        pause_calls = self._patch_common(monkeypatch, console_freed_for_gui=False)
        import gui_menu
        notice_calls = []
        monkeypatch.setattr(gui_menu, "_show_crash_notice",
                              lambda message: notice_calls.append(message))

        def _boom():
            raise RuntimeError("simulated crash, text menu path")

        monkeypatch.setattr(m, "_main", _boom)
        import pytest
        with pytest.raises(SystemExit) as exc_info:
            m.main()
        assert exc_info.value.code == 1
        assert pause_calls == [True]
        assert notice_calls == []

    def test_exception_falls_back_to_no_pause_when_gui_notice_itself_fails(self, monkeypatch):
        """Best-effort, как и _fatal_messagebox(): если САМО GUI-окно краша не может
        открыться (например, Tk уже недоступен), main() не должен зависнуть или упасть --
        crash.log уже написан заранее (_log_unexpected_crash), процесс просто завершается."""
        pause_calls = self._patch_common(monkeypatch, console_freed_for_gui=True)
        import gui_menu
        monkeypatch.setattr(gui_menu, "_show_crash_notice",
                              lambda message: (_ for _ in ()).throw(RuntimeError("no Tk")))

        def _boom():
            raise RuntimeError("simulated crash inside _run_wizard()")

        monkeypatch.setattr(m, "_main", _boom)
        import pytest
        with pytest.raises(SystemExit) as exc_info:
            m.main()
        assert exc_info.value.code == 1
        # _should_pause_before_exit() возвращает False безусловно для Windows bare launch
        # (см. её докстринг) -- даже когда GUI-нотис не смог показаться, фоллбэка на
        # консольную паузу здесь НЕТ, ровно как и для остальных Windows GUI-путей.
        assert pause_calls == []
