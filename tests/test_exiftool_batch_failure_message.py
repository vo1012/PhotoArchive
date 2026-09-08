"""exiftool_batch(): когда вызов exiftool падает на целом чанке (таймаут / битый argfile /
крах json), пользователь видит строку в зеркале экрана «Выполнение». 2026-09-03, живой отзыв
пользователя: раньше строка начиналась с «ВНИМАНИЕ:» и несла repr(исключения) (для
TimeoutExpired -- всю команду с путями и тег-флагами, выглядит как трейсбек). Теперь --
мягкое «Замечание», обычным языком, без технического хвоста; см. photosort_win.py:~3042."""
import subprocess
from types import SimpleNamespace

import photosort_win as m


def _run_failing_batch(monkeypatch, exc):
    def _boom(*a, **kw):
        raise exc

    monkeypatch.setattr(m.subprocess, "run", _boom)
    lines = []
    out = m.exiftool_batch(["/a/one.jpg", "/a/two.jpg", "/a/three.jpg"], log=lines.append)
    return out, "\n".join(lines)


def test_chunk_failure_yields_no_exif_but_does_not_raise(monkeypatch):
    out, _ = _run_failing_batch(monkeypatch, subprocess.TimeoutExpired(cmd=["exiftool"], timeout=120))
    assert out == {}


def test_message_is_plain_language_without_technical_tail(monkeypatch):
    _, log = _run_failing_batch(
        monkeypatch,
        subprocess.TimeoutExpired(
            cmd=["C:\\bin\\exiftool.exe", "-j", "-DateTimeOriginal", "-@", "C:\\Temp\\x.args"],
            timeout=120,
        ),
    )
    assert "Замечание:" in log
    # старый жаргон / технический хвост ушли полностью
    assert "ВНИМАНИЕ" not in log
    assert "exiftool" not in log.lower()
    assert "чанк" not in log
    assert "TimeoutExpired" not in log
    assert "cmd=" not in log
    assert ".args" not in log
    # последствие названо явно
    assert "точность даты" in log
    assert "3 шт." in log  # размер затронутой группы
    assert "в отчёте" in log


def test_message_shape_is_identical_for_any_exception_class(monkeypatch):
    _, log_json = _run_failing_batch(monkeypatch, ValueError("Expecting value: line 1 column 1"))
    assert "Замечание: у части файлов (3 шт.)" in log_json
    assert "Expecting value" not in log_json
    assert "ValueError" not in log_json


def test_warn_state_dedups_message_across_chunks(monkeypatch):
    """Накопитель F: при систематическом сбое exiftool (AV-лок, битый бинарник) except-ветка
    срабатывает на КАЖДОМ чанке -- на большом архиве это сотни одинаковых «Замечаний». С
    общим warn_state (создаёт _walk_with_exif_prefetch() один на прогон) пояснение выводится
    один раз."""
    def _boom(*a, **kw):
        raise subprocess.TimeoutExpired(cmd=["exiftool"], timeout=120)
    monkeypatch.setattr(m.subprocess, "run", _boom)

    warn_state = set()
    lines = []
    # два вызова exiftool_batch с общим warn_state (как две порции прогона)
    m.exiftool_batch(["/a/1.jpg", "/a/2.jpg"], log=lines.append, warn_state=warn_state)
    m.exiftool_batch(["/a/3.jpg", "/a/4.jpg"], log=lines.append, warn_state=warn_state)
    m.exiftool_batch(["/a/5.jpg"], log=lines.append, warn_state=warn_state)

    joined = "\n".join(lines)
    assert joined.count("не удалось прочитать") == 1
    assert "exif_batch_fail" in warn_state


def test_no_warn_state_still_warns_every_call(monkeypatch):
    """None (standalone-вызовы: analyze_batch() fallback, end-of-run retry, тесты) --
    поведение как раньше, по строке на вызов (там это один-два чанка, не стена)."""
    def _boom(*a, **kw):
        raise subprocess.TimeoutExpired(cmd=["exiftool"], timeout=120)
    monkeypatch.setattr(m.subprocess, "run", _boom)

    lines = []
    m.exiftool_batch(["/a/1.jpg"], log=lines.append)
    m.exiftool_batch(["/a/2.jpg"], log=lines.append)

    assert "\n".join(lines).count("не удалось прочитать") == 2


def test_walk_with_exif_prefetch_dedups_systematic_exiftool_failure(tmp_path, monkeypatch):
    """Сквозь настоящий _walk_with_exif_prefetch(): он сам заводит warn_state на весь обход."""
    def _boom(*a, **kw):
        raise subprocess.TimeoutExpired(cmd=["exiftool"], timeout=120)
    monkeypatch.setattr(m.subprocess, "run", _boom)

    items = [SimpleNamespace(read_path=f"/src/p{i}.jpg", ftype="image", size=10, mtime=1.0,
                             dvd_dest_path=None) for i in range(7)]
    lines = []
    list(m._walk_with_exif_prefetch(iter(items), str(tmp_path / "tmp"), batch_size=2,
                                     cache=None, log=lines.append))

    assert "\n".join(lines).count("не удалось прочитать") == 1


def test_dvd_item_between_batches_does_not_leak_the_warn_state(tmp_path, monkeypatch):
    """Раунд 218 218-2: DVD-ветка _walk_with_exif_prefetch() тоже сбрасывает накопленный
    батч -- раньше без warn_state -> «Замечание» протекало на второй сброс. Теперь один раз."""
    def _boom(*a, **kw):
        raise subprocess.TimeoutExpired(cmd=["exiftool"], timeout=120)
    monkeypatch.setattr(m.subprocess, "run", _boom)

    def _img(i):
        return SimpleNamespace(read_path=f"/src/p{i}.jpg", ftype="image", size=10, mtime=1.0,
                               dvd_dest_path=None)
    dvd = SimpleNamespace(read_path="/src/VIDEO_TS/VTS_01_0.VOB", ftype="video", size=10,
                          mtime=1.0, dvd_dest_path="/tgt/Albums/D/VIDEO_TS")
    # [img,img] -> flush #1 (предупреждает); DVD -> flush #2 накопленного (раньше -- 2-е
    # предупреждение); [img,img] -> flush #3
    items = [_img(0), _img(1), _img(2), dvd, _img(3), _img(4)]
    lines = []
    list(m._walk_with_exif_prefetch(iter(items), str(tmp_path / "tmp"), batch_size=2,
                                     cache=None, log=lines.append))

    assert "\n".join(lines).count("не удалось прочитать") == 1
