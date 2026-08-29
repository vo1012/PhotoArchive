"""2026-08-23, по прямой просьбе пользователя: пауза обработки по пробелу
(_check_pause_keypress(), photosort_win.py) -- отдельная задача, отложенная и реализованная
следом за переделкой консоли GUI-мастера в приборную панель (см. CLAUDE.md, "Рабочая консоль
GUI-мастера..."). msvcrt -- Windows-only builtin, реального импорта на этой (потенциально
не-Windows dev) машине лучше не требовать -- подменяется через sys.modules, тот же приём, что
уже применяется к ctypes.windll в других тестах этого репозитория.

2026-08-28 (A + B фикс, живая находка пользователя): опрос был только между файлами, а
"продолжить" читалось из общего буфера консоли -- пользователь долбил пробел во время долгой
операции, нажатия копились, каждая пара мгновенно проигрывалась как вход-выход из паузы,
конечное состояние зависело от чётности. Теперь буфер осушается на входе и на выходе; Ctrl-C
(b"\\x03") пробрасывается как KeyboardInterrupt.
"""
import os
import sys

import pytest

import photosort_win as m


class _FakeMsvcrt:
    """kbhit_results/getch_results -- очереди возвращаемых значений, по одному элементу на
    вызов. Когда очередь исчерпана: kbhit() -> False (буфер пуст), getch() -> баг теста
    (StopIteration-подобное AssertionError -- вызвано больше раз, чем ожидалось)."""

    def __init__(self, kbhit_results=(), getch_results=()):
        self.kbhit_calls = 0
        self.getch_calls = 0
        self._kbhit_results = list(kbhit_results)
        self._getch_results = list(getch_results)

    def kbhit(self):
        self.kbhit_calls += 1
        return self._kbhit_results.pop(0) if self._kbhit_results else False

    def getch(self):
        self.getch_calls += 1
        assert self._getch_results, "getch() вызван больше раз, чем предполагал тест"
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


def test_ignores_non_space_key_and_drains_its_tail(monkeypatch):
    monkeypatch.setattr(os, "name", "nt")
    # спец-клавиша: b"\xe0" + b"H" (стрелка вверх) -- не пробел, тэйл осушается, паузы нет
    fake = _FakeMsvcrt(kbhit_results=[True, True, False], getch_results=[b"\xe0", b"H"])
    _install_fake_msvcrt(monkeypatch, fake)
    logged = []
    m._check_pause_keypress(log=logged.append)
    assert logged == []


def test_space_key_pauses_and_waits_for_a_fresh_key_to_resume(monkeypatch):
    monkeypatch.setattr(os, "name", "nt")
    # kbhit: True (есть нажатие) -> False (буфер пуст, дренаж на входе) -> False (дренаж на выходе)
    fake = _FakeMsvcrt(kbhit_results=[True, False, False], getch_results=[b" ", b"q"])
    _install_fake_msvcrt(monkeypatch, fake)
    logged = []
    m._check_pause_keypress(log=logged.append)
    assert fake.getch_calls == 2
    assert len(logged) == 2
    assert "ПАУЗА" in logged[0]
    assert "Продолжаю" in logged[1]


def test_mashed_spaces_produce_exactly_one_pause(monkeypatch):
    """A: пользователь нажал пробел 4 раза во время долгой операции. Все 4 в буфере. Должна
    быть РОВНО одна пауза, снимаемая одним осознанным нажатием -- не 2 мгновенных цикла."""
    monkeypatch.setattr(os, "name", "nt")
    fake = _FakeMsvcrt(
        # 1: есть нажатие; дренаж на входе: True,True,True (ещё 3 пробела), False;
        # дренаж на выходе: False
        kbhit_results=[True, True, True, True, False, False],
        getch_results=[b" ", b" ", b" ", b" ", b"\r"],  # 1 прочитан как "клавиша", 3 слиты, \r = продолжить
    )
    _install_fake_msvcrt(monkeypatch, fake)
    logged = []
    m._check_pause_keypress(log=logged.append)
    assert [line for line in logged if "ПАУЗА" in line], "должна быть пауза"
    assert sum(1 for line in logged if "ПАУЗА" in line) == 1
    assert sum(1 for line in logged if "Продолжаю" in line) == 1


def test_ctrl_c_at_poll_raises_keyboardinterrupt(monkeypatch):
    monkeypatch.setattr(os, "name", "nt")
    fake = _FakeMsvcrt(kbhit_results=[True], getch_results=[b"\x03"])
    _install_fake_msvcrt(monkeypatch, fake)
    with pytest.raises(KeyboardInterrupt):
        m._check_pause_keypress(log=lambda *_: None)


def test_ctrl_c_while_draining_raises(monkeypatch):
    monkeypatch.setattr(os, "name", "nt")
    fake = _FakeMsvcrt(kbhit_results=[True, True], getch_results=[b" ", b"\x03"])
    _install_fake_msvcrt(monkeypatch, fake)
    with pytest.raises(KeyboardInterrupt):
        m._check_pause_keypress(log=lambda *_: None)


def test_ctrl_c_as_resume_key_raises(monkeypatch):
    monkeypatch.setattr(os, "name", "nt")
    fake = _FakeMsvcrt(kbhit_results=[True, False], getch_results=[b" ", b"\x03"])
    _install_fake_msvcrt(monkeypatch, fake)
    with pytest.raises(KeyboardInterrupt):
        m._check_pause_keypress(log=lambda *_: None)


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
