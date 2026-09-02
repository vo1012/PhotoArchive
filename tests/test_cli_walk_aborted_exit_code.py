"""REVIEW-HANDOFF.md Раунд 184, замечание 184-1: до Раунда 183 краш самого обходчика
источника на CLI-пути (`photosort_win.py archive/analyze --source ... --target ...`) давал
`st.interrupted=True` -> `KeyboardInterrupt` -> `main()` -> exit 130 + «Прервано». Ответ на
Раунд 183 (`b7c76b7`) переименовал этот флаг в `walk_aborted` и протянул его через
`RunResult`/`AnalyzeStats` в bare-launch/GUI (`_bare_launch_run_*` -> `_AbortedRunReport`),
но `_main()` (CLI-путь) новый флаг НЕ смотрел вовсе -> тот же краш стал давать тихий
`exit 0` без сообщения. Скрипт/`.bat`, проверяющий `%ERRORLEVEL%`, принимал упавший прогон
за успешный.

Все тесты red-before-green: на коде до фикса 184-1 `_main()` возвращает 0.
"""
import sys

import pytest
from PIL import Image

import photosort_win as m


def _make_jpeg(path, size=(320, 240), color=(10, 20, 30)):
    Image.new("RGB", size, color).save(path, "JPEG")


def _exploding_walk_after_first():
    real_walk = m._walk_with_exif_prefetch

    def _walk(*a, **kw):
        n = 0
        for pair in real_walk(*a, **kw):
            yield pair
            n += 1
            if n >= 1:
                raise RuntimeError("walker exploded mid-iteration")

    return _walk


def _setup(tmp_path, monkeypatch):
    source = tmp_path / "SOURCE"
    source.mkdir()
    _make_jpeg(source / "a.jpg")
    _make_jpeg(source / "b.jpg")
    target = tmp_path / "TARGET"
    target.mkdir()
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    monkeypatch.setattr(m, "WORKDIR", str(workdir))
    monkeypatch.setattr(m, "_walk_with_exif_prefetch", _exploding_walk_after_first())
    return source, target


def test_cli_archive_walk_crash_raises_aborted_run_report(tmp_path, monkeypatch):
    source, target = _setup(tmp_path, monkeypatch)
    monkeypatch.setattr(sys, "argv", ["photosort_win.py", "archive", "--source", str(source),
                                       "--target", str(target)])
    with pytest.raises(m._AbortedRunReport):
        m._main()


def test_cli_analyze_walk_crash_raises_aborted_run_report(tmp_path, monkeypatch):
    source, _target = _setup(tmp_path, monkeypatch)
    monkeypatch.setattr(sys, "argv", ["photosort_win.py", "analyze", "--source", str(source)])
    with pytest.raises(m._AbortedRunReport):
        m._main()


def test_cli_archive_walk_crash_exit_code_is_130_not_0(tmp_path, monkeypatch):
    """main() ловит _AbortedRunReport (подкласс KeyboardInterrupt) -> для настоящего CLI
    (bare_launch=False) sys.exit(130), НЕ 0."""
    source, target = _setup(tmp_path, monkeypatch)
    monkeypatch.setattr(sys, "argv", ["photosort_win.py", "archive", "--source", str(source),
                                       "--target", str(target)])
    with pytest.raises(SystemExit) as ei:
        m.main()
    assert ei.value.code == 130


def test_cli_analyze_walk_crash_exit_code_is_130_not_0(tmp_path, monkeypatch):
    source, _target = _setup(tmp_path, monkeypatch)
    monkeypatch.setattr(sys, "argv", ["photosort_win.py", "analyze", "--source", str(source)])
    with pytest.raises(SystemExit) as ei:
        m.main()
    assert ei.value.code == 130


def test_cli_archive_walk_crash_still_writes_partial_report(tmp_path, monkeypatch):
    source, target = _setup(tmp_path, monkeypatch)
    monkeypatch.setattr(sys, "argv", ["photosort_win.py", "archive", "--source", str(source),
                                       "--target", str(target)])
    with pytest.raises(m._AbortedRunReport):
        m._main()
    # частичный отчёт всё равно записан на диск (в TARGET\__служебные_файлы\)
    assert (target / "__служебные_файлы" / "report.html").exists()
