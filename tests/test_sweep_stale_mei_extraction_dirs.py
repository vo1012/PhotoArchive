"""_mark_own_mei_extraction_dir()/_sweep_stale_mei_extraction_dirs() -- 2026-08-24, живая
просьба пользователя ("что-то осталось -- не должно копиться, каждый новый запуск должен
подчищать всё, что было до него"). PyInstaller onefile-бутлоадер обычно сам убирает
распакованную _MEIxxxxxx-папку после graceful-выхода -- но не при os._exit()/крахе/
Task Manager/пропаже питания, где эта пара функций и нужна: следующий запуск подметает СВОИ
осиротевшие папки (по PID-маркеру внутри), оставляя чужие (без маркера) нетронутыми --
_MEI<цифры> генерирует ЛЮБОЕ PyInstaller onefile-приложение, не только эта программа."""
import os

import photosort_win as m


def test_mark_is_noop_without_meipass(monkeypatch):
    monkeypatch.delattr(m.sys, "_MEIPASS", raising=False)
    m._mark_own_mei_extraction_dir()  # must not raise


def test_sweep_is_noop_without_meipass(monkeypatch):
    monkeypatch.delattr(m.sys, "_MEIPASS", raising=False)
    m._sweep_stale_mei_extraction_dirs()  # must not raise


def test_mark_writes_own_pid_marker_inside_meipass(tmp_path, monkeypatch):
    mei_dir = tmp_path / "_MEI123456"
    mei_dir.mkdir()
    monkeypatch.setattr(m.sys, "_MEIPASS", str(mei_dir), raising=False)
    m._mark_own_mei_extraction_dir()
    marker = mei_dir / m._MEI_OWNER_MARKER_FILENAME
    assert marker.read_text(encoding="ascii") == str(os.getpid())


def test_sweep_removes_own_dead_sibling(tmp_path, monkeypatch):
    parent = tmp_path
    own_dir = parent / "_MEI999999"
    own_dir.mkdir()
    monkeypatch.setattr(m.sys, "_MEIPASS", str(own_dir), raising=False)

    stale_dir = parent / "_MEI111111"
    stale_dir.mkdir()
    (stale_dir / m._MEI_OWNER_MARKER_FILENAME).write_text("424242", encoding="ascii")

    monkeypatch.setattr(m, "_pid_is_alive", lambda pid: False)
    m._sweep_stale_mei_extraction_dirs(log=lambda *a, **kw: None)
    assert not stale_dir.exists()
    assert own_dir.exists()  # своя текущая папка не трогается


def test_sweep_leaves_sibling_with_live_marked_pid(tmp_path, monkeypatch):
    parent = tmp_path
    own_dir = parent / "_MEI999999"
    own_dir.mkdir()
    monkeypatch.setattr(m.sys, "_MEIPASS", str(own_dir), raising=False)

    live_dir = parent / "_MEI222222"
    live_dir.mkdir()
    (live_dir / m._MEI_OWNER_MARKER_FILENAME).write_text("424242", encoding="ascii")

    monkeypatch.setattr(m, "_pid_is_alive", lambda pid: True)
    m._sweep_stale_mei_extraction_dirs(log=lambda *a, **kw: None)
    assert live_dir.exists()  # процесс-владелец ещё жив -- не трогаем


def test_sweep_leaves_unmarked_sibling_untouched(tmp_path, monkeypatch):
    """Папка без нашего маркера -- либо чужое PyInstaller-приложение, либо наша же, но от
    прогона ДО появления этого фикса. В обоих случаях -- не трогаем, тот же принцип "в
    сомнении -- не трогаем", что и у _sweep_tmp_extract_dir()."""
    parent = tmp_path
    own_dir = parent / "_MEI999999"
    own_dir.mkdir()
    monkeypatch.setattr(m.sys, "_MEIPASS", str(own_dir), raising=False)

    foreign_dir = parent / "_MEI333333"
    foreign_dir.mkdir()
    (foreign_dir / "some_other_apps_file.txt").write_text("not ours", encoding="ascii")

    m._sweep_stale_mei_extraction_dirs(log=lambda *a, **kw: None)
    assert foreign_dir.exists()


def test_sweep_ignores_entries_not_matching_mei_naming(tmp_path, monkeypatch):
    parent = tmp_path
    own_dir = parent / "_MEI999999"
    own_dir.mkdir()
    monkeypatch.setattr(m.sys, "_MEIPASS", str(own_dir), raising=False)

    unrelated = parent / "some_other_temp_folder"
    unrelated.mkdir()

    m._sweep_stale_mei_extraction_dirs(log=lambda *a, **kw: None)
    assert unrelated.exists()


def test_sweep_swallows_missing_parent_directory(tmp_path, monkeypatch):
    mei_dir = tmp_path / "gone" / "_MEI123456"
    monkeypatch.setattr(m.sys, "_MEIPASS", str(mei_dir), raising=False)
    m._sweep_stale_mei_extraction_dirs()  # parent doesn't exist -- must not raise
