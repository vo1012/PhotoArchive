"""exiftool_batch(): когда вызов exiftool падает на целом чанке (таймаут / битый argfile /
крах json), пользователь видит строку в зеркале экрана «Выполнение». 2026-09-03, живой отзыв
пользователя: раньше строка начиналась с «ВНИМАНИЕ:» и несла repr(исключения) (для
TimeoutExpired -- всю команду с путями и тег-флагами, выглядит как трейсбек). Теперь --
мягкое «Замечание», обычным языком, без технического хвоста; см. photosort_win.py:~3042."""
import subprocess

import photosort_win as m


def _run_failing_batch(monkeypatch, exc):
    def _boom(*a, **kw):
        raise exc

    monkeypatch.setattr(m.subprocess, "run", _boom)
    lines = []
    out = m.exiftool_batch(["/a/one.jpg", "/a/two.jpg", "/a/three.jpg"], log=lines.append)
    return out, "\n".join(lines)


def test_chunk_failure_yields_no_exif_but_does_not_raise(monkeypatch):
    out, _ = _run_failing_batch(monkeypatch, subprocess.TimeoutExpired(cmd=["exiftool"], timeout=120))
    assert out == {}


def test_message_is_plain_language_without_technical_tail(monkeypatch):
    _, log = _run_failing_batch(
        monkeypatch,
        subprocess.TimeoutExpired(
            cmd=["C:\\bin\\exiftool.exe", "-j", "-DateTimeOriginal", "-@", "C:\\Temp\\x.args"],
            timeout=120,
        ),
    )
    assert "Замечание:" in log
    # старый жаргон / технический хвост ушли полностью
    assert "ВНИМАНИЕ" not in log
    assert "exiftool" not in log.lower()
    assert "чанк" not in log
    assert "TimeoutExpired" not in log
    assert "cmd=" not in log
    assert ".args" not in log
    # последствие названо явно
    assert "точность даты" in log
    assert "3 шт." in log  # размер затронутой группы
    assert "в отчёте" in log


def test_message_shape_is_identical_for_any_exception_class(monkeypatch):
    _, log_json = _run_failing_batch(monkeypatch, ValueError("Expecting value: line 1 column 1"))
    assert "Замечание: у части файлов (3 шт.)" in log_json
    assert "Expecting value" not in log_json
    assert "ValueError" not in log_json
