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
