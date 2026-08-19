"""Живая находка пользователя (2026-08-18): "режим dry-run не убирает за собой созданный
архив" -- CLI `--dry-run` (в отличие от интерактивного [2] "Пробный прогон") раньше писал
настоящие CSV-логи/`archive_cache.db`/пустой скелет архива (`Albums`/`ByDate`/`RAW`/
`_Unsorted`, `ensure_target_layout()`) прямо в TARGET, потому что `suppress_logs` для CLI
`--dry-run` был всегда `False` (см. старый докстринг `_finalize_target_report()`) -- на
несуществующем TARGET после dry-run оставался пустой скелет архива, который приходилось
чистить руками перед реальной сборкой.

Фикс -- `_main()` теперь передаёт `suppress_logs=args.dry_run` в `run_for_source()`, тот же
механизм, что уже безопасно использует `_bare_launch_run_dryrun()` ([2]): `_run_impl()`
собирает строки в памяти (`CollectingRunLogs`), `ensure_target_layout()`/
`check_rules_version()`/`archive_cache`-соединение/`TargetLock` пропускаются (все уже гейтятся
`not cfg.suppress_logs`, см. `run()`) -- TARGET физически не трогается вовсе. Отчёт строится
из `data=combined_rows` (`_finalize_target_report()`, новый параметр) -- in-memory строки
этого прогона, слитые с уже существующей историей TARGET (`report.parse_target_logs()`), тот
же приём, что и в `_bare_launch_run_dryrun()`."""
import os
import sys
import zipfile

from PIL import Image

import photosort_win as m


def _make_jpeg(path, size=(800, 600), color=(10, 20, 30)):
    Image.new("RGB", size, color).save(path, "JPEG")


def test_cli_dry_run_creates_no_target_on_fresh_target(tmp_path, monkeypatch):
    source = tmp_path / "SOURCE"
    source.mkdir()
    _make_jpeg(source / "a.jpg")
    target = tmp_path / "TARGET"
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    monkeypatch.setattr(m, "WORKDIR", str(workdir))
    monkeypatch.setattr(sys, "argv", ["photosort_win.py", "archive", "--source", str(source),
                                       "--target", str(target), "--dry-run"])

    exit_code = m._main()

    assert exit_code == 0
    assert not target.exists()  # ключевая проверка -- TARGET целиком отсутствует
    report_path = workdir / "report.html"
    assert report_path.exists()
    html = report_path.read_text(encoding="utf-8")
    assert "Что скопировано" in html
    assert "файлов будет добавлено" in html  # предпросмотр -- будущее время

    # Симметричная проверка (regression guard, живая находка 2026-08-09/2026-08-14): реальная
    # сборка без --dry-run по-прежнему пишет в TARGET, эта симметрия не должна была сломаться.
    monkeypatch.setattr(sys, "argv", ["photosort_win.py", "archive", "--source", str(source),
                                       "--target", str(target)])
    exit_code2 = m._main()
    assert exit_code2 == 0
    assert target.exists()
    assert (target / "__служебные_файлы" / "logs" / "appended.csv").exists()


def test_cli_dry_run_on_existing_archive_does_not_modify_it(tmp_path, monkeypatch):
    """Ключевой сценарий: TARGET уже существует (не первый архив с нуля) -- dry-run обязан не
    трогать реальную историю И корректно учитывать её в отчёте (новый файл -- "новый",
    существующий -- не задваивается)."""
    source1 = tmp_path / "SOURCE1"
    source1.mkdir()
    _make_jpeg(source1 / "a.jpg")
    target = tmp_path / "TARGET"
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    monkeypatch.setattr(m, "WORKDIR", str(workdir))

    monkeypatch.setattr(sys, "argv", ["photosort_win.py", "archive", "--source", str(source1),
                                       "--target", str(target)])
    assert m._main() == 0
    appended_csv = target / "__служебные_файлы" / "logs" / "appended.csv"
    before_content = appended_csv.read_text(encoding="utf-8")
    before_mtime = appended_csv.stat().st_mtime

    source2 = tmp_path / "SOURCE2"
    source2.mkdir()
    _make_jpeg(source2 / "b.jpg", color=(200, 100, 50))
    monkeypatch.setattr(sys, "argv", ["photosort_win.py", "archive", "--source", str(source2),
                                       "--target", str(target), "--dry-run"])
    exit_code = m._main()

    assert exit_code == 0
    assert appended_csv.read_text(encoding="utf-8") == before_content  # ни одной новой строки
    assert appended_csv.stat().st_mtime == before_mtime  # файл физически не тронут
    assert not any(f == "b.jpg" for _, _, files in os.walk(target) for f in files)  # не скопирован
    assert any(f == "a.jpg" for _, _, files in os.walk(target) for f in files)  # старое цело

    html = (workdir / "report.html").read_text(encoding="utf-8")
    assert "файлов будет добавлено" in html  # b.jpg корректно распознан как новый, несмотря
                                              # на то что appended.csv для a.jpg не тронут


def test_cli_dry_run_with_archive_source_creates_no_target(tmp_path, monkeypatch):
    """Живая находка пользователя, 2026-08-19: тест выше (test_cli_dry_run_creates_no_target_on_
    fresh_target) не ловил реальную течь -- его SOURCE не содержит ни одного архива, поэтому
    ни разу не проходит через SourceWalker._handle_archive(). Config.tmp_extract по умолчанию
    ВСЕГДА указывал под TARGET ({TARGET}\\__служебные_файлы\\tmp_extract), независимо от
    suppress_logs -- archive-распаковка (она реальна даже в dry-run, нужно заглянуть внутрь)
    физически создавала эту папку на диске. _handle_archive() убирает hash-именованную
    подпапку с распакованным содержимым по завершении, но родительскую цепочку
    __служебные_файлы\\tmp_extract\\ (и тем самым сам TARGET) этим не убрать -- см. фикс в
    Config.__post_init__() (tmp_extract редиректится на WORKDIR при suppress_logs=True)."""
    source = tmp_path / "SOURCE"
    source.mkdir()
    img_path = tmp_path / "a.jpg"
    _make_jpeg(img_path)
    with zipfile.ZipFile(source / "album.zip", "w") as zf:
        zf.write(img_path, arcname="a.jpg")
    target = tmp_path / "TARGET"
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    monkeypatch.setattr(m, "WORKDIR", str(workdir))
    monkeypatch.setattr(sys, "argv", ["photosort_win.py", "archive", "--source", str(source),
                                       "--target", str(target), "--dry-run"])

    exit_code = m._main()

    assert exit_code == 0
    assert not target.exists()  # ключевая проверка -- ни TARGET, ни __служебные_файлы\tmp_extract\
    html = (workdir / "report.html").read_text(encoding="utf-8")
    assert "файлов будет добавлено" in html
