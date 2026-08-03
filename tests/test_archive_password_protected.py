"""SourceWalker._handle_archive(): archive_password_protected -- REVIEW-HANDOFF.md, Раунд 45
[Замечание] 1 + уточнение Раунда 47. note (используется report.py для file://-ссылки, пункт
B.2) должен нести реальный путь ТОЛЬКО для архива, найденного прямо на SOURCE (depth==1, путь
стабилен весь прогон) -- для архива, найденного ВНУТРИ другого архива (depth>1), путь живёт
под cfg.tmp_extract и удаляется cleanup_dir() внешнего _handle_archive() ещё до того, как
report.py начинает писать отчёт: передавать его дальше значило бы дать ссылку, мёртвую с
рождения. Раунд 47: первая версия фикса передавала "" для depth>1 -- report.py использует
ОДИН И ТОТ ЖЕ note и как текст ссылки, и как её href, у пустой строки нет ни того, ни другого,
архив пропадал из списка вовсе, не мёртвой ссылкой. Теперь -- относительный display
("outer.zip → secret.zip", тот же текст, что уже в логе/статусе), _file_link_or_text() не
строит по нему ссылку (не абсолютный путь), но показывает как читаемый текст. Реальные
ZIP-архивы (не мок), только list_archive() подменена, чтобы не тащить в тест настоящее
шифрование -- сам факт "encrypted=True" уже покрыт другими тестами на уровне ArchiveInfo."""
import os
import zipfile

import photosort_win as m


def _make_cfg(tmp_path, **overrides):
    source = overrides.pop("source", None) or str(tmp_path / "source")
    target = overrides.pop("target", None) or str(tmp_path / "target")
    return m.Config(source=source, target=target, **overrides)


def _encrypted_list_archive(real_list_archive):
    def fake(path, fmt):
        if os.path.basename(path) == "secret.zip":
            return m.ArchiveInfo(encrypted=True, ok=True)
        return real_list_archive(path, fmt)
    return fake


def test_top_level_encrypted_archive_note_carries_the_real_path(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    with zipfile.ZipFile(source / "secret.zip", "w") as zf:
        zf.writestr("photo.jpg", b"x" * 100)
    (tmp_path / "target").mkdir()

    monkeypatch.setattr(m, "list_archive", _encrypted_list_archive(m.list_archive))
    cfg = _make_cfg(tmp_path)
    walker = m.SourceWalker(cfg, log=lambda *a, **k: None)
    list(walker.walk())

    encrypted = [(status, note) for _, status, note in walker.archive_logs
                 if status == "archive_password_protected"]
    assert len(encrypted) == 1
    _, note = encrypted[0]
    assert note == str(source / "secret.zip")
    assert os.path.exists(note)  # SOURCE не трогается программой -- путь реально стабилен


def test_nested_encrypted_archive_note_is_readable_text_not_empty_or_dead_link(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    inner_bytes_path = tmp_path / "_inner_for_outer.zip"
    with zipfile.ZipFile(inner_bytes_path, "w") as zf:
        zf.writestr("photo.jpg", b"x" * 100)
    with zipfile.ZipFile(source / "outer.zip", "w") as zf:
        zf.write(inner_bytes_path, arcname="secret.zip")
    (tmp_path / "target").mkdir()

    monkeypatch.setattr(m, "list_archive", _encrypted_list_archive(m.list_archive))
    cfg = _make_cfg(tmp_path)
    walker = m.SourceWalker(cfg, log=lambda *a, **k: None)
    list(walker.walk())

    encrypted = [(status, note) for _, status, note in walker.archive_logs
                 if status == "archive_password_protected"]
    assert len(encrypted) == 1
    _, note = encrypted[0]
    # Раунд 47: note теперь читаемый относительный текст ("outer.zip → secret.zip"), не
    # пустая строка -- архив по-прежнему опознаваем в отчёте, просто без ссылки (путь под
    # tmp_extract уже нестабилен -- см. проверку ниже).
    assert note == "outer.zip → secret.zip"
    # tmp_extract от внешнего архива к этому моменту уже вычищен -- живая проверка того, что
    # note ЗАКОНОМЕРНО не абсолютный путь под tmp_extract (тот уже нестабилен/удалён), не
    # просто "мы решили не передавать его".
    assert not os.path.isabs(note)
    assert not os.listdir(cfg.tmp_extract) if os.path.isdir(cfg.tmp_extract) else True
