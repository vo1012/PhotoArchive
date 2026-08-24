"""2026-08-23, по прямой просьбе пользователя: окно "Работа окончена" (gui_menu._make_ok_input_fn())
показывает "Найдено/Обработано объектов: X" -- X читается из m._last_bare_launch_object_count,
модульной переменной (тот же приём, что уже используют _console_freed_for_gui/
_work_console_allocated), которую каждая из четырёх _bare_launch_run_*() выставляет ПРЯМО ПЕРЕД
успешным return (см. их докстринги/тело). Эти тесты гоняют реальный УСПЕШНЫЙ путь (не Ctrl+C --
тот уже покрыт tests/test_ctrl_c_report.py) на минимальных живых фикстурах и проверяют, что
значение реально совпадает с количеством обработанных/найденных файлов, не просто "не ноль"."""
import photosort_win as m

from PIL import Image


def _make_jpeg(path, size=(800, 600), color=(10, 20, 30)):
    Image.new("RGB", size, color).save(path, "JPEG")


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
