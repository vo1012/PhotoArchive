"""Экран «Выполнение» (PROMPT_run_screen.md), по прямой команде пользователя. Движковая
инфраструктура (photosort_win.py): RunEventBus, _cooperative_checkpoint(), _HardExit,
ProgressReporter<->bus. Живой клик по реальному .exe -- задача Windows-сессии (см. CLAUDE.md,
эта сессия -- Linux/VPS, без дисплея); эти тесты покрывают саму механику (очередь/события/
кооперативная отмена), не тянут tkinter.

Конвенция файла -- как test_ctrl_c_report.py, БЕЗ моков движка там, где нужно проверить, что
реальный пайплайн (SourceWalker/analyze) действительно останавливается на кооперативном
чекпоинте и строит корректный частичный отчёт; как test_pause_keypress.py -- заглушка msvcrt
там, где нужна детерминированная симуляция msvcrt-пути делегирования."""
import os
import queue
import subprocess
import sys
import threading
import time

import pytest
from PIL import Image

import photosort_win as m


def _make_jpeg(path, size=(800, 600), color=(10, 20, 30)):
    Image.new("RGB", size, color).save(path, "JPEG")


class TestRunEventBus:
    def test_events_land_in_queue_in_order(self):
        bus = m.RunEventBus()
        bus.status("some status line")
        bus.log("a log line")
        bus.done("C:\\report.html", "ok")
        got = []
        try:
            while True:
                got.append(bus.queue.get_nowait())
        except queue.Empty:
            pass
        assert got == [
            ("status", "some status line"),
            ("log", "a log line"),
            ("done", "C:\\report.html", "ok"),
        ]

    def test_error_event_carries_text_and_crashlog_path(self):
        bus = m.RunEventBus()
        bus.error("boom", "C:\\crash.log")
        assert bus.queue.get_nowait() == ("error", "boom", "C:\\crash.log")

    def test_pause_and_cancel_events_start_unset(self):
        bus = m.RunEventBus()
        assert not bus.pause_event.is_set()
        assert not bus.cancel_event.is_set()
        assert bus.cancel_hard is False

    def test_each_bus_instance_has_its_own_events(self):
        """Не глобальные Event -- два bus'а (например, тест + реальный воркер по ошибке
        созданные одновременно) не должны делить состояние паузы/отмены."""
        a, b = m.RunEventBus(), m.RunEventBus()
        a.cancel_event.set()
        assert not b.cancel_event.is_set()


class TestNonTwoLinePhaseToStatus:
    """2026-09-01, живой отзыв: не-two_line фазы («Оцениваю объём работы», индексация архива,
    Фаза 3 — хеширование) в GUI-режиме слали периодические строки в скроллбэк-зеркало, а
    status-строка стояла пустой. Теперь их прогресс идёт в bus.status() (обновление на месте),
    а не в bus.log()."""

    def _drain(self, bus):
        out = []
        try:
            while True:
                out.append(bus.queue.get_nowait())
        except queue.Empty:
            pass
        return out

    def test_init_sends_initial_status_for_non_two_line_phase(self, monkeypatch):
        bus = m.RunEventBus()
        monkeypatch.setattr(m, "_run_event_bus", bus)
        with m.ProgressReporter(total=None, desc=" Оцениваю объём работы", unit="файл"):
            pass
        kinds = self._drain(bus)
        assert ("status", "Оцениваю объём работы") in kinds
        assert not any(k == "log" for k, *_ in kinds)

    def test_emit_plain_line_goes_to_status_not_log(self, monkeypatch):
        bus = m.RunEventBus()
        monkeypatch.setattr(m, "_run_event_bus", bus)
        bar = m.ProgressReporter(total=None, desc=" Оцениваю объём работы", unit="файл")
        self._drain(bus)  # выкинуть начальный status от __init__
        bar._t0 = m.time.time() - 3
        bar.count = 1234
        bar._emit_plain_line()
        events = self._drain(bus)
        statuses = [t for k, t in events if k == "status"]
        assert statuses and "Оцениваю объём работы" in statuses[-1]
        assert not any(k == "log" for k, *_ in events)

    def test_log_and_bus_status_logs_and_mirrors_to_status(self, monkeypatch):
        bus = m.RunEventBus()
        monkeypatch.setattr(m, "_run_event_bus", bus)
        logged = []
        m._log_and_bus_status("  [1/2] читаю логи прогона…", logged.append)
        assert logged == ["  [1/2] читаю логи прогона…"]
        assert self._drain(bus) == [("status", "[1/2] читаю логи прогона…")]

    def test_log_and_bus_status_no_bus_just_logs(self, monkeypatch):
        monkeypatch.setattr(m, "_run_event_bus", None)
        logged = []
        m._log_and_bus_status("Формирую итоговый отчёт…", logged.append)
        assert logged == ["Формирую итоговый отчёт…"]

    def test_two_line_phase_sends_only_build_two_line_status_no_phase_event(self, monkeypatch):
        bus = m.RunEventBus()
        monkeypatch.setattr(m, "_run_event_bus", bus)
        with m.ProgressReporter(total=None, desc=" Сканирую", unit="файл", two_line=True):
            pass
        events = self._drain(bus)
        # События `phase` больше нет вовсе (RunEventBus.phase() удалён 2026-09-01).
        assert not any(k == "phase" for k, *_ in events)
        # two_line-фаза не шлёт «сырой desc» -- только собранную _build_two_line_status()
        # (первый кадр форсит self.update(0) в конце __init__ через _never_refreshed).
        statuses = [t for k, t in events if k == "status"]
        assert statuses and statuses[0] != "Сканирую"
        assert "Сканирую" in statuses[0] and "обработано объектов" in statuses[0]


class TestNoWindowFlag:
    """REVIEW-HANDOFF.md Раунд 183, замечание 183-3 + Раунд 184, замечание 184-4: все вызовы
    bundled-инструментов несут startupinfo=_NO_WINDOW_STARTUPINFO (STARTF_USESHOWWINDOW|
    SW_HIDE), иначе на windowed-сборке мелькает окно консоли. Не creationflags=CREATE_NO_WINDOW
    (184-4): тот отвязывал ребёнка от консоли родителя -> на CLI-пути Ctrl-C переставал
    доходить до exiftool/ffmpeg/7z, а Popen.wait() блокировался до таймаута."""

    def test_no_window_startupinfo_constant_exists_and_is_none_off_windows(self):
        assert hasattr(m, "_NO_WINDOW_STARTUPINFO")
        if os.name != "nt":
            assert m._NO_WINDOW_STARTUPINFO is None  # startupinfo=None -> обычный default
        else:
            si = m._NO_WINDOW_STARTUPINFO
            assert si.dwFlags & subprocess.STARTF_USESHOWWINDOW
            assert si.wShowWindow == subprocess.SW_HIDE
            # 184-4: НЕ должно быть флага, отвязывающего от консоли родителя
            assert not hasattr(m, "_NO_WINDOW") or m._NO_WINDOW == 0

    def test_no_subprocess_call_uses_creationflags(self):
        """184-4: ни один subprocess.run/Popen больше НЕ передаёт creationflags= (был
        CREATE_NO_WINDOW, отвязывавший ребёнка от консоли родителя) -- только startupinfo=."""
        import ast
        import inspect
        tree = ast.parse(inspect.getsource(m))
        offenders = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            f = node.func
            is_sp = (isinstance(f, ast.Attribute) and f.attr in ("run", "Popen")
                     and isinstance(f.value, ast.Name) and f.value.id == "subprocess")
            if is_sp and any(kw.arg == "creationflags" for kw in node.keywords):
                offenders.append(getattr(node, "lineno", "?"))
        assert not offenders, f"creationflags= на subprocess-вызове (строки {offenders})"

    def test_run_subprocess_cooperative_passes_startupinfo(self, monkeypatch):
        captured = {}
        real_popen = subprocess.Popen

        def _spy(cmd, **kw):
            captured.update(kw)
            return real_popen(cmd, **kw)

        monkeypatch.setattr(m.subprocess, "Popen", _spy)
        monkeypatch.setattr(m, "_run_event_bus", None)
        m._run_subprocess_cooperative([sys.executable, "-c", "pass"], timeout=30,
                                       log=lambda *a, **k: None)
        assert "startupinfo" in captured
        assert captured["startupinfo"] is m._NO_WINDOW_STARTUPINFO
        assert "creationflags" not in captured  # 184-4

    def test_all_bundled_subprocess_calls_carry_startupinfo(self):
        """ast-скан: ни один subprocess.run/Popen в photosort_win.py не остаётся без
        startupinfo= (183-3/184-4 -- легко забыть при добавлении нового вызова)."""
        import ast
        import inspect
        tree = ast.parse(inspect.getsource(m))
        missing = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            f = node.func
            is_sp = (isinstance(f, ast.Attribute) and f.attr in ("run", "Popen")
                     and isinstance(f.value, ast.Name) and f.value.id == "subprocess")
            if not is_sp:
                continue
            if not any(kw.arg == "startupinfo" for kw in node.keywords):
                missing.append(getattr(node, "lineno", "?"))
        assert not missing, f"subprocess.run/Popen без startupinfo= на строках {missing}"


class TestRunEventBusChildProc:
    """REVIEW-HANDOFF.md Раунд 182, замечание 182-1: bus держит ссылку на активный дочерний
    распаковщик (7z/UnRAR), чтобы жёсткая отмена с главного потока могла убить его немедленно,
    не оставляя сиротой после sys.exit(0)."""

    def test_child_proc_starts_unset(self):
        assert m.RunEventBus()._child_proc is None

    def test_kill_child_is_noop_when_no_child(self):
        m.RunEventBus().kill_child()  # must not raise

    def test_kill_child_terminates_registered_process(self):
        bus = m.RunEventBus()
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        bus.register_child(proc)
        bus.kill_child()
        proc.wait(timeout=5)
        assert proc.poll() is not None
        bus.unregister_child()
        assert bus._child_proc is None

    def test_kill_child_swallows_error_on_already_dead_process(self):
        bus = m.RunEventBus()
        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        proc.wait(timeout=5)
        bus.register_child(proc)
        bus.kill_child()  # must not raise even though proc already exited


class TestRunSubprocessCooperative:
    """182-1: _run_subprocess_cooperative() -- замена блокирующего subprocess.run() внутри
    extract_archive() для 7z/UnRAR. Кооперативно опрашивает _cooperative_checkpoint() во время
    ожидания и убивает дочерний процесс при отмене/таймауте (иначе он остаётся сиротой)."""

    def test_returns_returncode_of_completed_process_no_bus(self, monkeypatch):
        monkeypatch.setattr(m, "_run_event_bus", None)
        rc = m._run_subprocess_cooperative(
            [sys.executable, "-c", "import sys; sys.exit(7)"], timeout=30,
            log=lambda *a, **k: None)
        assert rc == 7

    def test_registers_and_unregisters_child_on_bus(self, monkeypatch):
        bus = m.RunEventBus()
        monkeypatch.setattr(m, "_run_event_bus", bus)
        seen = []
        orig = bus.register_child
        bus.register_child = lambda p: (seen.append(p), orig(p))[1]
        m._run_subprocess_cooperative(
            [sys.executable, "-c", "pass"], timeout=30, log=lambda *a, **k: None)
        assert seen and seen[0].poll() == 0
        assert bus._child_proc is None  # снят в finally

    def test_hard_cancel_kills_child_and_raises_hard_exit(self, monkeypatch):
        bus = m.RunEventBus()
        monkeypatch.setattr(m, "_run_event_bus", bus)
        seen = []
        orig = bus.register_child
        bus.register_child = lambda p: (seen.append(p), orig(p))[1]

        def _cancel_soon():
            time.sleep(0.1)
            bus.cancel_hard = True
            bus.cancel_event.set()

        threading.Thread(target=_cancel_soon, daemon=True).start()
        started = time.time()
        with pytest.raises(m._HardExit):
            m._run_subprocess_cooperative(
                [sys.executable, "-c", "import time; time.sleep(60)"], timeout=120,
                log=lambda *a, **k: None)
        assert time.time() - started < 10  # не ждали весь sleep(60)
        assert seen and seen[0].poll() is not None  # дочерний процесс убит
        assert bus._child_proc is None

    def test_soft_cancel_kills_child_and_raises_keyboard_interrupt(self, monkeypatch):
        bus = m.RunEventBus()
        monkeypatch.setattr(m, "_run_event_bus", bus)
        seen = []
        orig = bus.register_child
        bus.register_child = lambda p: (seen.append(p), orig(p))[1]

        def _cancel_soon():
            time.sleep(0.1)
            bus.cancel_event.set()

        threading.Thread(target=_cancel_soon, daemon=True).start()
        with pytest.raises(KeyboardInterrupt) as exc_info:
            m._run_subprocess_cooperative(
                [sys.executable, "-c", "import time; time.sleep(60)"], timeout=120,
                log=lambda *a, **k: None)
        assert not isinstance(exc_info.value, m._HardExit)
        assert seen and seen[0].poll() is not None

    def test_timeout_kills_child_and_raises_timeout_expired(self, monkeypatch):
        monkeypatch.setattr(m, "_run_event_bus", None)
        started = time.time()
        with pytest.raises(subprocess.TimeoutExpired):
            m._run_subprocess_cooperative(
                [sys.executable, "-c", "import time; time.sleep(60)"], timeout=0.5,
                log=lambda *a, **k: None)
        assert time.time() - started < 10

    def test_pause_does_not_kill_process_resume_lets_it_finish(self, monkeypatch):
        bus = m.RunEventBus()
        bus.pause_event.set()
        monkeypatch.setattr(m, "_run_event_bus", bus)

        def _resume_soon():
            time.sleep(0.15)
            bus.pause_event.clear()

        threading.Thread(target=_resume_soon, daemon=True).start()
        rc = m._run_subprocess_cooperative(
            [sys.executable, "-c", "import time; time.sleep(0.4)"], timeout=30,
            log=lambda *a, **k: None)
        assert rc == 0  # пауза не убила процесс, после снятия он спокойно доработал


class TestCooperativeCheckpointDelegation:
    """Без bus (_run_event_bus is None) -- байт-в-байт делегирует в _check_pause_keypress(),
    текстовый режим/CLI/существующие тесты не должны увидеть НИКАКОГО изменения поведения."""

    def test_delegates_to_check_pause_keypress_when_no_bus(self, monkeypatch):
        monkeypatch.setattr(m, "_run_event_bus", None)
        calls = []
        monkeypatch.setattr(m, "_check_pause_keypress", lambda log=print: calls.append(log))
        sentinel_log = object()
        m._cooperative_checkpoint(log=sentinel_log)
        assert calls == [sentinel_log]

    def test_real_msvcrt_path_untouched_off_windows(self, monkeypatch):
        """red-before-green по духу: если делегирование когда-нибудь сломается, этот тест
        обязан упасть вместе с test_pause_keypress.py -- оба зовут ОДНУ и ту же функцию."""
        monkeypatch.setattr(m, "_run_event_bus", None)
        monkeypatch.setattr(os, "name", "posix")
        logged = []
        m._cooperative_checkpoint(log=logged.append)  # must not raise, must not log anything
        assert logged == []


class TestCooperativeCheckpointBus:
    """_cooperative_checkpoint() читает МОДУЛЬНЫЙ global m._run_event_bus, не параметр --
    каждый тест обязан monkeypatch.setattr(m, "_run_event_bus", bus), просто создать
    RunEventBus() локально недостаточно (тот же приём, что и во всех остальных тестах этого
    класса/файла, где нужен реально ПОДКЛЮЧЁННЫЙ bus, не просто существующий объект)."""

    def test_soft_cancel_raises_plain_keyboard_interrupt_not_interrupted_run_report(
            self, monkeypatch):
        """Мягкая отмена поднимает ОБЫЧНЫЙ KeyboardInterrupt, не _InterruptedRunReport
        напрямую -- см. _cooperative_checkpoint()'s докстринг в photosort_win.py: путь отчёта
        известен только вызывающему _bare_launch_run_*(), не этой точке глубоко в конвейере."""
        bus = m.RunEventBus()
        bus.cancel_event.set()
        monkeypatch.setattr(m, "_run_event_bus", bus)
        with pytest.raises(KeyboardInterrupt) as exc_info:
            m._cooperative_checkpoint(log=lambda *a, **k: None)
        assert not isinstance(exc_info.value, m._InterruptedRunReport)
        assert not isinstance(exc_info.value, m._HardExit)

    def test_hard_cancel_raises_hard_exit(self, monkeypatch):
        bus = m.RunEventBus()
        bus.cancel_hard = True
        bus.cancel_event.set()
        monkeypatch.setattr(m, "_run_event_bus", bus)
        with pytest.raises(m._HardExit):
            m._cooperative_checkpoint(log=lambda *a, **k: None)

    def test_hard_exit_is_not_a_keyboard_interrupt_subclass(self):
        """Ось «подключено ли реально»: если бы _HardExit наследовал KeyboardInterrupt, её
        поймал бы тот же `except KeyboardInterrupt: stats.interrupted = True`, что и мягкую
        отмену, глубоко в run_for_source()/run_analyze() -- жёсткий выход тихо превратился бы
        в мягкий (частичный отчёт вместо немедленного прекращения)."""
        assert not issubclass(m._HardExit, KeyboardInterrupt)
        assert issubclass(m._HardExit, BaseException)

    def test_pause_blocks_until_cleared_then_proceeds(self, monkeypatch):
        bus = m.RunEventBus()
        bus.pause_event.set()
        monkeypatch.setattr(m, "_run_event_bus", bus)

        def _resume_soon():
            time.sleep(0.05)
            bus.pause_event.clear()

        threading.Thread(target=_resume_soon, daemon=True).start()
        started = time.time()
        m._cooperative_checkpoint(log=lambda *a, **k: None)  # must not raise, must return
        assert time.time() - started >= 0.03

    def test_pause_then_cancel_raises_after_resume_gate(self, monkeypatch):
        """Пауза, а на выходе из неё отмена уже стоит -- checkpoint обязан поднять исключение
        сразу, не давать ложному "продолжаю" мгновению работы после снятия паузы."""
        bus = m.RunEventBus()
        bus.pause_event.set()
        monkeypatch.setattr(m, "_run_event_bus", bus)

        def _cancel_while_paused():
            time.sleep(0.02)
            bus.cancel_event.set()
            bus.pause_event.clear()

        threading.Thread(target=_cancel_while_paused, daemon=True).start()
        with pytest.raises(KeyboardInterrupt):
            m._cooperative_checkpoint(log=lambda *a, **k: None)

    def test_no_cancel_no_pause_returns_immediately(self, monkeypatch):
        bus = m.RunEventBus()
        monkeypatch.setattr(m, "_run_event_bus", bus)
        m._cooperative_checkpoint(log=lambda *a, **k: None)  # must not raise/block


class TestCooperativeCheckpointCallSitesConnected:
    """Ось «подключено ли реально» (PROMPT_review.md): все прежние точки вызова
    _check_pause_keypress() должны реально звать _cooperative_checkpoint(), не просто
    существовать рядом с ней. ast.walk(), не substring-подсчёт по исходнику -- узкие
    докстринги/комментарии этого файла регулярно упоминают "_check_pause_keypress()" словом
    (история находки A/B и т.п.), substring-счётчик считал бы и их, ast различает реальные
    Name/Call-узлы от текста внутри строковых литералов."""

    def test_no_direct_check_pause_keypress_references_remain_outside_delegation(self):
        import ast
        import inspect
        tree = ast.parse(inspect.getsource(m))
        refs = [node.id for node in ast.walk(tree)
                if isinstance(node, ast.Name) and node.id == "_check_pause_keypress"]
        # Единственная оставшаяся ссылка -- делегирующий вызов внутри самой
        # _cooperative_checkpoint() -- ни одной прямой ссылки/вызова с прежних точек
        # конвейера (SourceWalker/sha256_file/основные циклы).
        assert refs == ["_check_pause_keypress"], (
            f"ожидалась ровно 1 ссылка на _check_pause_keypress (делегирующий вызов в "
            f"_cooperative_checkpoint()), найдено {len(refs)} -- какая-то точка конвейера, "
            f"похоже, зовёт старую функцию напрямую в обход шины")


class TestProgressReporterBus:
    def test_no_tqdm_bar_when_bus_set(self, monkeypatch):
        bus = m.RunEventBus()
        monkeypatch.setattr(m, "_run_event_bus", bus)
        with m.ProgressReporter(total=None, desc=" Копирую", unit="файл") as bar:
            assert bar._bar is None

    def test_constructor_emits_initial_status_for_non_two_line(self, monkeypatch):
        bus = m.RunEventBus()
        monkeypatch.setattr(m, "_run_event_bus", bus)
        with m.ProgressReporter(total=None, desc=" Копирую", unit="файл"):
            pass
        kind, label = bus.queue.get_nowait()
        # phase-события больше нет -- не-two_line фаза сразу ставит desc в status-строку.
        assert (kind, label) == ("status", "Копирую")

    def test_no_bus_no_status_event_text_mode_unaffected(self, monkeypatch):
        monkeypatch.setattr(m, "_run_event_bus", None)
        with m.ProgressReporter(total=None, desc=" Копирую", unit="файл"):
            pass  # must not raise -- no bus to enqueue into

    def test_transient_op_emits_immediate_status_event(self, monkeypatch):
        """§6 ТЗ ("Извлекаю" -- обновление НЕМЕДЛЕННО, не ждать следующего тика). Событие
        `phase` снято 2026-09-01 -- set_transient_op() сама кладёт status со свежей
        _build_two_line_status() (op-поле = transient_op), не полагаясь на следующий update()."""
        bus = m.RunEventBus()
        monkeypatch.setattr(m, "_run_event_bus", bus)

        def _drain():
            try:
                while True:
                    bus.queue.get_nowait()
            except queue.Empty:
                pass

        with m.ProgressReporter(total=None, desc=" Копирую", unit="файл", two_line=True) as bar:
            _drain()  # __init__()'s update(0) уже положил первый status()
            bar.set_transient_op(" Извлекаю (1.2ГБ)")
            kind, label = bus.queue.get_nowait()
            assert kind == "status" and "Извлекаю (1.2ГБ)" in label
            _drain()
            bar.set_transient_op(None)
            kind, label = bus.queue.get_nowait()
            assert kind == "status" and "Копирую" in label  # вернулся к resting-desc


class TestRealPipelineSoftCancelViaBus:
    """Без единого мока движка (см. test_ctrl_c_report.py за тем же принципом) -- реальный
    _bare_launch_run_view() на синтетическом источнике, отменённый через RunEventBus ДО старта
    (первый же _cooperative_checkpoint() внутри основного цикла обхода видит cancel_event уже
    установленным -- детерминированно, без гонки/таймингов на файлах)."""

    def test_cancel_before_start_raises_interrupted_run_report_with_partial_html(
            self, tmp_path, monkeypatch):
        source = tmp_path / "source"
        source.mkdir()
        _make_jpeg(source / "a.jpg")
        _make_jpeg(source / "b.jpg")
        workdir = tmp_path / "workdir"
        workdir.mkdir()
        monkeypatch.setattr(m, "WORKDIR", str(workdir))

        bus = m.RunEventBus()
        bus.cancel_event.set()
        monkeypatch.setattr(m, "_run_event_bus", bus)

        raised = None
        try:
            m._bare_launch_run_view([str(source)], log=lambda *a, **k: None)
        except m._InterruptedRunReport as e:
            raised = e
        assert raised is not None
        assert raised.report_path is not None
        html = open(raised.report_path, encoding="utf-8").read()
        assert "прервана пользователем" in html.lower()

    def test_partial_report_is_readable_by_a_next_run_without_error(self, tmp_path, monkeypatch):
        """Ось «что дальше» (PROMPT_review.md): частичный отчёт -- не только валидный HTML сам
        по себе, но и не мешает следующему обычному прогону над тем же источником (никакого
        оставшегося LOCK/полусостояния архива, специфичного для view-режима -- он вообще
        ничего не пишет на диск, кроме отчёта; проверка на регрессию, если это когда-нибудь
        изменится)."""
        source = tmp_path / "source"
        source.mkdir()
        _make_jpeg(source / "a.jpg")
        _make_jpeg(source / "b.jpg")
        workdir = tmp_path / "workdir"
        workdir.mkdir()
        monkeypatch.setattr(m, "WORKDIR", str(workdir))

        bus = m.RunEventBus()
        bus.cancel_event.set()
        monkeypatch.setattr(m, "_run_event_bus", bus)
        try:
            m._bare_launch_run_view([str(source)], log=lambda *a, **k: None)
        except m._InterruptedRunReport:
            pass

        monkeypatch.setattr(m, "_run_event_bus", None)  # следующий "прогон" -- без шины/отмены
        report_path = m._bare_launch_run_view([str(source)], log=lambda *a, **k: None)
        html = open(report_path, encoding="utf-8").read()
        assert "прервана пользователем" not in html.lower()


class TestBusTeeStream:
    def test_write_splits_on_newlines_into_separate_log_events(self):
        bus = m.RunEventBus()
        tee = m._BusTeeStream(bus)
        tee.write("first line\nsecond line\n")
        got = []
        try:
            while True:
                got.append(bus.queue.get_nowait())
        except queue.Empty:
            pass
        assert got == [("log", "first line"), ("log", "second line")]

    def test_partial_line_without_trailing_newline_is_buffered_not_emitted(self):
        bus = m.RunEventBus()
        tee = m._BusTeeStream(bus)
        tee.write("no newline yet")
        assert bus.queue.empty()
        tee.write(" -- now done\n")
        assert bus.queue.get_nowait() == ("log", "no newline yet -- now done")

    def test_isatty_is_false_and_flush_is_a_noop(self):
        bus = m.RunEventBus()
        tee = m._BusTeeStream(bus)
        assert tee.isatty() is False
        tee.flush()  # must not raise

    def test_print_through_tee_reaches_bus_as_log_event(self):
        """Верхний тройник (§2.4 ТЗ) -- реальный print(), не только .write() напрямую, должен
        доходить до bus.log() -- страховка от print()/трейсбека в обход log()."""
        bus = m.RunEventBus()
        tee = m._BusTeeStream(bus)
        orig_stdout = sys.stdout
        sys.stdout = tee
        try:
            print("hello from a stray print()")
        finally:
            sys.stdout = orig_stdout
        assert bus.queue.get_nowait() == ("log", "hello from a stray print()")
