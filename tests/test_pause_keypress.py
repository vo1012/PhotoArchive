"""2026-08-23, по прямой просьбе пользователя: пауза обработки по пробелу
(_check_pause_keypress(), photosort_win.py) -- отдельная задача, отложенная и реализованная
следом за переделкой консоли GUI-мастера в приборную панель (см. CLAUDE.md, "Рабочая консоль
GUI-мастера..."). msvcrt -- Windows-only builtin, реального импорта на этой (потенциально
не-Windows dev) машине лучше не требовать -- подменяется через sys.modules, тот же приём, что
уже применяется к ctypes.windll в других тестах этого репозитория."""
import os
import sys

import photosort_win as m


class _FakeMsvcrt:
    """kbhit_results/getch_results -- очереди возвращаемых значений, по одному элементу на
    вызов; StopIteration означал бы баг теста (вызвано больше раз, чем ожидалось)."""

    def __init__(self, kbhit_results=(), getch_results=()):
        self.kbhit_calls = 0
        self.getch_calls = 0
        self._kbhit_results = list(kbhit_results)
        self._getch_results = list(getch_results)

    def kbhit(self):
        self.kbhit_calls += 1
        return self._kbhit_results.pop(0)

    def getch(self):
        self.getch_calls += 1
        return self._getch_results.pop(0)


def _install_fake_msvcrt(monkeypatch, fake):
    monkeypatch.setitem(sys.modules, "msvcrt", fake)


def test_is_noop_off_windows(monkeypatch):
    monkeypatch.setattr(os, "name", "posix")
    logged = []
    m._check_pause_keypress(log=logged.append)
    assert logged == []


def test_noop_when_no_key_pressed(monkeypatch):
    monkeypatch.setattr(os, "name", "nt")
    fake = _FakeMsvcrt(kbhit_results=[False])
    _install_fake_msvcrt(monkeypatch, fake)
    logged = []
    m._check_pause_keypress(log=logged.append)
    assert fake.kbhit_calls == 1
    assert fake.getch_calls == 0
    assert logged == []


def test_ignores_non_space_key(monkeypatch):
    monkeypatch.setattr(os, "name", "nt")
    fake = _FakeMsvcrt(kbhit_results=[True], getch_results=[b"x"])
    _install_fake_msvcrt(monkeypatch, fake)
    logged = []
    m._check_pause_keypress(log=logged.append)
    assert fake.getch_calls == 1  # прочитан РОВНО один раз -- не входим во вторую (паузную) getch()
    assert logged == []


def test_space_key_pauses_and_waits_for_any_key_to_resume(monkeypatch):
    monkeypatch.setattr(os, "name", "nt")
    # Первый getch() -- сама нажатая клавиша (пробел); второй -- ожидание "любой клавиши"
    # внутри самой паузы (getch() внутри блока паузы -- реально блокирующий, здесь просто
    # следующее значение из очереди, поскольку тест синхронный).
    fake = _FakeMsvcrt(kbhit_results=[True], getch_results=[b" ", b"q"])
    _install_fake_msvcrt(monkeypatch, fake)
    logged = []
    m._check_pause_keypress(log=logged.append)
    assert fake.getch_calls == 2
    assert len(logged) == 2
    assert "Пауза" in logged[0]
    assert "Продолжаю" in logged[1]


def test_swallows_kbhit_failure(monkeypatch):
    monkeypatch.setattr(os, "name", "nt")

    class _RaisingMsvcrt:
        def kbhit(self):
            raise OSError("simulated console read failure")

    _install_fake_msvcrt(monkeypatch, _RaisingMsvcrt())
    logged = []
    m._check_pause_keypress(log=logged.append)  # must not raise
    assert logged == []


def test_swallows_getch_failure_after_space_detected(monkeypatch):
    monkeypatch.setattr(os, "name", "nt")

    class _FailingGetchMsvcrt:
        def kbhit(self):
            return True

        def getch(self):
            raise OSError("simulated console read failure")

    _install_fake_msvcrt(monkeypatch, _FailingGetchMsvcrt())
    logged = []
    m._check_pause_keypress(log=logged.append)  # must not raise
    assert logged == []
