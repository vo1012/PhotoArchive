"""SourceWalker._walk_dir(): a file whose os.stat() fails (SOURCE disconnects physically
mid-walk, Сценарий 3 in SESSION-HANDOFF.txt) used to be silently `continue`-d -- a real
hardware disconnect test found files vanishing from every log (not even unreadable.csv).
Now such files are collected into walker.stat_failed_logs and surfaced via run_logs.unreadable()
in run_for_source() (photosort_win.py), same mechanism as archive_logs/sidecar_logs."""
import os

import photosort_win as m


def _make_cfg(tmp_path, **overrides):
    source = overrides.pop("source", None) or str(tmp_path / "source")
    target = overrides.pop("target", None) or str(tmp_path / "target")
    return m.Config(source=source, target=target, **overrides)


def test_stat_failure_is_recorded_not_silently_dropped(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    (source / "good.jpg").write_bytes(b"x" * 10)
    (source / "vanished.jpg").write_bytes(b"x" * 10)
    (tmp_path / "target").mkdir()

    real_stat = os.stat

    def flaky_stat(path, *a, **kw):
        if "vanished.jpg" in str(path):
            raise OSError("device removed")
        return real_stat(path, *a, **kw)

    monkeypatch.setattr(m.os, "stat", flaky_stat)

    cfg = _make_cfg(tmp_path)
    walker = m.SourceWalker(cfg, log=lambda *a, **k: None)
    items = list(walker.walk())

    yielded_names = {os.path.basename(it.read_path) for it in items}
    assert yielded_names == {"good.jpg"}
    assert len(walker.stat_failed_logs) == 1
    disp, err = walker.stat_failed_logs[0]
    assert disp == "vanished.jpg"
    assert "device removed" in err
