"""gui_menu.py -- экран «Выполнение» (PROMPT_run_screen.md), по прямой команде пользователя.
Реального tk.Tk() здесь нет (tkinter не установлен на этой Linux dev-машине, см. модульный
докстринг gui_menu.py/test_gui_menu.py) -- либо чистая логика без единого import tkinter
(_run_worker_thread() -- воркер-поток НИКОГДА не трогает tkinter, см. её докстринг), либо
duck-typed заглушки на _Wizard()-методы, которые его трогают. `_Wizard()`
сам по себе безопасно конструировать без дисплея (см. test_gui_menu.py) -- только некоторые её
методы делают `import tkinter as tk` внутри себя (_stop_run_timer()/_append_run_log_line() --
нужен только `tk.TclError` для `except`), для них -- инъекция фейкового модуля tkinter в
sys.modules, та же конвенция, что и test_gui_menu.py::_inject_fake_tkinter()."""
import os
import sys
import types

import gui_menu as g
import photosort_win as m


def _inject_fake_tkinter(monkeypatch):
    fake_mod = types.ModuleType("tkinter")
    fake_mod.TclError = type("TclError", (Exception,), {})
    monkeypatch.setitem(sys.modules, "tkinter", fake_mod)
    return fake_mod


# §6 ТЗ (маппинг движкового desc на дружелюбный подзаголовок фазы, _friendly_phase_label())
# не реализован -- подзаголовок и событие `phase` шины сняты 2026-09-01, см. комментарий у
# «Экран 4» в gui_menu.py. Тесты на _friendly_phase_label() удалены вместе с функцией.


class TestFmtElapsedClock:
    """_fmt_elapsed_clock() -- живой таймер «Прошло:» экрана «Выполнение». Адаптивный:
    MM:SS -> Ч:MM:SS -> «Nд Ч:MM:SS» (боевой прогон на большом архиве длится дольше суток,
    раньше минуты в MM:SS пухли без ограничения)."""

    def test_under_hour_is_mm_ss(self):
        assert g._fmt_elapsed_clock(0) == "00:00"
        assert g._fmt_elapsed_clock(5 * 60 + 3) == "05:03"
        assert g._fmt_elapsed_clock(59 * 60 + 59) == "59:59"

    def test_under_day_is_h_mm_ss(self):
        assert g._fmt_elapsed_clock(3600) == "1:00:00"
        assert g._fmt_elapsed_clock(3 * 3600 + 7 * 60 + 41) == "3:07:41"
        assert g._fmt_elapsed_clock(23 * 3600 + 59 * 60 + 59) == "23:59:59"

    def test_over_day_prefixes_days(self):
        assert g._fmt_elapsed_clock(86400) == "1д 0:00:00"
        assert g._fmt_elapsed_clock(2 * 86400 + 3 * 3600 + 7 * 60 + 41) == "2д 3:07:41"

    def test_negative_clamped(self):
        assert g._fmt_elapsed_clock(-5) == "00:00"


class _FakeBus:
    def __init__(self):
        self.events = []

    def done(self, report_path, outcome):
        self.events.append(("done", report_path, outcome))

    def error(self, text, crashlog_path):
        self.events.append(("error", text, crashlog_path))


class TestRunWorkerThread:
    """_run_worker_thread() -- тело воркер-потока (§2.1 ТЗ), НИКОГДА не трогает tkinter (сама
    функция не делает ни одного `import tkinter`) -- тестируется напрямую, без единой заглушки
    дисплея, как обычная синхронная функция."""

    def test_view_success_reports_done_ok(self, monkeypatch):
        monkeypatch.setattr(m, "_bare_launch_run_view", lambda sources, log: "C:\\r.html")
        bus = _FakeBus()
        g._run_worker_thread(bus, "view", "C:\\src", None, print)
        assert bus.events == [("done", "C:\\r.html", "ok")]

    def test_passport_success_reports_done_ok(self, monkeypatch):
        monkeypatch.setattr(m, "_bare_launch_run_passport", lambda target, log: "C:\\r.html")
        bus = _FakeBus()
        g._run_worker_thread(bus, "passport", None, "C:\\tgt", print)
        assert bus.events == [("done", "C:\\r.html", "ok")]

    def test_dry_run_success_reports_done_ok_with_auto_yes_input_fn(self, monkeypatch):
        captured = {}

        def _fake_dryrun(sources, target, input_fn, log):
            captured["input_fn"] = input_fn
            return "C:\\r.html"

        monkeypatch.setattr(m, "_bare_launch_run_dryrun", _fake_dryrun)
        bus = _FakeBus()
        g._run_worker_thread(bus, "dry_run", "C:\\src", "C:\\tgt", print)
        assert bus.events == [("done", "C:\\r.html", "ok")]
        assert captured["input_fn"] is g._auto_yes_input_fn

    def test_build_success_reports_done_ok(self, monkeypatch):
        monkeypatch.setattr(
            m, "_bare_launch_run_build", lambda sources, target, input_fn, log, outcome=None: "C:\\r.html")
        bus = _FakeBus()
        g._run_worker_thread(bus, "build", "C:\\src", "C:\\tgt", print)
        assert bus.events == [("done", "C:\\r.html", "ok")]

    def test_build_stopped_for_space_reports_done_warnings(self, monkeypatch):
        """Раунд 189, ответ на REVIEW-HANDOFF.md (вне формата) "outcome=warnings не
        реализован": m._bare_launch_run_build(outcome=...) заполняет
        {"stopped_for_space": True} через её необязательный out-параметр -- воркер должен
        прочитать это и слать "warnings", не "ok" (архив собрался, но не полностью)."""
        def _fake_build(sources, target, input_fn, log, outcome=None):
            if outcome is not None:
                outcome["stopped_for_space"] = True
            return "C:\\r.html"

        monkeypatch.setattr(m, "_bare_launch_run_build", _fake_build)
        bus = _FakeBus()
        g._run_worker_thread(bus, "build", "C:\\src", "C:\\tgt", print)
        assert bus.events == [("done", "C:\\r.html", "warnings")]

    def test_build_not_stopped_for_space_still_reports_done_ok(self, monkeypatch):
        def _fake_build(sources, target, input_fn, log, outcome=None):
            if outcome is not None:
                outcome["stopped_for_space"] = False
            return "C:\\r.html"

        monkeypatch.setattr(m, "_bare_launch_run_build", _fake_build)
        bus = _FakeBus()
        g._run_worker_thread(bus, "build", "C:\\src", "C:\\tgt", print)
        assert bus.events == [("done", "C:\\r.html", "ok")]

    def test_build_none_report_path_reports_done_nothing_not_a_crash(self, monkeypatch):
        """REVIEW-HANDOFF.md Раунд 182, замечание 182-2: m._bare_launch_run_build() -> None --
        ни один источник не дал ни одного успеха, НЕ краш (её докстринг в photosort_win.py).
        Раньше воркер слал bus.error(...) -> исход `failed` -> у пользователя пропадала кнопка
        «Главное меню». Теперь -- отдельный исход `nothing` через bus.done(None, "nothing")."""
        monkeypatch.setattr(
            m, "_bare_launch_run_build", lambda sources, target, input_fn, log, outcome=None: None)
        bus = _FakeBus()
        g._run_worker_thread(bus, "build", "C:\\src", "C:\\tgt", print)
        assert bus.events == [("done", None, "nothing")]

    def test_view_none_report_path_also_reports_done_nothing(self, monkeypatch):
        monkeypatch.setattr(m, "_bare_launch_run_view", lambda sources, log: None)
        bus = _FakeBus()
        g._run_worker_thread(bus, "view", "C:\\src", None, print)
        assert bus.events == [("done", None, "nothing")]

    def test_interrupted_run_report_maps_to_done_interrupted_with_its_report_path(
            self, monkeypatch):
        def _boom(sources, target, input_fn, log, outcome=None):
            raise m._InterruptedRunReport("C:\\partial.html")

        monkeypatch.setattr(m, "_bare_launch_run_build", _boom)
        bus = _FakeBus()
        g._run_worker_thread(bus, "build", "C:\\src", "C:\\tgt", print)
        assert bus.events == [("done", "C:\\partial.html", "interrupted")]

    def test_interrupted_run_report_with_no_report_path_still_reports_done(self, monkeypatch):
        """Ctrl+C-пакет ДО того, как что-либо успело сформировать отчёт -- report_path=None,
        воркер обязан всё равно доложить done (не проглотить событие молча, экран должен уметь
        показать «Работа прервана» даже без ссылки на отчёт)."""
        def _boom(sources, target, input_fn, log, outcome=None):
            raise m._InterruptedRunReport(None)

        monkeypatch.setattr(m, "_bare_launch_run_build", _boom)
        bus = _FakeBus()
        g._run_worker_thread(bus, "build", "C:\\src", "C:\\tgt", print)
        assert bus.events == [("done", None, "interrupted")]

    def test_aborted_run_report_maps_to_done_aborted_before_interrupted_branch(self, monkeypatch):
        """183-1/183-2: _AbortedRunReport (подкласс _InterruptedRunReport) должна ловиться
        РАНЬШЕ -> outcome="aborted", не "interrupted"."""
        def _boom(sources, target, input_fn, log, outcome=None):
            raise m._AbortedRunReport("C:\\partial.html")

        monkeypatch.setattr(m, "_bare_launch_run_build", _boom)
        bus = _FakeBus()
        g._run_worker_thread(bus, "build", "C:\\src", "C:\\tgt", print)
        assert bus.events == [("done", "C:\\partial.html", "aborted")]

    def test_hard_exit_reports_nothing_main_thread_already_exiting(self, monkeypatch):
        """_HardExit -- крестик, main-поток уже сам ведёт процесс к sys.exit(0) (см.
        _Wizard._on_run_hard_exit()) -- воркер не должен класть событие, экран этого уже не
        увидит."""
        def _boom(sources, target, input_fn, log, outcome=None):
            raise m._HardExit()

        monkeypatch.setattr(m, "_bare_launch_run_build", _boom)
        bus = _FakeBus()
        g._run_worker_thread(bus, "build", "C:\\src", "C:\\tgt", print)
        assert bus.events == []

    def test_unexpected_exception_reports_error_and_writes_crash_log(self, monkeypatch, tmp_path):
        monkeypatch.setattr(m, "_app_dir", lambda: str(tmp_path))

        def _boom(sources, target, input_fn, log, outcome=None):
            raise RuntimeError("kaboom")

        monkeypatch.setattr(m, "_bare_launch_run_build", _boom)
        bus = _FakeBus()
        g._run_worker_thread(bus, "build", "C:\\src", "C:\\tgt", lambda *a, **k: None)
        assert len(bus.events) == 1
        kind, text, crashlog_path = bus.events[0]
        assert kind == "error"
        assert crashlog_path == os.path.join(str(tmp_path), "crash.log")
        assert os.path.exists(crashlog_path)
        assert "kaboom" in open(crashlog_path, encoding="utf-8").read()


class TestAppendRunLogLine:
    """Кольцевой буфер (§5/§8.4 ТЗ, _RUN_MIRROR_MAX_LINES) -- заглушка на self._run_mirror
    (duck-typed Text-виджет: yview/config/insert/delete/see), инъекция фейкового tkinter только
    ради tk.TclError в except-ветке самого метода."""

    class _FakeMirror:
        def __init__(self):
            self.lines = []
            self.state = "disabled"
            self.saw_end = False

        def yview(self):
            return (0.0, 1.0)  # всегда "внизу" -- автопрокрутка должна сработать

        def config(self, **kw):
            if "state" in kw:
                self.state = kw["state"]

        def insert(self, index, text):
            assert index == "end"
            self.lines.append(text.rstrip("\n"))

        def delete(self, start, end):
            assert (start, end) == ("1.0", "2.0")
            self.lines.pop(0)

        def see(self, index):
            assert index == "end"
            self.saw_end = True

    def _make_wizard_with_fake_mirror(self, monkeypatch):
        _inject_fake_tkinter(monkeypatch)
        wiz = g._Wizard()
        wiz._run_mirror = self._FakeMirror()
        wiz._run_mirror_line_count = 0
        return wiz

    def test_single_line_appended_and_seen(self, monkeypatch):
        wiz = self._make_wizard_with_fake_mirror(monkeypatch)
        wiz._append_run_log_line("hello")
        assert wiz._run_mirror.lines == ["hello"]
        assert wiz._run_mirror.saw_end is True
        assert wiz._run_mirror.state == "disabled"  # вернулся в readonly после вставки

    def test_ring_buffer_trims_oldest_line_past_the_cap(self, monkeypatch):
        wiz = self._make_wizard_with_fake_mirror(monkeypatch)
        for i in range(g._RUN_MIRROR_MAX_LINES + 50):
            wiz._append_run_log_line(f"line {i}")
        assert len(wiz._run_mirror.lines) == g._RUN_MIRROR_MAX_LINES
        assert wiz._run_mirror.lines[0] == "line 50"  # первые 50 обрезаны
        assert wiz._run_mirror.lines[-1] == f"line {g._RUN_MIRROR_MAX_LINES + 49}"

    def test_noop_when_mirror_not_yet_created(self, monkeypatch):
        _inject_fake_tkinter(monkeypatch)
        wiz = g._Wizard()
        wiz._append_run_log_line("too early")  # must not raise -- self._run_mirror is None


class TestAppendRunEndMarker:
    """Живой отзыв пользователя 2026-09-03: в зеркало экрана «Выполнение» пишется время
    СТАРТА прогона (движок, _log_run_start_header), но не время ФИНИША -- _append_run_end_marker()
    добавляет его на событии done/error."""

    def _wiz(self, monkeypatch):
        _inject_fake_tkinter(monkeypatch)
        wiz = g._Wizard()
        wiz._run_mirror = TestAppendRunLogLine._FakeMirror()
        wiz._run_mirror_line_count = 0
        return wiz

    def test_marker_has_timestamp_and_elapsed_when_started_at_known(self, monkeypatch):
        import time
        wiz = self._wiz(monkeypatch)
        wiz._run_started_at = time.time() - (2 * 60 + 5)  # 2:05 назад
        wiz._append_run_end_marker()
        line = wiz._run_mirror.lines[-1]
        assert line.startswith("[") and "] Работа завершена — прошло " in line
        assert line.rstrip().endswith("02:05") or line.rstrip().endswith("02:06")  # ±1с

    def test_marker_without_elapsed_when_started_at_missing(self, monkeypatch):
        wiz = self._wiz(monkeypatch)
        wiz._run_started_at = None
        wiz._append_run_end_marker()
        line = wiz._run_mirror.lines[-1]
        assert "] Работа завершена" in line and "прошло" not in line


class TestHandleRunEvent:
    """Диспетчер событий шины -> обновление виджетов (§2.2 ТЗ). Подменяет сами
    render/finish-методы -- проверяется МАРШРУТИЗАЦИЯ (какой обработчик на какое событие), не
    реальная отрисовка (та требует настоящего tk.Text/Label, см. TestAppendRunLogLine выше за
    единственным исключением, которое реально нужно проверить построчно)."""

    def _make_wizard(self):
        wiz = g._Wizard()
        calls = []
        wiz._append_run_log_line = lambda text: calls.append(("log", text))
        wiz._append_run_end_marker = lambda: calls.append(("end_marker",))
        wiz._finish_worker = lambda: calls.append(("finish",))
        wiz.render_run_outcome = lambda outcome, **kw: calls.append(("outcome", outcome, kw))
        return wiz, calls

    def test_status_event_updates_status_var(self):
        wiz, calls = self._make_wizard()

        class _FakeVar:
            def __init__(self):
                self.value = None

            def set(self, v):
                self.value = v

        wiz._run_status_var = _FakeVar()
        wiz._handle_run_event(("status", "some status line"))
        assert wiz._run_status_var.value == "some status line"

    def test_log_event_routes_to_append_run_log_line(self):
        wiz, calls = self._make_wizard()
        wiz._handle_run_event(("log", "hello"))
        assert calls == [("log", "hello")]

    def test_done_event_finishes_worker_then_renders_outcome(self):
        wiz, calls = self._make_wizard()
        wiz._handle_run_event(("done", "C:\\r.html", "ok"))
        # метка времени финиша в зеркало -> снятие bus/stdio -> отрисовка исхода
        assert calls == [
            ("end_marker",), ("finish",), ("outcome", "ok", {"report_path": "C:\\r.html"}),
        ]

    def test_error_event_finishes_worker_then_renders_failed_outcome(self):
        wiz, calls = self._make_wizard()
        wiz._handle_run_event(("error", "boom", "C:\\crash.log"))
        assert calls == [
            ("end_marker",),
            ("finish",),
            ("outcome", "failed", {"error_text": "boom", "crashlog_path": "C:\\crash.log"}),
        ]

    def test_done_nothing_event_routes_to_outcome_nothing(self):
        """182-2: done(None, "nothing") идёт тем же путём, что и обычный done -- finish_worker()
        затем render_run_outcome("nothing"), НЕ через error-ветку."""
        wiz, calls = self._make_wizard()
        wiz._handle_run_event(("done", None, "nothing"))
        assert calls == [
            ("end_marker",), ("finish",), ("outcome", "nothing", {"report_path": None}),
        ]


class TestDrainBusCostAxis:
    """Ось стоимости (PROMPT_review.md/§5 ТЗ): один вызов _drain_bus() обязан вычерпать ВСЮ
    накопленную очередь за один проход, не одно событие на один `after`-тик -- иначе GUI
    отставала бы от реального темпа записи объект-строк на большом архиве."""

    def test_drain_bus_consumes_entire_backlog_in_one_call(self, monkeypatch):
        _inject_fake_tkinter(monkeypatch)
        wiz = g._Wizard()
        handled = []
        wiz._handle_run_event = lambda item: handled.append(item)
        wiz._run_bus = m.RunEventBus()
        wiz._run_state = "outcome"  # не планировать следующий after() -- проверяем только drain
        for i in range(10_000):
            wiz._run_bus.log(f"line {i}")
        wiz._drain_bus()
        assert len(handled) == 10_000
        assert handled[0] == ("log", "line 0")
        assert handled[-1] == ("log", "line 9999")


class _FakeWidget:
    def __init__(self, *a, **kw):
        self.kw = kw

    def pack(self, *a, **kw):
        return self

    def pack_propagate(self, *a, **kw):
        if a:
            self.propagate = a[0]

    def config(self, *a, **kw):
        self.kw.update(kw)

    configure = config

    def bind(self, *a, **kw):
        pass

    def winfo_children(self):
        return []

    def destroy(self):
        pass


def _inject_recording_tkinter(monkeypatch):
    """Фейковый tkinter, записывающий каждый созданный виджет (name, kwargs) -- нужен, чтобы
    проверить НАБОР кнопок/текст заголовка в состояниях исхода, не поднимая настоящий tk.Tk()
    (его нет на этой Linux-машине, см. модульный докстринг)."""
    fake = types.ModuleType("tkinter")
    fake.TclError = type("TclError", (Exception,), {})
    created = []

    def _mk(name):
        def _ctor(*a, **kw):
            created.append((name, kw))
            return _FakeWidget(*a, **kw)
        return _ctor

    for _n in ("Label", "Button", "Frame"):
        setattr(fake, _n, _mk(_n))
    monkeypatch.setitem(sys.modules, "tkinter", fake)
    return created


def _button_texts(created):
    return [kw.get("text") for name, kw in created if name == "Button"]


def _label_texts(created):
    return [kw.get("text") for name, kw in created if name == "Label"]


class TestRenderRunOutcome:
    """182-2: набор кнопок и текст заголовка в состояниях исхода экрана «Выполнение».
    Проверяется, что `nothing` -- НЕ `failed`: «Главное меню» на месте, crash.log не
    упоминается; и что «Открыть папку архива» для `nothing` нет (ничего не создано)."""

    def _wiz(self, monkeypatch):
        created = _inject_recording_tkinter(monkeypatch)
        wiz = g._Wizard()
        wiz._run_header_frame = _FakeWidget()
        wiz._run_button_frame = _FakeWidget()
        wiz.state = {"mode": "build"}
        return wiz, created

    def test_header_fixed_height_for_ok_interrupted_nothing_grows_for_errors(self, monkeypatch):
        # 2026-09-01, живой отзыв: панель-зеркало не должна «прыгать» при «идёт» -> «Работа
        # окончена» -- ok/interrupted/nothing фикс. высоты _RUN_HEADER_HEIGHT + propagate(False).
        # 187-1: aborted/failed -- propagate(True), шапка растёт под пояснение + ссылку на отчёт/
        # crash.log (фикс. бюджет рисковал обрезать нижнюю кромку указателя).
        for oc in ("ok", "interrupted", "nothing"):
            wiz, _ = self._wiz(monkeypatch)
            wiz._render_run_header_outcome(oc, "C:\\r.html" if oc != "nothing" else None, None, None)
            assert wiz._run_header_frame.kw["height"] == g._px(g._RUN_HEADER_HEIGHT), oc
            assert wiz._run_header_frame.propagate is False, oc
        for oc in ("aborted", "failed"):
            wiz, _ = self._wiz(monkeypatch)
            wiz._render_run_header_outcome(oc, "C:\\r.html", "err", "C:\\c.log")
            assert wiz._run_header_frame.propagate is True, oc

    def test_nothing_keeps_main_menu_drops_open_folder(self, monkeypatch):
        wiz, created = self._wiz(monkeypatch)
        wiz._render_run_buttons_outcome("nothing")
        texts = _button_texts(created)
        assert "Главное меню" in texts
        assert "Открыть папку архива" not in texts
        assert "Выход" in texts
        assert "Сохранить лог…" not in texts  # убрана 2026-09-01 (буфер-хвост, не весь лог)

    def test_failed_has_neither_main_menu_nor_open_folder(self, monkeypatch):
        wiz, created = self._wiz(monkeypatch)
        wiz._render_run_buttons_outcome("failed")
        texts = _button_texts(created)
        assert "Главное меню" not in texts
        assert "Открыть папку архива" not in texts
        assert "Выход" in texts

    def test_ok_has_both_main_menu_and_open_folder(self, monkeypatch):
        wiz, created = self._wiz(monkeypatch)
        wiz._render_run_buttons_outcome("ok")
        texts = _button_texts(created)
        assert "Главное меню" in texts
        assert "Открыть папку архива" in texts  # _wiz() ставит mode="build"

    def test_open_folder_only_in_build_mode(self, monkeypatch):
        # 2026-09-01, живой отзыв: «Просмотр»/«Паспорт»/«Пробный прогон» ничего не пишут в
        # TARGET -- кнопки «Открыть папку архива» там быть не должно, даже на исходе ok.
        for mode in ("view", "passport", "dry_run"):
            wiz, created = self._wiz(monkeypatch)
            wiz.state = {"mode": mode}
            wiz._render_run_buttons_outcome("ok")
            texts = _button_texts(created)
            assert "Открыть папку архива" not in texts, mode
            assert "Главное меню" in texts  # само меню остаётся

    def test_nothing_header_shows_friendly_message_not_crashlog(self, monkeypatch):
        wiz, created = self._wiz(monkeypatch)
        wiz._render_run_header_outcome("nothing", None, None, None)
        labels = _label_texts(created)
        assert g._RUN_NOTHING_MESSAGE in labels
        assert g._RUN_OUTCOME_TITLES["nothing"][0] in labels
        assert not any("crash.log" in (t or "") for t in labels)

    def test_nothing_title_is_registered(self):
        assert "nothing" in g._RUN_OUTCOME_TITLES
        title, _color = g._RUN_OUTCOME_TITLES["nothing"]
        assert title and title != g._RUN_OUTCOME_TITLES["failed"][0]

    def test_aborted_keeps_main_menu_drops_open_folder(self, monkeypatch):
        wiz, created = self._wiz(monkeypatch)
        wiz._render_run_buttons_outcome("aborted")
        texts = _button_texts(created)
        assert "Главное меню" in texts  # 183-2: не «Сбой», меню доступно
        assert "Открыть папку архива" not in texts
        assert "Выход" in texts
        assert "Сохранить лог…" not in texts

    def test_aborted_header_shows_crashlog_hint_and_partial_report_link(self, monkeypatch):
        wiz, created = self._wiz(monkeypatch)
        wiz._render_run_header_outcome("aborted", "C:\\partial.html", None, None)
        labels = _label_texts(created)
        assert g._RUN_ABORTED_MESSAGE in labels
        assert any("crash.log" in (t or "") for t in labels)          # 183-2: указатель есть
        assert any("Частичный отчёт" in (t or "") for t in labels)

    def test_aborted_title_registered_and_distinct_from_interrupted(self):
        assert "aborted" in g._RUN_OUTCOME_TITLES
        title, _c = g._RUN_OUTCOME_TITLES["aborted"]
        assert title and title != g._RUN_OUTCOME_TITLES["interrupted"][0]
