"""Раунд 115 ревью (придирка, закрыта раунд 116-раунд ответа): probe_display_available()'s
except Exception раньше оборачивал ОБА шага (tk.Tk() И probe.destroy()) одним try/except -- сбой
именно очистки (окно успело открыться, значит GUI реально доступен) ложно классифицировался как
"GUI недоступен" наравне со сбоем самого tk.Tk(). Реальный tkinter на этой (Linux) машине
отсутствует вовсе (ModuleNotFoundError) -- инъекция фейкового модуля в sys.modules обходит это,
тот же приём, что уже использовал ревизор для проверки DPI-инварианта (REVIEW-HANDOFF.md, Раунд
116)."""
import sys
import types

import gui_menu as g


class _FakeTkRaisesOnInit:
    def __init__(self):
        raise RuntimeError("no display, tk.Tk() itself fails")


class _FakeTkRaisesOnDestroy:
    def destroy(self):
        raise RuntimeError("cleanup failure, window opened fine though")


def _inject_fake_tkinter(monkeypatch, tk_cls):
    fake_mod = types.ModuleType("tkinter")
    fake_mod.Tk = tk_cls
    monkeypatch.setitem(sys.modules, "tkinter", fake_mod)


def test_probe_returns_false_when_tk_init_itself_fails(monkeypatch):
    monkeypatch.setattr(g, "_dpi_awareness_set", True)
    _inject_fake_tkinter(monkeypatch, _FakeTkRaisesOnInit)
    assert g.probe_display_available() is False


def test_probe_returns_true_when_only_destroy_fails(monkeypatch):
    """The window itself opened -- GUI is available -- a cleanup-only failure must not be
    mistaken for "GUI unavailable"."""
    monkeypatch.setattr(g, "_dpi_awareness_set", True)
    _inject_fake_tkinter(monkeypatch, _FakeTkRaisesOnDestroy)
    assert g.probe_display_available() is True


class _FakeWorkAreaRoot:
    """Раунд 118 ревью (замечание): _px()/_compute_dpi_scale()/_get_work_area()/
    _cap_dpi_scale_to_fit() (gui_menu.py:142-227) не имели ни одного автотеста, хотя не
    нуждаются в реальном tkinter -- та же заглушка, что ревизор использовал для
    самостоятельной проверки формул (REVIEW-HANDOFF.md, Раунд 118)."""

    def __init__(self, width=1920, height=1080):
        self._width = width
        self._height = height

    def winfo_screenwidth(self):
        return self._width

    def winfo_screenheight(self):
        return self._height


class TestPx:
    def test_scales_by_dpi_scale(self, monkeypatch):
        monkeypatch.setattr(g, "_dpi_scale", 2.0)
        assert g._px(10) == 20

    def test_identity_at_scale_one(self, monkeypatch):
        monkeypatch.setattr(g, "_dpi_scale", 1.0)
        assert g._px(37) == 37

    def test_rounds_to_nearest_int(self, monkeypatch):
        monkeypatch.setattr(g, "_dpi_scale", 1.5)
        assert g._px(3) == round(3 * 1.5)


class TestComputeDpiScale:
    def test_reads_real_tk_scaling_not_a_recomputed_dpi_ratio(self, monkeypatch):
        """Раунд 118 фон (SESSION-HANDOFF.txt): промежуточная версия фикса пересчитывала DPI
        через winfo_fpixels("1i")/96 вместо того, чтобы взять реальный `tk scaling` -- расхождение
        96 vs 72 в знаменателе (×1.33). Тест закрепляет правильную версию: значение берётся
        буквально из `root.tk.call("tk", "scaling")`, никакого собственного пересчёта."""

        class _FakeTclInterp:
            def call(self, *args):
                assert args == ("tk", "scaling")
                return 2.5

        class _FakeRoot:
            tk = _FakeTclInterp()

        monkeypatch.setattr(g, "_dpi_scale", 1.0)
        g._compute_dpi_scale(_FakeRoot())
        assert g._dpi_scale == 2.5


class TestGetWorkArea:
    """Только не-Windows откат (winfo_screenheight() - запас на панель задач) -- реальный путь
    SPI_GETWORKAREA требует настоящего ctypes.windll (Windows-only), не мокается дёшево без
    полной реимплементации марш win32-структур; проверен вручную клик-тестом на HTPC
    (SESSION-HANDOFF.txt), не автотестом."""

    def test_non_windows_fallback_subtracts_taskbar_margin(self, monkeypatch):
        monkeypatch.setattr(g.os, "name", "posix")
        width, height = g._get_work_area(_FakeWorkAreaRoot(1920, 1080))
        assert (width, height) == (1920, 1000)

    def test_fallback_never_goes_below_one_pixel(self, monkeypatch):
        monkeypatch.setattr(g.os, "name", "posix")
        _, height = g._get_work_area(_FakeWorkAreaRoot(1920, 50))
        assert height == 1


class TestCapDpiScaleToFit:
    """Раунд 118 (замечание): ревизор сам исполнил эту арифметику без tkinter и подтвердил её
    корректность -- закрепляем это исполнение как регрессионный тест, включая ровно тот сценарий
    ("2472px требуется, 2160px доступно"), который реальный клик-тест поймал на HTPC."""

    def test_no_change_when_layout_already_fits(self, monkeypatch):
        monkeypatch.setattr(g, "_get_work_area", lambda root: (1920, 1080))
        monkeypatch.setattr(g, "_dpi_scale", 1.0)
        changed = g._cap_dpi_scale_to_fit(_FakeWorkAreaRoot(), 800, 300)
        assert changed is False
        assert g._dpi_scale == 1.0

    def test_shrinks_scale_proportionally_when_overflowing(self, monkeypatch):
        monkeypatch.setattr(g, "_get_work_area", lambda root: (1920, 1080))
        monkeypatch.setattr(g, "_dpi_scale", 2.0)
        changed = g._cap_dpi_scale_to_fit(_FakeWorkAreaRoot(), g._px(1000), g._px(1200))
        assert changed is True
        assert g._dpi_scale == 1.0

    def test_never_shrinks_scale_below_floor_of_one(self, monkeypatch):
        """Уже на полу (1.0) и всё равно не помещается -- лучше слегка не поместиться, чем
        уйти ниже уже провалидированного кликом минимума (docstring _cap_dpi_scale_to_fit())."""
        monkeypatch.setattr(g, "_get_work_area", lambda root: (100, 100))
        monkeypatch.setattr(g, "_dpi_scale", 1.0)
        changed = g._cap_dpi_scale_to_fit(_FakeWorkAreaRoot(), 5000, 5000)
        assert changed is False
        assert g._dpi_scale == 1.0


class _FakeChromeMarginRoot:
    """Раунд 121 ревью (замечание): _chrome_margin() не имела ни одного автотеста -- та же
    заглушка-приём, что и у остальных DPI-функций выше (не нуждается в реальном tkinter,
    winfo_fpixels() -- единственный вызов на root)."""

    def __init__(self, fpixels_per_inch=96.0):
        self._fpixels_per_inch = fpixels_per_inch

    def winfo_fpixels(self, s):
        assert s == "1i"
        return self._fpixels_per_inch


class TestChromeMargin:
    """Раунд 121 ревью (замечание, закрыто этим раундом ответа): константа была `50`, не
    воспроизводила заявленное измерение ~103px на тестовой машине (DPI≈288, os_scale=3.0 ->
    round(50*3.0)=150, разошлось на ~46%) -- ревизор исполнил формулу сам через ту же заглушку и
    поймал расхождение. Закреплено откалиброванное значение (34) как регрессия."""

    def test_matches_measured_chrome_on_test_machine(self):
        # DPI≈288 на HTPC (SESSION-HANDOFF.txt) -> os_scale = 288/96 = 3.0 -- тот самый сценарий,
        # под который _chrome_margin() калибровалась (~103px реально измерено кликом).
        assert g._chrome_margin(_FakeChromeMarginRoot(288.0)) == 102

    def test_identity_at_96_dpi(self):
        assert g._chrome_margin(_FakeChromeMarginRoot(96.0)) == 34

    def test_never_goes_below_96_dpi_floor(self):
        # os_scale никогда не должен уйти ниже 1.0, даже если winfo_fpixels() вернёт что-то
        # меньше 96 (нетипично, но не должно уменьшать запас ниже базового).
        assert g._chrome_margin(_FakeChromeMarginRoot(48.0)) == 34

    def test_falls_back_to_96_dpi_on_exception(self):
        class _RaisingRoot:
            def winfo_fpixels(self, s):
                raise RuntimeError("no display")

        assert g._chrome_margin(_RaisingRoot()) == 34


class TestCapAndShow:
    """Раунд 121 ревью (замечание): _cap_and_show() не имела ни одного автотеста -- чистая
    управляющая логика (вызов render_fn(), проверка cap, при переполнении -- destroy()+
    build_shell(_retry=True)+повтор render_fn()) не требует реального tkinter, только
    duck-typed root/wiz с нужными методами."""

    class _FakeRoot:
        def __init__(self, name):
            self.name = name
            self.destroyed = False

        def winfo_reqwidth(self):
            return 100

        def winfo_reqheight(self):
            return 100

        def destroy(self):
            self.destroyed = True

    class _FakeWiz:
        def __init__(self):
            self.root = TestCapAndShow._FakeRoot("first")
            self.build_shell_calls = 0

        def build_shell(self, _retry=False):
            assert _retry is True
            self.build_shell_calls += 1
            self.root = TestCapAndShow._FakeRoot(f"rebuilt-{self.build_shell_calls}")

    def test_renders_once_and_never_rebuilds_when_it_already_fits(self, monkeypatch):
        monkeypatch.setattr(g, "_cap_dpi_scale_to_fit", lambda root, w, h: False)
        wiz = self._FakeWiz()
        render_calls = []
        g._cap_and_show(wiz, lambda: render_calls.append(wiz.root.name))
        assert render_calls == ["first"]
        assert wiz.build_shell_calls == 0
        assert wiz.root.destroyed is False

    def test_destroys_and_rebuilds_once_then_stops_when_it_fits(self, monkeypatch):
        results = iter([True, False])
        monkeypatch.setattr(g, "_cap_dpi_scale_to_fit", lambda root, w, h: next(results))
        wiz = self._FakeWiz()
        first_root = wiz.root
        render_calls = []
        g._cap_and_show(wiz, lambda: render_calls.append(wiz.root.name))
        assert render_calls == ["first", "rebuilt-1"]
        assert wiz.build_shell_calls == 1
        assert first_root.destroyed is True
        assert wiz.root.name == "rebuilt-1"

    def test_keeps_rebuilding_until_cap_stops_shrinking(self, monkeypatch):
        results = iter([True, True, False])
        monkeypatch.setattr(g, "_cap_dpi_scale_to_fit", lambda root, w, h: next(results))
        wiz = self._FakeWiz()
        render_calls = []
        g._cap_and_show(wiz, lambda: render_calls.append(wiz.root.name))
        assert render_calls == ["first", "rebuilt-1", "rebuilt-2"]
        assert wiz.build_shell_calls == 2


class TestDescribePassportTarget:
    """Раунд 115 ревью (придирка, закрыта раунд 116-раунд ответа): для голого корня диска
    сообщение раньше не называло резолвленный путь на экране 2 (только на экране 3) -- "в этой
    папке" было двусмысленным (сам корень диска или подпапка __PhotoArchive__ в нём)."""

    def test_bare_drive_root_with_archive_names_resolved_path(self, monkeypatch):
        monkeypatch.setattr(g.m, "_is_bare_drive_root", lambda t: True)
        monkeypatch.setattr(g.m, "_target_has_existing_archive", lambda t: True)
        info = g._describe_passport_target("D:\\")
        expected_resolved = g.os.path.join("D:\\", "__PhotoArchive__")
        assert info["ok"] is True
        assert info["resolved"] == expected_resolved
        assert "D:\\" in info["message"]
        assert expected_resolved in info["message"]

    def test_bare_drive_root_without_archive_names_resolved_path(self, monkeypatch):
        monkeypatch.setattr(g.m, "_is_bare_drive_root", lambda t: True)
        monkeypatch.setattr(g.m, "_target_has_existing_archive", lambda t: False)
        info = g._describe_passport_target("D:\\")
        expected_resolved = g.os.path.join("D:\\", "__PhotoArchive__")
        assert info["ok"] is False
        assert expected_resolved in info["message"]

    def test_regular_folder_unaffected_by_bare_root_wording(self, monkeypatch):
        monkeypatch.setattr(g.m, "_is_bare_drive_root", lambda t: False)
        monkeypatch.setattr(g.m, "_target_has_existing_archive", lambda t: True)
        info = g._describe_passport_target("D:\\Photos\\Archive")
        assert info["resolved"] == "D:\\Photos\\Archive"
        assert info["message"] == "В этой папке найден архив PhotoArchive — можно проверить."


class TestMakeOkInputFn:
    """2026-08-22, живая просьба пользователя ("добавить в окно 'работа окончена' ещё одну
    кнопку 'в главное меню' перед 'выход'") -- _notice_window() сама требует реального tkinter
    (блокирующий mainloop() до клика), не юнит-тестируема напрямую (тот же принцип, что и у
    остального модуля, см. его докстринг) -- но _make_ok_input_fn()'s РЕШЕНИЕ по возврату
    _notice_window() (continue -> "" / exit -> KeyboardInterrupt) -- чистая логика, тестируется
    подменой самой _notice_window()."""

    def test_continue_choice_returns_empty_string(self, monkeypatch):
        calls = []
        monkeypatch.setattr(g, "_notice_window",
                              lambda *a, **kw: calls.append((a, kw)) or "continue")
        fn = g._make_ok_input_fn("C:\\archive\\report.html")
        assert fn("Работа окончена. Нажмите Enter...") == ""
        (message,), kwargs = calls[0]
        assert "C:\\archive\\report.html" in message
        assert kwargs["show_exit"] is True

    def test_exit_choice_raises_gui_explicit_exit(self, monkeypatch):
        """2026-08-22, Раунд 123 ревью (замечание): раньше поднимался голый KeyboardInterrupt,
        неотличимый от настоящего Ctrl-C -- main() пропускало паузу "Нажмите Enter" только по
        косвенному признаку (состояние консоли), который для ЭТОЙ кнопки всегда был "консоль
        видна" (см. m._GuiExplicitExit докстринг в photosort_win.py) -- явный тип нужен именно
        для того, чтобы main() отличало этот клик от настоящего прерывания."""
        monkeypatch.setattr(g, "_notice_window", lambda *a, **kw: "exit")
        fn = g._make_ok_input_fn("C:\\archive\\report.html")
        import pytest
        with pytest.raises(g.m._GuiExplicitExit):
            fn("Работа окончена. Нажмите Enter...")


class TestResetPaths:
    """2026-08-22, Раунд 123 ревью (придирка): reset_paths() не имела автотеста, хотя
    _Wizard() явно не трогает tkinter в конструкторе (см. её докстринг) -- не нужен даже
    duck-typed root, только сам объект."""

    def test_clears_source_target_and_target_comment(self):
        wiz = g._Wizard()
        wiz.state["mode"] = "build"
        wiz.state["source"] = "C:\\Photos"
        wiz.state["target"] = "D:\\Archive"
        wiz.state["target_comment"] = {"tone": "neutral", "message": "..."}
        wiz.reset_paths()
        assert wiz.state["source"] is None
        assert wiz.state["target"] is None
        assert wiz.state["target_comment"] is None

    def test_leaves_mode_untouched(self):
        # mode -- выбор экрана 1, не экрана 2/3 -- reset_paths() не должна его трогать (следующий
        # рендер экрана 1 всё равно перезапишет его явным кликом пользователя).
        wiz = g._Wizard()
        wiz.state["mode"] = "passport"
        wiz.reset_paths()
        assert wiz.state["mode"] == "passport"


class TestPathButtonLabel:
    """2026-08-22, живая просьба пользователя ("размер кнопок выбора путей должен быть
    одинаковым") -- кнопка раньше СЖИМАЛАСЬ до голого "Изменить" после выбора пути, резко меняя
    размер. Теперь меняется только глагол, весь остальной текст остаётся тем же самым."""

    def test_unselected_path_keeps_original_text(self):
        assert g._path_button_label("Выбрать источник фотографий…", None) == \
            "Выбрать источник фотографий…"

    def test_selected_path_swaps_only_the_verb(self):
        assert g._path_button_label("Выбрать источник фотографий…", "C:\\Photos") == \
            "Изменить источник фотографий…"

    def test_swaps_verb_for_every_target_button_text(self):
        for mode, text in g._TARGET_BUTTON_TEXT.items():
            changed = g._path_button_label(text, "D:\\Archive")
            assert changed.startswith("Изменить"), mode
            assert changed[len("Изменить"):] == text[len("Выбрать"):]


class TestContentInnerWidth:
    """2026-08-22, живая находка пользователя (экран 3: "сообщение комментария не влезло в
    окно, обрезалось") -- Label-ы прямо в content использовали wraplength=_px(_CONTENT_WIDTH)
    без учёта собственного padx content (24*2=48) -- запрошенная ширина текста оказывалась
    ШИРЕ, чем реально доступно внутри зафиксированного (pack_propagate(False)) content.
    Живой замер (см. диагностику этого раунда) подтвердил: req=2213 > actual=2030 на реальном
    экране 3 ДО фикса -- эта регрессия закрепляет правильную формулу."""

    def test_equals_content_width_minus_content_padx(self):
        assert g._CONTENT_INNER_WIDTH == g._CONTENT_WIDTH - 48

    def test_no_code_still_uses_unadjusted_content_width_as_wraplength(self):
        import inspect
        source = inspect.getsource(g)
        assert "wraplength=_px(_CONTENT_WIDTH)" not in source


class TestFixedScreenSizes:
    """2026-08-22, живая просьба пользователя ("размер окна меню для каждого шага должен быть
    фиксирован -- не нужно его динамически менять в зависимости от выводимого текста") --
    отменяет более раннее решение той же сессии ("сделай высоту адаптивной по экрану") --
    _fit_content_height()/_fit_paths_screen_height() (живое измерение на каждый рендер) удалены
    целиком, заменены _apply_fixed_content_size() с заранее посчитанными константами."""

    def test_dynamic_fit_methods_removed(self):
        assert not hasattr(g._Wizard, "_fit_content_height")
        assert not hasattr(g._Wizard, "_fit_paths_screen_height")

    def test_apply_fixed_content_size_exists(self):
        assert hasattr(g._Wizard, "_apply_fixed_content_size")

    def test_per_screen_height_constants_are_positive(self):
        assert g._MODE_SCREEN_HEIGHT > 0
        assert g._PATHS_SCREEN_HEIGHT > 0
        assert g._CONFIRM_SCREEN_HEIGHT > 0


class _FakeRootForReclaim:
    """Duck-typed root -- никакого реального tkinter, тот же приём, что и
    TestCapAndShow._FakeRoot выше. update() просто считает вызовы (имитирует прокачку
    Tcl-event-loop без реального окна); winfo_id() -- фиктивный hwnd."""

    def __init__(self, hwnd=777):
        self._hwnd = hwnd
        self.update_calls = 0

    def winfo_id(self):
        return self._hwnd

    def update(self):
        self.update_calls += 1


class _FakeUser32ForReclaim:
    """Функция-замыкание, назначенная атрибутом экземпляра, а не обычный метод -- реальный код
    ставит .restype/.argtypes на ctypes.windll.user32.SetForegroundWindow, обычный bound method
    Python этого не умеет (нет __dict__ у него), только у настоящих function-объектов, тот же
    приём, что и у _FakeConsoleWindll в test_main_bare_launch_gui_branch.py."""

    def __init__(self, raise_on_call=False):
        self.calls = []

        def SetForegroundWindow(hwnd):
            self.calls.append(hwnd)
            if raise_on_call:
                raise OSError("simulated SetForegroundWindow failure")
            return 1
        self.SetForegroundWindow = SetForegroundWindow


class TestReclaimWizardFocus:
    """2026-08-23, по прямой просьбе пользователя -- перенос доказанно рабочей синхронной схемы
    photosort_win._reclaim_console_focus() (SetForegroundWindow() дважды, с нарастающей паузой)
    на окно мастера. Тестируется как отдельная функция (духом TestCapAndShow выше) -- не через
    реальный tk.Tk()/build_shell(), время (time.sleep/time.time) замокано, чтобы тест не занимал
    реальную секунду."""

    def _patch_time(self, monkeypatch):
        """time.time() тикает маленькими шагами (0.1) на каждый вызов вместо реального времени
        -- внутренний while-цикл _reclaim_wizard_focus() (`while time.time() < end`) успевает
        прокрутиться пару раз за задержку 0.3/0.7 (значит root.update() реально вызывается), но
        тест не занимает реальную секунду."""
        import time as time_module
        state = {"t": 0.0}

        def fake_time():
            state["t"] += 0.1
            return state["t"]

        monkeypatch.setattr(time_module, "time", fake_time)
        monkeypatch.setattr(time_module, "sleep", lambda s: None)

    def test_calls_set_foreground_window_twice(self, monkeypatch):
        self._patch_time(monkeypatch)
        import ctypes
        fake_user32 = _FakeUser32ForReclaim()
        fake_windll = type("W", (), {"user32": fake_user32})()
        monkeypatch.setattr(ctypes, "windll", fake_windll, raising=False)
        root = _FakeRootForReclaim(hwnd=42)
        g._reclaim_wizard_focus(root)
        assert fake_user32.calls == [42, 42]  # ровно два раза, по одному на каждую задержку
        assert root.update_calls >= 1  # окно успело прокачаться хотя бы раз

    def test_swallows_ctypes_failure(self, monkeypatch):
        self._patch_time(monkeypatch)
        import ctypes
        fake_user32 = _FakeUser32ForReclaim(raise_on_call=True)
        fake_windll = type("W", (), {"user32": fake_user32})()
        monkeypatch.setattr(ctypes, "windll", fake_windll, raising=False)
        root = _FakeRootForReclaim()
        g._reclaim_wizard_focus(root)  # must not raise
        assert fake_user32.calls == [777]  # первая попытка упала -- второй итерации не было

    def test_swallows_missing_winfo_id(self, monkeypatch):
        """root без winfo_id() (совсем сломанный duck-typed объект) -- не должно падать наружу,
        как и любой другой сбой в этой best-effort функции."""
        self._patch_time(monkeypatch)

        class _BrokenRoot:
            def update(self):
                pass

        g._reclaim_wizard_focus(_BrokenRoot())  # must not raise
