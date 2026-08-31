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

    def test_shows_window_only_once_after_content_and_geometry_are_final(self, monkeypatch):
        """Живая находка пользователя, 2026-08-24: маленькое окно мелькало в левом верхнем углу
        экрана при каждом запуске мастера, потом "исчезало" (на самом деле пересобиралось до
        нормального центрированного вида). Причина -- _ensure_new_wizard_window_normal()
        (deiconify()/SetForegroundWindow()) звалась из build_shell(), ДО того как содержимое
        экрана и центрирование вообще существуют. Теперь зовётся отсюда (_cap_and_show()),
        ПОСЛЕ render_fn() и DPI-cap-цикла -- ровно один раз, с уже финальным root (после
        возможных пересборок), не с промежуточным."""
        results = iter([True, False])
        monkeypatch.setattr(g, "_cap_dpi_scale_to_fit", lambda root, w, h: next(results))
        shown = []
        monkeypatch.setattr(g, "_ensure_new_wizard_window_normal", lambda root: shown.append(root))
        wiz = self._FakeWiz()
        g._cap_and_show(wiz, lambda: None)
        assert shown == [wiz.root]  # финальный (пересобранный) root, и только один раз
        assert shown[0].name == "rebuilt-1"

    def test_does_not_show_window_before_render_fn_runs(self, monkeypatch):
        """Регрессия по порядку: показ окна не должен случиться ДО render_fn() (иначе content
        всё ещё заглушка) -- проверяем порядок вызовов, не только сам факт вызова."""
        monkeypatch.setattr(g, "_cap_dpi_scale_to_fit", lambda root, w, h: False)
        order = []
        monkeypatch.setattr(g, "_ensure_new_wizard_window_normal",
                              lambda root: order.append("show"))
        wiz = self._FakeWiz()
        g._cap_and_show(wiz, lambda: order.append("render"))
        assert order == ["render", "show"]


class TestSamePathError:
    """Живая находка пользователя, 2026-08-24: Шаг 2 разрешал нажать "Далее", даже когда
    SOURCE и TARGET указывают на один и тот же путь -- реальный запуск тут же падал с "ОШИБКОЙ
    КОНФИГУРАЦИИ" (Config.__post_init__(), photosort_win.py:2100-2106) уже при исполнении, не
    раньше. По аналогии с отсутствием архива для паспорта (_describe_passport_target()) --
    отловить это уже на Шаге 2, тем же приёмом (tone="error"/ok=False блокирует "Далее" через
    _paths_valid())."""

    def test_identical_paths_block(self, monkeypatch, tmp_path):
        monkeypatch.setattr(g.m, "_is_bare_drive_root", lambda t: False)
        calls = []
        monkeypatch.setattr(g.m, "_target_has_existing_archive",
                              lambda t: calls.append("archive") or False)
        monkeypatch.setattr(g.m, "_target_needs_confirmation",
                              lambda t: calls.append("confirm") or False)
        p = str(tmp_path)
        info = g._describe_target("build", p, p)
        assert info["ok"] is False
        assert info["tone"] == "error"
        assert "один и тот же путь" in info["message"]
        # Проверка same-path -- ПЕРВАЯ (приоритет ТЗ), остальные состояния не должны даже
        # проверяться, если конфликт уже найден.
        assert calls == []

    def test_same_path_different_case_blocks_on_windows(self, monkeypatch, tmp_path):
        """Раунд 139 ревью (замечание): реальный Windows -- os.path.normcase() приводит
        регистр (NTFS регистронезависима), но на POSIX (эта dev-машина, unit-tests-джоб CI)
        os.path.normcase() -- no-op, ".upper()"/".lower()" остаются разными строками, гейт не
        срабатывает независимо от платформы, для которой написан продакшн-код. Форсируем
        os.path.normcase() на g.os (тот же модуль, что реально зовёт _same_path_error()) в
        предсказуемую регистронезависимую реализацию -- тестируем, что вызывающий код
        ПРАВИЛЬНО ИСПОЛЬЗУЕТ normcase(), не полагаемся на регистронезависимость хост-ОС."""
        monkeypatch.setattr(g.m, "_is_bare_drive_root", lambda t: False)
        monkeypatch.setattr(g.os.path, "normcase", str.lower)
        p = str(tmp_path)
        info = g._describe_target("dry_run", p.upper(), p.lower())
        assert info["ok"] is False

    def test_different_paths_unaffected(self, monkeypatch, tmp_path):
        monkeypatch.setattr(g.m, "_is_bare_drive_root", lambda t: False)
        monkeypatch.setattr(g.m, "_target_has_existing_archive", lambda t: False)
        monkeypatch.setattr(g.m, "_target_needs_confirmation", lambda t: False)
        source = tmp_path / "source"
        target = tmp_path / "target"
        source.mkdir()
        target.mkdir()
        info = g._describe_target("build", str(target), str(source))
        assert info["ok"] is True

    def test_no_source_is_not_applicable(self, monkeypatch, tmp_path):
        """view-режиму TARGET не нужен вовсе -- source=None не должен считаться "тем же
        путём"."""
        assert g._same_path_error(str(tmp_path), None) is None

    def test_bare_drive_root_resolved_path_equal_to_source_blocks(self, monkeypatch, tmp_path):
        """SOURCE указывает ПРЯМО на {диск}:\\__PhotoArchive__, TARGET -- голый корень того же
        диска -- после резолва (_describe_target()) они совпадают, хотя сырые строки разные."""
        monkeypatch.setattr(g.m, "_is_bare_drive_root", lambda t: True)
        resolved = g.os.path.join(str(tmp_path), "__PhotoArchive__")
        info = g._describe_target("build", str(tmp_path), resolved)
        assert info["ok"] is False
        assert info["resolved"] == resolved

    def test_bare_drive_root_nested_target_not_flagged(self, monkeypatch, tmp_path):
        """Поддерживаемый сценарий (TARGET подпапкой внутри SOURCE, см. photosort_win.py'с
        комментарий у Config.__post_init__()) -- SOURCE сам корень диска, TARGET -- тот же
        корень (резолвится в подпапку __PhotoArchive__) -- НЕ конфликт."""
        monkeypatch.setattr(g.m, "_is_bare_drive_root", lambda t: True)
        monkeypatch.setattr(g.m, "_target_has_existing_archive", lambda t: False)
        info = g._describe_target("build", str(tmp_path), str(tmp_path))
        assert info["ok"] is True


class TestConfigGuardsError:
    """Найдено ревизором вне раунда, 2026-08-24 (по прямому вопросу пользователя): три
    соседних жёстких `ValueError` из `Config.__post_init__()` (`photosort_win.py`) -- SOURCE
    внутри TARGET, TARGET == рабочая папка программы, рабочая папка внутри TARGET -- ничем не
    предвосхищались в GUI Шага 2 (в отличие от буквального равенства путей, см.
    TestSamePathError выше), хотя реальный запуск с ними падает так же жёстко. Пользователь
    попросил закрыть находку -- та же схема (`os.path.normcase(os.path.realpath(...))`+
    `startswith`), что и сам `Config.__post_init__()`."""

    def test_source_inside_target_blocks(self, monkeypatch, tmp_path):
        monkeypatch.setattr(g.m, "_is_bare_drive_root", lambda t: False)
        monkeypatch.setattr(g.m, "WORKDIR", str(tmp_path / "elsewhere"))
        target = tmp_path / "Архив"
        source = target / "Старое"
        source.mkdir(parents=True)
        info = g._describe_target("build", str(target), str(source))
        assert info["ok"] is False
        assert info["tone"] == "error"
        assert "внутри архива" in info["message"]

    def test_target_equals_workdir_blocks(self, monkeypatch, tmp_path):
        monkeypatch.setattr(g.m, "_is_bare_drive_root", lambda t: False)
        workdir = tmp_path / "app"
        workdir.mkdir()
        monkeypatch.setattr(g.m, "WORKDIR", str(workdir))
        info = g._describe_target("build", str(workdir), None)
        assert info["ok"] is False
        assert info["tone"] == "error"
        assert "рабочей папкой программы" in info["message"]

    def test_workdir_inside_target_blocks(self, monkeypatch, tmp_path):
        monkeypatch.setattr(g.m, "_is_bare_drive_root", lambda t: False)
        target = tmp_path / "Архив"
        workdir = target / "app"
        workdir.mkdir(parents=True)
        monkeypatch.setattr(g.m, "WORKDIR", str(workdir))
        info = g._describe_target("build", str(target), None)
        assert info["ok"] is False
        assert info["tone"] == "error"
        assert "Рабочая папка программы находится внутри" in info["message"]

    def test_target_inside_source_is_supported_not_flagged(self, monkeypatch, tmp_path):
        """Обратный случай (TARGET подпапкой внутри SOURCE) -- намеренно ПОДДЕРЖИВАЕМЫЙ
        сценарий (см. комментарий в Config.__post_init__()), не должен блокироваться этой
        проверкой -- та же семантика, что уже проверяет test_bare_drive_root_nested_target_
        not_flagged для голого корня диска, здесь для обычной вложенной папки."""
        monkeypatch.setattr(g.m, "_is_bare_drive_root", lambda t: False)
        monkeypatch.setattr(g.m, "WORKDIR", str(tmp_path / "elsewhere"))
        monkeypatch.setattr(g.m, "_target_has_existing_archive", lambda t: False)
        monkeypatch.setattr(g.m, "_target_needs_confirmation", lambda t: False)
        source = tmp_path / "Фото"
        target = source / "Архив"
        source.mkdir()
        target.mkdir()
        info = g._describe_target("build", str(target), str(source))
        assert info["ok"] is True

    def test_unrelated_paths_unaffected(self, monkeypatch, tmp_path):
        monkeypatch.setattr(g.m, "_is_bare_drive_root", lambda t: False)
        monkeypatch.setattr(g.m, "WORKDIR", str(tmp_path / "app"))
        monkeypatch.setattr(g.m, "_target_has_existing_archive", lambda t: False)
        monkeypatch.setattr(g.m, "_target_needs_confirmation", lambda t: False)
        source = tmp_path / "source"
        target = tmp_path / "target"
        source.mkdir()
        target.mkdir()
        info = g._describe_target("build", str(target), str(source))
        assert info["ok"] is True

    def test_no_source_skips_source_containment_check_without_erroring(self, monkeypatch, tmp_path):
        """source=None (режимам, которым он не нужен) -- SOURCE-containment неприменима и не
        должна падать на None; TARGET==WORKDIR/workdir-внутри-TARGET по-прежнему проверяются
        независимо от source (см. соседние тесты выше с явным source=None)."""
        monkeypatch.setattr(g.m, "WORKDIR", str(tmp_path / "elsewhere"))
        assert g._config_guards_error(str(tmp_path / "Архив"), None) is None


class TestPathsValidBlocksSamePathCollision:
    """_paths_valid() -- гейт кнопки "Далее" на Шаге 2 -- должен учитывать ok=False из
    _describe_target(), не только "оба пути выбраны" (до этой находки проверял только
    присутствие, не содержание)."""

    def test_build_mode_blocks_next_on_identical_paths(self, monkeypatch, tmp_path):
        monkeypatch.setattr(g.m, "_is_bare_drive_root", lambda t: False)
        wiz = g._Wizard()
        p = str(tmp_path)
        wiz.state["mode"] = "build"
        wiz.state["source"] = p
        wiz.state["target"] = p
        assert wiz._paths_valid() is False

    def test_dry_run_mode_blocks_next_on_identical_paths(self, monkeypatch, tmp_path):
        monkeypatch.setattr(g.m, "_is_bare_drive_root", lambda t: False)
        wiz = g._Wizard()
        p = str(tmp_path)
        wiz.state["mode"] = "dry_run"
        wiz.state["source"] = p
        wiz.state["target"] = p
        assert wiz._paths_valid() is False

    def test_build_mode_allows_next_on_different_paths(self, monkeypatch, tmp_path):
        monkeypatch.setattr(g.m, "_is_bare_drive_root", lambda t: False)
        monkeypatch.setattr(g.m, "_target_has_existing_archive", lambda t: False)
        monkeypatch.setattr(g.m, "_target_needs_confirmation", lambda t: False)
        source = tmp_path / "source"
        target = tmp_path / "target"
        source.mkdir()
        target.mkdir()
        wiz = g._Wizard()
        wiz.state["mode"] = "build"
        wiz.state["source"] = str(source)
        wiz.state["target"] = str(target)
        assert wiz._paths_valid() is True


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
        # 2026-08-23, живая находка пользователя ("задвоение"): путь раньше повторялся и в
        # message, и в отдельной ссылке -- теперь ТОЛЬКО в link_text (см. тест ниже), не в
        # message вовсе.
        assert "C:\\archive\\report.html" not in message
        assert "C:\\archive\\report.html" in kwargs["link_text"]
        assert kwargs["show_exit"] is True

    def test_message_does_not_reuse_cli_enter_wording(self, monkeypatch):
        """2026-08-23, живая находка пользователя ("нажмите Enter -- и две кнопки, нет логики"):
        раньше текст нотиса был просто CLI-`prompt`, переданный `_pause_for_report()` для
        консольного `input()` -- в GUI с двумя настоящими кнопками "нажмите Enter" не имело
        смысла. Сообщение теперь строится САМО, не из `prompt` -- проверяем, что старой
        формулировки больше нет, и что осталось объяснение поведения кнопок (сворачивание в
        трей/панель задач при "В главное меню", "Выход" отдельно).

        2026-08-23, второй заход (по прямой просьбе пользователя): открытие браузера убрано из
        побочных эффектов кнопки "В главное меню" -- стало отдельной кликабельной ссылкой
        (link_text/link_command), см. _notice_window()/_make_ok_input_fn()'s докстринги за
        обоснованием (устраняет саму причину гонки "окно мастера позади браузера").

        2026-08-23, третий заход (живая находка пользователя, "задвоение"): путь к отчёту
        сначала показывался И в message ("Отчёт сохранён здесь: ..."), И отдельной ссылкой с
        текстом-заглушкой "Открыть отчёт в браузере" -- два упоминания одного и того же файла.
        Путь теперь ТОЛЬКО в link_text (сам путь и есть текст ссылки), message его не
        повторяет вовсе.

        2026-08-23, четвёртый заход (по прямой просьбе пользователя, явный порядок блоков):
        описание кнопок ("сворачивание в трей", "Выход") переехало из message в footer_text --
        message теперь несёт только заголовок + "Найдено/Обработано объектов: X" (см. отдельные
        тесты про count_label ниже), рисуется ДО ссылки; footer_text рисуется ПОСЛЕ ссылки."""
        captured = {}

        def _fake_notice_window(message, **kw):
            captured["message"] = message
            captured["kw"] = kw
            return "continue"

        monkeypatch.setattr(g, "_notice_window", _fake_notice_window)
        fn = g._make_ok_input_fn("C:\\archive\\report.html")
        fn("\nНажмите Enter, чтобы открыть отчёт и вернуться в главное меню: ")
        message = captured["message"]
        footer_text = captured["kw"]["footer_text"]
        assert "Нажмите Enter" not in message
        assert "C:\\archive\\report.html" not in message  # путь -- только в link_text, не тут
        assert "трей" not in message and "панель задач" not in message  # теперь в footer_text
        assert "трей" in footer_text or "панель задач" in footer_text
        assert "Выход" in footer_text
        assert "C:\\archive\\report.html" in captured["kw"]["link_text"]
        assert callable(captured["kw"]["link_command"])

    def test_link_command_opens_the_actual_report_path(self, monkeypatch):
        """link_command -- замыкание над report_path, не над каким-то другим путём (см. Раунд
        про открытие браузера убранное из кнопки выше) -- проверяем, что реально зовёт
        _open_report_link() с ТЕМ ЖЕ путём, что был передан в _make_ok_input_fn().

        2026-08-24, живая находка пользователя: link_command больше не зовёт
        m._open_report_in_browser() (та несёт унаследованную попытку вернуть фокус консоли) --
        только голый webbrowser.open() через _open_report_link(), см. её докстринг."""
        opened = []
        monkeypatch.setattr(g.m.webbrowser, "open", lambda p: opened.append(p))
        captured = {}

        def _fake_notice_window(message, **kw):
            captured["kw"] = kw
            return "continue"

        monkeypatch.setattr(g, "_notice_window", _fake_notice_window)
        fn = g._make_ok_input_fn("C:\\archive\\report.html")
        fn("prompt text irrelevant now")
        captured["kw"]["link_command"]()
        # Раунд 139 ревью (замечание): _open_report_link() зовёт os.path.abspath() -- на
        # реальном Windows это не меняет уже-абсолютный "C:\\..."-путь, но на POSIX (эта
        # dev-машина, unit-tests-джоб CI) abspath() считает такую строку ОТНОСИТЕЛЬНОЙ и клеит
        # cwd спереди. Сравниваем с g.os.path.abspath(...) с ОБЕИХ сторон (тот же приём, что уже
        # использует TestOpenReportLink::test_opens_the_report_path, тот же коммит) -- не
        # платформо-зависимая голая строка.
        assert opened == [g.os.path.abspath("C:\\archive\\report.html")]

    def test_message_shows_object_count_with_given_label(self, monkeypatch):
        """2026-08-23, по прямой просьбе пользователя: "Найдено объектов: X"/"Обработано
        объектов: X" (X -- m._last_bare_launch_object_count, та же переменная, что читает
        статус-строка терминала) -- ДО ссылки на отчёт (см. тест выше про порядок: message
        рисуется первой, до link_text)."""
        monkeypatch.setattr(g.m, "_last_bare_launch_object_count", 42)
        captured = {}
        monkeypatch.setattr(g, "_notice_window",
                              lambda message, **kw: captured.setdefault("message", message) or "continue")
        fn = g._make_ok_input_fn("C:\\archive\\report.html", count_label="Найдено объектов")
        fn("irrelevant")
        assert "Найдено объектов: 42" in captured["message"]

    def test_count_label_defaults_to_processed(self, monkeypatch):
        # dry_run/build не передают count_label явно (см. run_bare_launch()) -- дефолт должен
        # быть "Обработано объектов", не "Найдено".
        monkeypatch.setattr(g.m, "_last_bare_launch_object_count", 7)
        captured = {}
        monkeypatch.setattr(g, "_notice_window",
                              lambda message, **kw: captured.setdefault("message", message) or "continue")
        fn = g._make_ok_input_fn("C:\\archive\\report.html")
        fn("irrelevant")
        assert "Обработано объектов: 7" in captured["message"]

    def test_view_and_passport_call_sites_use_found_label(self):
        """view/passport -- read-only режимы (см. run_bare_launch()) -- "Найдено объектов", не
        "Обработано" (там ничего не обрабатывается/не копируется)."""
        import inspect
        src = inspect.getsource(g.run_bare_launch)
        assert src.count('count_label="Найдено объектов"') == 2

    def test_auto_open_browser_disabled_at_all_four_call_sites(self):
        """2026-08-23, по прямой просьбе пользователя: браузер должен открываться только по
        клику на ссылку в нотисе (см. тесты выше), не автоматически при "В главное меню" --
        m._pause_for_report()'s auto_open_browser=False должно стоять на ВСЕХ GUI-путях
        (view/passport/dry_run/build), не на части их. Регрессия в любом из четырёх мест
        молча вернула бы старое "клик открывает браузер" поведение только для этого режима."""
        import inspect
        src = inspect.getsource(g.run_bare_launch)
        assert src.count("m._pause_for_report(") == 4
        assert src.count("auto_open_browser=False") == 4

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


class TestOpenSiteLink:
    """2026-08-23, по прямой просьбе пользователя: футер экрана 1 (версия/автор/ссылка на
    лендинг) -- _open_site_link() сама (клик по ссылке в render_mode_screen()) требует реального
    tkinter для рендера виджета, но её тело -- чистый вызов webbrowser.open(), тестируется без
    единого окна (тот же принцип, что и у остального модуля, реальный tk.Tk() недоступен на
    Linux dev-сессии, см. докстринг файла)."""

    def test_opens_the_project_site_url(self, monkeypatch):
        opened = []
        monkeypatch.setattr(g.m.webbrowser, "open", lambda url: opened.append(url))
        g._open_site_link()
        assert opened == [g.m.SITE_URL]

    def test_best_effort_swallows_webbrowser_failure(self, monkeypatch):
        # Тот же принцип, что и у photosort_win._open_report_in_browser() -- сбой браузера не
        # должен мочь сломать сам мастер.
        def _raise(url):
            raise OSError("no browser registered")

        monkeypatch.setattr(g.m.webbrowser, "open", _raise)
        g._open_site_link()  # не должно поднять исключение


class TestOpenReportLink:
    """Живая находка пользователя, 2026-08-24: клик по ссылке отчёта в нотисе "Работа
    окончена" сворачивал/перефокусировал окно консоли -- ненужный побочный эффект
    (m._open_report_in_browser() несёт унаследованную _reclaim_console_focus()). Пользователь
    явно попросил "просто запустить браузер" -- _open_report_link() зовёт голый
    webbrowser.open(), тот же паттерн, что и _open_site_link() (см. её тесты выше)."""

    def test_opens_the_report_path(self, monkeypatch):
        opened = []
        monkeypatch.setattr(g.m.webbrowser, "open", lambda p: opened.append(p))
        g._open_report_link("C:\\archive\\report.html")
        assert opened == [g.os.path.abspath("C:\\archive\\report.html")]

    def test_does_not_touch_console_focus_helper(self, monkeypatch):
        """Регрессия именно этой находки: _open_report_link() не должна звать
        m._open_report_in_browser()/m._reclaim_console_focus() вовсе, даже гейтованно."""
        monkeypatch.setattr(g.m.webbrowser, "open", lambda p: None)
        calls = []
        monkeypatch.setattr(g.m, "_open_report_in_browser",
                              lambda p: calls.append(p))
        monkeypatch.setattr(g.m, "_reclaim_console_focus", lambda: calls.append("reclaim"))
        g._open_report_link("C:\\archive\\report.html")
        assert calls == []

    def test_best_effort_swallows_webbrowser_failure(self, monkeypatch):
        def _raise(url):
            raise OSError("no browser registered")

        monkeypatch.setattr(g.m.webbrowser, "open", _raise)
        g._open_report_link("C:\\archive\\report.html")  # не должно поднять исключение


class TestEnsureNewWizardWindowNormal:
    """2026-08-23, живая находка пользователя (ручное тестирование): новое окно мастера после
    нотиса "Работа окончена" иногда оказывалось свёрнутым. Тестируется только Tk-независимая
    часть (root.deiconify()/root.state("normal")) через duck-typed fake root -- ctypes/
    SetForegroundWindow-часть недостижима без реальной Windows-машины (тот же класс разрыва,
    что и у _set_crisp_taskbar_icon(), см. Раунд 134 ревью, придирка)."""

    class _FakeRoot:
        def __init__(self):
            self.calls = []

        def deiconify(self):
            self.calls.append("deiconify")

        def state(self, value):
            self.calls.append(("state", value))

        def winfo_id(self):
            return 12345

    def test_deiconifies_and_sets_normal_state(self, monkeypatch):
        monkeypatch.setattr(g.os, "name", "posix")  # ctypes.windll недоступен вне Windows --
        # ранний return после deiconify/state, та же граница, что и у os.name != "nt" в файле.
        root = self._FakeRoot()
        g._ensure_new_wizard_window_normal(root)
        assert root.calls == ["deiconify", ("state", "normal")]

    def test_best_effort_swallows_tk_failure(self, monkeypatch):
        monkeypatch.setattr(g.os, "name", "posix")

        class _RaisingRoot:
            def deiconify(self):
                raise RuntimeError("no display")

        g._ensure_new_wizard_window_normal(_RaisingRoot())  # не должно поднять исключение


class TestBuildShellWithdrawsBeforePacking:
    """Живая находка пользователя, 2026-08-24, второй заход (первая попытка -- перенос
    _ensure_new_wizard_window_normal() из build_shell() в _cap_and_show() -- не помогла).
    Диагностика по кадрам (EnumWindows-полинг через реальный собранный .exe) показала: окно
    становится visible=True размером ~79x101 (крошечное) ЗАДОЛГО до готовых content/геометрии,
    потому что Tk-окно видимо по умолчанию сразу после tk.Tk() -- откладывать МОМЕНТ явного
    deiconify() бесполезно, если само окно ни разу не было спрятано. Реальный tk.Tk() тут
    недостижим (см. остальной класс), но порядок вызовов -- строковый факт исходника, тот же
    приём, что уже применяют test_view_and_passport_call_sites_use_found_label()/test_auto_
    open_browser_disabled_at_all_four_call_sites() выше."""

    def test_withdraw_called_immediately_after_tk_creation(self):
        import inspect
        import re
        src = inspect.getsource(g._Wizard.build_shell)
        m_tk = re.search(r"root\s*=\s*tk\.Tk\(\)", src)
        m_withdraw = re.search(r"root\.withdraw\(\)", src)
        m_pack = re.search(r"\.pack\(", src)
        assert m_tk and m_withdraw and m_pack
        # withdraw() -- сразу после создания root, до ПЕРВОГО .pack() любого виджета (иначе
        # окно успевает замапиться с уже частично упакованным содержимым до withdraw()).
        assert m_tk.start() < m_withdraw.start() < m_pack.start()


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


class _FakeScalingRoot:
    """Duck-typed root для _apply_dpi_and_font_scale()/_compute_dpi_scale(): единственное, что
    им нужно на root -- `root.tk.call("tk", "scaling"[, value])`. Читает системный масштаб,
    записывает установленный (если был)."""

    def __init__(self, system_scaling=2.0):
        self._system_scaling = system_scaling
        self.scaling_writes = []
        outer = self

        class _Interp:
            def call(self, *args):
                assert args[:2] == ("tk", "scaling")
                if len(args) == 2:
                    return outer._system_scaling
                outer.scaling_writes.append(args[2])
                return ""

        self.tk = _Interp()


class TestGetUiFontScale:
    def test_reads_and_caches_from_photosort_win(self, monkeypatch):
        monkeypatch.setattr(g, "_ui_font_scale", None)
        calls = []

        def _fake_scale(*a, **kw):
            calls.append(1)
            return 1.4

        monkeypatch.setattr(g.m, "gui_font_scale", _fake_scale)
        assert g._get_ui_font_scale() == 1.4
        assert g._get_ui_font_scale() == 1.4  # второй вызов -- из кэша
        assert len(calls) == 1

    def test_falls_back_to_default_if_reader_raises(self, monkeypatch):
        monkeypatch.setattr(g, "_ui_font_scale", None)
        monkeypatch.setattr(g.m, "gui_font_scale",
                            lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom")))
        assert g._get_ui_font_scale() == g.m.GUI_FONT_SCALE_DEFAULT


class TestApplyDpiAndFontScale:
    """Крупный шрифт GUI (PROMPT_run_screen.md §7): один множитель домножает И _dpi_scale
    (_px(), контейнеры), И реальный `tk scaling` Tk (шрифты в pt) -- «виртуальный более высокий
    DPI», режим, который _cap_dpi_scale_to_fit() уже умеет."""

    def test_first_build_multiplies_both_dpi_scale_and_tk_scaling(self, monkeypatch):
        monkeypatch.setattr(g, "_ui_font_scale", 1.5)
        monkeypatch.setattr(g, "_dpi_scale", 1.0)
        root = _FakeScalingRoot(system_scaling=2.0)
        g._apply_dpi_and_font_scale(root, _retry=False)
        assert g._dpi_scale == 3.0            # 2.0 (система) * 1.5 (укрупнение)
        assert root.scaling_writes == [3.0]   # тот же коэффициент ушёл и в шрифты

    def test_scale_one_is_byte_identical_to_old_behaviour(self, monkeypatch):
        """gui_font_scale == 1.0 -> только чтение системного масштаба, `tk scaling` не
        переписывается вовсе (в т.ч. на retry) -- прежнее поведение сохранено."""
        monkeypatch.setattr(g, "_ui_font_scale", 1.0)
        monkeypatch.setattr(g, "_dpi_scale", 5.0)
        root = _FakeScalingRoot(system_scaling=2.0)
        g._apply_dpi_and_font_scale(root, _retry=False)
        assert g._dpi_scale == 2.0
        assert root.scaling_writes == []
        root2 = _FakeScalingRoot(system_scaling=2.0)
        g._apply_dpi_and_font_scale(root2, _retry=True)
        assert root2.scaling_writes == []

    def test_retry_keeps_capped_dpi_scale_and_syncs_tk_scaling_to_it(self, monkeypatch):
        """После _cap_dpi_scale_to_fit() _dpi_scale уже уменьшен под экран -- retry его НЕ
        пересчитывает (иначе затёр бы коррекцию), но подтягивает `tk scaling` под него, чтобы
        шрифт не оказался крупнее ужатых padding'ов."""
        monkeypatch.setattr(g, "_ui_font_scale", 1.5)
        monkeypatch.setattr(g, "_dpi_scale", 2.4)  # уже скорректировано cap'ом
        root = _FakeScalingRoot(system_scaling=2.0)
        g._apply_dpi_and_font_scale(root, _retry=True)
        assert g._dpi_scale == 2.4                 # не тронут
        assert root.scaling_writes == [2.4]        # шрифты подтянуты под него

    def test_all_tk_window_factories_route_through_the_single_helper(self):
        """ось «подключено ли реально» (PROMPT_review.md): _compute_dpi_scale() больше не
        зовётся напрямую ниоткуда, кроме самого _apply_dpi_and_font_scale()."""
        import inspect
        call_lines = [ln.strip() for ln in inspect.getsource(g).splitlines()
                      if ln.strip() == "_compute_dpi_scale(root)"]
        assert call_lines == ["_compute_dpi_scale(root)"]  # ровно один -- внутри хелпера
