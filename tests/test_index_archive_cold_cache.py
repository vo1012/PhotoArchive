"""SESSION-HANDOFF.txt п.12 (2026-08-05, боевой прогон): index_archive() (Фаза 1) на холодном
archive_cache.db может занимать часы на большом архиве, но ETA видна только ~12с ПОСЛЕ старта
бара (первый refresh), не до него. Живой пример: 32406 файлов, ~2 файла/сек, ETA ~4.5ч. Фикс --
дешёвый (без I/O) подсчёт cache-miss ДО старта бара, явное предупреждение, если холодных файлов
много (_COLD_CACHE_WARNING_THRESHOLD)."""
import photosort_win as m


def _make_cfg(tmp_path, **overrides):
    source = overrides.pop("source", None) or str(tmp_path / "source")
    target = overrides.pop("target", None) or str(tmp_path / "target")
    # archive_hash_cache=True (дефолт) -- реальный archive_cache.db открывается/читается,
    # см. test_index_archive_no_warning_when_hash_cache_disabled() за случаем False.
    return m.Config(source=source, target=target, **overrides)


def _make_archive_with_images(tmp_path, n: int, **cfg_overrides):
    cfg = _make_cfg(tmp_path, **cfg_overrides)
    m.ensure_target_layout(cfg)
    for i in range(n):
        (tmp_path / "target" / "Albums" / f"a{i}.jpg").write_bytes(b"x" * 10)
    return cfg


def test_index_archive_warns_when_cold_files_exceed_threshold(tmp_path, monkeypatch):
    monkeypatch.setattr(m, "_COLD_CACHE_WARNING_THRESHOLD", 2)
    cfg = _make_archive_with_images(tmp_path, 3)
    conn = m.db_reset(cfg.index_db)
    lines = []
    m.index_archive(cfg, conn, log=lines.append)
    conn.close()
    assert any("3 из 3 файлов ещё не в кэше" in ln for ln in lines)


def test_index_archive_no_warning_below_threshold(tmp_path):
    # Дефолтный порог (500) -- пара файлов не должна печатать предупреждение.
    cfg = _make_archive_with_images(tmp_path, 2)
    conn = m.db_reset(cfg.index_db)
    lines = []
    m.index_archive(cfg, conn, log=lines.append)
    conn.close()
    assert not any("ещё не в кэше" in ln for ln in lines)


def test_index_archive_warning_has_no_time_estimate(tmp_path, monkeypatch):
    # СОГЛАСОВАНО (SESSION-HANDOFF.txt п.12): без числовой оценки времени -- нет надёжной
    # априорной скорости диска до реального старта, только качественный сигнал.
    monkeypatch.setattr(m, "_COLD_CACHE_WARNING_THRESHOLD", 1)
    cfg = _make_archive_with_images(tmp_path, 2)
    conn = m.db_reset(cfg.index_db)
    lines = []
    m.index_archive(cfg, conn, log=lines.append)
    conn.close()
    warning = next(ln for ln in lines if "ещё не в кэше" in ln)
    assert "заметное время" in warning
    assert "час" not in warning and "мин" not in warning  # никакой числовой оценки времени


def test_index_archive_no_warning_when_hash_cache_disabled(tmp_path, monkeypatch):
    # REVIEW-HANDOFF.md, Раунд 66, придирка: archive_hash_cache=False -- явный опт-аут
    # пользователя от персистентного кэша -- cache={} ПОСТОЯННО на каждом прогоне (не только
    # первом), без этой проверки предупреждение печаталось бы на каждой обычной сборке.
    monkeypatch.setattr(m, "_COLD_CACHE_WARNING_THRESHOLD", 1)
    cfg = _make_archive_with_images(tmp_path, 3, archive_hash_cache=False)
    conn = m.db_reset(cfg.index_db)
    lines = []
    m.index_archive(cfg, conn, log=lines.append)
    conn.close()
    assert not any("ещё не в кэше" in ln for ln in lines)


def test_index_archive_no_warning_when_suppress_logs(tmp_path, monkeypatch):
    # REVIEW-HANDOFF.md, Раунд 67, замечание 1: Раунд 66 закрыл только archive_hash_cache=False
    # -- suppress_logs=True ([2] Пробный прогон) остался открытым сценарием: cache_conn там
    # никогда не открывается (см. условие открытия cache_conn в index_archive()), cache={}
    # ПОСТОЯННО на каждом прогоне, тот же класс "постоянно холодный", не "первый раз холодный".
    monkeypatch.setattr(m, "_COLD_CACHE_WARNING_THRESHOLD", 1)
    cfg = _make_archive_with_images(tmp_path, 3, suppress_logs=True)
    conn = m.db_reset(cfg.index_db)
    lines = []
    m.index_archive(cfg, conn, log=lines.append)
    conn.close()
    assert not any("ещё не в кэше" in ln for ln in lines)
