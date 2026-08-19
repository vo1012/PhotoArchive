"""Речь пользователя, 2026-08-18: dry-run и реальный прогон обязаны показывать один и тот же
report.html (отличие только в наклонении) -- "Объём по категориям" (report.py,
_render_run_copied()) молча пропадала в dry-run, потому что считалась через
os.path.getsize(dest), а dest физически не создаётся при dry_run (см. report._row_size()).
Фикс -- новый агрегат stats["bytes_appended_image"/"_video"/"_raw"] (photosort_win.py), растущий
из item.size (известен независимо от dry_run, SOURCE физический) в тех же местах, где уже растёт
stats["bytes_appended"]. Этот файл проверяет сам агрегат исполнением реального прогона -- не
report.py-сторону (та уже покрыта tests/test_run_report_section1_copied.py)."""
import photosort_win as m


def _make_jpeg(path, size=(800, 600), color=(10, 20, 30)):
    from PIL import Image
    Image.new("RGB", size, color).save(path, "JPEG")


def _stub_exiftool(monkeypatch, tags_by_path=None):
    monkeypatch.setattr(m, "exiftool_batch",
                         lambda paths, **kw: {p: (tags_by_path or {}).get(p, {}) for p in paths})


def _run(tmp_path, dry_run, monkeypatch):
    _stub_exiftool(monkeypatch)
    source = tmp_path / f"source_{dry_run}"
    source.mkdir()
    jpeg_path = source / "a.jpg"
    _make_jpeg(jpeg_path)
    raw_bytes = b"raw" * 500
    (source / "b.cr2").write_bytes(raw_bytes)
    target = tmp_path / f"target_{dry_run}"
    target.mkdir()
    workdir = tmp_path / f"workdir_{dry_run}"
    workdir.mkdir()
    cfg = m.Config(source=str(source), target=str(target), dry_run=dry_run, sample_limit=0,
                    workdir=str(workdir))
    stats, *_ = m.run(cfg, log=lambda *a, **k: None)
    return stats, jpeg_path.stat().st_size, len(raw_bytes)


def test_bytes_appended_by_kind_populated_on_real_run(tmp_path, monkeypatch):
    stats, jpeg_size, raw_size = _run(tmp_path, dry_run=False, monkeypatch=monkeypatch)
    assert stats["bytes_appended_image"] == jpeg_size
    assert stats["bytes_appended_raw"] == raw_size
    assert stats["bytes_appended_video"] == 0


def test_bytes_appended_by_kind_populated_in_dry_run_too(tmp_path, monkeypatch):
    """Ключевая проверка фикса: dest ничего не пишет на диск (dry_run=True), но байты по
    категориям всё равно посчитаны -- item.size читается из SOURCE, не из TARGET."""
    stats, jpeg_size, raw_size = _run(tmp_path, dry_run=True, monkeypatch=monkeypatch)
    assert stats["bytes_appended_image"] == jpeg_size
    assert stats["bytes_appended_raw"] == raw_size
    assert stats["bytes_appended_video"] == 0
