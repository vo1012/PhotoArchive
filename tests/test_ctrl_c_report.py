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
