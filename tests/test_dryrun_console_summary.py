"""Живая находка пользователя (2026-08-09): [3] (_bare_launch_run_build(), реальная сборка через
меню) уже подавляет техническую консольную сводку _run_impl() ("===== Прогон ..." / "Инструменты:
..." / "--- Итог прогона ---", print_summary=False, пакет п.4 SESSION-HANDOFF.txt) -- эта сводка
дублирует report.html. [2] (_bare_launch_run_dryrun(), пробный прогон) эту же настройку не
передавал -- единственный пункт меню, где сводка всё ещё дублировалась на экран."""
import photosort_win as m

from PIL import Image


def _make_jpeg(path, size=(800, 600), color=(10, 20, 30)):
    Image.new("RGB", size, color).save(path, "JPEG")


def test_bare_launch_run_dryrun_does_not_print_technical_summary(tmp_path, monkeypatch):
    source = tmp_path / "NewBatch"
    source.mkdir()
    _make_jpeg(source / "photo.jpg")
    target = tmp_path / "MyArchive"
    target.mkdir()
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    monkeypatch.setattr(m, "WORKDIR", str(workdir))

    logs = []
    m._bare_launch_run_dryrun([str(source)], str(target), input_fn=lambda *a, **k: "",
                               log=logs.append)
    joined = "\n".join(logs)

    assert "===== Прогон" not in joined
    assert "Инструменты:" not in joined
    assert "--- Итог прогона ---" not in joined
    assert "Обработано:" not in joined
    # Отчёт по-прежнему формируется и путь к нему по-прежнему сообщается -- подавлена именно
    # техническая сводка _run_impl(), не весь вывод [2] целиком.
    assert any(s.startswith("Отчёт:") for s in logs), logs
