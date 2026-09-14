"""_copy_file_cooperative() (2026-09-12, живой боевой репорт -- см. REVIEW-HANDOFF.md Раунд
225, находка 225-1) -- atomic_copy() раньше копировал ОДНИМ блокирующим shutil.copy2() без
единой точки проверки cancel_event; на большом файле (19ГБ видео) "Прервать работу"/крестик
окна ждали минуты. Эти тесты защищают ровно от повтора того бага: (а) чанковая копия должна
оставаться байт-в-байт идентичной shutil.copy2() на границах чанка, (б) progress_cb обязан
реально прерывать копирование ПОСРЕДИ файла, а не только между файлами."""
import os

import pytest

import photosort_win as m


def test_copy_multi_chunk_file_matches_shutil_copy2(tmp_path):
    """Файл размером НЕ кратным chunk_size, на несколько чанков -- побайтовое совпадение +
    сохранённые метаданные (shutil.copystat() в конце функции)."""
    src = tmp_path / "src.bin"
    chunk_size = 64 * 1024
    data = os.urandom(chunk_size * 3 + 12345)  # 3 полных чанка + хвост
    src.write_bytes(data)
    mtime = 1_600_000_000.0
    os.utime(src, (mtime, mtime))

    dst = tmp_path / "dst.bin"
    m._copy_file_cooperative(str(src), str(dst), chunk_size=chunk_size)

    assert dst.read_bytes() == data
    assert abs(os.path.getmtime(dst) - mtime) < 2


def test_copy_exact_multiple_of_chunk_size(tmp_path):
    """Размер файла РОВНО кратен chunk_size -- readinto() должен корректно увидеть конец
    файла (n == 0) на следующей итерации, не пытаться дописать лишний пустой чанк."""
    src = tmp_path / "src.bin"
    chunk_size = 64 * 1024
    data = os.urandom(chunk_size * 2)
    src.write_bytes(data)

    dst = tmp_path / "dst.bin"
    m._copy_file_cooperative(str(src), str(dst), chunk_size=chunk_size)

    assert dst.read_bytes() == data


def test_copy_empty_file(tmp_path):
    src = tmp_path / "src.bin"
    src.write_bytes(b"")
    dst = tmp_path / "dst.bin"
    m._copy_file_cooperative(str(src), str(dst), chunk_size=64 * 1024)
    assert dst.read_bytes() == b""


def test_progress_cb_can_cancel_mid_copy_not_only_after_whole_file(tmp_path):
    """Regression-тест на сам баг: progress_cb, поднимающий исключение на N-м чанке, должен
    остановить копирование ПОСЛЕ N чанков -- если бы копирование по-прежнему шло одним
    блокирующим вызовом (старый shutil.copy2()), progress_cb вообще не позвался бы посреди
    файла, и этот тест не смог бы существовать в таком виде."""
    class Cancelled(Exception):
        pass

    chunk_size = 64 * 1024
    src = tmp_path / "src.bin"
    src.write_bytes(os.urandom(chunk_size * 10))
    dst = tmp_path / "dst.bin"

    calls = []

    def cb():
        calls.append(1)
        if len(calls) == 3:
            raise Cancelled()

    with pytest.raises(Cancelled):
        m._copy_file_cooperative(str(src), str(dst), chunk_size=chunk_size, progress_cb=cb)

    assert len(calls) == 3
    # ровно 3 чанка успели записаться до отмены -- не весь файл (10 чанков)
    assert os.path.getsize(dst) == chunk_size * 3
