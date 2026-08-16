"""SourceWalker._walk_dir(): a directory whose os.listdir() fails (permission denied/long
path/corrupted filesystem, REVIEW-HANDOFF.md Раунд 32 задача 4) used to be logged as plain
text and nothing else -- the whole subtree vanished from every count, with no signal that
anything was skipped. Now such directories are collected into walker.listdir_failed and
surfaced as stats["listdir_failed_count"] in run_for_source() (photosort_win.py)."""
import os

import photosort_win as m


def _make_cfg(tmp_path, **overrides):
    source = overrides.pop("source", None) or str(tmp_path / "source")
    target = overrides.pop("target", None) or str(tmp_path / "target")
    return m.Config(source=source, target=target, **overrides)


def test_listdir_failure_is_counted_not_silently_dropped(tmp_path, monkeypatch):
    source = tmp_path / "source"
    (source / "good_dir").mkdir(parents=True)
    (source / "good_dir" / "a.jpg").write_bytes(b"x" * 10)
    blocked = source / "blocked_dir"
    blocked.mkdir()
    (blocked / "b.jpg").write_bytes(b"x" * 10)
    (tmp_path / "target").mkdir()

    real_listdir = os.listdir

    def flaky_listdir(path, *a, **kw):
        if "blocked_dir" in str(path):
            raise OSError("Отказано в доступе")
        return real_listdir(path, *a, **kw)

    monkeypatch.setattr(m.os, "listdir", flaky_listdir)

    cfg = _make_cfg(tmp_path)
    walker = m.SourceWalker(cfg, log=lambda *a, **k: None)
    items = list(walker.walk())

    yielded_names = {os.path.basename(it.read_path) for it in items}
    assert yielded_names == {"a.jpg"}  # b.jpg внутри blocked_dir не дошёл до пайплайна
    assert len(walker.listdir_failed) == 1
    assert "blocked_dir" in walker.listdir_failed[0]


def test_listdir_failure_empty_when_no_errors(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "a.jpg").write_bytes(b"x" * 10)
    (tmp_path / "target").mkdir()

    cfg = _make_cfg(tmp_path)
    walker = m.SourceWalker(cfg, log=lambda *a, **k: None)
    list(walker.walk())

    assert walker.listdir_failed == []


def test_run_analyze_surfaces_listdir_failed_paths(tmp_path, monkeypatch):
    # SESSION-HANDOFF.txt, 2026-08-11 (отложенная задача): в отличие от run_for_source() выше
    # (только len(walker.listdir_failed)), run_analyze() до этого фикса не читал
    # walker.listdir_failed вовсе -- непрочитанная папка была видна только в консоли/
    # actions.log, report.html для analyze ничего не показывал.
    monkeypatch.setattr(m, "exiftool_batch", lambda paths, **kw: {})
    source = tmp_path / "NewBatch"
    (source / "good_dir").mkdir(parents=True)
    (source / "good_dir" / "a.jpg").write_bytes(b"x" * 10)
    blocked = source / "blocked_dir"
    blocked.mkdir()
    (blocked / "b.jpg").write_bytes(b"x" * 10)
    target = tmp_path / "MyArchive"
    target.mkdir()
    workdir = tmp_path / "appdir"
    workdir.mkdir()

    real_listdir = os.listdir

    def flaky_listdir(path, *a, **kw):
        if "blocked_dir" in str(path):
            raise OSError("Отказано в доступе")
        return real_listdir(path, *a, **kw)

    monkeypatch.setattr(m.os, "listdir", flaky_listdir)

    cfg = m.Config(source=str(source), target=str(target), sample_limit=0, workdir=str(workdir))
    stats = m.run_analyze(cfg, "analyze", log=lambda *a, **k: None)

    assert len(stats.listdir_failed_paths) == 1
    assert "blocked_dir" in stats.listdir_failed_paths[0]
