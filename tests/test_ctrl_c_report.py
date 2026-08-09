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
