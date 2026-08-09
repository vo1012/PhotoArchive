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


def test_archive_subfolder_not_lost_when_target_under_system_dir(tmp_path, monkeypatch):
    """Живая находка (боевой прогон analyze без --target, 2026-08-09): is_under_system_dir()
    (гейт для настоящего SOURCE-содержимого, живущего под %TEMP%/AppData/Program Files/...)
    ошибочно применялся и к содержимому, РАСПАКОВАННОМУ ИЗ АРХИВА -- если cfg.tmp_extract (а
    значит любая подпапка внутри распакованного архива) физически лежит под системной
    директорией, эта подпапка тихо считалась "системным мусором" и пропадала из результата
    целиком, хотя origin_prefix однозначно говорит, что это НЕ настоящий путь SOURCE, а наша
    собственная временная распаковка. Воспроизводится безусловно для CLI/`[1]` `analyze` БЕЗ
    явного `--target` -- там cfg.target=_NO_TARGET_PLACEHOLDER всегда живёт под %TEMP%, а
    %TEMP% -- в SYSTEM_DIR_ENV_VARS."""
    source_zip = tmp_path / "vacation.zip"
    with zipfile.ZipFile(source_zip, "w") as zf:
        zf.writestr("Album/photo.jpg", b"x" * 100)  # subfolder inside the archive -- the trigger
    target = tmp_path / "system_like_target"
    target.mkdir()
    # Симулирует cfg.target, физически лежащий под системной директорией (см. SYSTEM_DIRS/
    # is_under_system_dir()) -- та же ситуация, что и у _NO_TARGET_PLACEHOLDER под %TEMP%,
    # без реальной зависимости теста от переменных окружения текущей машины.
    import os as _os
    monkeypatch.setattr(m, "SYSTEM_DIRS", [_os.path.normcase(_os.path.realpath(str(target)))])

    cfg = _make_cfg(tmp_path, source=str(source_zip), target=str(target))
    walker = m.SourceWalker(cfg, log=lambda *a, **k: None)
    items = list(walker.walk())

    assert [it.origin_display for it in items] == ["Album/photo.jpg"]
