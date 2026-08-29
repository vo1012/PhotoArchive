# -*- coding: utf-8 -*-
"""Строит синтетический SOURCE (нейтральные плейсхолдеры -- см. CLAUDE.md, "Боевые прогоны":
реальные имена папок пользователя никогда не попадают в постоянные артефакты) для скриншотов
сайта: смесь папок с понятными именами (-> Albums) и россыпи файлов без папки (-> ByDate).
"""
import argparse
import os
import random
import shutil
import subprocess
import tempfile

from PIL import Image

EXIFTOOL = None
FFMPEG = None


def image(path, w, h, dt, make="Canon", model="Canon EOS 80D", gps=None):
    """gps: (lat, lon) в градусах -- если задано, пишем GPS-теги, чтобы в analyze-отчёте
    появился раздел «География» (offline-геокодинг). make/model варьируем между вызовами,
    чтобы раздел «Топ камер/устройств» показывал больше одной строки."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    im = Image.new("RGB", (w, h))
    px = im.load()
    random.seed(path)
    for x in range(w):
        for y in range(h):
            px[x, y] = (random.randint(0, 255), random.randint(0, 255), random.randint(0, 255))
    im.save(path, "JPEG", quality=90)
    args = [f"-DateTimeOriginal={dt}", f"-Make={make}", f"-Model={model}"]
    if gps is not None:
        lat, lon = gps
        args += [f"-GPSLatitude={abs(lat)}", f"-GPSLatitudeRef={'N' if lat >= 0 else 'S'}",
                 f"-GPSLongitude={abs(lon)}", f"-GPSLongitudeRef={'E' if lon >= 0 else 'W'}"]
    with tempfile.NamedTemporaryFile(mode="w", suffix=".args", delete=False, encoding="utf-8") as af:
        af.write(path + "\n")
        argfile_path = af.name
    try:
        r = subprocess.run(
            [EXIFTOOL, "-charset", "filename=utf8", "-overwrite_original", *args,
             "-@", argfile_path],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
    finally:
        os.unlink(argfile_path)
    if r.returncode != 0:
        raise RuntimeError(f"exiftool failed on {path!r}: {r.stderr}")


def main():
    global EXIFTOOL, FFMPEG

    parser = argparse.ArgumentParser(
        description="Строит синтетический SOURCE для скриншотов сайта (см. модуль docstring).")
    parser.add_argument("source", help="Папка SOURCE, будет создана/перезаписана")
    parser.add_argument("bin_dir", help="Папка с exiftool.exe/ffmpeg.exe (например, bin/ репозитория)")
    args = parser.parse_args()

    SOURCE = args.source
    EXIFTOOL = os.path.join(args.bin_dir, "exiftool.exe")
    FFMPEG = os.path.join(args.bin_dir, "ffmpeg.exe")

    # ~Москва / ~Санкт-Петербург -- две точки, чтобы «География» показала больше одного места.
    MSK = (55.751, 37.618)
    SPB = (59.939, 30.314)
    # Album folders (понятные имена -> Albums\...)
    for i in range(1, 6):
        image(os.path.join(SOURCE, "Свадьба", f"IMG_{i:04d}.jpg"), 1200, 800, "2018:06:16 14:20:00",
              gps=MSK)
    for i in range(1, 4):
        image(os.path.join(SOURCE, "Отпуск 2015", f"IMG_{i:04d}.jpg"), 1200, 800, "2015:08:03 11:05:00",
              make="NIKON CORPORATION", model="NIKON D750", gps=SPB)
    # Loose files, no album folder -> ByDate\...
    image(os.path.join(SOURCE, "IMG_0500.jpg"), 1200, 800, "2021:03:12 09:15:00",
          make="Apple", model="iPhone 12", gps=MSK)
    image(os.path.join(SOURCE, "IMG_0501.jpg"), 1200, 800, "2021:03:12 09:16:00",
          make="Apple", model="iPhone 12", gps=MSK)
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

    # Ещё несколько видео -- чтобы сегмент "Видео" в диаграммах "Тип медиа"/"Объём по
    # категориям" был заметным, а не 1-2%.
    for i in range(2, 7):
        vp = os.path.join(SOURCE, "Отпуск 2015", f"VID_{i:04d}.mp4")
        subprocess.run(
            [FFMPEG, "-y", "-f", "lavfi", "-i", f"testsrc=duration={2 + i}:size=1280x720:rate=25",
             "-pix_fmt", "yuv420p", vp], capture_output=True, text=True)
        with tempfile.NamedTemporaryFile(mode="w", suffix=".args", delete=False, encoding="utf-8") as af:
            af.write(vp + "\n")
            ap = af.name
        subprocess.run([EXIFTOOL, "-charset", "filename=utf8", "-overwrite_original",
                        f"-CreateDate=2015:08:0{i} 1{i}:00:00", "-@", ap],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
        os.unlink(ap)

    # RAW-файлы (.dng) рядом с JPEG того же кадра -- "Тип медиа" получает третий сегмент,
    # "Топ форматов" -- строку .dng. photosort_win.py классифицирует RAW по расширению
    # (RAW_EXTS), содержимое/EXIF для диаграмм analyze-отчёта не читает; exiftool отказывается
    # писать теги в JPEG с расширением .dng ("Not a valid DNG"), поэтому просто кладём JPEG-байты
    # под .dng без тегов -- для демо-диаграмм этого достаточно.
    for i in range(1, 9):
        dng = os.path.join(SOURCE, "Свадьба", f"IMG_{i:04d}.dng")
        os.makedirs(os.path.dirname(dng), exist_ok=True)
        di = Image.new("RGB", (1600, 1067))
        dpx = di.load()
        random.seed(dng)
        for x in range(1600):
            for y in range(1067):
                dpx[x, y] = (random.randint(0, 255), random.randint(0, 255), random.randint(0, 255))
        di.save(dng, "JPEG", quality=92)

    print("SOURCE built:", SOURCE)


if __name__ == "__main__":
    main()
