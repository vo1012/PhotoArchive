"""Ctrl+C-пакет, 2026-08-07 (по прямой просьбе пользователя, распространено с [3]/CLI archive
на ВСЕ режимы): раньше только _run_impl() (архивная сборка/[2] пробный прогон) ловил
KeyboardInterrupt и генерировал отчёт с баннером прерывания перед тем, как заново возбудить
исключение -- run_analyze() (питает [1]/[4]/CLI analyze) вообще не ловил его, Ctrl+C во время
диагностики источника/паспорта либо проваливался наружу без единого отчёта, либо (для [2],
которое реально использует архивный конвейер) молча проглатывался вызывающим кодом, который не
проверял result.interrupted вовсе.

Эти тесты реально инжектируют KeyboardInterrupt (monkeypatch analyze_batch -- общая точка,
используемая и run_analyze(), и _process_record()/_run_impl()) и проверяют исполнением, не по
чтению кода, что: (а) стат-объект помечается interrupted=True и остаётся валидным (частичным),
(б) вызывающий bare-launch-код заново возбуждает KeyboardInterrupt (через _InterruptedRunReport,
несущий report_path) после того, как отчёт с баннером прерывания уже записан на диск."""
import os
import subprocess
import sys
import tempfile
import zipfile

from PIL import Image

import photosort_win as m


def _make_jpeg(path, size=(800, 600), color=(10, 20, 30)):
    Image.new("RGB", size, color).save(path, "JPEG")


def _flaky_analyze_batch(real_analyze_batch, interrupt_on_call=2):
    calls = []

    def flaky(items, *a, **k):
        calls.append(1)
        if len(calls) == interrupt_on_call:
            raise KeyboardInterrupt()
        return real_analyze_batch(items, *a, **k)

    return flaky


def test_run_analyze_sets_interrupted_flag_and_returns_partial_stats(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    _make_jpeg(source / "a.jpg")
    _make_jpeg(source / "b.jpg")
    _make_jpeg(source / "c.jpg")
    target = tmp_path / "target"
    target.mkdir()

    real_analyze_batch = m.analyze_batch
    monkeypatch.setattr(m, "analyze_batch", _flaky_analyze_batch(real_analyze_batch, interrupt_on_call=2))

    cfg = m.Config(source=str(source), target=str(target))
    stats = m.run_analyze(cfg, "analyze-quick", log=lambda *a, **k: None)

    assert stats.interrupted is True
    # Первый файл (call 1) успел обработаться до того, как второй вызов (call 2) прервал --
    # total_files должен отразить хотя бы это, не остаться нулевым/не потеряться совсем.
    assert stats.total_files >= 1
    assert stats.total_files < 3  # третий файл не должен был дойти до обработки вообще


def test_bare_launch_run_view_raises_interrupted_report_with_banner(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    _make_jpeg(source / "a.jpg")
    _make_jpeg(source / "b.jpg")

    real_analyze_batch = m.analyze_batch
    monkeypatch.setattr(m, "analyze_batch", _flaky_analyze_batch(real_analyze_batch, interrupt_on_call=1))
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    monkeypatch.setattr(m, "WORKDIR", str(workdir))

    raised = None
    try:
        m._bare_launch_run_view([str(source)], log=lambda *a, **k: None)
    except m._InterruptedRunReport as e:
        raised = e

    assert raised is not None
    assert raised.report_path is not None
    html = open(raised.report_path, encoding="utf-8").read()
    assert "прервана пользователем" in html.lower()


def test_bare_launch_run_dryrun_raises_interrupted_report_with_banner(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    _make_jpeg(source / "a.jpg")
    _make_jpeg(source / "b.jpg")
    target = tmp_path / "target"
    target.mkdir()

    real_analyze_batch = m.analyze_batch
    monkeypatch.setattr(m, "analyze_batch", _flaky_analyze_batch(real_analyze_batch, interrupt_on_call=1))
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    monkeypatch.setattr(m, "WORKDIR", str(workdir))

    raised = None
    try:
        m._bare_launch_run_dryrun([str(source)], str(target), input_fn=lambda *a, **k: "",
                                   log=lambda *a, **k: None)
    except m._InterruptedRunReport as e:
        raised = e

    assert raised is not None
    assert raised.report_path is not None
    html = open(raised.report_path, encoding="utf-8").read()
    assert "прервана пользователем" in html.lower()
    # dry-run -- ничего не должно было физически записаться в TARGET несмотря на прерывание.
    assert not (target / "Albums").exists() or not any((target / "Albums").iterdir())


def test_bare_launch_run_passport_raises_interrupted_report_with_banner(tmp_path, monkeypatch):
    archive = tmp_path / "archive"
    archive.mkdir()
    # 2026-08-24: run_passport() теперь жёстко требует реальный маркер архива (живая просьба
    # пользователя, "если паспорт пытаются сделать на что угодно, кроме архива, не стартовать")
    # -- голая папка с фото больше не проходит гейт, тест не про это, просто отмечаем её.
    (archive / "__служебные_файлы").mkdir()
    _make_jpeg(archive / "a.jpg")
    _make_jpeg(archive / "b.jpg")

    real_analyze_batch = m.analyze_batch
    monkeypatch.setattr(m, "analyze_batch", _flaky_analyze_batch(real_analyze_batch, interrupt_on_call=1))

    raised = None
    try:
        m._bare_launch_run_passport(str(archive), log=lambda *a, **k: None)
    except m._InterruptedRunReport as e:
        raised = e

    assert raised is not None
    assert raised.report_path is not None
    html = open(raised.report_path, encoding="utf-8").read()
    assert "прервана пользователем" in html.lower()


# ---------------------------------------------------------------------------
# Живая находка пользователя, 2026-08-09: временные распакованные папки архива
# (__служебные_файлы\tmp_extract\<hash>\...) не убирались после Ctrl+C -- ни в
# _run_impl() (реальная сборка/--dry-run), ни в run_analyze() ([1]/CLI analyze/[4] Паспорт
# архива).
#
# Архив внутри которого лежит dump-именованная папка (например DCIM) без шанса найти альбом
# глубже -- двухфазный обход (см. SourceWalker.__init__()) откладывает очистку extract_dir в
# self._pending_cleanup_dirs, а не чистит немедленно в собственном try/finally
# _handle_archive() -- реальный drain происходит ТОЛЬКО в самом конце _drain_deferred_phases().
# Если генератор обхода прерван (Ctrl+C) ДО этой точки, обычный механизм "GeneratorExit при
# сборке мусора вызывает finally" здесь НЕ спасает -- тот код физически недостижим, генератор
# так и не был возобновлён до конца (подтверждено эмпирически: этот же сценарий БЕЗ фикса ниже
# оставляет extract_dir на диске, а плоский зип без dump-подпапки -- НЕТ, generator-refcounting
# сам справляется, см. _make_zip_with_two_jpegs() -- используется отдельно, для менее
# требовательного happy-path сценария). Тесты ниже реально прерывают прогон MID-архив
# (KeyboardInterrupt на первом же item, извлечённом ИЗ архива) и проверяют исполнением, что
# временная папка распаковки всё равно исчезает к моменту возврата из функции, не откладывается
# до следующего запуска программы.
# ---------------------------------------------------------------------------

def _make_zip_with_two_jpegs(zip_path, tmp_path):
    img1 = tmp_path / "_src1.jpg"
    img2 = tmp_path / "_src2.jpg"
    _make_jpeg(img1, color=(10, 20, 30))
    _make_jpeg(img2, color=(200, 40, 60))
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.write(img1, "a.jpg")
        zf.write(img2, "b.jpg")


def _make_zip_with_deferred_dump_folder(zip_path, tmp_path):
    """Двухфазный обход (см. SourceWalker.__init__()) откладывает очистку extract_dir в
    self._pending_cleanup_dirs (не чистит немедленно в собственном try/finally), если Фаза 1
    внутри архива на что-то натыкается, что нужно отложить на Фазу 2/3 -- например, поддерево
    ниже dump-именованной папки ("DCIM" -- см. DEFAULT_DUMP_SEGMENT_NAMES), у которой нет
    шанса найти альбом глубже (_is_terminal_bydate_branch()). pending_cleanup_dirs реально
    драйнится ТОЛЬКО в самом конце _drain_deferred_phases() -- если обход прерван ДО этой
    точки, generator-based cleanup (GeneratorExit -> finally в _handle_archive()) НЕ помогает:
    этот код физически недостижим, если генератор так и не был возобновлён до конца."""
    img1 = tmp_path / "_src1.jpg"
    img2 = tmp_path / "_src2.jpg"
    _make_jpeg(img1, color=(10, 20, 30))
    _make_jpeg(img2, color=(200, 40, 60))
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.write(img1, "DCIM/a.jpg")
        zf.write(img2, "DCIM/b.jpg")


def _own_tmp_extract_entries(tmp_extract_dir):
    if not os.path.isdir(tmp_extract_dir):
        return []
    return [n for n in os.listdir(tmp_extract_dir) if m._OWN_TMP_EXTRACT_ENTRY_RE.match(n)]


def test_run_analyze_cleans_up_tmp_extract_after_interrupt_mid_archive(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    _make_zip_with_deferred_dump_folder(source / "album.zip", tmp_path)
    target = tmp_path / "target"
    target.mkdir()
    tmp_extract_dir = tmp_path / "tmpx"

    real_analyze_batch = m.analyze_batch
    monkeypatch.setattr(m, "analyze_batch", _flaky_analyze_batch(real_analyze_batch, interrupt_on_call=1))

    cfg = m.Config(source=str(source), target=str(target), tmp_extract_dir=str(tmp_extract_dir))
    stats = m.run_analyze(cfg, "analyze-quick", log=lambda *a, **k: None)

    assert stats.interrupted is True
    assert _own_tmp_extract_entries(cfg.tmp_extract) == []


def test_run_impl_cleans_up_tmp_extract_after_interrupt_mid_archive(tmp_path, monkeypatch):
    """Тот же сценарий, что и выше, но для реальной сборки/--dry-run (_run_impl()) -- именно
    тот код-путь, на который пользователь указал ("структура архива при run-dry")."""
    source = tmp_path / "source"
    source.mkdir()
    _make_zip_with_deferred_dump_folder(source / "album.zip", tmp_path)
    target = tmp_path / "target"
    target.mkdir()
    tmp_extract_dir = tmp_path / "tmpx"

    real_analyze_batch = m.analyze_batch
    monkeypatch.setattr(m, "analyze_batch", _flaky_analyze_batch(real_analyze_batch, interrupt_on_call=1))

    cfg = m.Config(source=str(source), target=str(target), dry_run=True, tmp_extract_dir=str(tmp_extract_dir))
    m._run_impl(cfg, log=lambda *a, **k: None, print_summary=False)

    assert _own_tmp_extract_entries(cfg.tmp_extract) == []


def test_dry_run_default_tmp_extract_leaves_no_trace_on_target_after_interrupt(tmp_path, monkeypatch):
    """Живая находка пользователя, 2026-08-19: два теста выше явно переопределяют
    tmp_extract_dir ВНЕ TARGET -- они проверяют, что hash-именованная подпапка внутри НЕГО
    убирается, но никогда не проверяли реальный сценарий "[2] Пробный прогон"/CLI --dry-run
    (suppress_logs=True, tmp_extract_dir НЕ задан в конфиге). Дефолтный Config.tmp_extract
    раньше ВСЕГДА указывал под TARGET, независимо от suppress_logs -- archive-распаковка
    (реальна даже в dry-run) физически создавала TARGET\\__служебные_файлы\\tmp_extract\\ на
    диске, и по завершении/после Ctrl+C убиралась только hash-именованная подпапка внутри неё,
    не сама эта цепочка -- TARGET оставался существовать, пустой, но существовал.

    Фикс -- Config.__post_init__() редиректит tmp_extract на системный %TEMP% (не WORKDIR --
    портативный .exe многие запускают прямо с флешки, WORKDIR тогда физически совпадает с ней,
    ровно то ограниченное место, которого dry-run обязан не требовать), когда suppress_logs=True."""
    source = tmp_path / "source"
    source.mkdir()
    _make_zip_with_deferred_dump_folder(source / "album.zip", tmp_path)
    target = tmp_path / "target"  # НЕ создаётся заранее -- должен остаться отсутствующим
    workdir = tmp_path / "workdir"  # намеренно НЕ там, где окажется tmp_extract -- см. докстрин
    workdir.mkdir()

    real_analyze_batch = m.analyze_batch
    monkeypatch.setattr(m, "analyze_batch", _flaky_analyze_batch(real_analyze_batch, interrupt_on_call=1))

    cfg = m.Config(source=str(source), target=str(target), dry_run=True, suppress_logs=True,
                    workdir=str(workdir))
    assert cfg.tmp_extract.startswith(os.path.normpath(tempfile.gettempdir()))
    m._run_impl(cfg, log=lambda *a, **k: None, print_summary=False)

    assert not target.exists()
    assert not (workdir / "tmp_extract").exists()  # WORKDIR не стал заменой TARGET-течи
    assert _own_tmp_extract_entries(cfg.tmp_extract) == []  # %TEMP%-сторона тоже чиста


def test_real_build_sweeps_stale_global_dry_run_tmp_extract_leftover(tmp_path, monkeypatch):
    """Живая находка ревизора, 2026-08-19 (Раунд 106, придирка 2, по итогам фикса выше в этом
    файле): dry-run/analyze/паспорт (suppress_logs=True) распаковывают в ЕДИНЫЙ глобальный
    _DRY_RUN_TMP_EXTRACT_DIR под %TEMP%, не привязанный к TARGET. Если такой прогон убьют
    "жёстко" (Task Manager/крах -- не Ctrl+C, тот перехватывается надёжно и подчищает сразу),
    остаток раньше подхватывал только следующий прогон НА ТОМ ЖЕ TARGET (когда tmp_extract был
    его подпапкой) -- теперь путь общий, TARGET ни при чём, так что нужно, чтобы ЛЮБОЙ
    следующий прогон программы (в т.ч. реальная сборка на СОВЕРШЕННО ДРУГОМ TARGET) тоже его
    подмёл, а не только очередной suppress_logs=True прогон.

    2026-08-19, Раунд 107 ревью: верхний уровень _DRY_RUN_TMP_EXTRACT_DIR теперь PID-подпапки
    (не сами sha256-папки распаковки напрямую) -- остаток кладётся под PID заведомо МЁРТВОГО
    процесса (реально порождённого и дождавшегося завершения, не выдуманное число -- чтобы не
    зависеть от того, свободен ли конкретный PID в моменте на этой машине) ДО запуска реальной
    сборки (suppress_logs=False, TARGET из этого теста никак не пересекается с местом остатка)
    и проверяет, что он исчезает уже к началу этого прогона."""
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()

    fake_dry_run_tmp_extract = tmp_path / "fake_temp" / "PhotoArchive_tmp_extract"
    monkeypatch.setattr(m, "_DRY_RUN_TMP_EXTRACT_DIR", str(fake_dry_run_tmp_extract))
    stale_pid_dir = fake_dry_run_tmp_extract / str(dead.pid)
    stale = stale_pid_dir / ("b" * 64)
    stale.mkdir(parents=True)
    (stale / "leftover.tmp").write_text("orphaned by a hard kill of a previous dry-run")

    source = tmp_path / "source"
    source.mkdir()
    _make_jpeg(source / "a.jpg")
    target = tmp_path / "target"  # СОВЕРШЕННО другой TARGET -- никак не связан с местом остатка

    cfg = m.Config(source=str(source), target=str(target))  # suppress_logs=False -- реальная сборка
    m._run_impl(cfg, log=lambda *a, **k: None, print_summary=False)

    assert not stale_pid_dir.exists()  # глобальный dry-run путь подметён, хотя эта сборка -- НЕ dry-run


def test_stale_dry_run_pid_dir_of_still_alive_process_is_not_swept(tmp_path, monkeypatch):
    """Раунд 107 ревью (сама причина фикса выше -- PID-подпапки вместо плоских sha256-папок):
    конкурентный прогон НЕ должен удалять активную распаковку архива ДРУГОГО, ещё не
    завершившегося прогона под общим _DRY_RUN_TMP_EXTRACT_DIR -- только реально мёртвые PID.
    Red-before-green этого теста -- сам факт, что до PID-изоляции (плоские sha256-папки, любая
    "чужая" распознанная папка подметалась безусловно) такой сценарий воспроизводился ревизором
    исполнением (см. REVIEW-HANDOFF.md, Раунд 107)."""
    alive = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        fake_dry_run_tmp_extract = tmp_path / "fake_temp" / "PhotoArchive_tmp_extract"
        monkeypatch.setattr(m, "_DRY_RUN_TMP_EXTRACT_DIR", str(fake_dry_run_tmp_extract))
        active_dir = fake_dry_run_tmp_extract / str(alive.pid) / ("c" * 64)
        active_dir.mkdir(parents=True)
        (active_dir / "in_progress.tmp").write_text("still being extracted by a live process")

        source = tmp_path / "source"
        source.mkdir()
        _make_jpeg(source / "a.jpg")
        target = tmp_path / "target"  # СОВЕРШЕННО другой TARGET -- никак не связан с местом остатка

        cfg = m.Config(source=str(source), target=str(target))  # suppress_logs=False -- реальная сборка
        m._run_impl(cfg, log=lambda *a, **k: None, print_summary=False)

        assert active_dir.exists()  # чужой ЖИВОЙ прогон не тронут
    finally:
        alive.terminate()
        alive.wait()


def test_run_analyze_sweeps_stale_tmp_extract_leftovers_at_start(tmp_path):
    """Раньше run_analyze() вообще не подчищал tmp_extract в начале прогона (в отличие от
    _run_impl()'s Фазы 0) -- остатки прошлого прерванного прогона копились неограниченно.
    Не инжектирует прерывание -- просто вручную кладёт "свой" (sha256-именованный) остаток
    ДО запуска и проверяет, что он исчезает уже к началу этого прогона."""
    source = tmp_path / "source"
    source.mkdir()
    _make_jpeg(source / "a.jpg")
    target = tmp_path / "target"
    target.mkdir()
    tmp_extract_dir = tmp_path / "tmpx"
    tmp_extract_dir.mkdir()
    stale = tmp_extract_dir / ("a" * 64)
    stale.mkdir()
    (stale / "leftover.tmp").write_text("stale extraction remnant")

    cfg = m.Config(source=str(source), target=str(target), tmp_extract_dir=str(tmp_extract_dir))
    m.run_analyze(cfg, "analyze-quick", log=lambda *a, **k: None)

    assert not stale.exists()


def test_run_analyze_tmp_extract_sweep_never_touches_foreign_content(tmp_path):
    """Симметрично security audit #1 (ci/windows_ci_test.py::test_tmp_extract_wipe_protection,
    _run_impl()-путь) -- та же гарантия теперь нужна и для run_analyze(), раз она тоже метёт
    tmp_extract. Не архивно-именованное содержимое (не 64-hex-символьное имя) остаётся
    нетронутым, даже если TMP_EXTRACT_DIR указывает не туда по ошибке конфига."""
    source = tmp_path / "source"
    source.mkdir()
    _make_jpeg(source / "a.jpg")
    target = tmp_path / "target"
    target.mkdir()
    tmp_extract_dir = tmp_path / "tmpx"
    tmp_extract_dir.mkdir()
    foreign = tmp_extract_dir / "my_important_documents"
    foreign.mkdir()
    (foreign / "precious.txt").write_text("do not delete me")

    cfg = m.Config(source=str(source), target=str(target), tmp_extract_dir=str(tmp_extract_dir))
    m.run_analyze(cfg, "analyze-quick", log=lambda *a, **k: None)

    assert (foreign / "precious.txt").exists()
