"""TargetLock: снятие LOCK-файла по РЕАЛЬНОЙ проверке "жив ли процесс-владелец", а не только
по 12-часовому mtime-порогу. Живая находка пользователя 2026-08-29: реальный прогон прерван
Ctrl-C, Windows прибила процесс раньше, чем отработал TargetLock.__exit__ -> LOCK остался, и
перезапуск упирался в него на ~12ч ("удалите файл вручную или подождите"). PID владельца и так
пишется в сам файл (os.write в __enter__) -- теперь __enter__ его читает и сверяет через
_pid_is_alive() (та же консервативная проверка, что у _sweep_stale_dry_run_pid_dirs()).

Плюс публичные inspect_target_lock()/clear_target_lock() -- для преполётной проверки GUI
(gui_menu._ensure_target_unlocked): "PID жив" / "PID неизвестен" доводятся до пользователя
окном, а не тихим возвратом в меню.
"""
import os
import subprocess
import sys
import time

import pytest

import photosort_win as m


def _lock_file(target):
    return os.path.join(str(target), "__служебные_файлы", "LOCK")


def _make_lock(target, contents: str, age_seconds: float = 0.0):
    path = _lock_file(target)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="ascii") as f:
        f.write(contents)
    if age_seconds:
        old = time.time() - age_seconds
        os.utime(path, (old, old))
    return path


def test_raises_when_holder_pid_is_alive(tmp_path, monkeypatch):
    _make_lock(tmp_path, "999999", age_seconds=5)
    monkeypatch.setattr(m, "_pid_is_alive", lambda pid: True)
    with pytest.raises(m.TargetLocked):
        with m.TargetLock(str(tmp_path), log=lambda *a: None):
            pass


def test_clears_when_holder_pid_is_dead(tmp_path, monkeypatch):
    """Ядро фикса: свежий LOCK (возраст << 12ч), но процесс-владелец мёртв -> снимаем сразу."""
    _make_lock(tmp_path, "424242", age_seconds=30)
    monkeypatch.setattr(m, "_pid_is_alive", lambda pid: False)
    logged = []
    with m.TargetLock(str(tmp_path), log=lambda msg: logged.append(msg)):
        # LOCK пересоздан под наш собственный PID
        with open(_lock_file(tmp_path), encoding="ascii") as f:
            assert f.read().strip() == str(os.getpid())
    assert any("PID 424242" in msg and "больше нет" in msg for msg in logged)
    assert not os.path.exists(_lock_file(tmp_path))


def test_clears_when_holder_process_really_exited(tmp_path):
    """То же, но через настоящий _pid_is_alive() и настоящий завершившийся процесс -- без
    monkeypatch (verification-by-execution для самого механизма, не только для ветки)."""
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    _make_lock(tmp_path, str(proc.pid), age_seconds=10)
    with m.TargetLock(str(tmp_path), log=lambda *a: None):
        pass
    assert not os.path.exists(_lock_file(tmp_path))


def test_fresh_lock_with_unreadable_pid_still_blocks(tmp_path):
    """PID не прочитать (старый формат/повреждение) -> падаем на прежнее mtime-правило:
    свежий файл по-прежнему блокирует."""
    _make_lock(tmp_path, "not-a-pid", age_seconds=60)
    with pytest.raises(m.TargetLocked):
        with m.TargetLock(str(tmp_path), log=lambda *a: None):
            pass


def test_stale_by_mtime_with_unreadable_pid_is_cleared(tmp_path):
    _make_lock(tmp_path, "", age_seconds=13 * 3600)
    logged = []
    with m.TargetLock(str(tmp_path), log=lambda msg: logged.append(msg)):
        pass
    assert any("устаревший LOCK" in msg for msg in logged)


def test_lock_removed_on_normal_exit(tmp_path):
    with m.TargetLock(str(tmp_path), log=lambda *a: None):
        assert os.path.exists(_lock_file(tmp_path))
    assert not os.path.exists(_lock_file(tmp_path))


def test_inspect_target_lock_none_when_absent(tmp_path):
    assert m.inspect_target_lock(str(tmp_path)) is None


def test_inspect_target_lock_reports_state(tmp_path, monkeypatch):
    _make_lock(tmp_path, "777", age_seconds=120)
    monkeypatch.setattr(m, "_pid_is_alive", lambda pid: True)
    info = m.inspect_target_lock(str(tmp_path))
    assert info["pid"] == 777
    assert info["pid_alive"] is True
    assert 110 <= info["age_seconds"] <= 200
    # чистая проверка -- файл не тронут
    assert os.path.exists(_lock_file(tmp_path))


def test_inspect_target_lock_pid_alive_none_when_unreadable(tmp_path):
    _make_lock(tmp_path, "garbage", age_seconds=5)
    info = m.inspect_target_lock(str(tmp_path))
    assert info["pid"] is None
    assert info["pid_alive"] is None


def test_clear_target_lock(tmp_path):
    _make_lock(tmp_path, "1", age_seconds=5)
    assert m.clear_target_lock(str(tmp_path), log=lambda *a: None) is True
    assert not os.path.exists(_lock_file(tmp_path))
    # повторный вызов на отсутствующем файле -- тоже True
    assert m.clear_target_lock(str(tmp_path), log=lambda *a: None) is True
