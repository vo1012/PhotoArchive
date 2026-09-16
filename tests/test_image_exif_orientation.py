"""image_phash_and_size() -- корректная обработка EXIF Orientation.

Живой баг-репорт 2026-09-15 (реальный архив, тот же кадр в двух местах архива): то же самое
фото, сохранённое один раз с "сырыми" пикселями + EXIF Orientation=6 (типичное поведение
камеры/телефона), другой раз уже повёрнутым в пикселях приложением/облаком/синком (Orientation
не задан или =1), давало НИКАК не связанные между собой phash (hamming ~32 из 64 -- как у
случайных разных фото) и взаимно обратные width/height (aspect тоже не совпадает, near-dup
даже не находит подходящий бакет кандидатов) -- дубликат тихо не находился. Фикс:
ImageOps.exif_transpose() применяется ДО хеширования/размера."""
from PIL import Image

import photosort_win as m


def _asymmetric_image(w=64, h=48):
    # Асимметричный узор -- поворот на 90 градусов обязан реально изменить пиксели (иначе
    # phash совпал бы и без коррекции, тест ничего бы не проверял).
    im = Image.new("RGB", (w, h), "white")
    for x in range(min(20, w)):
        for y in range(min(15, h)):
            im.putpixel((x, y), (0, 0, 0))
    return im


def test_phash_matches_regardless_of_which_copy_baked_the_rotation(tmp_path):
    base = _asymmetric_image()

    # "Уже повёрнутая" копия -- пиксели в итоговой (видимой) ориентации, Orientation не задан
    # (эквивалентно 1) -- типичный результат ре-сохранения приложением/облаком.
    baked_path = tmp_path / "baked.jpg"
    base.save(baked_path, "JPEG")

    # "Сырая" копия -- как реально хранит камера/телефон: пиксели повёрнуты относительно
    # видимой ориентации, коррекция -- через EXIF Orientation=6 (ImageOps.exif_transpose()
    # исправляет её через ROTATE_270 -- обратная операция к повороту ниже).
    raw_path = tmp_path / "raw.jpg"
    exif = Image.Exif()
    exif[274] = 6  # Orientation
    base.transpose(Image.Transpose.ROTATE_90).save(raw_path, "JPEG", exif=exif)

    ph_baked, w_baked, h_baked = m.image_phash_and_size(str(baked_path))
    ph_raw, w_raw, h_raw = m.image_phash_and_size(str(raw_path))

    assert ph_baked is not None and ph_raw is not None
    assert m.hamming(ph_baked, ph_raw) == 0
    assert (w_baked, h_baked) == (w_raw, h_raw)


def test_orientation_1_is_unaffected():
    # Регресс-заслон: обычное фото без переворота (Orientation=1/отсутствует) не должно
    # менять поведение после фикса -- ImageOps.exif_transpose() на нём no-op.
    base = _asymmetric_image()
    import io
    buf = io.BytesIO()
    base.save(buf, "JPEG")
    ph, w, h = m.image_phash_and_size(buf.getvalue())
    assert ph is not None
    assert (w, h) == (64, 48)
