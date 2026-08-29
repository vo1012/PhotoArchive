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


# ---------------------------------------------------------------------------
# Речь пользователя, 2026-08-07: "при создании нового архива (не пополнения), когда
# индексировать нечего, выводится 'Просматриваю уже собранный архив...: всего обработано
# файлов: 0 [00:00, ?файл/с]'. Если архива нет, то индексировать нечего, можно строку не
# выводить." -- entries==[] на новом (только что созданном, пустом) TARGET раньше всё равно
# конструировал ProgressReporter (total=len(entries) or None -- 0 превращалось в None,
# indeterminate-режим), печатался один "пустой" кадр бара без единой реальной итерации.
# ---------------------------------------------------------------------------

def test_index_archive_empty_target_does_not_create_progress_bar(tmp_path, monkeypatch):
    cfg = _make_cfg(tmp_path)
    m.ensure_target_layout(cfg)  # пустой скелет архива (Albums/ByDate/RAW), ни одного файла
    conn = m.db_reset(cfg.index_db)

    created_desc = []
    real_pr = m.ProgressReporter

    class _SpyProgressReporter(real_pr):
        def __init__(self, *a, **kw):
            created_desc.append(kw.get("desc"))
            super().__init__(*a, **kw)

    monkeypatch.setattr(m, "ProgressReporter", _SpyProgressReporter)

    lines = []
    result = m.index_archive(cfg, conn, log=lines.append)
    conn.close()

    assert created_desc == []  # бар вообще не создавался -- индексировать было нечего
    assert result == (0, 0)
    assert not any("Фаза 1" in ln for ln in lines)  # ни бара, ни итоговой строки про 0 файлов


def test_index_archive_pause_poll_is_bound_to_the_passed_log(tmp_path, monkeypatch):
    """REVIEW-HANDOFF.md Раунд 149 (придирка): опрос паузы по пробелу в index_archive() (Фаза 1)
    должен быть замкнут на реальный log= функции — и поштучный между файлами, и progress_cb
    внутри sha256_file() — а не звать _check_pause_keypress() с дефолтным console_log мимо
    вызывающего кода (единообразие с analyze_batch()/_handle_dvd_unit(); нужно для тестов
    вида «пауза поймана во время индексации»)."""
    # _pause_cb внутри index_archive() строится только при os.name == "nt" -- фейкаем его.
    # m.os -- это буквально модуль os целиком, так что фейк глобален на время теста, а
    # winlong() при os.name == "nt" безусловно лепит "\\?\"-префикс, реальный только на
    # настоящем Windows -- на POSIX это ломает реальные файловые операции ensure_target_layout()/
    # db_reset() (создаётся мусорное дерево в cwd, REVIEW-HANDOFF.md Раунд 150). Нейтрализуем
    # winlong() тем же заходом -- os.name == "nt"-гейт внутри index_archive() всё равно видит
    # нужное значение, а файловые вызовы идут по настоящим путям.
    monkeypatch.setattr(m.os, "name", "nt")
    monkeypatch.setattr(m, "winlong", lambda p: p)
    seen = []
    monkeypatch.setattr(m, "_check_pause_keypress", lambda log=None: seen.append(log))

    cfg = _make_archive_with_images(tmp_path, 2)
    conn = m.db_reset(cfg.index_db)
    lines = []
    log_fn = lines.append  # стабильная ссылка (lines.append каждый раз новый bound-method)
    m.index_archive(cfg, conn, log=log_fn)
    conn.close()

    assert seen, "опрос паузы ни разу не вызван во время индексации Фазы 1"
    assert all(cb is log_fn for cb in seen)  # все вызовы — с тем самым log, что передали


def test_index_archive_nonempty_target_still_creates_progress_bar_and_summary(tmp_path):
    # Контрольный случай -- на непустом TARGET (обычное пополнение архива) поведение не
    # изменилось: бар создаётся, итоговая строка печатается как раньше.
    cfg = _make_archive_with_images(tmp_path, 2)
    conn = m.db_reset(cfg.index_db)
    lines = []
    result = m.index_archive(cfg, conn, log=lines.append)
    conn.close()

    assert result == (2, 20)  # 2 файла по 10 байт ("x" * 10 в _make_archive_with_images())
    assert any("Фаза 1: проиндексировано существующего архива — 2 файлов" in ln for ln in lines)
