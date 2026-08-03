"""SourceWalker.walk()/_walk_dir(): REVIEW-HANDOFF.md Раунд 50 [БЛОКЕР] -- SOURCE указан как
одиночный файл-архив (RULES.md:347, штатный режим "Фаза 2а"), содержащий хотя бы одну подпапку,
всегда крашился с AttributeError: 'SourceWalker' object has no attribute
'_root_under_system_dir'. Причина: этот атрибут выставлялся только в walk()'s ветке для
SOURCE-папки, ветка для SOURCE-файла-архива делает return раньше той строки -- атрибут никогда
не создавался, а _walk_dir() (вызываемый из _handle_archive() после распаковки) обращается к
нему безусловно, как только доходит до любой неархивной-корневой директории. Фикс -- выставлять
self._root_under_system_dir безусловно в __init__(), не только в walk()'s SOURCE-папка ветке."""
import zipfile

import photosort_win as m


def _make_cfg(tmp_path, **overrides):
    source = overrides.pop("source", None) or str(tmp_path / "source")
    target = overrides.pop("target", None) or str(tmp_path / "target")
    return m.Config(source=source, target=target, **overrides)


def test_source_archive_file_with_subfolder_does_not_crash(tmp_path):
    source_zip = tmp_path / "vacation.zip"
    with zipfile.ZipFile(source_zip, "w") as zf:
        zf.writestr("Album/photo.jpg", b"x" * 100)  # subfolder inside the archive -- the trigger
    (tmp_path / "target").mkdir()

    cfg = _make_cfg(tmp_path, source=str(source_zip))
    walker = m.SourceWalker(cfg, log=lambda *a, **k: None)
    items = list(walker.walk())  # must not raise AttributeError

    assert [it.origin_display for it in items] == ["Album/photo.jpg"]


def test_source_archive_file_without_subfolder_still_works(tmp_path):
    # Control case (Round 50's own reproduction): a flat archive (no subfolder) never hit this
    # code path at all -- confirms the fix doesn't regress the already-working case.
    source_zip = tmp_path / "vacation.zip"
    with zipfile.ZipFile(source_zip, "w") as zf:
        zf.writestr("photo.jpg", b"x" * 100)
    (tmp_path / "target").mkdir()

    cfg = _make_cfg(tmp_path, source=str(source_zip))
    walker = m.SourceWalker(cfg, log=lambda *a, **k: None)
    items = list(walker.walk())

    assert [it.origin_display for it in items] == ["photo.jpg"]
