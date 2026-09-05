"""2026-08-23, по прямой просьбе пользователя: окно "Работа окончена" (gui_menu._make_ok_input_fn())
показывает "Найдено/Обработано объектов: X" -- X читается из m._last_bare_launch_object_count,
модульной переменной (тот же приём, что уже используют _console_freed_for_gui/
_work_console_allocated), которую каждая из четырёх _bare_launch_run_*() выставляет ПРЯМО ПЕРЕД
успешным return (см. их докстринги/тело). Эти тесты гоняют реальный УСПЕШНЫЙ путь (не Ctrl+C --
тот уже покрыт tests/test_ctrl_c_report.py) на минимальных живых фикстурах и проверяют, что
значение реально совпадает с количеством обработанных/найденных файлов, не просто "не ноль".

Живая находка (боевой прогон source_meta_cache, Windows-сессия 2026-09-04): на "Прервана" все
четыре функции раньше поднимали _InterruptedRunReport ДО присвоения _last_bare_launch_object_count
-- экран «Выполнение» показывал устаревшее значение от ПРЕДЫДУЩЕГО успешного прогона в том же
процессе (в живом прогоне -- 3000 вместо реальных ~74 скопированных). Класс *_interrupted_* ниже
-- регресс на это (red-before-green проверен вручную: без фикса он падает, видя старое значение
сентинела вместо посчитанного этим прогоном)."""
import photosort_win as m

from PIL import Image


def _make_jpeg(path, size=(800, 600), color=(10, 20, 30)):
    Image.new("RGB", size, color).save(path, "JPEG")


def _flaky_analyze_batch(real_analyze_batch, interrupt_on_call=2):
    # Тот же хелпер, что tests/test_ctrl_c_report.py -- своя копия, не импорт, тем же
    # соображением самодостаточности тестовых модулей, что и _make_jpeg() выше.
    calls = []

    def flaky(items, *a, **k):
        calls.append(1)
        if len(calls) == interrupt_on_call:
            raise KeyboardInterrupt()
        return real_analyze_batch(items, *a, **k)

    return flaky


def test_bare_launch_run_view_sets_count_to_files_found(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    _make_jpeg(source / "a.jpg")
    _make_jpeg(source / "b.jpg")
    _make_jpeg(source / "c.jpg")
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    monkeypatch.setattr(m, "WORKDIR", str(workdir))
    monkeypatch.setattr(m, "_last_bare_launch_object_count", -1)  # заведомо неверное -- must be overwritten

    report_path = m._bare_launch_run_view([str(source)], log=lambda *a, **k: None)

    assert report_path is not None
    assert m._last_bare_launch_object_count == 3


def test_bare_launch_run_passport_sets_count_to_files_found(tmp_path, monkeypatch):
    archive = tmp_path / "archive"
    archive.mkdir()
    # 2026-08-24: run_passport() теперь жёстко требует реальный маркер архива -- голая папка с
    # фото больше не проходит гейт, тест не про это, просто отмечаем её.
    (archive / "__служебные_файлы").mkdir()
    _make_jpeg(archive / "a.jpg")
    _make_jpeg(archive / "b.jpg")
    monkeypatch.setattr(m, "_last_bare_launch_object_count", -1)

    report_path = m._bare_launch_run_passport(str(archive), log=lambda *a, **k: None)

    assert report_path is not None
    assert m._last_bare_launch_object_count == 2


def test_bare_launch_run_dryrun_sets_count_to_files_processed(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    _make_jpeg(source / "a.jpg")
    _make_jpeg(source / "b.jpg")
    _make_jpeg(source / "c.jpg")
    _make_jpeg(source / "d.jpg")
    target = tmp_path / "target"
    target.mkdir()
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    monkeypatch.setattr(m, "WORKDIR", str(workdir))
    monkeypatch.setattr(m, "_last_bare_launch_object_count", -1)

    report_path = m._bare_launch_run_dryrun([str(source)], str(target), input_fn=lambda *a, **k: "",
                                             log=lambda *a, **k: None)

    assert report_path is not None
    assert m._last_bare_launch_object_count == 4


def test_bare_launch_run_build_sets_count_to_files_processed(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    _make_jpeg(source / "a.jpg")
    _make_jpeg(source / "b.jpg")
    target = tmp_path / "target"
    target.mkdir()
    monkeypatch.setattr(m, "_last_bare_launch_object_count", -1)

    report_path = m._bare_launch_run_build([str(source)], str(target), input_fn=lambda *a, **k: "да",
                                            log=lambda *a, **k: None)

    assert report_path is not None
    assert m._last_bare_launch_object_count == 2


def test_bare_launch_run_view_interrupted_does_not_leave_stale_count(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    _make_jpeg(source / "a.jpg")
    _make_jpeg(source / "b.jpg")
    _make_jpeg(source / "c.jpg")
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    monkeypatch.setattr(m, "WORKDIR", str(workdir))
    real_analyze_batch = m.analyze_batch
    monkeypatch.setattr(m, "analyze_batch", _flaky_analyze_batch(real_analyze_batch, interrupt_on_call=2))
    monkeypatch.setattr(m, "_last_bare_launch_object_count", 12345)  # sentinel от "прошлого прогона"

    raised = None
    try:
        m._bare_launch_run_view([str(source)], log=lambda *a, **k: None)
    except m._InterruptedRunReport:
        raised = True

    assert raised is True
    # run_analyze() считает total_files по-другому, чем total_processed сборки/dry-run ниже --
    # 2 файла успели попасть в счётчик до того, как call 2 прервал; главное -- не сентинел 12345.
    assert m._last_bare_launch_object_count == 2


def test_bare_launch_run_passport_interrupted_does_not_leave_stale_count(tmp_path, monkeypatch):
    archive = tmp_path / "archive"
    archive.mkdir()
    (archive / "__служебные_файлы").mkdir()
    _make_jpeg(archive / "a.jpg")
    _make_jpeg(archive / "b.jpg")
    _make_jpeg(archive / "c.jpg")
    real_analyze_batch = m.analyze_batch
    monkeypatch.setattr(m, "analyze_batch", _flaky_analyze_batch(real_analyze_batch, interrupt_on_call=2))
    monkeypatch.setattr(m, "_last_bare_launch_object_count", 12345)

    raised = None
    try:
        m._bare_launch_run_passport(str(archive), log=lambda *a, **k: None)
    except m._InterruptedRunReport:
        raised = True

    assert raised is True
    assert m._last_bare_launch_object_count == 2  # см. комментарий у view-теста выше


def test_bare_launch_run_dryrun_interrupted_does_not_leave_stale_count(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    _make_jpeg(source / "a.jpg")
    _make_jpeg(source / "b.jpg")
    _make_jpeg(source / "c.jpg")
    target = tmp_path / "target"
    target.mkdir()
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    monkeypatch.setattr(m, "WORKDIR", str(workdir))
    real_analyze_batch = m.analyze_batch
    monkeypatch.setattr(m, "analyze_batch", _flaky_analyze_batch(real_analyze_batch, interrupt_on_call=2))
    monkeypatch.setattr(m, "_last_bare_launch_object_count", 12345)

    raised = None
    try:
        m._bare_launch_run_dryrun([str(source)], str(target), input_fn=lambda *a, **k: "",
                                   log=lambda *a, **k: None)
    except m._InterruptedRunReport:
        raised = True

    assert raised is True
    assert m._last_bare_launch_object_count == 1


def test_bare_launch_run_build_interrupted_does_not_leave_stale_count(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    _make_jpeg(source / "a.jpg")
    _make_jpeg(source / "b.jpg")
    _make_jpeg(source / "c.jpg")
    target = tmp_path / "target"
    target.mkdir()
    real_analyze_batch = m.analyze_batch
    monkeypatch.setattr(m, "analyze_batch", _flaky_analyze_batch(real_analyze_batch, interrupt_on_call=2))
    monkeypatch.setattr(m, "_last_bare_launch_object_count", 12345)

    raised = None
    try:
        m._bare_launch_run_build([str(source)], str(target), input_fn=lambda *a, **k: "да",
                                  log=lambda *a, **k: None)
    except m._InterruptedRunReport:
        raised = True

    assert raised is True
    assert m._last_bare_launch_object_count == 1
