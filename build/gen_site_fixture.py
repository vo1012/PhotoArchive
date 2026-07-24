# -*- coding: utf-8 -*-
"""Строит синтетический SOURCE (нейтральные плейсхолдеры -- см. CLAUDE.md, "Боевые прогоны":
реальные имена папок пользователя никогда не попадают в постоянные артефакты) для скриншотов
сайта: смесь папок с понятными именами (-> Albums) и россыпи файлов без папки (-> ByDate).
"""
import os
import random
import shutil
import subprocess
import sys
import tempfile

from PIL import Image

SOURCE = r"C:\Users\HTPC\AppData\Local\Temp\claude\C--photo-sort-win\03081898-c2db-420e-9a54-69129033e6f5\scratchpad\site_fixture_source"
TARGET = r"C:\Users\HTPC\AppData\Local\Temp\claude\C--photo-sort-win\03081898-c2db-420e-9a54-69129033e6f5\scratchpad\site_fixture_target"
BIN = r"C:\Users\HTPC\AppData\Local\Temp\claude\C--photo-sort-win\03081898-c2db-420e-9a54-69129033e6f5\scratchpad\isolated_app\bin"
EXIFTOOL = os.path.join(BIN, "exiftool.exe")
FFMPEG = os.path.join(BIN, "ffmpeg.exe")


def image(path, w, h, dt):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    im = Image.new("RGB", (w, h))
    px = im.load()
    random.seed(path)
    for x in range(w):
        for y in range(h):
            px[x, y] = (random.randint(0, 255), random.randint(0, 255), random.randint(0, 255))
    im.save(path, "JPEG", quality=90)
    with tempfile.NamedTemporaryFile(mode="w", suffix=".args", delete=False, encoding="utf-8") as af:
        af.write(path + "\n")
        argfile_path = af.name
    try:
        r = subprocess.run(
            [EXIFTOOL, "-charset", "filename=utf8", "-overwrite_original",
             f"-DateTimeOriginal={dt}", "-Make=Canon", "-Model=Canon EOS 80D",
             "-@", argfile_path],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
    finally:
        os.unlink(argfile_path)
    if r.returncode != 0:
        raise RuntimeError(f"exiftool failed on {path!r}: {r.stderr}")


def main():
    # Album folders (понятные имена -> Albums\...)
    for i in range(1, 6):
        image(os.path.join(SOURCE, "Свадьба", f"IMG_{i:04d}.jpg"), 1200, 800, "2018:06:16 14:20:00")
    for i in range(1, 4):
        image(os.path.join(SOURCE, "Отпуск 2015", f"IMG_{i:04d}.jpg"), 1200, 800, "2015:08:03 11:05:00")
    # Loose files, no album folder -> ByDate\...
    image(os.path.join(SOURCE, "IMG_0500.jpg"), 1200, 800, "2021:03:12 09:15:00")
    image(os.path.join(SOURCE, "IMG_0501.jpg"), 1200, 800, "2021:03:12 09:16:00")
    image(os.path.join(SOURCE, "IMG_0700.jpg"), 1200, 800, "2023:12:24 18:00:00")
    image(os.path.join(SOURCE, "IMG_0701.jpg"), 1200, 800, "2023:12:24 18:02:00")

    # Near-dup pair inside Свадьба (same shot, tiny difference) -- so "Топ решений"/дедуп
    # charts show more than one segment.
    near_a = os.path.join(SOURCE, "Свадьба", "IMG_0010.jpg")
    image(near_a, 1200, 800, "2018:06:16 14:25:00")
    im = Image.open(near_a)
    px = im.load()
    for x in range(0, 30):
        for y in range(0, 30):
            px[x, y] = (255, 255, 255)
    near_b = os.path.join(SOURCE, "Свадьба", "IMG_0011.jpg")
    im.save(near_b, "JPEG", quality=90)
    with tempfile.NamedTemporaryFile(mode="w", suffix=".args", delete=False, encoding="utf-8") as af:
        af.write(near_b + "\n")
        argfile_path = af.name
    subprocess.run(
        [EXIFTOOL, "-charset", "filename=utf8", "-overwrite_original",
         "-DateTimeOriginal=2018:06:16 14:25:05", "-Make=Canon", "-Model=Canon EOS 80D",
         "-@", argfile_path],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    os.unlink(argfile_path)

    # Exact duplicate (same bytes, different name) -- точные дубли в отчёте.
    dup = os.path.join(SOURCE, "Отпуск 2015", "IMG_0001_копия.jpg")
    shutil.copyfile(os.path.join(SOURCE, "Отпуск 2015", "IMG_0001.jpg"), dup)

    # No-EXIF file -- "Надёжность дат" получает второй сегмент (не только Tier A/EXIF).
    no_exif = os.path.join(SOURCE, "IMG_0900.jpg")
    os.makedirs(os.path.dirname(no_exif), exist_ok=True)
    random.seed(no_exif)
    im2 = Image.new("RGB", (1200, 800))
    px2 = im2.load()
    for x in range(1200):
        for y in range(800):
            px2[x, y] = (random.randint(0, 255), random.randint(0, 255), random.randint(0, 255))
    im2.save(no_exif, "JPEG", quality=90)

    # Short synthetic video (ffmpeg lavfi) -- "Тип медиа" получает Видео-сегмент.
    video_path = os.path.join(SOURCE, "Отпуск 2015", "VID_0001.mp4")
    subprocess.run(
        [FFMPEG, "-y", "-f", "lavfi", "-i", "testsrc=duration=3:size=640x480:rate=15",
         "-pix_fmt", "yuv420p", video_path],
        capture_output=True, text=True,
    )
    with tempfile.NamedTemporaryFile(mode="w", suffix=".args", delete=False, encoding="utf-8") as af:
        af.write(video_path + "\n")
        argfile_path = af.name
    subprocess.run(
        [EXIFTOOL, "-charset", "filename=utf8", "-overwrite_original",
         "-CreateDate=2015:08:03 12:30:00", "-@", argfile_path],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    os.unlink(argfile_path)

    print("SOURCE built:", SOURCE)


if __name__ == "__main__":
    main()
