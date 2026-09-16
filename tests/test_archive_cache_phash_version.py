"""REVIEW-HANDOFF.md Раунд 234, находка 234-1: archive_cache (в отличие от source_meta_cache,
у которого есть _SOURCE_META_CACHE_TTL_DAYS) не имел вообще никакого механизма инвалидации --
строка для уже заархивированного файла (стабильные size/mtime между прогонами) пережила бы фикс
EXIF Orientation (b556f5c) навсегда, пока пользователь не удалит archive_cache.db вручную.
Фикс: колонка phash_version + _ARCHIVE_PHASH_VERSION -- index_archive() на ftype=="image"
требует совпадения версии для cache-хита, не только size/mtime."""
import sqlite3

from PIL import Image

import photosort_win as m


def _make_cfg(tmp_path, **overrides):
    source = overrides.pop("source", None) or str(tmp_path / "source")
    target = overrides.pop("target", None) or str(tmp_path / "target")
    return m.Config(source=source, target=target, **overrides)


def _seed_one_image(tmp_path):
    cfg = _make_cfg(tmp_path)
    m.ensure_target_layout(cfg)
    im = Image.new("RGB", (64, 48), "white")
    im.save(tmp_path / "target" / "Albums" / "a.jpg", "JPEG")
    return cfg


def test_archive_cache_reuses_phash_when_version_matches(tmp_path, monkeypatch):
    cfg = _seed_one_image(tmp_path)
    real = m.image_phash_and_size
    calls = []
    monkeypatch.setattr(m, "image_phash_and_size", lambda p: (calls.append(p), real(p))[1])

    conn = m.db_reset(cfg.index_db)
    m.index_archive(cfg, conn, log=lambda *a: None)
    conn.close()
    assert len(calls) == 1  # cold cache -- реально посчитан один раз

    cache_conn = m.connect(m.archive_cache_db_path(cfg.target))
    row = cache_conn.execute("SELECT phash_version FROM archive_cache").fetchone()
    cache_conn.close()
    assert row[0] == m._ARCHIVE_PHASH_VERSION

    conn = m.db_reset(cfg.index_db)
    m.index_archive(cfg, conn, log=lambda *a: None)
    conn.close()
    assert len(calls) == 1  # warm cache, версия совпадает -- НЕ пересчитан повторно


def test_archive_cache_recomputes_phash_when_version_stale(tmp_path, monkeypatch):
    cfg = _seed_one_image(tmp_path)
    real = m.image_phash_and_size
    calls = []
    monkeypatch.setattr(m, "image_phash_and_size", lambda p: (calls.append(p), real(p))[1])

    conn = m.db_reset(cfg.index_db)
    m.index_archive(cfg, conn, log=lambda *a: None)
    conn.close()
    assert len(calls) == 1

    # Симуляция строки, посчитанной ДО фикса (старый код никогда не писал phash_version --
    # ALTER TABLE ADD COLUMN без DEFAULT даёт именно NULL старым строкам, см. миграцию).
    cache_db = m.archive_cache_db_path(cfg.target)
    raw_conn = sqlite3.connect(cache_db)
    raw_conn.execute("UPDATE archive_cache SET phash_version = NULL")
    raw_conn.commit()
    raw_conn.close()

    conn = m.db_reset(cfg.index_db)
    m.index_archive(cfg, conn, log=lambda *a: None)
    conn.close()
    assert len(calls) == 2  # версия не совпала -- phash пересчитан заново, не унаследован

    cache_conn = m.connect(cache_db)
    row = cache_conn.execute("SELECT phash_version FROM archive_cache").fetchone()
    cache_conn.close()
    assert row[0] == m._ARCHIVE_PHASH_VERSION  # строка обновилась до текущей версии
