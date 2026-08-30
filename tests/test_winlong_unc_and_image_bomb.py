r"""Раунды 164-165 ревью (разбор стороннего аудита):
- придирка Раунда 165: `Config` принимает UNC-путь (`\\сервер\ресурс\`), но `winlong()` был
  no-op на UNC -> глубокий UNC-путь падал необработанным `OSError` в глубине дерева. Теперь
  `winlong()` строит форму `\\?\UNC\server\share\...` (снимает MAX_PATH так же, как `\\?\` на
  локальных путях).
- придирка Раунда 164 (A) + Раунд 166 [замечание 2]: `Image.MAX_IMAGE_PIXELS` оставлен на
  дефолте Pillow (~89M пикс). Кап 300M, пробовавшийся в 164A, РАСШИРЯЛ полосу "декодируется
  целиком" (0..600M вместо 0..179M) -- откачен. Настоящие фото-бомбы (>2·MAX) как и раньше
  ловятся `DecompressionBombError` на `Image.open()` (kept-not-lost). Дополнительно при импорте
  глушится сам `DecompressionBombWarning` (полоса 89M..179M: PIL декодирует легитимное большое
  фото, но печатал advisory-warning в stderr, ломавший tqdm-бар).
"""
import struct
import zlib

import photosort_win as m


# --- winlong() UNC ----------------------------------------------------------

def _nt(monkeypatch):
    monkeypatch.setattr(m.os, "name", "nt")


def test_winlong_unc_gets_extended_unc_prefix(monkeypatch):
    _nt(monkeypatch)
    assert m.winlong(r"\\nas\photos\2015\a.jpg") == r"\\?\UNC\nas\photos\2015\a.jpg"


def test_winlong_unc_normalizes_dotdot_and_separators(monkeypatch):
    _nt(monkeypatch)
    assert m.winlong(r"\\nas\photos\x\..\y\a.jpg") == r"\\?\UNC\nas\photos\y\a.jpg"


def test_winlong_already_extended_returned_unchanged(monkeypatch):
    _nt(monkeypatch)
    assert m.winlong(r"\\?\UNC\nas\photos\a.jpg") == r"\\?\UNC\nas\photos\a.jpg"
    assert m.winlong(r"\\?\C:\photos\a.jpg") == r"\\?\C:\photos\a.jpg"


def test_winlong_bare_unc_no_longer_returned_raw(monkeypatch):
    _nt(monkeypatch)
    out = m.winlong(r"\\nas\share\deep\path\file.jpg")
    assert out.startswith("\\\\?\\UNC\\")
    assert not out.startswith(r"\\nas")  # раньше возвращался как есть


def test_winlong_noop_off_windows(monkeypatch):
    monkeypatch.setattr(m.os, "name", "posix")
    assert m.winlong(r"\\nas\share\a.jpg") == r"\\nas\share\a.jpg"
    assert m.winlong("/home/user/a.jpg") == "/home/user/a.jpg"


def test_strip_winlong_is_exact_inverse_of_winlong(monkeypatch):
    # Раунд 166 ревью [замечание 1]: _strip_winlong() должен быть точным обратным winlong()
    # для ОБЕИХ форм -- иначе на UNC-TARGET в БД/CSV пишется "UNC\nas\x" вместо "\\nas\x",
    # ломая кросс-прогонный resume.
    _nt(monkeypatch)
    for plain in (r"\\nas\photos\2015\a.jpg", r"\\server\share\deep\tree\x.mp4"):
        assert m._strip_winlong(m.winlong(plain)) == plain
    assert m._strip_winlong(r"\\?\UNC\nas\photos\a.jpg") == r"\\nas\photos\a.jpg"
    # локальный путь -- как раньше
    assert m._strip_winlong(r"\\?\C:\Архив\a.jpg") == r"C:\Архив\a.jpg"
    assert m._strip_winlong(r"C:\Архив\a.jpg") == r"C:\Архив\a.jpg"  # без префикса -- no-op


# --- decompression bomb / warning (Раунд 166: кап откачен на дефолт, warning заглушён) ----

def test_max_image_pixels_left_at_pillow_default():
    # Раунд 166: кап 300M (введён в 77b094e) откачен -- расширял полосу "декодируется целиком".
    from PIL import Image as _PILImage
    assert m.Image.MAX_IMAGE_PIXELS == _PILImage.MAX_IMAGE_PIXELS  # дефолт, не тронут


def _fake_png(w, h):
    def chunk(typ, data):
        return (struct.pack(">I", len(data)) + typ + data
                + struct.pack(">I", zlib.crc32(typ + data) & 0xFFFFFFFF))
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)  # 8-bit RGB
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(b"\x00" * 64)) + chunk(b"IEND", b""))


def test_decompression_bomb_rejected_cleanly_not_crash(tmp_path):
    # >2·MAX (50000x50000 = 2.5e9 пикс) -> DecompressionBombError на open() -> ловится
    # существующим except -> kept-not-lost. Дефолт Pillow это и так делает.
    p = tmp_path / "bomb.png"
    p.write_bytes(_fake_png(50000, 50000))
    assert m.image_phash_and_size(str(p)) == (None, None, None)
    assert m.image_size_only(str(p)) == (None, None)


def test_decompression_bomb_warning_silenced_at_import():
    # Полоса MAX < px <= 2·MAX: PIL декодирует, но печатал DecompressionBombWarning в stderr
    # (ломает tqdm-бар). photosort_win при импорте ставит warnings.filterwarnings("ignore").
    # Проверяем в отдельном процессе -- pytest подменяет warnings.filters на время теста,
    # так что в самом тесте модульного фильтра не видно.
    import subprocess
    import sys
    code = (
        "import warnings; import photosort_win; from PIL import Image; "
        "assert any(f[0] == 'ignore' and f[2] is Image.DecompressionBombWarning "
        "for f in warnings.filters), warnings.filters"
    )
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
