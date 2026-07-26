"""report.html — визуальный отчёт по итогам работы PhotoArchive.

ТЗ и вся история решений: PROMPT_archive_report.md (в корне репозитория) — читать его
перед правкой этого файла, здесь только реализация уже согласованной логики.

Границы (PROMPT_archive_report.md, раздел 0): отдельный модуль, НЕ импортирует
photosort_win (photosort_win делает один тонкий вызов generate_report()/
generate_placeholder_report() в нужной точке жизненного цикла прогона, не наоборот) —
поэтому несколько констант (IMAGE_EXTS/RAW_EXTS/VIDEO_EXTS/DUMP_TAG) продублированы здесь
локальными копиями вместо импорта; держать в синхроне вручную при правке оригиналов в
photosort_win.py. Self-contained HTML/CSS, без внешних CDN, графики — инлайновый SVG.
"""

import csv
import html
import math
import os
import re
import time
from collections import Counter, defaultdict

# ============================================================================
# Палитра (MARKETING_BOOKLET.md) — без градиентов, без неона.
# ============================================================================

COLOR_ACCENT = "#24544A"
COLOR_ACCENT_SECONDARY = "#A85A2A"  # использовать скупо
COLOR_BG = "#F0F2EC"
COLOR_LINE = "#B9C2B2"
COLOR_TEXT = "#2B2B26"
COLOR_TEXT_MUTED = "#6B6B5E"

# Палитра для категориальных срезов (Лист 2, круговые диаграммы) — производные от
# акцентных цветов буклета, не новые случайные цвета: тёплый/холодный/приглушённый ряд той
# же гаммы.
CATEGORY_PALETTE = [COLOR_ACCENT, COLOR_ACCENT_SECONDARY, "#6E8C74", "#C9A063", "#9AA593"]

# ============================================================================
# Локальные копии констант photosort_win.py (см. докстринг модуля — НЕ импортировать)
# ============================================================================

IMAGE_EXTS = {"jpg", "jpeg", "png", "heic", "heif", "tif", "tiff", "bmp", "webp", "gif"}
RAW_EXTS = {"cr2", "cr3", "nef", "arw", "dng"}
VIDEO_EXTS = {"mp4", "mov", "m4v", "avi", "mkv", "3gp", "mts", "m2ts", "wmv", "flv", "webm", "mod", "tod"}
DUMP_TAG = " [PhotoArchive]"

NEAR_DUP_CATEGORIES = ("appended_near_dup", "appended_better", "appended_crop")

CSV_NAMES = ("appended", "skipped", "disputes", "dates_review", "albums_merged",
             "unreadable", "rejected_noise", "near_dup_edges", "undated_media")

TOP_N = 10  # PROMPT_archive_report.md, раздел 0: топ-N + отсылка к полному CSV, не всё целиком

# 4.9 (PROMPT_report_marketing.md): единый глоссарий терминов -- вынесен в отдельный
# REPORT_TERM_GLOSSARY.md (2026-07-24, round 29 придирка: константа была справочной, не
# использовалась программно ни в одном месте этого файла -- естественнее как документация,
# не Python-словарь).


def _winlong(path: str) -> str:
    """Локальная копия photosort_win.winlong() — глубоко вложенные ByDate/Albums-пути
    (те же, что этот модуль читает) иначе не открываются на Windows после 260 символов."""
    if os.name != "nt" or not path:
        return path
    if path.startswith("\\\\"):
        return path
    return "\\\\?\\" + os.path.abspath(path)


# ============================================================================
# 1. Источники данных → единый промежуточный формат dict[str, list[dict]]
# ============================================================================


_ROTATED_CSV_RE = re.compile(r"^(.+)-\d{8}-\d{6}\.csv$")


def parse_target_logs(logs_dir: str) -> dict:
    """TARGET-уровень (PROMPT_archive_report.md, 1.1): разбор существующих CSV-логов
    целиком. Отсутствующий файл (near_dup_edges.csv на архивах, собранных до этой фичи) —
    пустой список, не ошибка.

    Ротация логов (photosort_win.py:_rotate_log_if_needed, 20 МБ) переименовывает старый
    файл в "name-YYYYMMDD-HHMMSS.csv" и открывает новый пустой "name.csv" -- без учёта
    ротированных файлов здесь отчёт на давних/крупных архивах молча терял бы историю до
    последней ротации (найдено 2026-07-26, обсуждение с пользователем про будущую страницу
    сверки дублей -- тот же риск уже был у report.html, просто не проявлялся на архивах
    меньше 20 МБ логов). Сортировка по имени файла = сортировка по времени (формат
    YYYYMMDD-HHMMSS лексикографически совпадает с хронологическим), старые ротации читаются
    первыми, текущий "name.csv" -- последним.

    Живая регрессия (ci/windows_ci_test.py::test_log_rotation, найдена этим же изменением):
    ротированный файл -- это переименованный "как есть" файл на момент ротации, без гарантии,
    что его содержимое -- валидный CSV (тест форсирует ротацию файлом без единого "\\n" --
    csv.DictReader падает с _csv.Error "field larger than field limit", не OSError, и раньше
    ронял весь прогон). Испорченный/нечитаемый ротированный файл теперь просто пропускается
    (как и отсутствующий) -- та же философия "log it, keep going", что и у остального кода
    этого модуля, не полагаться на то, что старый файл лога обязан быть цел."""
    data = {}
    for name in CSV_NAMES:
        rows = []
        try:
            rotated = sorted(
                f for f in os.listdir(_winlong(logs_dir))
                if (m := _ROTATED_CSV_RE.match(f)) and m.group(1) == name
            )
        except OSError:
            rotated = []
        for fname in rotated:
            try:
                with open(_winlong(os.path.join(logs_dir, fname)), newline="", encoding="utf-8") as f:
                    rows.extend(csv.DictReader(f))
            except (OSError, csv.Error, UnicodeDecodeError):
                pass
        path = os.path.join(logs_dir, f"{name}.csv")
        try:
            with open(_winlong(path), newline="", encoding="utf-8") as f:
                rows.extend(csv.DictReader(f))
        except (OSError, csv.Error, UnicodeDecodeError):
            pass
        data[name] = rows
    return data


# ============================================================================
# 2. Агрегация dict[str, list[dict]] → модель для листов 1-3
# ============================================================================


def _size_of(path: str, cache: dict) -> int:
    if not path:
        return 0
    if path in cache:
        return cache[path]
    try:
        size = os.path.getsize(_winlong(path))
    except OSError:
        size = 0
    cache[path] = size
    return size


def _row_size(row: dict, cache: dict) -> int:
    """ТОЛЬКО "dest" -- реальный абсолютный путь на TARGET, файл физически там лежит после
    archive-прогона. "source" -- НЕ путь на диске: RunLogs.appended/skipped/... получают
    item.origin_display, человекочитаемую строку для логов ("Foto2015.zip -> .../a.jpg",
    см. photosort_win.py:995), не абсолютный путь -- os.path.getsize() на неё либо резолвится
    относительно левого cwd, либо просто не существует, тихо давая 0 в обоих случаях.
    Следствие: для WORKDIR-уровня ([2]/--dry-run), где dest никогда физически не создаётся
    (dry_run пропускает place_file), байтовая статистика недоступна -- пустая категория,
    график/плашка скрывается целиком (см. раздел 0 ТЗ), не считается ошибкой в этой версии."""
    return _size_of(row.get("dest"), cache)


def _ext(path: str) -> str:
    return os.path.splitext(path)[1].lstrip(".").lower()


def _media_kind(path: str) -> str:
    e = _ext(path)
    if e in IMAGE_EXTS:
        return "image"
    if e in RAW_EXTS:
        return "raw"
    if e in VIDEO_EXTS:
        return "video"
    return "other"


def _type_breakdown_caption(counts, label: str = "") -> str:
    """"{label}, в т.ч.: фото — N, RAW — N, видео — N[, прочее — N]" -- только ненулевые
    категории (2026-07-26, по просьбе пользователя разбить статистику по типу файла). label=""
    -- просто "в т.ч.: ..." без повторения названия категории (для подписи внутри тайла,
    которая и так уже стоит рядом со своим .label). Пустая строка, если считать нечего (все
    нули/пустой Counter) -- вызывающая сторона тогда не рендерит подпись вообще, как и
    остальные условные строки в этом модуле."""
    parts = [f"{name} — {_n_files(n)}" for name, key in
             (("фото", "image"), ("RAW", "raw"), ("видео", "video"), ("прочее", "other"))
             if (n := counts.get(key, 0))]
    if not parts:
        return ""
    prefix = f"{label}, " if label else ""
    return f"{prefix}в т.ч.: " + ", ".join(parts)


def _win_dirname(path: str) -> str:
    """os.path.dirname()/basename() ниже НЕ подходят -- тот же случай, что и в
    _parse_bydate_segment/_parse_album (см. их комментарии): `dest`/`source` -- всегда
    Windows-путь (программа только для Windows), а этот модуль импортируется под pytest
    на не-Windows раннере (public-репозиторий гоняет tests/ на ubuntu-latest в CI) --
    там os.path == posixpath, который не понимает `\\` как разделитель и вернёт путь
    целиком там, где на Windows (ntpath) корректно разделил бы на папку/имя файла."""
    head, _, _ = path.rpartition("\\")
    return head


def _win_basename(path: str) -> str:
    return path.rpartition("\\")[-1]


_MONTH_FOLDER_RE = re.compile(r"^(\d{4})-(\d{2})(?:-(\d{2}))?")


def _parse_bydate_segment(dest: str):
    """Достаёт (year, month, day, place) из пути вида
    ...\\ByDate\\<year>\\<YYYY-MM[-DD]> [место][ [PhotoArchive]]\\file — под любую
    bydate_granularity (day/month/year/flat). Возвращает None, если по пути нельзя
    восстановить хотя бы год (flat-раскладка, либо 0000-undated).

    `dest` -- всегда путь реального Windows TARGET (программа только для Windows), поэтому
    разделитель фиксирован на `\\` явно, а не через os.sep -- иначе разбор ломается, когда сам
    report.py импортируется под pytest на не-Windows раннере (public-репозиторий гоняет
    tests/ на ubuntu-latest в CI), хотя реальные данные всегда приходят с Windows."""
    parts = dest.split("\\")
    if "ByDate" not in parts:
        return None
    idx = parts.index("ByDate")
    remaining = len(parts) - idx - 1  # сегментов после ByDate, включая имя файла
    if remaining < 2:
        return None  # flat: ByDate\file, года не восстановить
    year_part = parts[idx + 1]
    if not (year_part.isdigit() and len(year_part) == 4):
        return None  # 0000-undated и т.п.
    year = int(year_part)
    if remaining == 2:
        return (year, None, None, None)  # granularity=year: ByDate\<year>\file
    folder = parts[idx + 2]
    m = _MONTH_FOLDER_RE.match(folder)
    if not m:
        return (year, None, None, None)
    month = int(m.group(2)) if m.group(2) != "00" else None
    day = int(m.group(3)) if m.group(3) else None
    rest = folder[m.end():]
    if rest.endswith(DUMP_TAG):  # сравнивать ДО strip() -- DUMP_TAG сам начинается с пробела
        rest = rest[:-len(DUMP_TAG)]
    rest = rest.strip()
    if rest == "month-unknown":  # photosort_win.py:build_bydate_dest_dir -- фиксированный
        rest = ""                # маркер (precision=="year"), не место
    return (year, month, day, rest or None)


def _parse_album(dest: str):
    # Тот же случай, что и в _parse_bydate_segment() выше -- dest всегда Windows-путь.
    parts = dest.split("\\")
    if "Albums" not in parts:
        return None
    idx = parts.index("Albums")
    if idx + 1 >= len(parts):
        return None
    return parts[idx + 1]


_DATE_COLUMN_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")


def _parse_date_column(date_str):
    """Резерв для _parse_bydate_segment() -- файлы в Albums\\... (SESSION-HANDOFF.txt, баг 9)
    не имеют сегмента ByDate в пути вообще, дату из dest никак не восстановить. RunLogs.appended()
    пишет её отдельной колонкой ("date" в appended.csv) начиная с этой версии -- старые архивы
    без этой колонки (row.get("date") -> None/"") просто не получают резерв, как и раньше до
    фикса. place всегда None -- у этой колонки нет своего понятия о месте, сама дата не несёт
    геоданных -- реальный резерв места читается ОТДЕЛЬНО, из колонки "place" (см. вызывающий
    код build_model_from_rows() и "place or album" там же)."""
    if not date_str:
        return None
    if len(date_str) == 4 and date_str.isdigit():
        return (int(date_str), None, None, None)
    m = _DATE_COLUMN_RE.match(date_str)
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)), None)


def _build_checklist_fields(data: dict) -> dict:
    """Поля Листа 3 ("Что стоит проверить") -- вынесено из build_model_from_rows() отдельной
    функцией 2026-07-20, чтобы её можно было вызвать ДВАЖДЫ на разных подмножествах строк
    (см. _split_rows_by_time()/generate_report()): "новое из этого пополнения" и "накопилось
    раньше" -- без разделения Лист 3 читался как "результат этого прогона", хотя на самом
    деле кумулятивная история архива (то же путаница, что была с "сэкономлено на точных
    повторах" до явной оговорки в _render_sheet1()).

    "albums_merged" здесь больше НЕТ (убрано по решению пользователя 2026-07-20 вторым
    заходом) -- "N файлов пополнили уже существующие альбомы" не предлагало никакого
    действия, а Лист 3 по ТЗ (PROMPT_archive_report.md) -- именно чек-лист действий, не
    список фактов; сам факт по-прежнему виден в описательной части ("Пополнение архива"),
    просто не дублируется здесь без пользы."""
    disputes = data.get("disputes", [])
    dates_review = data.get("dates_review", [])
    appended = data.get("appended", [])
    return {
        "near_dup_clusters": _cluster_near_dup(data.get("near_dup_edges", [])),
        # Раунд 31 (REVIEW-HANDOFF.md): живёт в этом же словаре ради переиспользования
        # разбивки "новое"/"раньше" (_split_rows_by_time()), рендерится ОТДЕЛЬНОЙ карточкой
        # (_render_exact_dup_examples()), не входит в Лист 3/_build_checklist_items().
        "exact_dup_groups": _cluster_exact_dup(data.get("skipped", [])),
        "disputes_by_folder": Counter(_win_dirname(r.get("source", "")) for r in disputes),
        "disputes_total": len(disputes),
        # Раунд 32, задача 2 (REVIEW-HANDOFF.md): имя файла + причина спора, не только счётчик
        # по папке -- см. _cluster_disputes()/_dispute_checklist_item(). Пусто для analyze
        # (AnalyzeStats считает только n_broken_or_zero агрегатом, без source/reason на файл)
        # -- _build_checklist_items() откатывается на старый агрегат по .get(..., []).
        "disputes_detail": _cluster_disputes(disputes),
        "dates_review_by_folder": Counter(
            _win_dirname(r.get("source", "")) for r in dates_review if r.get("tier") in ("B", "C")
        ),
        "dates_review_bc_total": sum(1 for r in dates_review if r.get("tier") in ("B", "C")),
        # 2026-07-26, по просьбе пользователя (общий аудит "путь для проверки" по Листу 3):
        # тот же приём, что и disputes_detail -- путь+имя каждого файла, не только счётчик по
        # папке. Пусто для analyze (см. build_model_from_analyze_stats() -- не отслеживает
        # source/dest на файл, только n_uncertain_dates агрегатом), _build_checklist_items()
        # откатывается на старый агрегат по .get(..., []), тот же паттерн, что у disputes.
        "dates_review_detail": _cluster_dates_review(dates_review),
        "unreadable": data.get("unreadable", []),
        # Флаг из appended.csv (RunLogs.appended()) -- раньше виден был только агрегатом в
        # диаграмме Листа 2 ("Качество кадров"), без указания, что с этим делать.
        "quality_flags": Counter(r.get("flags", "") or "" for r in appended),
        # Tier D (undated_media.csv) -- "даты нет вообще", не путать с Tier B/C выше
        # ("дата есть, но приблизительная") -- разные по природе находки, разные пункты.
        "undated_total": len(data.get("undated_media", [])),
        # 2026-07-26, обсуждение с пользователем: раньше здесь был только счётчик -- пункт
        # "N файлов вообще без даты" был безадресным, найти файлы можно было только через
        # undated_media.csv напрямую. dest -- путь в АРХИВЕ (файл всё равно дописан, просто
        # без даты, см. photosort_win.py:run_logs.undated_media()), не source в источнике --
        # пользователю нужно то место, где искать СЕЙЧАС, тот же принцип, что у "Самый старый
        # файл" (Раунд 40).
        "undated_media": data.get("undated_media", []),
    }


def _split_rows_by_time(data: dict, run_start: str) -> tuple:
    """Делит CSV-строки (только категории Листа 3) на "этот прогон" (timestamp >= run_start)
    и "раньше" -- по первой колонке timestamp, которая уже есть у каждого CSV-лога
    (RunLogs._ts(), формат "%Y-%m-%d %H:%M:%S", лексикографически сравнимый). `run_start` --
    тот же формат, захваченный в photosort_win.py ДО начала обработки источников -- см.
    generate_report(). "appended"/"undated_media" нужны здесь для флагов качества/Tier D
    в _build_checklist_fields() -- сами по себе не категории Листа 3, но их разбивка по
    времени строится по тому же timestamp, тем же способом. "skipped" (Раунд 31,
    REVIEW-HANDOFF.md) -- та же логика для exact_dup_groups, отдельная от Листа 3 карточка
    (_render_exact_dup_examples()), но нуждается в том же новое/раньше разделении."""
    names = ("near_dup_edges", "disputes", "dates_review", "unreadable", "appended",
              "undated_media", "skipped")
    new, before = {}, {}
    for name in names:
        rows = data.get(name, [])
        new[name] = [r for r in rows if (r.get("timestamp") or "") >= run_start]
        before[name] = [r for r in rows if (r.get("timestamp") or "") < run_start]
    return new, before


def build_model_from_rows(data: dict) -> dict:
    """Общая агрегация для TARGET-уровня (parse_target_logs) и WORKDIR
    [2]/--dry-run-уровня (CollectingRunLogs.rows) — обе формы идентичны по структуре
    (PROMPT_archive_report.md, раздел 3), эта функция не знает, откуда пришли данные."""
    appended = data.get("appended", [])
    near_dup = data.get("near_dup_edges", [])
    skipped = data.get("skipped", [])
    disputes = data.get("disputes", [])
    dates_review = data.get("dates_review", [])
    unreadable = data.get("unreadable", [])
    rejected_noise = data.get("rejected_noise", [])
    undated_media = data.get("undated_media", [])

    size_cache = {}

    counts = Counter()
    bytes_by_kind = Counter()
    years = Counter()
    year_months = Counter()
    cities = Counter()
    albums_bytes = Counter()
    albums_count = Counter()
    oldest = None  # (sort_key, dest_path, place_or_none)
    total_bytes = 0
    video_duration_seconds = 0.0  # 4.6 (PROMPT_report_marketing.md): кумулятивно, по всему
                                   # архиву -- сумма персистентной колонки "duration"
                                   # (появилась вместе с этим разделом), дешёвое чтение CSV,
                                   # не повторное чтение контейнера каждого видео при рендере.

    for row in appended:
        dest = row.get("dest", "") or ""
        kind = _media_kind(dest)
        size = _row_size(row, size_cache)
        counts[kind] += 1
        bytes_by_kind[kind] += size
        total_bytes += size

        if kind == "video":
            duration_str = row.get("duration")
            if duration_str:
                try:
                    video_duration_seconds += float(duration_str)
                except ValueError:
                    pass  # старый архив/повреждённая строка -- не считается ошибкой

        album = _parse_album(dest)
        if album:
            albums_bytes[album] += size
            albums_count[album] += 1

        # RAW с парным JPEG (raw_with_jpeg) осознанно не участвует во временной/гео-статистике
        # -- та же дата уже учтена через сам JPEG, повторный учёт задвоил бы цифры. RAW БЕЗ
        # пары (raw_without_jpeg) -- своя, не дублирующая ничей другой файл дата, ошибочно
        # резалась тем же фильтром (SESSION-HANDOFF.txt, баг 8) -- у него нет причины быть
        # невидимым для дат/года/города, путь RAW\ByDate\... парсится тем же _parse_bydate_segment.
        if kind not in ("image", "video") and not (kind == "raw" and row.get("reason") == "raw_without_jpeg"):
            continue
        # Файлы в Albums\... не имеют сегмента ByDate в пути вообще -- _parse_bydate_segment()
        # для них всегда None, независимо от того, насколько надёжна их дата (SESSION-HANDOFF.txt,
        # баг 9). Резерв -- колонка "date" в appended.csv (см. RunLogs.appended()/photosort_win.py,
        # call site у image/video), доступна только на архивах, собранных этой или более новой
        # версией -- для старых архивов (колонки нет) поведение не меняется, файл по-прежнему
        # пропускается, как и раньше.
        parsed = _parse_bydate_segment(dest) or _parse_date_column(row.get("date"))
        if parsed is None:
            continue
        year, month, day, place = parsed
        # Живая находка 2026-07-25 (боевой прогон F:\, архив целиком ушёл в Albums\..., ни
        # одного города в отчёте): _parse_bydate_segment()/_parse_date_column() восстанавливают
        # place только из пути (ByDate-папка несёт его в имени), а у Albums-файлов такого
        # сегмента нет вообще. Резерв -- колонка "place" в appended.csv (RunLogs.appended(),
        # новая с этого фикса) -- доступна только на архивах, собранных этой или более новой
        # версией, тот же паттерн, что уже применяется для "date" (баг 9).
        if not place:
            place = row.get("place") or None
        years[year] += 1
        if month:
            year_months[f"{year}-{month:02d}"] += 1
        if place:
            cities[place] += 1
        sort_key = (year, month or 0, day or 0)
        if oldest is None or sort_key < oldest[0]:
            # REVIEW-HANDOFF.md, Раунд 40: dest (реальный путь в АРХИВЕ), не row["source"]
            # (origin_display -- путь в источнике, для файлов из архивов вида "Foto.zip →
            # путь/файл.jpg", см. докстрины _row_size()/bytes_saved выше в этой же функции) --
            # _render_sheet1() показывает папку+имя тем же способом, что и near-dup/точные
            # повторы (_friendly_target_dir/_win_basename), а тот способ ищет ByDate/Albums --
            # маркер бывает только в путях архива.
            oldest = (sort_key, dest, place or album)

    # matched_with -- НЕ origin_display, а реальный путь: decision.matched_dest (TARGET) для
    # skipped_present, item.sibling_path (реальный абсолютный SOURCE-путь, не display-строка,
    # см. photosort_win.py:1929/1984) для raw_skipped -- оба стабильно стат-абельны, в отличие
    # от "source" (см. _row_size выше).
    bytes_saved = sum(_size_of(r.get("matched_with"), size_cache) for r in skipped)

    decisions = Counter({
        "appended": max(len(appended) - len(near_dup), 0),
        "near_dup": len(near_dup),
        "skipped_present": len(skipped),
        "unreadable": len(unreadable),
        "disputed": len(disputes),
    })
    # 2026-07-26, по просьбе пользователя: "Точные повторы" на диаграмме "Итог решений
    # программы" не показывали разбивку по типу файла -- та же классификация по расширению
    # (_media_kind()), что уже используется для "Тип медиа"/"Объём по категориям" выше по
    # модулю, применённая к matched_with (реальный путь в архиве, decisions["skipped_present"]
    # считает те же строки skipped -- ЛЮБАЯ причина, не только already_present, см. коммент у
    # _cluster_exact_dup() про осознанное расхождение с карточкой "Точные повторы — примеры").
    skipped_present_by_type = Counter(_media_kind(r.get("matched_with", "")) for r in skipped)

    tier_counts = Counter(r.get("tier", "") for r in dates_review if r.get("tier"))
    # REVIEW-HANDOFF.md, раунд 3 [БЛОКЕР]: Tier D (без EXIF/имени/соседей/mtime-сигнала)
    # никогда не попадает в dates_review.csv (там гейт date_value is not None, а у Tier D
    # date_value всегда None) -- без undated_media.csv эти файлы неотличимы от настоящего
    # Tier A ниже, оба "отсутствуют в dates_review". Ставить ДО строки "A" -- сумма для
    # вычитания должна уже включать D.
    tier_counts["D"] = len(undated_media)
    dated_media_count = counts["image"] + counts["video"]
    tier_counts["A"] = max(dated_media_count - sum(tier_counts.values()), 0)

    top_albums = sorted(albums_bytes.items(), key=lambda kv: kv[1], reverse=True)[:TOP_N]

    model = {
        "counts": counts,
        "bytes_by_kind": bytes_by_kind,
        "total_bytes": total_bytes,
        "video_duration_seconds": video_duration_seconds,
        "total_media": counts["image"] + counts["video"] + counts["raw"],
        "years": years,
        "year_months": year_months,
        "cities": cities,
        "oldest": oldest,
        "bytes_saved": bytes_saved,
        "exact_dupes": len(skipped),
        "decisions": decisions,
        "skipped_present_by_type": skipped_present_by_type,
        "tier_counts": tier_counts,
        "top_albums": top_albums,
        "rejected_noise_total": len(rejected_noise),
    }
    model.update(_build_checklist_fields(data))  # добавляет quality_flags/undated_total тоже
    return model


def _cluster_near_dup(near_dup_rows: list) -> list:
    """Union-find по рёбрам (dest, matched_dest) — PROMPT_archive_report.md, 1.2б/раздел 3.
    Возвращает кластеры размером >=2, отсортированные по убыванию размера (топ-N берёт
    вызывающая сторона при рендере, раздел 0: "не вываливать всё целиком")."""
    parent = {}

    def find(x):
        parent.setdefault(x, x)
        root = x
        while parent[root] != root:
            root = parent[root]
        while parent[x] != root:
            parent[x], x = root, parent[x]
        return root

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for r in near_dup_rows:
        dest, matched = r.get("dest"), r.get("matched_dest")
        if dest and matched:
            union(dest, matched)

    groups = defaultdict(set)
    for node in parent:
        groups[find(node)].add(node)

    clusters = [sorted(members) for members in groups.values() if len(members) >= 2]
    clusters.sort(key=len, reverse=True)
    return clusters


def _cluster_exact_dup(skipped_rows: list) -> list:
    """REVIEW-HANDOFF.md, Раунд 31: точные повторы (33% файлов реального прогона) были
    единственной крупной категорией отчёта без единого примера -- "Похожие кадры" (2%)
    показывают каждую группу поимённо, "Точные повторы" -- только число. Данные уже есть в
    skipped.csv (source/matched_with), новых вычислений не требуется, только группировка.

    reason=="already_present" ТОЛЬКО -- реальный "файл уже есть в архиве" (пул дедупа,
    decide()), не raw_skipped_has_jpeg (RAW осознанно не зеркалирован при MIRROR_RAW=false --
    решение конфига, не совпадение содержимого) и не identical_at_destination (коллизия имён
    при записи, редкий отдельный случай) -- иначе число здесь разошлось бы с тем, что реально
    означает "точный повтор" по сути, а не просто со всем, что когда-либо попало в
    skipped.csv. Диаграмма "Итог решений программы" (model["decisions"]["skipped_present"])
    по-прежнему считает все три причины -- та цифра осталась как есть, разошлась с этой
    осознанно (см. обсуждение с пользователем).

    Группировка -- по папке уже заархивированного файла (matched_with), не плоским списком
    исходных путей: source в CSV не всегда настоящий путь (файлы из вложенных zip/rar выглядят
    как "Foto2015.zip → 2015/Crimea/IMG_1234.jpg"), matched_with -- всегда полный путь в
    TARGET, из него же берётся и папка (_friendly_target_dir), и имя (_win_basename) -- тот же
    приём, что уже применяется для near-dup кластеров. Каждая архивная запись, для которой
    нашёлся хотя бы один дубль в источнике, становится одной строкой в группе, с числом
    найденных повторов -- при 8-9 тыс. повторов на большом архиве это на порядки меньше строк,
    чем плоский список пар (обычно один и тот же файл дублируется много раз подряд, например
    вся папка скопирована с телефона дважды).

    Возвращает [(folder, total_count, [(matched_with, dup_count), ...]), ...], отсортировано
    по убыванию total_count -- тот же принцип, что _cluster_near_dup() (крупные группы
    первыми, топ-N берёт вызывающая сторона при рендере)."""
    by_folder = defaultdict(lambda: defaultdict(int))
    for r in skipped_rows:
        if r.get("reason") != "already_present":
            continue
        matched = r.get("matched_with")
        if not matched:
            continue
        by_folder[_friendly_target_dir(matched)][matched] += 1

    groups = []
    for folder, counts in by_folder.items():
        total = sum(counts.values())
        items = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
        groups.append((folder, total, items))
    groups.sort(key=lambda g: -g[1])
    return groups


def _cluster_exact_dup_full(data: dict) -> list:
    """2026-07-26, обсуждение с пользователем: недоверчивый пользователь, который не
    принимает описание алгоритма как аргумент, хочет проверить дедуп САМ в файловой
    системе -- для каждого заархивированного файла увидеть, откуда он взят, и какие именно
    файлы источника были признаны его дублями (путь+имя, не просто число). Это отдельная,
    ПОЛНАЯ (без урезания топ-N/"и ещё N") версия _cluster_exact_dup() -- для отдельной
    страницы сверки (см. generate_dedup_verification_page()), не для превью в Листе 3.

    Та же фильтрация (reason=="already_present") и группировка по папке (matched_with), что
    и _cluster_exact_dup(), но каждая запись дополнительно несёт origin (source из
    appended.csv для этого же dest -- "откуда скопирован") и полный список source всех
    найденных дублей (не только счётчик).

    Возвращает [(folder, [(matched_dest, origin_source, [dup_source, ...]), ...]), ...],
    группы отсортированы по убыванию суммарного числа дублей, файлы внутри группы -- тоже."""
    dest_source = {r.get("dest"): r.get("source", "") for r in data.get("appended", []) if r.get("dest")}
    by_matched = defaultdict(list)
    for row in data.get("skipped", []):
        if row.get("reason") != "already_present":
            continue
        matched = row.get("matched_with")
        if not matched:
            continue
        by_matched[matched].append(row.get("source", ""))

    by_folder = defaultdict(list)
    for matched, sources in by_matched.items():
        by_folder[_friendly_target_dir(matched)].append((matched, dest_source.get(matched, ""), sources))

    groups = []
    for folder, items in by_folder.items():
        items.sort(key=lambda t: (-len(t[2]), t[0]))
        groups.append((folder, items))
    groups.sort(key=lambda g: -sum(len(item[2]) for item in g[1]))
    return groups


_DISPUTE_REASON_LABELS = {
    # classify_image()/analyze_batch() note-коды (photosort_win.py) -> короткая человеческая
    # формулировка для report.py. Неизвестный код (будущая версия добавит новый) -- просто
    # показывается как есть, не падает и не теряется молча.
    "icon_or_svg": "похоже на иконку/SVG",
    "animated_gif": "анимированный GIF",
    "tiny_image": "слишком маленькое изображение",
    "empty_file": "пустой файл (0 байт)",
    "unreadable_image": "не удалось открыть как изображение",
    "unreadable_video": "не удалось открыть как видео",
    "not_media": "не похоже на фото или видео",
}


def _dispute_reason_label(reason: str) -> str:
    return _DISPUTE_REASON_LABELS.get(reason, reason or "причина не определена")


def _cluster_disputes(disputes_rows: list) -> list:
    """REVIEW-HANDOFF.md, Раунд 32, задача 2: "Спорные" были единственной категорией отчёта
    совсем без деталей -- ни имени файла, ни причины, хотя оба поля уже есть в disputes.csv
    (source/reason), новых вычислений не требуется. Та же форма группировки, что
    _cluster_exact_dup() -- по исходной папке (_win_dirname), не плоским списком.

    Пусто для analyze (AnalyzeStats считает только n_broken_or_zero одним агрегатным числом,
    без source/reason на файл) -- тот же асимметричный охват, что уже есть у
    disputes_by_folder/disputes_total, вызывающая сторона (_build_checklist_items())
    откатывается на старое поведение (только числа по папкам), когда это поле пусто.

    Возвращает [(folder, [(name, reason), ...]), ...], отсортировано по убыванию размера
    группы."""
    by_folder = defaultdict(list)
    for r in disputes_rows:
        source = r.get("source", "")
        name = _win_basename(source) or source
        by_folder[_win_dirname(source)].append((name, r.get("reason", "")))
    groups = [(folder, items) for folder, items in by_folder.items()]
    groups.sort(key=lambda g: -len(g[1]))
    return groups


def _cluster_dates_review(dates_review_rows: list) -> list:
    """2026-07-26, по просьбе пользователя (общий аудит "путь для проверки" по Листу 3):
    "N файлов получили дату приблизительно" (Tier B/C) показывал только счётчик по папке, без
    имени файла -- тот же класс проблемы, что уже был у Tier D ("без даты") и у "Спорных" до
    Раунда 32. Та же форма группировки, что _cluster_disputes()/_cluster_exact_dup().

    Группировка по dest (папка в АРХИВЕ, через _friendly_target_dir), не по source -- в
    отличие от _cluster_disputes() (файлы уходят в _Unsorted, зеркалируя структуру источника,
    там что source, что dest дают одно и то же дерево), файлы с приблизительной датой лежат
    как обычно в Albums/ByDate -- "где искать СЕЙЧАС" однозначно только через dest (тот же
    принцип, что уже применён к undated_media/Tier D выше).

    Возвращает [(folder, [(name, tier), ...]), ...], отсортировано по убыванию размера
    группы."""
    by_folder = defaultdict(list)
    for r in dates_review_rows:
        if r.get("tier") not in ("B", "C"):
            continue
        dest = r.get("dest", "")
        name = _win_basename(dest) or dest
        by_folder[_friendly_target_dir(dest)].append((name, r.get("tier", "")))
    groups = [(folder, items) for folder, items in by_folder.items()]
    groups.sort(key=lambda g: -len(g[1]))
    return groups


def _plural(n: int, one: str, few: str, many: str) -> str:
    """Стандартное русское склонение по числу (1 файл / 2 файла / 5 файлов)."""
    n10, n100 = n % 10, n % 100
    if n10 == 1 and n100 != 11:
        return one
    if 2 <= n10 <= 4 and not (12 <= n100 <= 14):
        return few
    return many


def _n_files(n: int) -> str:
    return f"{n} {_plural(n, 'файл', 'файла', 'файлов')}"


def _fmt_video_duration(total_seconds: float) -> str:
    """4.6 (PROMPT_report_marketing.md): суммарная длительность видео, округлённая до целых
    часов -- минуты для архивов меньше часа (иначе "0 часов" читался бы как отсутствие видео,
    а не маленькое, но реальное число)."""
    hours = round(total_seconds / 3600)
    if hours < 1:
        minutes = max(round(total_seconds / 60), 1)
        return f"{minutes} {_plural(minutes, 'минута', 'минуты', 'минут')}"
    return f"{hours} {_plural(hours, 'час', 'часа', 'часов')}"


# ============================================================================
# 3. SVG-графики (инлайн, без внешних библиотек)
# ============================================================================


def _svg_bar_chart(counter: Counter, width=680, height=220, color=COLOR_ACCENT) -> str:
    items = sorted(counter.items())
    if not items:
        return ""
    max_v = max(v for _, v in items) or 1
    n = len(items)
    # margin_top 30, не 20 -- освобождает строку под подпись единицы измерения (см. ниже),
    # не наезжая на числа-подписи над самыми высокими столбцами (SESSION-HANDOFF.txt баг 6:
    # голое число без "шт."/"файлов" неоднозначно само по себе).
    margin_left, margin_bottom, margin_top, margin_right = 8, 26, 30, 8
    plot_w = width - margin_left - margin_right
    plot_h = height - margin_bottom - margin_top
    gap = plot_w / n
    bar_w = min(max(gap * 0.6, 4), 64)  # верхний предел -- иначе при n==1 (данные только за
    # один год) единственный столбец растягивается почти на всю ширину графика (2026-07-21).
    # 2026-07-26, живая находка пользователя на архиве с 18 разными годами (1973, 2003-2019):
    # подпись-год ("2005", 4 цифры, ~24-28px при font-size 11) при большом n слипается с
    # соседними -- MAX_LABELS подобран под plot_w=664 и такую ширину подписи (664/40 ~= 16.6,
    # округлено). Столбцы рисуются ВСЕ независимо от n (bar_w не опускается ниже 4px до n~170,
    # далеко за пределами реалистичного архива) -- прореживаются только подписи (и год снизу, и
    # число сверху -- показывать голое число без года-подписи под ним запутывало бы не меньше).
    # Первый и последний год -- всегда, остальные через шаг: без этого на 18 годах с шагом 2
    # ровно последний (самый актуальный, i=17) год мог бы не попасть на подписанные индексы.
    MAX_LABELS = 16
    label_step = max(1, math.ceil(n / MAX_LABELS))
    parts = [f'<text x="{width - margin_right}" y="14" font-size="10" text-anchor="end" '
             f'fill="{COLOR_TEXT_MUTED}">число файлов</text>']
    for i, (label, v) in enumerate(items):
        bar_h = plot_h * (v / max_v)
        x = margin_left + i * gap + (gap - bar_w) / 2
        y = margin_top + (plot_h - bar_h)
        parts.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_w:.1f}" height="{bar_h:.1f}" '
                      f'fill="{color}" rx="2"/>')
        if i % label_step == 0 or i == n - 1:
            parts.append(f'<text x="{x + bar_w / 2:.1f}" y="{height - margin_bottom + 16:.1f}" '
                          f'font-size="11" text-anchor="middle" fill="{COLOR_TEXT_MUTED}">{html.escape(str(label))}</text>')
            parts.append(f'<text x="{x + bar_w / 2:.1f}" y="{max(y - 4, 12):.1f}" '
                          f'font-size="11" text-anchor="middle" fill="{COLOR_TEXT}">{v}</text>')
    return (f'<svg viewBox="0 0 {width} {height}" width="100%" height="{height}" role="img" '
            f'aria-label="Медиафайлы по годам">' + "".join(parts) + "</svg>")


def _svg_pie(segments: list, size=170, value_fmt=_n_files) -> tuple:
    """segments: [(label, value, color), ...]. Возвращает (svg, legend_html) -- полный круг
    с секторами от центра (не кольцо/донат) -- площадь сектора на глаз сравнить проще, чем
    толщину дугового кольца, а аудитория отчёта нетехническая (RULES.md/
    PROMPT_archive_report.md: 45-70 лет, ценит простоту, не дашборд).

    value_fmt -- как показать value в легенде (по умолчанию _n_files -- "45 файлов", с
    единицей измерения, а не голое число, см. SESSION-HANDOFF.txt баг 6); _fmt_bytes -- для
    диаграмм по объёму (см. "Объём по категориям" в _render_sheet2)."""
    segments = [(label, v, c) for label, v, c in segments if v > 0]
    total = sum(v for _, v, _ in segments)
    if total <= 0:
        return "", ""
    r = size / 2 - 1
    cx = cy = size / 2

    def point(angle_deg):
        a = math.radians(angle_deg)
        return cx + r * math.sin(a), cy - r * math.cos(a)

    parts, legend = [], []
    angle = 0.0
    for label, v, color in segments:
        frac = v / total
        sweep = frac * 360
        if frac >= 0.9999:  # единственная непустая категория -- дуга вырождается в точку
            parts.append(f'<circle cx="{cx}" cy="{cy}" r="{r:.2f}" fill="{color}" '
                          f'stroke="#fff" stroke-width="1.5"/>')
        else:
            x1, y1 = point(angle)
            x2, y2 = point(angle + sweep)
            large_arc = 1 if sweep > 180 else 0
            parts.append(
                f'<path d="M{cx:.2f},{cy:.2f} L{x1:.2f},{y1:.2f} '
                f'A{r:.2f},{r:.2f} 0 {large_arc} 1 {x2:.2f},{y2:.2f} Z" '
                f'fill="{color}" stroke="#fff" stroke-width="1.5"/>'
            )
        angle += sweep
        pct = frac * 100
        pct_label = "<1%" if pct > 0 and round(pct) == 0 else f"{pct:.0f}%"
        legend.append(f'<div class="legend-row"><span class="swatch" style="background:{color}"></span>'
                       f'{html.escape(label)} — {value_fmt(v)} ({pct_label})</div>')
    svg = (f'<svg viewBox="0 0 {size} {size}" width="{size}" height="{size}" role="img" '
           f'aria-label="Диаграмма">' + "".join(parts) + "</svg>")
    return svg, "".join(legend)


def _svg_hbar_chart(items: list, width=680, bar_h=22, gap=8, color=COLOR_ACCENT,
                     colors: list = None, aria_label: str = "Топ альбомов по размеру") -> str:
    """items: [(label, value, display_str), ...], уже отсортированные по убыванию.
    `colors` — по цвету на каждый item (категориальные срезы, донат-палитра), иначе один
    `color` на все бары (сравнение однородных величин, напр. топ альбомов)."""
    if not items:
        return ""
    max_v = max(v for _, v, _ in items) or 1
    margin_left, margin_right = 170, 70
    plot_w = width - margin_left - margin_right
    height = len(items) * (bar_h + gap) + gap
    parts = []
    y = gap
    for i, (label, v, disp) in enumerate(items):
        w = plot_w * (v / max_v)
        bar_color = colors[i] if colors else color
        short_label = label if len(label) <= 26 else label[:23] + "…"
        parts.append(f'<text x="{margin_left - 8}" y="{y + bar_h * 0.68:.1f}" font-size="12" '
                      f'text-anchor="end" fill="{COLOR_TEXT}">{html.escape(short_label)}</text>')
        parts.append(f'<rect x="{margin_left}" y="{y}" width="{w:.1f}" height="{bar_h}" '
                      f'fill="{bar_color}" rx="3"/>')
        parts.append(f'<text x="{margin_left + w + 6:.1f}" y="{y + bar_h * 0.68:.1f}" font-size="12" '
                      f'fill="{COLOR_TEXT_MUTED}">{html.escape(disp)}</text>')
        y += bar_h + gap
    return (f'<svg viewBox="0 0 {width} {height}" width="100%" height="{height}" role="img" '
            f'aria-label="{html.escape(aria_label)}">' + "".join(parts) + "</svg>")


# ============================================================================
# 4. HTML-каркас страницы (строковые константы, без файлов-шаблонов — см. границы)
# ============================================================================


def _fmt_bytes(n: int) -> str:
    gb = n / 1024 ** 3
    if gb >= 1:
        return f"{gb:.1f} ГБ"
    mb = n / 1024 ** 2
    return f"{mb:.0f} МБ"


def _page_shell(title: str, body_html: str) -> str:
    return f"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<style>
:root {{
  --accent: {COLOR_ACCENT};
  --accent2: {COLOR_ACCENT_SECONDARY};
  --bg: {COLOR_BG};
  --line: {COLOR_LINE};
  --text: {COLOR_TEXT};
  --muted: {COLOR_TEXT_MUTED};
}}
* {{ box-sizing: border-box; }}
body {{
  margin: 0; padding: 0; background: var(--bg); color: var(--text);
  font-family: "Segoe UI", -apple-system, Roboto, Arial, sans-serif; line-height: 1.5;
}}
.sheet {{ max-width: 780px; margin: 0 auto; padding: 28px 20px 12px; }}
.card {{ background: #fff; border: 1px solid var(--line); border-radius: 12px; padding: 20px 22px; margin-bottom: 16px; }}
h1 {{ color: var(--accent); font-size: 28px; margin: 0 0 6px; }}
h2 {{ color: var(--accent); font-size: 19px; margin: 0 0 12px; border-bottom: 1px solid var(--line); padding-bottom: 6px; }}
p {{ margin: 8px 0; }}
.subtitle {{ color: var(--muted); margin: 0 0 18px; }}
.stat-row {{ display: flex; flex-wrap: wrap; gap: 18px 28px; margin-bottom: 6px; }}
.stat {{ flex: 1 1 140px; min-width: 130px; }}
.stat .value {{ font-size: 25px; font-weight: 600; color: var(--accent); }}
/* Раздел 7 приёмочного чек-листа (PROMPT_report_marketing.md, п.7): вспомогательный текст
   был 12-13px -- источники ТЗ независимо сходятся на минимуме 16px для аудитории 45-70 лет;
   поднято до 14px (не все 16px сразу -- 16px совпал бы с font-size .grid-3 h2/некоторых
   заголовков и сгладил визуальную иерархию, дальше -- решение при следующем визуальном
   аудите на реальном экране, не вслепую отсюда). */
.stat .label {{ font-size: 14px; color: var(--muted); }}
/* 2026-07-26, по просьбе пользователя: "итого + в т.ч. фото/RAW/видео" под главным числом
   тайла -- меньше основного .label, чтобы не спорить с ним за внимание, но всё ещё читаемо. */
.stat .breakdown {{ font-size: 12px; color: var(--muted); margin-top: 2px; }}
.legend-row {{ font-size: 14px; margin: 4px 0; }}
.swatch {{ display: inline-block; width: 10px; height: 10px; border-radius: 2px; margin-right: 6px; }}
.chart-block {{ display: flex; align-items: center; gap: 24px; flex-wrap: wrap; }}
.chart-block .legend {{ min-width: 160px; }}
.grid-2, .grid-3 {{ display: grid; gap: 16px; margin-bottom: 20px; }}
.grid-2 {{ grid-template-columns: 1fr 1fr; }}
.grid-3 {{ grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); }}
.grid-2 .card, .grid-3 .card {{ margin-bottom: 0; padding: 18px 20px; }}
.grid-2 h2, .grid-3 h2 {{ font-size: 16px; margin-bottom: 10px; padding-bottom: 6px; }}
/* Заголовки разной длины ("Тип медиа" — 1 строка, "Итог решений программы" — 2) иначе
   переносятся по-разному и сдвигают сами диаграммы вниз вразнобой — фикс. высота под 2
   строки (line-height 1.5 * font-size 16px) держит диаграммы на одном уровне независимо
   от того, влез заголовок в 1 строку или в 2. */
.grid-3 h2 {{ min-height: 3em; }}
.grid-2 .chart-block, .grid-3 .chart-block {{ gap: 14px; }}
.grid-2 .legend, .grid-3 .legend {{ min-width: 0; }}
@media (max-width: 640px) {{ .grid-2 {{ grid-template-columns: 1fr; }} }}
.city-list {{ display: flex; flex-wrap: wrap; gap: 6px 10px; padding: 0; margin: 0; list-style: none; }}
.city-list li {{ background: var(--bg); border: 1px solid var(--line); border-radius: 999px; padding: 3px 12px; font-size: 14px; }}
.checklist {{ list-style: none; padding: 0; margin: 0; }}
.checklist li {{ padding: 10px 0; border-bottom: 1px solid var(--line); }}
.checklist li:last-child {{ border-bottom: none; }}
.checklist .title {{ font-weight: 600; }}
.checklist .detail {{ color: var(--muted); font-size: 14px; margin-top: 2px; }}
/* "Показать ещё N" -- <details> без JS, показывает все находки категории (не топ-N с
   отсылкой к CSV, решение пользователя 2026-07-20) без простыни на весь экран сразу. */
.checklist li.expand {{ padding: 0; }}
.checklist li.expand > details > summary {{
  padding: 10px 0; color: var(--accent); font-weight: 600; cursor: pointer; list-style: none;
}}
.checklist li.expand > details > summary::-webkit-details-marker {{ display: none; }}
.checklist li.expand > details > summary::before {{ content: "▸ "; }}
.checklist li.expand > details[open] > summary::before {{ content: "▾ "; }}
.checklist .nested {{ margin: 0 0 10px; }}
.checklist .nested li {{ padding: 8px 0 8px 18px; }}
.bridge {{ color: var(--accent); font-style: italic; margin-top: 12px; }}
.muted {{ color: var(--muted); font-size: 14px; }}
/* Раздел 7 приёмочного чек-листа: свой цвет #8a8a7c на --bg давал контраст 3.1:1, не
   проходит WCAG AA для обычного текста (нужно >=4.5:1, посчитано явно, не принято на веру)
   -- заменён на var(--muted), тот же цвет, что и остальной вспомогательный текст (4.79:1 на
   --bg / 5.4:1 на белом фоне карточек -- оба проходят). */
.footer {{ text-align: center; color: var(--muted); font-size: 13px; margin: 16px 0 24px; }}
/* 4.1/4.3 (PROMPT_report_marketing.md): баннер доверия -- одна строка, не карточка, чтобы не
   раздувать первый экран; чек-лист рядом -- компактный, с нейтральными зелёными отметками. */
.trust-banner {{ font-size: 17px; font-weight: 600; color: var(--accent); margin: 4px 0 8px; }}
.trust-list {{ list-style: none; padding: 0; margin: 0 0 20px; font-size: 14px; color: var(--text); }}
.trust-list li {{ padding: 2px 0; }}
.trust-list li::before {{ content: "✓  "; color: var(--accent); font-weight: 700; }}
/* Ctrl+C-пакет: баннер прерывания -- самая первая строка отчёта (крупнее trust-banner,
   ochre-акцент вместо зелёного -- та же логика, что у "ОШИБКА" в консоли: не пугающий
   красный, но заметно отличается от обычного нейтрального тона отчёта). */
.interrupted-banner {{
  font-size: 20px; font-weight: 700; color: var(--accent2);
  border: 2px solid var(--accent2); border-radius: 8px;
  padding: 10px 16px; margin: 0 0 16px;
}}
@media print {{ body {{ background: #fff; }} .card {{ break-inside: avoid; }} }}
</style>
</head>
<body>
<div class="sheet">
{body_html}
<div class="footer">Сформировано PhotoArchive · {time.strftime("%Y-%m-%d %H:%M")}</div>
</div>
</body>
</html>"""


def _write(out_path: str, html_doc: str) -> None:
    """Best-effort — падение при записи report.html не должно валить весь прогон (тот же
    принцип устойчивости к проблемам записи, что и у RunLogs._write_row)."""
    try:
        with open(_winlong(out_path), "w", encoding="utf-8") as f:
            f.write(html_doc)
    except OSError:
        pass


# ============================================================================
# 5. Заглушка (PROMPT_archive_report.md, 1.1а/1.2)
# ============================================================================


def _render_interrupted_banner() -> str:
    """Ctrl+C-пакет: прервано пользователем во время [3] Сборка архива (или CLI archive,
    включая --dry-run -- та же ветка _finalize_target_report()). Первая строка отчёта,
    крупнее обычного trust-banner (см. CSS .interrupted-banner) -- пользователь должен
    увидеть это раньше, чем начнёт читать цифры, которые описывают НЕполный прогон."""
    return ('<p class="interrupted-banner">Работа режима прервана пользователем по CTRL-C. '
            'Отчёт содержит данные на момент остановки программы.</p>')


def generate_placeholder_report(reason: str, out_path: str, program_name: str = "PhotoArchive",
                                 interrupted: bool = False, suggest_other_location: bool = False) -> None:
    """suggest_other_location (Раунд 34, REVIEW-HANDOFF.md): пустой/недоступный источник --
    сегодня сухая заглушка ("подробности в консоли"), хотя почти всегда означает одно из
    двух: пользователь указал не тот диск/папку, либо архив уже собран в другое место раньше
    -- в обоих случаях есть немедленный следующий шаг, отчёт молчал об этом. Активная
    подсказка вместо констатации факта -- только для этого конкретного исхода (не для
    interrupted, где причина и так ясна пользователю самому). Порог "почти пусто" (не только
    буквальный ноль) сознательно НЕ реализован в этом заходе -- решение пользователя, нужны
    реальные малые архивы перед выбором порога, не гадать заранее."""
    # 4.1 (PROMPT_report_marketing.md): баннер доверия -- "ещё важнее здесь, потому что
    # заглушка появляется как раз в неоднозначных исходах" (источник не пишет ни один файл
    # пользователя ни в каком из случаев, приводящих к заглушке -- см. вызывающий код).
    trust_banner = ('<p class="trust-banner">Оригиналы не изменены и не удалены — программа '
                     'работает только с копиями.</p>')
    interrupted_banner = _render_interrupted_banner() if interrupted else ""
    if suggest_other_location and not interrupted:
        detail = (
            'Здесь почти ничего не нашлось — стоит проверить, может, фото на другом диске, '
            'флешке или в папке «Фото» на телефоне. Программа проверяет источник за '
            'несколько минут — можно запустить её ещё раз на другом месте.'
        )
    else:
        detail = 'Подробности — в консоли программы и summary.txt.'
    body = f"""
{interrupted_banner}
{trust_banner}
<div class="card">
  <h1>{html.escape(program_name)}</h1>
  <p class="subtitle">{time.strftime("%Y-%m-%d %H:%M")}</p>
  <p>{html.escape(reason)}</p>
  <p class="muted">{html.escape(detail)}</p>
</div>
"""
    _write(out_path, _page_shell(f"{program_name} — отчёт", body))


# ============================================================================
# 6. Листы 1-3
# ============================================================================


def _render_sheet1(model: dict, level: str = "target") -> str:
    # 2026-07-24: level=="analyze" -- SOURCE ещё только просканирован, ничего не собрано и не
    # "пополнено" -- заголовок/подписи "Ваш архив"/"с учётом только что добавленного в этом
    # пополнении" были бы неправдой (живая находка 2026-07-21, подтверждена чтением кода в
    # этой сессии: сборки в analyze-режимах нет вообще, run_stats для них не передаётся).
    # Вызов из _render_found_archive_block() (найденный СУЩЕСТВУЮЩИЙ архив внутри SOURCE) не
    # затронут -- level там не передаётся, остаётся по умолчанию "target", и это корректно:
    # для найденного архива "Ваш архив" -- правда, там реально есть история прошлых прогонов.
    is_scan = level == "analyze"
    total_media = model["total_media"]
    years = model["years"]
    stats = [f'<div class="stat"><div class="value">{total_media}</div>'
             f'<div class="label">{"фото и видео найдено" if is_scan else "фото и видео в архиве"}</div></div>']

    if model["total_bytes"]:
        stats.append(f'<div class="stat"><div class="value">{_fmt_bytes(model["total_bytes"])}</div>'
                      f'<div class="label">{"занимает на диске" if is_scan else "занимает архив"}</div></div>')

    if years:
        span = max(years) - min(years) + 1
        stats.append(f'<div class="stat"><div class="value">{span}</div>'
                      f'<div class="label">{"год" if span == 1 else "лет"} истории</div></div>')

    if model["bytes_saved"]:
        stats.append(f'<div class="stat"><div class="value">{_fmt_bytes(model["bytes_saved"])}</div>'
                      f'<div class="label">сэкономлено на точных повторах</div></div>')

    if is_scan:
        heading, subtitle = "Что нашлось на этом диске", (
            'Цифры — по всему, что программа увидела при сканировании прямо сейчас. Сборка '
            'архива ещё не запускалась — ничего не скопировано и не изменено.')
    else:
        # Все данные отчёта -- из CSV-логов TARGET, которые копятся с первого прогона
        # программы на этом архиве и никогда не очищаются между прогонами (см.
        # _finalize_target_report/PROMPT_archive_report.md) -- без этой строки цифры читались
        # бы как "результат вот этого пополнения", а на самом деле это история архива целиком.
        heading, subtitle = "Ваш архив", (
            'Цифры — по архиву целиком, с учётом только что добавленного в этом пополнении, '
            'за всё время, что вы пользуетесь программой с этим архивом.')

    parts = ['<div class="card">', f"<h1>{heading}</h1>",
             f'<p class="subtitle">{subtitle}</p>',
             '<div class="stat-row">'] + stats + ["</div>"]

    oldest = model["oldest"]
    if oldest:
        (year, month, day), oldest_path, place = oldest
        if day and month:
            date_str = f"{day:02d}.{month:02d}.{year}"
        elif month:
            date_str = f"{month:02d}.{year}"
        else:
            date_str = str(year)
        place_str = f" ({html.escape(place)})" if place else ""
        # REVIEW-HANDOFF.md, Раунд 40: путь уже вычислен (build_model_from_rows()), но раньше
        # никогда не рендерился -- у пользователя не было способа быстро найти именно этот
        # файл. Тот же способ отображения, что у near-dup/точных повторов (папка + имя, не
        # сырой путь целиком). level=="analyze" (build_model_from_analyze_stats()) кладёт сюда
        # origin_display (путь в ИСТОЧНИКЕ, ByDate/Albums там не бывает) -- folder тогда пусто,
        # деградирует до одного имени файла, не ошибка.
        file_str = ""
        if oldest_path:
            name = html.escape(_win_basename(oldest_path))
            folder = _friendly_target_dir(oldest_path)
            file_str = f' — {html.escape(folder)}\\{name}' if folder else f' — {name}'
        parts.append(f'<p><b>Самый старый файл:</b> {date_str}{place_str}{file_str}</p>')

    if model["year_months"]:
        busiest_ym, busiest_n = model["year_months"].most_common(1)[0]
        parts.append(f'<p><b>Самый насыщенный месяц:</b> {busiest_ym} — {busiest_n} файлов</p>')
    elif years:
        busiest_y, busiest_n = years.most_common(1)[0]
        parts.append(f'<p><b>Самый насыщенный год:</b> {busiest_y} — {busiest_n} файлов</p>')

    cities = model["cities"]
    if cities:
        top_cities = [c for c, _ in cities.most_common(TOP_N)]
        city_items = "".join(f"<li>{html.escape(c)}</li>" for c in top_cities)
        parts.append(f'<p><b>География:</b></p><ul class="city-list">{city_items}</ul>')

    if model["video_duration_seconds"]:
        parts.append(f'<p><b>Видео в архиве:</b> суммарно '
                      f'{_fmt_video_duration(model["video_duration_seconds"])} отснятого материала</p>')

    parts.append('<p class="bridge">Дальше — ваш архив в цифрах.</p>')
    parts.append("</div>")
    return "".join(parts)


def _render_trust_block(level: str) -> str:
    """4.1/4.3 (PROMPT_report_marketing.md): баннер доверия -- самая частая рекомендация всех
    шести источников маркетингового ТЗ, полностью отсутствовала в HTML (фраза была только в
    консоли, photosort_win.py:6484/6209). Одна строка, НЕ карточка целиком (раздел 4.1 явно
    просит не раздувать плотный первый экран) + компактный чек-лист из уже существующих фактов
    рядом с ней (раздел 4.3, perplexity-источник) -- задача снять тревогу за первые секунды
    просмотра, не сообщить что-то новое. В самом начале ЛЮБОГО отчёта (все level), включая
    заглушку (см. generate_placeholder_report())."""
    items = [
        "Файлы не удалялись.",
        "Оригиналы сохранены на своих местах.",
        "Ошибки чтения показаны отдельно, не смешаны с остальным.",
    ]
    if level != "target":
        items.append("Реальных изменений на диске нет — это предпросмотр.")
    li = "".join(f"<li>{html.escape(t)}</li>" for t in items)
    return (
        '<p class="trust-banner">Оригиналы не изменены и не удалены — программа работает '
        'только с копиями.</p>'
        f'<ul class="trust-list">{li}</ul>'
    )


def _render_this_run(run_stats: dict, level: str = "target") -> str:
    """Секция "Пополнение архива"/"Пробный прогон" -- в отличие от остального отчёта
    (кумулятивная история архива из CSV-логов, см. _render_sheet1/build_model_from_rows), эти
    цифры -- только то, что сделал ИМЕННО ЭТОТ вызов программы. `run_stats` -- сумма
    RunResult.stats по всем SOURCE одного вызова (см. photosort_win.py:
    _bare_launch_run_build/_bare_launch_run_dryrun/_main), тот же словарь, что уже питает
    консольный build_final_summary() -- report.py не импортирует photosort_win (граница
    модуля, см. докстринг модуля), поэтому просто читает переданный dict по известным
    ключам, никакой новой агрегации/бизнес-логики здесь нет.

    level!="target" (CLI --dry-run/интерактивный [2], 2026-07-20, третий заход) -- ничего
    реально не записано на диск (place_file() пропущен), заголовок/формулировки меняются на
    гипотетические ("было бы", не "было"), чтобы не выдавать предпросмотр за факт.

    None/{} -- вызывающий код не передал этот параметр (старые вызовы) -- секция не
    рендерится вообще, не пустая карточка."""
    if not run_stats:
        return ""

    n_appended_images = run_stats.get("appended_images", 0)
    n_appended_videos = run_stats.get("appended_videos", 0)
    # 2026-07-26, по просьбе пользователя (согласовано явно): RAW теперь входит в "итого новых
    # файлов" -- raw_mirrored раньше нигде не показывался в этой секции вообще, хотя стат
    # существовал с самого начала (свой отдельный счётчик, не появлялся ни в одном .stat).
    n_appended_raw = run_stats.get("raw_mirrored", 0)
    n_new_total = n_appended_images + n_appended_videos + n_appended_raw
    n_near_dup = (run_stats.get("appended_near_dup", 0) + run_stats.get("appended_better", 0)
                  + run_stats.get("appended_crop", 0))
    n_skipped = run_stats.get("skipped_present", 0)
    n_disputed = run_stats.get("disputed", 0)
    n_unreadable = run_stats.get("unreadable_count", 0)
    # 4.2 (PROMPT_report_marketing.md): триада исхода -- остановка по нехватке места ДОЛЖНА
    # быть видна, даже если по совпадению ничего не успело дописаться (n_new_total==0) --
    # без него в "any(...)" ниже такой прогон молча ушёл бы в пустую секцию, а отчёт выглядел
    # бы неотличимо от "SOURCE был пуст".
    stopped_for_space = bool(run_stats.get("stopped_for_space"))

    if not any((n_new_total, n_skipped, n_disputed, n_unreadable, stopped_for_space)):
        return ""  # SOURCE был пуст/всё уже было в архиве -- нет смысла в пустой секции

    preview = level != "target"
    heading = "Пробный прогон" if preview else "Пополнение архива"
    intro = (
        "Показывает, что произошло бы, если бы это была настоящая сборка — реальных "
        "изменений на диске нет, ничего не скопировано."
        if preview else
        "Только то, что сделал именно этот запуск программы — весь остальной отчёт ниже "
        "про архив целиком, за всё время."
    )
    # "новых файлов", не "фото и видео" -- RAW теперь тоже входит в n_new_total (см. выше).
    added_label = "новых файлов было бы добавлено" if preview else "новых файлов добавлено"
    saved_label = "было бы сэкономлено на дублях" if preview else "сэкономлено на дублях в этот раз"

    new_files_breakdown = _type_breakdown_caption(
        Counter({"image": n_appended_images, "raw": n_appended_raw, "video": n_appended_videos}))
    breakdown_html = f'<div class="breakdown">{new_files_breakdown}</div>' if new_files_breakdown else ""
    stats_html = [f'<div class="stat"><div class="value">{n_new_total}</div>'
                  f'<div class="label">{added_label}</div>{breakdown_html}</div>']
    # Пакет п.2 (SESSION-HANDOFF.txt): объём этого прогона -- есть в run_stats с самого
    # появления секции, просто не рендерился нигде (REPORT_STRUCTURE.md, "известные пробелы").
    bytes_appended = run_stats.get("bytes_appended", 0)
    if bytes_appended:
        appended_label = "было бы добавлено в архив" if preview else "добавлено в архив"
        stats_html.append(f'<div class="stat"><div class="value">{_fmt_bytes(bytes_appended)}</div>'
                           f'<div class="label">{appended_label}</div></div>')
    bytes_saved = run_stats.get("bytes_saved_by_dedup", 0)
    if bytes_saved:
        stats_html.append(f'<div class="stat"><div class="value">{_fmt_bytes(bytes_saved)}</div>'
                           f'<div class="label">{saved_label}</div></div>')
    archives_extracted = run_stats.get("archives_extracted", 0)
    if archives_extracted:
        stats_html.append(
            f'<div class="stat"><div class="value">{archives_extracted}</div>'
            f'<div class="label">{_plural(archives_extracted, "архив распакован", "архива распаковано", "архивов распаковано")}</div></div>'
        )
    # free_disk_bytes -- ТОЛЬКО [2] (_bare_launch_run_dryrun() кладёт его в merged перед
    # вызовом generate_report(), см. photosort_win.py) -- прогноз именно на момент пробного
    # прогона, report.py сам ничего не пересчитывает, просто читает переданное число.
    free_disk_bytes = run_stats.get("free_disk_bytes", 0)
    if free_disk_bytes:
        stats_html.append(f'<div class="stat"><div class="value">{_fmt_bytes(free_disk_bytes)}</div>'
                           f'<div class="label">свободно на диске сейчас</div></div>')
    undated = run_stats.get("undated", 0)
    if undated:
        undated_label = ("не удалось бы распознать дату" if preview else
                          "не удалось распознать дату")
        stats_html.append(f'<div class="stat"><div class="value">{undated}</div>'
                           f'<div class="label">{undated_label}</div></div>')
    # REVIEW-HANDOFF.md, Раунд 32, задача 4: "всего найдено на источнике" -- база для сверки
    # (нет способа проверить, что программа ничего не пропустила молча, ни с чем не сравнить
    # "обработано"). processed_count считает КАЖДЫЙ элемент, дошедший до пайплайна (успешный
    # или нет), независимо от исхода -- сумма всех категорий ниже (appended/skipped/near_dup/
    # unreadable/disputed/raw_*/rejected_noise) должна совпадать с этим числом.
    processed_total = run_stats.get("processed_count", 0)
    if processed_total:
        found_label = "нашлось бы на источнике" if preview else "найдено на источнике"
        stats_html.append(f'<div class="stat"><div class="value">{processed_total}</div>'
                           f'<div class="label">{found_label}</div></div>')

    parts = [
        '<div class="card">', f"<h2>{html.escape(heading)}</h2>",
        f'<p class="muted">{intro}</p>',
    ]
    if stopped_for_space:
        # 4.2 (PROMPT_report_marketing.md): "успех с оговоркой", не тревога -- та же формулировка
        # по смыслу, что уже честно говорит консоль (photosort_win.py, ОСТАНОВКА: недостаточно
        # места), просто без слов "ошибка"/"критично" (раздел 2/6 этого же ТЗ). EXIT_INSUFFICIENT_SPACE
        # реален и уже обрабатывается -- report.py раньше нигде не читал этот флаг, отчёт после
        # частичной остановки выглядел точно так же, как отчёт после полного успеха.
        parts.append(
            '<p><b>Почти всё разложено.</b> Не хватило места на диске назначения — программа '
            'остановилась, ничего не испортив. Освободите место и запустите ещё раз — '
            'продолжится с того же места.</p>'
        )
    listdir_failed = run_stats.get("listdir_failed_count", 0)
    if listdir_failed:
        # REVIEW-HANDOFF.md, Раунд 32, задача 4: прямой сигнал (не просто число для ручной
        # сверки) -- хотя бы одна папка не прочиталась вообще (права доступа/длинный путь/
        # повреждённая ФС), её содержимое не попало ни в один список ниже.
        parts.append(
            f'<p class="muted">{listdir_failed} '
            f'{_plural(listdir_failed, "папку", "папки", "папок")} '
            'не удалось прочитать при обходе источника — их содержимое не попало ни в один '
            'из списков ниже. Подробности — в actions.log.</p>'
        )
    parts.append('<div class="stat-row">')
    parts += stats_html + ["</div>"]

    segments = [
        ("Новые файлы", max(n_new_total - n_near_dup, 0), CATEGORY_PALETTE[0]),
        ("Точные повторы", n_skipped, CATEGORY_PALETTE[1]),
        ("Похожие кадры сохранены", n_near_dup, CATEGORY_PALETTE[2]),
        ("Не прочитано", n_unreadable, CATEGORY_PALETTE[3]),
        ("Спорные", n_disputed, CATEGORY_PALETTE[4]),
    ]
    svg, legend = _svg_pie(segments)
    if svg:
        parts.append(f'<div class="chart-block">{svg}<div class="legend">{legend}</div></div>')

    # 2026-07-26, по просьбе пользователя: разбивка по типу файла для каждой категории этой
    # диаграммы, не только для "Новых файлов" выше (у которой уже есть своя подпись в тайле).
    # near_dup -- только фото/видео (decide() никогда не возвращает near-dup-семейство для raw,
    # см. photosort_win.py:_process_record()), остальные три -- фото/RAW/видео/прочее.
    breakdown_captions = [c for c in (
        _type_breakdown_caption(Counter({
            "image": run_stats.get("skipped_present_image", 0),
            "raw": run_stats.get("skipped_present_raw", 0),
            "video": run_stats.get("skipped_present_video", 0),
            "other": run_stats.get("skipped_present_other", 0),
        }), "Точные повторы"),
        _type_breakdown_caption(Counter({
            "image": run_stats.get("near_dup_image", 0),
            "video": run_stats.get("near_dup_video", 0),
        }), "Похожие кадры"),
        _type_breakdown_caption(Counter({
            "image": run_stats.get("unreadable_count_image", 0),
            "raw": run_stats.get("unreadable_count_raw", 0),
            "video": run_stats.get("unreadable_count_video", 0),
            "other": run_stats.get("unreadable_count_other", 0),
        }), "Не прочитано"),
        _type_breakdown_caption(Counter({
            "image": run_stats.get("disputed_image", 0),
            "raw": run_stats.get("disputed_raw", 0),
            "video": run_stats.get("disputed_video", 0),
            "other": run_stats.get("disputed_other", 0),
        }), "Спорные"),
    ) if c]
    if breakdown_captions:
        parts.append('<p class="muted">' + '<br>'.join(html.escape(c) for c in breakdown_captions) + '</p>')

    # Куда И откуда -- album_merge_events это пары (альбом, prefix), prefix -- реальный путь
    # от корня SOURCE до места, откуда пришли файлы (см. photosort_win.py:find_album()/
    # _note_album_source()). Раньше показывалось только имя альбома (только "куда"), prefix
    # отбрасывался -- по прямой просьбе пользователя 2026-07-20 показываем оба конца.
    merge_events = run_stats.get("album_merge_events") or []
    if merge_events:
        merge_heading = "Альбомы бы пополнились из нескольких мест:" if preview else "Альбомы пополнились из нескольких мест:"
        by_album = {}
        for album, prefix in merge_events:
            by_album.setdefault(album, set()).add(prefix)
        parts.append(f'<p><b>{merge_heading}</b></p>')
        for album in sorted(by_album)[:TOP_N]:
            sources = "; ".join(html.escape(p) for p in sorted(by_album[album]))
            parts.append(f'<p class="muted">«{html.escape(album)}» ← {sources}</p>')

    # "Альбом умер" -- источник целиком совпал с уже существующим содержимым архива: столько
    # файлов встретилось (source_album_seen), сколько реально дописалось (source_album_appended,
    # 0 -- полностью дубль). См. photosort_win.py:_process_record() -- оба словаря собираются
    # по find_album() над item.rel_path, независимо от исхода (appended/skipped), суммируются
    # по всем SOURCE через _sum_stats(). Без этого узнать такое можно было только из logs\.
    seen = run_stats.get("source_album_seen") or {}
    appended_by_album = run_stats.get("source_album_appended") or {}
    fully_duplicate = sorted(a for a, n in seen.items() if n > 0 and not appended_by_album.get(a))
    if fully_duplicate:
        names = ", ".join(f"«{html.escape(a)}»" for a in fully_duplicate[:TOP_N])
        parts.append(f'<p><b>Уже было в архиве:</b> {names} — всё содержимое совпало с уже '
                      f'существующими файлами, новых файлов не добавилось.</p>')

    parts.append("</div>")
    return "".join(parts)


_YEAR_GAP_MIN_SPAN = 5  # 4.5: короче нескольких лет истории -- "провал" неотличим от шума
_YEAR_GAP_MIN_NEIGHBOR_AVG = 5  # соседи сами малочисленны -- честно не с чем сравнивать
_YEAR_GAP_MIN_RATIO = 10  # "на порядок меньше соседних", консервативный порог по тексту ТЗ


def _find_year_gap(years: Counter):
    """4.5 (PROMPT_report_marketing.md, лучшая находка gigachat-источника): мягкое,
    консервативное наблюдение -- ищет ОДИН самый заметный провал (год с числом файлов на
    порядок меньше среднего соседних лет), не более одного (раздел 4.5 явно просит не более
    одного наблюдения) и только если истории достаточно, чтобы провал не был шумом на
    маленьком архиве. Возвращает год-кандидат или None. Пороги -- консервативные по духу ТЗ
    ("показывать только явные, бесспорные провалы"), не точная наука -- намеренно оставлены
    константами выше, чтобы подстроить после того, как разработчик увидит реальные архивы
    (раздел 4.5 самого ТЗ прямо разрешает донастройку без нового согласования)."""
    if not years:
        return None
    all_years = sorted(years)
    span = all_years[-1] - all_years[0] + 1
    if span < _YEAR_GAP_MIN_SPAN:
        return None
    best_ratio, best_year = 0, None
    for y in range(all_years[0] + 1, all_years[-1]):
        neighbor_avg = (years.get(y - 1, 0) + years.get(y + 1, 0)) / 2
        if neighbor_avg < _YEAR_GAP_MIN_NEIGHBOR_AVG:
            continue  # соседи и сами малочисленны -- честно не с чем сравнивать
        this_n = years.get(y, 0)
        ratio = neighbor_avg / max(this_n, 1)
        if ratio >= _YEAR_GAP_MIN_RATIO and ratio > best_ratio:
            best_ratio, best_year = ratio, y
    return best_year


def _render_sheet2(model: dict) -> str:
    parts = ['<div class="card">', "<h2>Медиафайлы по годам</h2>"]
    years_svg = _svg_bar_chart(model["years"])
    if years_svg:
        parts.append(years_svg)
    else:
        parts.append('<p class="muted">Недостаточно данных для графика.</p>')
    gap_year = _find_year_gap(model["years"])
    if gap_year is not None:
        # Тон -- нейтральное наблюдение с намёком, НЕ утверждение и НЕ "проблема архива"
        # (раздел 6 ТЗ, антипримеры) -- та же дисциплина тона, что уже принята для
        # unreadable.csv/disputes.csv в остальном отчёте.
        parts.append(
            f'<p class="muted">Похоже, за {gap_year} год сохранилось заметно меньше снимков, '
            f'чем за соседние годы — возможно, часть этих воспоминаний ждёт на другом диске '
            f'или карте памяти.</p>'
        )
    parts.append("</div>")

    # 2026-07-26, по просьбе пользователя: "Точные повторы" на диаграмме "Итог решений
    # программы" не показывали разбивку по типу файла -- см. model["skipped_present_by_type"]
    # (build_model_from_rows(), классификация matched_with тем же _media_kind(), что и "Тип
    # медиа"/"Объём по категориям" выше). Подпись только ненулевых категорий, "" если считать
    # нечего (analyze-уровень: build_model_from_analyze_stats() не строит эту разбивку вообще,
    # .get() выше по функции даёт пустой Counter).
    dup_type_caption = _type_breakdown_caption(
        model.get("skipped_present_by_type", Counter()), "Точные повторы")

    pie_charts = [
        ("Тип медиа", [
            ("Фото", model["counts"]["image"], CATEGORY_PALETTE[0]),
            ("Видео", model["counts"]["video"], CATEGORY_PALETTE[1]),
            ("RAW", model["counts"]["raw"], CATEGORY_PALETTE[2]),
        ], _n_files, ""),
        # Байты, не штуки -- та же тройка категорий, что "Тип медиа" выше, но по занятому
        # месту: видео обычно куда тяжелее по ГБ, чем по числу файлов, само по себе
        # интересное сравнение с первой диаграммой -- поэтому сразу следом, не через другие
        # темы (решение пользователя 2026-07-20, третий заход). Раньше был отдельной
        # карточкой-таблицей ("Объём по категориям", просто 3 числа) -- решение пользователя
        # 2026-07-20 (второй заход): диаграммой выглядит пропорциональнее (не пустует
        # полкарточки) и встаёт в общий ряд секторов, а не отдельной парой с "Топ альбомов"
        # (та пара ужимала hbar-график до нечитаемого).
        ("Объём по категориям", [
            ("Фото", model["bytes_by_kind"]["image"], CATEGORY_PALETTE[0]),
            ("Видео", model["bytes_by_kind"]["video"], CATEGORY_PALETTE[1]),
            ("RAW", model["bytes_by_kind"]["raw"], CATEGORY_PALETTE[2]),
        ], _fmt_bytes, ""),
        ("Итог решений программы", [
            ("Новые файлы", model["decisions"]["appended"], CATEGORY_PALETTE[0]),
            ("Точные повторы", model["decisions"]["skipped_present"], CATEGORY_PALETTE[1]),
            ("Похожие кадры сохранены", model["decisions"]["near_dup"], CATEGORY_PALETTE[2]),
            ("Не прочитано", model["decisions"]["unreadable"], CATEGORY_PALETTE[3]),
            ("Спорные", model["decisions"]["disputed"], CATEGORY_PALETTE[4]),
        ], _n_files, dup_type_caption),
        # REVIEW-HANDOFF.md, Раунд 32, задача 1: RAW-файлы не участвуют в tier-расчёте
        # (dated_media_count = counts["image"]+counts["video"] выше, tier для raw_mirrored
        # реально вычисляется в photosort_win.py, но не персистируется -- осознанное
        # ограничение, не баг) -- заголовок явно называет, что диаграмма не про весь архив,
        # а не молчит о 25% файлов, отсутствующих из знаменателя.
        ("Надёжность дат — фото и видео, без RAW", [
            ("Точная (EXIF)", model["tier_counts"].get("A", 0), CATEGORY_PALETTE[0]),
            ("Высокая", model["tier_counts"].get("B", 0), CATEGORY_PALETTE[1]),
            ("Оценочная", model["tier_counts"].get("C", 0), CATEGORY_PALETTE[2]),
            ("Низкая", model["tier_counts"].get("D", 0), CATEGORY_PALETTE[3]),
        ], _n_files, ""),
        # Флаг из appended.csv (small_image/low_confidence_photo, см. RunLogs.appended()) --
        # раньше нигде не визуализировался, был доступен только тем, кто откроет сам CSV.
        # 2026-07-20, по запросу пользователя: отчёт должен закрывать это без похода в logs\.
        ("Качество кадров", [
            ("Обычные", model["quality_flags"].get("", 0), CATEGORY_PALETTE[0]),
            ("Маленькие фото", model["quality_flags"].get("small_image", 0), CATEGORY_PALETTE[1]),
            ("Низкая уверенность", model["quality_flags"].get("low_confidence_photo", 0), CATEGORY_PALETTE[3]),
        ], _n_files, ""),
    ]

    # География -- топ-5 мест + "остальные" одним сектором (иначе десяток тонких клиньев не
    # читается); те же места уже показаны как теги-плашки в "Ваш архив", здесь -- с числами и
    # долями, не просто список имён.
    if model["cities"]:
        top_cities = model["cities"].most_common(5)
        rest = sum(model["cities"].values()) - sum(v for _, v in top_cities)
        geo_segments = [(name, v, CATEGORY_PALETTE[i % len(CATEGORY_PALETTE)])
                        for i, (name, v) in enumerate(top_cities)]
        if rest > 0:
            geo_segments.append(("Остальные места", rest, COLOR_LINE))
        pie_charts.append(("География", geo_segments, _n_files, ""))

    # Раскладка по типу подачи, не просто "рядом чтобы компактно" (решение пользователя
    # 2026-07-20): круговые диаграммы -- у всех подпись сбоку (circle+legend, одна визуальная
    # мелодия) -- своя группа (grid-3, авто-число колонок), включая "Объём по категориям"
    # теперь тоже сектор. "Топ альбомов" -- никакой легенды нет вообще, цифры прямо у
    # полосы -- другая мелодия, полная ширина отдельно (см. её же комментарий ниже).
    pie_cells = []
    for title, segments, value_fmt, caption in pie_charts:
        svg, legend = _svg_pie(segments, value_fmt=value_fmt)
        if not svg:
            continue
        caption_html = f'<p class="muted">{html.escape(caption)}</p>' if caption else ""
        pie_cells.append(
            f'<div class="card"><h2>{html.escape(title)}</h2>'
            f'<div class="chart-block">{svg}<div class="legend">{legend}</div></div>'
            f'{caption_html}</div>'
        )
    if pie_cells:
        parts.append(f'<div class="grid-3">{"".join(pie_cells)}</div>')

    # Полная ширина, не пара с чем-либо в grid-2 -- пробовали пару с "Объём по категориям"
    # (2026-07-20), текст съёживался вдвое вместе с шириной колонки (viewBox фиксирован под
    # ~680px) и переставал читаться. hbar-графику ширина нужна по-настоящему, не для симметрии.
    if model["top_albums"]:
        # 4.4 (PROMPT_report_marketing.md): объясняет ПОЧЕМУ файл оказался в альбоме, а не в
        # папке по дате -- данные для этого уже есть (_parse_album/_parse_bydate_segment
        # выше), просто не были проговорены текстом до этого раздела ТЗ.
        hbar = _svg_hbar_chart([(name, b, _fmt_bytes(b)) for name, b in model["top_albums"]])
        parts.append(
            '<div class="card"><h2>Топ альбомов по размеру</h2>'
            '<p class="muted">Где у исходных папок были понятные названия — они сохранены '
            'как альбомы. Остальное разложено по датам съёмки.</p>'
            f'{hbar}</div>'
        )

    return "".join(parts)


def _folder_label(path: str) -> str:
    """_win_dirname() файла прямо в корне SOURCE даёт "" -- _win_basename("") тоже
    "", без этого получалась бы пустая метка перед счётчиком ("  (2)")."""
    return _win_basename(path) or path or "корень источника"


def _dispute_checklist_item(group: tuple) -> tuple:
    folder, items = group
    labels = [f"{html.escape(name)} ({html.escape(_dispute_reason_label(reason))})"
              for name, reason in items[:5]]
    more = f" и ещё {len(items) - 5} {_plural(len(items) - 5, 'файл', 'файла', 'файлов')}" if len(items) > 5 else ""
    folder_line = f"Папка: {html.escape(_folder_label(folder))}." if folder else ""
    action_line = f"Лежат в _Unsorted: {', '.join(labels)}{more}."
    detail = f"{folder_line}<br>{action_line}" if folder_line else action_line
    return f"{_n_files(len(items))} не удалось однозначно распознать", detail


_DATE_TIER_LABELS = {"B": "высокая уверенность", "C": "оценочная"}


def _dates_review_checklist_item(group: tuple) -> tuple:
    """2026-07-26: тот же паттерн, что _dispute_checklist_item() -- имя файла + короткая
    метка (здесь -- уровень достоверности Tier B/C, те же слова, что уже использует диаграмма
    "Надёжность дат" -- "Высокая"/"Оценочная", не изобретать новую терминологию)."""
    folder, items = group
    labels = [f"{html.escape(name)} ({html.escape(_DATE_TIER_LABELS.get(tier, tier))})"
              for name, tier in items[:5]]
    more = f" и ещё {len(items) - 5} {_plural(len(items) - 5, 'файл', 'файла', 'файлов')}" if len(items) > 5 else ""
    folder_line = f"Папка: {html.escape(folder)}." if folder else ""
    action_line = f"Стоит перепроверить при желании: {', '.join(labels)}{more}."
    detail = f"{folder_line}<br>{action_line}" if folder_line else action_line
    return f"{_n_files(len(items))} получили дату приблизительно", detail


def _friendly_target_dir(dest: str) -> str:
    """Путь к папке на TARGET начиная с ByDate/Albums, без диска и корня архива -- тот
    префикс аудитория (RULES.md/PROMPT_archive_report.md: 45-70 лет, нетехническая) и так
    знает (это папка, куда она сама указала программу), лишний абсолютный Windows-путь
    только пугает длиной. Пустая строка, если маркер не нашёлся (нестандартная раскладка)."""
    parts = dest.split("\\")
    for marker in ("ByDate", "Albums"):
        if marker in parts:
            idx = parts.index(marker)
            return "\\".join(parts[idx:-1])
    return ""


CHECKLIST_PREVIEW_N = 2  # решение пользователя 2026-07-20: не топ-N-и-см.CSV (пользователь
# отчёт открывает вместо логов, а не в дополнение к ним), а показать ВСЁ, но не одним
# полотном -- превью + сворачиваемый <details> на категорию (см. _render_checklist_card()).


def _li(title: str, detail: str) -> str:
    return f'<li><div class="title">{html.escape(title)}</div><div class="detail">{detail}</div></li>'


def _cluster_checklist_item(cluster: list) -> tuple:
    names = [_win_basename(p) for p in cluster[:5]]
    more = f" и ещё {len(cluster) - 5}" if len(cluster) > 5 else ""
    dirs = {_win_dirname(p) for p in cluster}
    # Кластер почти всегда лежит в одной папке (near-dup совпал с уже размещённым соседом по
    # своей же дате/месту) -- один путь один раз, не на каждое имя файла. Разные папки --
    # редкий случай (даты разошлись по краю месяца/при рубеже bydate_granularity) -- тогда
    # путь при каждом имени.
    if len(dirs) == 1:
        folder = _friendly_target_dir(cluster[0])
        folder_line = f"Папка: {html.escape(folder)}." if folder else ""
        files = ", ".join(html.escape(n) for n in names)
    else:
        folder_line = ""
        files = ", ".join(
            html.escape((_friendly_target_dir(p) + "\\" if _friendly_target_dir(p) else "") + n)
            for p, n in zip(cluster[:5], names, strict=True)
        )
    action_line = "Стоит вручную выбрать лучший: " + files + more
    # Папка и список файлов -- две разные мысли (где искать / что сравнить), раздельные
    # строки читаются, склеенные в одну через точку -- нет.
    detail = f"{folder_line}<br>{action_line}" if folder_line else action_line
    return f"Похожая серия из {len(cluster)} кадров", detail


def _build_checklist_items(fields: dict) -> list:
    """Строит список готовых <li>...</li> Листа 3 из полей _build_checklist_fields() --
    вынесено отдельно от рендера 2026-07-20, чтобы вызывать на "новом" и "старом"
    подмножестве раздельно (см. _generate_from_model()). Каждая категория с несколькими
    находками (сейчас только near-dup-серии) сворачивается независимо от других -- превью
    CHECKLIST_PREVIEW_N + <details> на оставшееся, БЕЗ отсылки к CSV (пользователь отчёт
    открывает вместо логов -- решение пользователя 2026-07-20)."""
    items = []

    clusters = fields["near_dup_clusters"]
    if clusters:
        # REVIEW-HANDOFF.md, Раунд 32, задача 5: все серии подписаны "стоит вручную выбрать
        # лучший" -- нигде не сказано, что это необязательно (все кадры уже физически
        # сохранены). Одна фраза-разграничитель на весь блок, не на каждую серию отдельно --
        # 622 повтора одной и той же оговорки читались бы как спам.
        items.append(_li(
            "Похожие кадры — выбор необязателен",
            "Все кадры уже сохранены, ничего не сломается, если оставить как есть — "
            "разбор ниже просто для удобства, не для того, чтобы что-то доделать.",
        ))
        cluster_lis = [_li(*_cluster_checklist_item(c)) for c in clusters]
        items.extend(cluster_lis[:CHECKLIST_PREVIEW_N])
        rest = cluster_lis[CHECKLIST_PREVIEW_N:]
        if rest:
            n = len(rest)
            label = f"Показать ещё {n} {_plural(n, 'похожую серию', 'похожие серии', 'похожих серий')}"
            items.append(
                f'<li class="expand"><details><summary>{html.escape(label)}</summary>'
                f'<ul class="checklist nested">{"".join(rest)}</ul></details></li>'
            )

    if fields["disputes_total"]:
        dispute_groups = fields.get("disputes_detail", [])
        if dispute_groups:
            # Раунд 32, задача 2 (REVIEW-HANDOFF.md): имя файла + причина спора, не только
            # число на папку -- тот же <details>/"Показать ещё" паттерн, что near-dup выше.
            dispute_lis = [_li(*_dispute_checklist_item(g)) for g in dispute_groups]
            items.extend(dispute_lis[:CHECKLIST_PREVIEW_N])
            rest = dispute_lis[CHECKLIST_PREVIEW_N:]
            if rest:
                n = len(rest)
                label = f"Показать ещё {n} {_plural(n, 'папку', 'папки', 'папок')}"
                items.append(
                    f'<li class="expand"><details><summary>{html.escape(label)}</summary>'
                    f'<ul class="checklist nested">{"".join(rest)}</ul></details></li>'
                )
        else:
            # analyze-уровень (build_model_from_analyze_stats) не отслеживает source/reason на
            # файл -- только итоговое число; TARGET/dry-run уровень отслеживает (см.
            # build_model_from_rows()/_cluster_disputes()).
            folders = fields["disputes_by_folder"].most_common(TOP_N)
            folder_detail = "; ".join(f"{html.escape(_folder_label(f))} ({n})" for f, n in folders)
            # "где искать" и "какие папки-источники" -- две разные мысли, отдельные строки.
            detail = "Лежат в _Unsorted."
            if folder_detail:
                detail += f"<br>Сгруппированы по исходной папке: {folder_detail}."
            items.append(_li(f"{_n_files(fields['disputes_total'])} не удалось однозначно распознать", detail))

    if fields["dates_review_bc_total"]:
        review_groups = fields.get("dates_review_detail", [])
        if review_groups:
            # 2026-07-26: имя файла + уровень достоверности, не только счётчик по папке --
            # тот же <details>/"Показать ещё" паттерн, что disputes/near-dup выше.
            review_lis = [_li(*_dates_review_checklist_item(g)) for g in review_groups]
            items.extend(review_lis[:CHECKLIST_PREVIEW_N])
            rest = review_lis[CHECKLIST_PREVIEW_N:]
            if rest:
                n = len(rest)
                label = f"Показать ещё {n} {_plural(n, 'папку', 'папки', 'папок')}"
                items.append(
                    f'<li class="expand"><details><summary>{html.escape(label)}</summary>'
                    f'<ul class="checklist nested">{"".join(rest)}</ul></details></li>'
                )
        else:
            # analyze-уровень (build_model_from_analyze_stats) не отслеживает source/dest на
            # файл -- только итоговое число, тот же асимметричный охват, что у disputes выше.
            folders = fields["dates_review_by_folder"].most_common(TOP_N)
            folder_detail = "; ".join(f"{html.escape(_folder_label(f))} ({n})" for f, n in folders)
            detail = "Стоит перепроверить при желании."
            if folder_detail:
                detail += f"<br>Папки-источники: {folder_detail}."
            items.append(_li(f"{_n_files(fields['dates_review_bc_total'])} получили дату приблизительно", detail))

    if fields["undated_total"]:
        # Tier D -- дата отсутствует вообще (ни EXIF, ни имя файла, ни соседи по папке), не
        # путать с Tier B/C выше ("дата есть, но приблизительная") -- разные находки.
        # 2026-07-26: путь+имя на каждый файл, не просто число -- их всегда мало (Tier D --
        # редкий случай), сворачивание "и ещё N" здесь не нужно. analyze-уровень не
        # отслеживает undated_media поштучно (AnalyzeStats только агрегат) -- .get()
        # деградирует до старого текста без списка, не падает.
        undated_rows = fields.get("undated_media", [])
        names = []
        for row in undated_rows:
            dest = row.get("dest", "")
            if not dest:
                continue
            name = html.escape(_win_basename(dest))
            folder = _friendly_target_dir(dest)
            names.append(f"{html.escape(folder)}\\{name}" if folder else name)
        detail = ("Дата не определилась ни по EXIF, ни по имени файла, ни по соседям в папке — "
                   "стоит проставить вручную при желании.")
        if names:
            detail += "<br>Где искать: " + ", ".join(names) + "."
        items.append(_li(f"{_n_files(fields['undated_total'])} вообще без даты", detail))

    small = fields["quality_flags"].get("small_image", 0)
    low_conf = fields["quality_flags"].get("low_confidence_photo", 0)
    if small or low_conf:
        parts = []
        if small:
            parts.append(f"{_n_files(small)} маленького размера — возможно, скриншоты или миниатюры")
        if low_conf:
            parts.append(f"{_n_files(low_conf)} с низкой уверенностью распознавания")
        items.append(_li(f"{_n_files(small + low_conf)} стоит проверить на качество", "; ".join(parts) + "."))

    if fields["unreadable"]:
        items.append(_li(
            f"{_n_files(len(fields['unreadable']))} не прочитано",
            "Обычно помогает закрыть программу, которая могла держать файл открытым, и "
            "запустить тот же прогон ещё раз (см. FAQ).",
        ))

    return items


def _render_checklist_card(heading: str, items: list, intro: str = "") -> str:
    if not items:
        return ""
    intro_html = f'<p class="muted">{html.escape(intro)}</p>' if intro else ""
    return (f'<div class="card"><h2>{html.escape(heading)}</h2>{intro_html}'
            f'<ul class="checklist">{"".join(items)}</ul></div>')


def _exact_dup_checklist_item(group: tuple) -> tuple:
    folder, total, items = group

    def _label(matched, count):
        name = html.escape(_win_basename(matched))
        return f"{name} (×{count})" if count > 1 else name

    labels = [_label(m, c) for m, c in items[:5]]
    more = f" и ещё {len(items) - 5} {_plural(len(items) - 5, 'файл', 'файла', 'файлов')}" if len(items) > 5 else ""
    files = ", ".join(labels)
    folder_line = f"Папка: {html.escape(folder)}." if folder else ""
    action_line = f"Уже в архиве: {files}{more}."
    detail = f"{folder_line}<br>{action_line}" if folder_line else action_line
    return f"{_n_files(total)} — точные повторы файлов из этой папки", detail


EXACT_DUP_PREVIEW_N = 2  # тот же порядок превью, что CHECKLIST_PREVIEW_N.
EXACT_DUP_INTRO = "Ничего делать не нужно — показано для тех, кто хочет убедиться сам."


def _build_exact_dup_items(fields: dict) -> list:
    """REVIEW-HANDOFF.md, Раунд 31: паттерн прогрессивного раскрытия -- буквально тот же
    приём, что _build_checklist_items() использует для near_dup_clusters, но отдельная
    функция: тон здесь другой (см. _render_exact_dup_examples()) и категория живёт вне Листа 3
    (не "стоит проверить" -- действие не требуется вообще, см. докстринг _cluster_exact_dup())."""
    groups = fields.get("exact_dup_groups", [])
    if not groups:
        return []
    lis = [_li(*_exact_dup_checklist_item(g)) for g in groups]
    items = lis[:EXACT_DUP_PREVIEW_N]
    rest = lis[EXACT_DUP_PREVIEW_N:]
    if rest:
        n = len(rest)
        label = f"Показать ещё {n} {_plural(n, 'группу', 'группы', 'групп')}"
        items.append(
            f'<li class="expand"><details><summary>{html.escape(label)}</summary>'
            f'<ul class="checklist nested">{"".join(rest)}</ul></details></li>'
        )
    return items


def _render_exact_dup_examples(fields: dict, heading: str, intro: str = "",
                                verify_link: str = None) -> str:
    """Отдельная от _render_recommendations() карточка -- та же механика
    (_render_checklist_card, <details>/"Показать ещё"), но НЕ в "Что стоит проверить": тон
    здесь "ничего делать не нужно, показано для тех, кто хочет убедиться сам", не "стоит
    проверить/исправить". fields -- checklist_new/checklist_before/model, любой dict с ключом
    "exact_dup_groups" (см. _build_checklist_fields()).

    verify_link (2026-07-26): ссылка на отдельную страницу "Полная сверка дублей" (см.
    generate_dedup_verification_page()) -- сразу ПОД этой же карточкой, не в хвосте всей
    страницы отчёта (живая находка пользователя: раньше ссылка была в конце body, физически
    оторвана от карточки "Точные повторы — примеры", к которой относится по смыслу -- "почему
    это примеры, если рядом полная информация" читалось необъяснимо без видимой связи)."""
    if fields is None:
        return ""
    card = _render_checklist_card(heading, _build_exact_dup_items(fields), intro=intro)
    if card and verify_link:
        card += (
            '<p class="muted">Показаны только первые несколько — '
            f'<a href="{html.escape(verify_link)}">полная сверка построчно, по каждому файлу →</a>.</p>'
        )
    return card


DEDUP_VERIFICATION_FILENAME = "dedup_verification.html"


def _render_dedup_verification_page(data: dict) -> str:
    """2026-07-26: тело отдельной страницы "Полная сверка дублей" -- построчно, без
    сворачивания "и ещё N" (в отличие от _render_exact_dup_examples()/Листа 3, здесь весь
    смысл страницы -- ничего не урезать). Группировка по папке архива визуально разделяет
    находки (отдельная карточка на папку, тот же .card/h2, что и везде в отчёте) -- по
    прямой просьбе пользователя не гнать всё сплошным потоком.

    Возвращает "" если группировать нечего (нет точных повторов вообще) -- вызывающая
    сторона (generate_dedup_verification_page()) тогда не пишет файл и не даёт на него
    ссылку из основного отчёта."""
    groups = _cluster_exact_dup_full(data)
    if not groups:
        return ""
    cards = []
    for folder, items in groups:
        rows = []
        for matched, origin, sources in items:
            name = html.escape(_win_basename(matched))
            origin_line = f" — скопировано из {html.escape(origin)}" if origin else ""
            n = len(sources)
            dup_word = _plural(n, "дубль", "дубля", "дублей")
            verb = "отклонён" if n == 1 else "отклонены"
            dup_list = ", ".join(html.escape(s) for s in sources)
            rows.append(
                f'<li><div class="title">{name}</div>'
                f'<div class="detail">В архиве{origin_line}.<br>'
                f'{n} {dup_word} {verb}: {dup_list}.</div></li>'
            )
        cards.append(
            f'<div class="card"><h2>{html.escape(folder or "Корень архива")}</h2>'
            f'<ul class="checklist">{"".join(rows)}</ul></div>'
        )
    total_files = sum(len(items) for _, items in groups)
    total_dups = sum(len(sources) for _, items in groups for _, _, sources in items)
    header = (
        '<div class="card">'
        '<h1>Полная сверка точных повторов</h1>'
        f'<p class="subtitle">{_n_files(total_files)} в архиве имеют хотя бы один точный '
        f'повтор в источнике — {_n_files(total_dups)} отклонено как дубли и не попало в '
        'архив вторично. Список сгруппирован по папкам архива; внутри каждой папки — '
        'файл в архиве, откуда он скопирован, и какие файлы источника оказались его '
        'дублями.</p>'
        # Тот же принцип честности, что уже применяется к _render_found_archive_block()
        # (photosort_win.py:_finalize_target_report -- "данные взяты из служебных файлов,
        # не из повторной проверки диска"): страница строится из CSV-логов, не сканирует
        # файловую систему заново, поэтому явно называет источник этой достоверности.
        '<p class="muted">Построено из служебных файлов логов архива ('
        '<code>__служебные_файлы\\logs</code>), которые не удаляются между прогонами — '
        'при следующем пополнении архива эта страница перегенерируется и пополнится, а не '
        'потеряет уже показанное здесь.</p>'
        '<p class="muted"><a href="report.html">← назад к отчёту</a></p>'
        '</div>'
    )
    return header + "".join(cards)


def generate_dedup_verification_page(data: dict, report_out_path: str,
                                      program_name: str = "PhotoArchive") -> str:
    """Пишет файл-сосед report_out_path (тот же каталог, DEDUP_VERIFICATION_FILENAME) --
    полная построчная сверка "какой файл в архиве откуда, какие файлы источника были его
    дублями" (см. _cluster_exact_dup_full()), для пользователя, который не принимает
    описание алгоритма и хочет проверить дедуп сам в файловой системе (2026-07-26,
    обсуждение с пользователем). Возвращает имя файла (относительный href для ссылки из
    основного отчёта) или None, если точных повторов нет вообще -- тогда ничего не пишется
    и ссылка не появляется."""
    body = _render_dedup_verification_page(data)
    if not body:
        return None
    out_path = os.path.join(os.path.dirname(report_out_path), DEDUP_VERIFICATION_FILENAME)
    _write(out_path, _page_shell(f"{program_name} — сверка дублей", body))
    return DEDUP_VERIFICATION_FILENAME


def _render_sheet3_single(model: dict, level: str) -> str:
    """WORKDIR/analyze/старые вызовы без run_start -- один неразделённый список, ОБЯЗАТЕЛЬНО
    кумулятивный за всю историю архива (для TARGET-уровня с run_start Лист 3 физически
    разнесён на две части отчёта -- см. _render_recommendations()/_generate_from_model()).

    level=="analyze" раньше безусловно дописывал заглушку "рекомендации дорабатываются" --
    убрано 2026-07-24: с реализацией analyze как "2 части" (ROADMAP.md) _build_checklist_items
    уже строит настоящий рабочий чек-лист и для part 1 (сырой скан), и для найденных архивов
    part 2 (_render_found_archive_block), заглушка рядом с реальными находками (near-dup-серии,
    unreadable и т.п.) была не честной, а устаревшей (найдено живым прогоном 2026-07-21,
    подтверждено чтением кода в этой сессии)."""
    items = _build_checklist_items(model)
    return _render_checklist_card("Что стоит проверить", items)


def _render_recommendations(fields: dict, heading: str, intro: str = "") -> str:
    """checklist_new/checklist_before (2026-07-20, второй заход -- по прямой просьбе
    пользователя физически разнести Лист 3 на две части отчёта, а не просто пометить
    заголовками): рекомендации по ЭТОМУ прогону идут сразу после "Пополнение архива" (часть 1
    отчёта), рекомендации, накопившиеся раньше -- в конце, после "Ваш архив"/диаграмм (часть
    2) -- см. _generate_from_model(). None -- соответствующая половина не сформирована
    (например, level=="workdir", туда run_start не передаётся вовсе)."""
    if fields is None:
        return ""
    return _render_checklist_card(heading, _build_checklist_items(fields), intro=intro)


def _render_found_archive_block(root: str, nested_paths: list, program_name: str) -> str:
    """ROADMAP.md, analyze как "2 части", часть 2 -- один блок на найденный архив, построенный
    ТЕМ ЖЕ кодом, что и level="target" (parse_target_logs -> build_model_from_rows ->
    _render_sheet1/_render_sheet2/_render_sheet3_single), без разбивки "новое/накопилось"
    (analyze ничего не пишет — делить по времени нечего)."""
    logs_dir = os.path.join(root, "__служебные_файлы", "logs")
    model = build_model_from_rows(parse_target_logs(logs_dir))

    caveat_seen = (
        "Эти файлы уже учтены в части 1 выше — не дополнительные, просто показано, как "
        "выглядит этот архив в отдельности."
    )
    if nested_paths:
        caveat_stale = (
            "Внутри этого архива обнаружена посторонняя структура (см. пункт ниже) — прямая "
            "улика ручного вмешательства в обход программы, поэтому данные о состоянии архива "
            "в этом случае НЕДОСТОВЕРНЫ, а не просто могут быть неточны."
        )
    else:
        caveat_stale = (
            "Данные взяты из служебных файлов архива (истории прошлых прогонов), не из "
            "повторной проверки текущего состояния диска — если служебные файлы удалили или "
            "архив правили вручную в обход программы, картина может не соответствовать "
            "действительности."
        )

    items = _build_checklist_items(model)
    if nested_paths:
        n = len(nested_paths)
        label = _plural(n, "постороннюю структуру", "посторонние структуры", "посторонних структур")
        names = "; ".join(html.escape(p) for p in nested_paths[:TOP_N])
        items.insert(0, _li(
            f"Обнаружено {n} {label} внутри архива",
            f"Найдено внутри организованной структуры (Albums/ByDate/RAW/_Unsorted): {names}. "
            "Стоит разобрать постороннюю структуру и повторить анализ.",
        ))

    return (
        '<div class="card">'
        f'<h2>Архив {html.escape(program_name)}: {html.escape(root)}</h2>'
        f'<p class="muted">{caveat_seen}</p>'
        f'<p class="muted">{caveat_stale}</p>'
        '</div>'
        + _render_sheet1(model) + _render_sheet2(model)
        + _render_exact_dup_examples(model, "Точные повторы — примеры", intro=EXACT_DUP_INTRO)
        + _render_checklist_card("Что стоит проверить в этом архиве", items)
    )


def _render_found_archives(top_level: list, nested: dict, program_name: str = "PhotoArchive") -> str:
    """ROADMAP.md, analyze как "2 части" -- секция появляется, только если внутри
    просканированного дерева нашлась хотя бы одна папка __служебные_файлы (см.
    classify_found_archives()). top_level/nested — уже классифицированы вызывающим кодом
    (вложенные архивы не суммируются с внешним, см. photosort_win.py)."""
    if not top_level:
        return ""
    n = len(top_level)
    if n == 1:
        heading = f"На этом диске найден архив {html.escape(program_name)}"
    else:
        label = _plural(n, "архив", "архива", "архивов")
        heading = f"На этом диске найдено {n} {label} {html.escape(program_name)}"
    blocks = "".join(_render_found_archive_block(r, nested.get(r, []), program_name) for r in top_level)
    return f'<div class="card"><h1>{heading}</h1></div>' + blocks


def build_model_from_analyze_stats(stats) -> dict:
    """analyze/analyze-full/analyze-quick (PROMPT_archive_report.md, 1.2а) -- AnalyzeStats
    не хранит построчные записи (плоский агрегат + несколько точечных Counter/list-полей,
    см. photosort_win.py:AnalyzeStats), поэтому модель строится напрямую из его полей, не
    через build_model_from_rows. Форма результата — ТА ЖЕ, что у build_model_from_rows,
    чтобы _render_sheet1/2/3 не знали, откуда пришли данные (раздел 3 ТЗ). Категории, для
    которых AnalyzeStats физически не считает нужных чисел (байты по альбомам, разбивка
    "разногласий"/приблизительных дат по папкам, гео) — пустые Counter/None, соответствующая
    плашка/график скрывается графически (раздел 0, "пустая категория")."""
    counts = Counter({"image": stats.n_images, "raw": stats.n_raw, "video": stats.n_videos})

    oldest = None
    if stats.oldest_date is not None:
        d = stats.oldest_date
        oldest = ((d.year, d.month, d.day), stats.oldest_display or "", None)

    n_near_dup = stats.n_near_dupes
    decisions = Counter({
        "appended": max(stats.predicted_unique_count - n_near_dup, 0),
        "near_dup": n_near_dup,
        "skipped_present": stats.n_exact_dupes,
        "unreadable": stats.n_broken_or_zero,
        "disputed": 0,  # analyze не разделяет "разногласия" и "битые/нечитаемые" отдельно
    })

    return {
        "counts": counts,
        "bytes_by_kind": Counter(),  # AnalyzeStats не хранит байты по типу медиа
        # Пакет п.2 (SESSION-HANDOFF.txt): stats.total_bytes -- реальный объём просканированного
        # SOURCE (уже пишется в analyze_report.csv на каждый [1]/analyze* прогон), не
        # stats.predicted_unique_bytes -- та величина считает "что добавилось бы после дедупа"
        # (доступна только analyze/analyze-full после полного прохода хеширования, для
        # analyze-quick, т.е. самого [1], всегда 0) -- разные по смыслу числа, здесь нужен
        # именно объём того, что программа увидела, а не прогноз дедупа.
        "total_bytes": stats.total_bytes,
        # 4.6: AnalyzeStats не пишет appended.csv (analyze-режимы ничего не архивируют) --
        # источника для кумулятивной длительности видео здесь нет, плашка просто не появится.
        "video_duration_seconds": 0.0,
        "total_media": stats.n_images + stats.n_videos + stats.n_raw,
        "years": Counter(stats.dates_by_year),
        "year_months": Counter(stats.dates_by_year_month),
        "cities": Counter(),  # analyze не резолвит GPS -> место (place_for_gps не вызывается)
        "oldest": oldest,
        "bytes_saved": 0,  # нет постатейного байтового учёта точных дублей в analyze
        "exact_dupes": stats.n_exact_dupes,
        "decisions": decisions,
        "tier_counts": Counter(stats.tier_counts),
        "top_albums": [],  # AnalyzeStats не считает байты по альбомам
        "near_dup_clusters": _cluster_near_dup(stats.near_dup_edges),
        "disputes_by_folder": Counter(),
        "disputes_total": stats.n_broken_or_zero,
        "dates_review_by_folder": Counter(),
        "dates_review_bc_total": stats.tier_counts.get("B", 0) + stats.tier_counts.get("C", 0),
        "unreadable": [{}] * stats.n_broken_or_zero,
        "rejected_noise_total": 0,
        "quality_flags": Counter(),  # analyze ничего не дописывает -- appended.csv нет
        "undated_total": stats.tier_counts.get("D", 0),
        # REVIEW-HANDOFF.md, Раунд 33: closing-рекомендация analyze через рамку "уязвимо/
        # защищено" (см. _render_cta_block()) -- уменьшенная версия того, что предложил
        # ревизор: считаем архивы ТОЛЬКО внутри одного этого источника (уже есть в
        # AnalyzeStats), не "M мест" по нескольким разным прогонам analyze -- у report.html
        # для analyze архитектурно нет памяти о предыдущих источниках (каждый анализ
        # перезаписывает WORKDIR\report.html, "один слот, не персистентно per-источник",
        # см. photosort_win.py:_main()) -- честно ограничиться тем, что реально измеримо.
        "archives_found": stats.n_archives_found,
        # REVIEW-HANDOFF.md, Раунд 36: секция "Рекомендации" (_render_analyze_recommendations)
        # -- нужен только факт "на источнике уже есть собранный архив", уже посчитан
        # unconditionally в run_analyze() (classify_found_archives(), тот же список, что
        # питает found_archives-параметр generate_report_from_analyze_stats() для отдельного
        # блока "На этом диске найден архив").
        "found_archive_count": len(stats.found_archive_top_level),
    }


def _render_analyze_recommendations(model: dict) -> str:
    """REVIEW-HANDOFF.md, Раунд 36: секция "Рекомендации" в конце analyze-отчёта -- момент,
    который должен убедить настороженного пользователя пойти дальше в реальную сборку. Каждый
    пункт уже посчитан AnalyzeStats для любого режима analyze (даже analyze-quick) -- кроме
    near-dup серий: n_near_dupes/near_dup_edges требуют полного прохода хеширования (mode in
    ("analyze", "analyze-full"), см. run_analyze()), в analyze-quick остаются пустыми. Пункты
    естественно скрываются по отсутствию данных, без явной проверки mode -- тот же паттерн,
    что у остальных необязательных карточек отчёта."""
    items = []

    total_bytes = model.get("total_bytes", 0)
    if total_bytes:
        items.append(_li(
            f"Архиву потребуется примерно {_fmt_bytes(total_bytes)}",
            "Проверьте, что на диске назначения есть столько свободного места.",
        ))

    gap_year = _find_year_gap(model.get("years") or Counter())
    if gap_year is not None:
        items.append(_li(
            f"За {gap_year} год сохранилось заметно меньше снимков, чем за соседние годы",
            "Если есть ещё карта памяти или диск за этот период — стоит проанализировать "
            "и его перед реальной сборкой.",
        ))

    if model.get("found_archive_count", 0):
        items.append(_li(
            "На этом источнике уже есть собранный архив",
            "Новые файлы просто добавятся к уже собранному архиву — дублирования не будет.",
        ))

    n_series = len(model.get("near_dup_clusters") or [])
    if n_series:
        items.append(_li(
            f"Уже сейчас видно {n_series} {_plural(n_series, 'серию', 'серии', 'серий')} "
            "похожих кадров",
            "При сборке ни один из них не удалится — программа сохраняет оба варианта, "
            "если сомневается.",
        ))

    approx = model.get("tier_counts", Counter()).get("C", 0)
    if approx:
        items.append(_li(
            f"У {_n_files(approx)} дата определена приблизительно",
            "При сборке они всё равно попадут в архив.",
        ))

    return _render_checklist_card("Рекомендации", items)


# ============================================================================
# 7. Публичный вход
# ============================================================================


def _render_cta_block(level: str, target_path: str = None, model: dict = None) -> str:
    """4.7/4.8 (PROMPT_report_marketing.md): финальный блок отчёта -- "что дальше" (все шесть
    источников ТЗ называют отсутствие явного финального действия пробелом) + для успешной
    полной сборки единственный совет-не-апсейл про резервную копию (4.8, решение пользователя
    2026-07-22: показывать всегда после успешной полной сборки, без порога по размеру архива).
    HTML не может запустить программу заново -- честно ограничиться текстом/ссылкой на файл,
    не изображать интерактивность, которой нет.

    model (Раунд 33, REVIEW-HANDOFF.md): только для level=="analyze" -- closing-рекомендация
    через рамку "уязвимо/защищено" (аудитория этого экрана боится необратимой потери, не
    неэффективности), а не нейтральное "можно запустить сборку". None -- старый неразделённый
    вызов без модели, откатывается на прежний нейтральный текст (см. ветку level=="analyze"
    ниже)."""
    parts = ['<div class="card">']
    if level == "target":
        if target_path:
            # file://-ссылка на реальном Windows-браузере ведёт себя по-разному (некоторые
            # ограничивают переходы file://->file:// из соображений безопасности) -- раздел 8
            # ТЗ явно просит проверить на реальном железе (Windows-сессия) до того, как
            # полагаться на неё как на основной CTA; путь продублирован текстом рядом на
            # случай, если сама ссылка не сработает -- НЕ os.path (target_path -- всегда
            # Windows-путь, тот же случай, что и у _win_dirname/_parse_bydate_segment выше,
            # этот модуль импортируется под pytest и на не-Windows раннере).
            href = "file:///" + target_path.replace("\\", "/")
            parts.append(f'<p><a href="{html.escape(href)}">Открыть папку с архивом</a> '
                          f'— {html.escape(target_path)}</p>')
        parts.append('<p class="muted">Хотите проверить ещё один диск или флешку — запустите '
                      'программу снова с новым источником.</p>')
        parts.append(
            '<p><b>Совет:</b> теперь, когда архив собран, стоит сделать его резервную копию '
            'на другом диске или в облаке — так воспоминания не будут зависеть от одного '
            'носителя.</p>'
        )
        # REVIEW-HANDOFF.md, Раунд 32, задача 6: отчёт советует бэкапить НОВЫЙ архив, но ни
        # слова о судьбе старых носителей, ради разбора которых всё затевалось -- пользователь
        # не знает, можно ли их освободить. PhotoArchive_ot_avtora.md уже даёт этот совет ("не
        # спешите удалять старые папки"), но это отдельный документ, который открывается один
        # раз (или не открывается вовсе) -- report.html открывается после каждого прогона.
        # Своими словами, не цитата письма (решение пользователя) -- та же мысль, другая форма.
        parts.append(
            '<p class="muted">А со старыми носителями, с которых всё это собрано, торопиться '
            'не нужно — пусть полежат рядом, пока вы не убедитесь, что новый архив полностью '
            'вас устраивает.</p>'
        )
    elif level == "analyze" and model is not None:
        years = model.get("years") or Counter()
        span = (max(years) - min(years) + 1) if years else 0
        years_label = f'{span} {_plural(span, "год", "года", "лет")}' if span else "Эта"
        archives_found = model.get("archives_found", 0)
        total_bytes = model.get("total_bytes", 0)
        if archives_found:
            # Уменьшенная версия предложения ревизора: считаем архивы (zip/rar) ТОЛЬКО внутри
            # этого источника (AnalyzeStats.n_archives_found) -- report.html для analyze
            # архитектурно не помнит предыдущие источники, "M мест" по нескольким прогонам не
            # посчитать честно (см. докстринг build_model_from_analyze_stats()).
            arch_label = _plural(archives_found, "отдельном архиве", "отдельных архивах", "отдельных архивах")
            parts.append(
                f'<p class="muted">Прямо сейчас {years_label.lower()} памяти на этом диске '
                f'лежат не только россыпью, но ещё и в {archives_found} {arch_label} (zip/rar) '
                '— каждый из них может испортиться независимо от остальных, и вы не узнаете об '
                'этом, пока не станет поздно. Сборка соберёт всё это в одном месте, которое '
                'проще защитить одним действием, чем следить за несколькими.</p>'
            )
        elif span or total_bytes:
            bytes_part = f'{_fmt_bytes(total_bytes)} ' if total_bytes else ""
            parts.append(
                f'<p class="muted">{years_label} и {bytes_part}памяти сейчас хранятся на одном '
                'источнике. Сборка архива — только первый шаг: после неё стоит сделать '
                'резервную копию уже собранного архива на другом носителе, чтобы воспоминания '
                'не зависели от одного диска.</p>'
            )
        else:
            parts.append('<p class="muted">Нравится результат? Можно запустить настоящую сборку — '
                          'ничего из увиденного здесь пока не записано.</p>')
    else:
        parts.append('<p class="muted">Нравится результат? Можно запустить настоящую сборку — '
                      'ничего из увиденного здесь пока не записано.</p>')
    parts.append('</div>')
    return "".join(parts)


def _generate_from_model(model: dict, out_path: str, level: str, program_name: str,
                          run_stats: dict = None, checklist_new: dict = None,
                          checklist_before: dict = None, found_archives: tuple = None,
                          target_path: str = None, interrupted: bool = False,
                          full_workdir: bool = False, verify_link: str = None) -> None:
    # level=="workdir" (CLI --dry-run/интерактивный [2], решение пользователя 2026-07-20,
    # третий заход) -- по умолчанию ТОЛЬКО часть 1 ("Пробный прогон" + рекомендации по нему),
    # без "Ваш архив"/диаграмм: и содержательно нечего показывать (для [2] данные чисто
    # in-memory, архива в этом смысле не существует), и для CLI --dry-run опасно -- он пишет
    # персистентные CSV TARGET по-настоящему (RunLogs, не CollectingRunLogs), но БЕЗ
    # реального копирования файла (place_file() пропущен) -- повторные --dry-run на один
    # TARGET накапливают в этих CSV фантомные "appended"-строки, которые никогда не станут
    # архивом. checklist_before (если вообще посчитан -- см. generate_report()) СОЗНАТЕЛЬНО
    # не рендерится по той же причине; checklist_new (если run_start передан) уже
    # отфильтрован по времени -- используем его, а не полную (потенциально засорённую) model.
    #
    # REVIEW-HANDOFF.md, Раунд 38: интерактивный [2] на уже существующем Target -- другой
    # случай, безопасный (suppress_logs=True там всегда, никаких фантомных записей своей же
    # истории быть не может). full_workdir=True (см. photosort_win.py:_bare_launch_run_dryrun) --
    # явный сигнал вызывающего кода "я смёржил настоящую историю Target с гипотетическими
    # строками этого прогона и посчитал run_start" -- отдельный флаг, не переиспользование
    # checklist_before is not None, чтобы CLI --dry-run (который тоже передаёт run_start,
    # но с потенциально засорённой СВОЕЙ ЖЕ историей) не попал сюда неявно.
    if level == "workdir" and not full_workdir:
        fields = checklist_new if checklist_new is not None else model
        body = _render_this_run(run_stats, level) + _render_sheet3_single(fields, level)
    # Часть 1 -- "Пополнение архива" (только этот запуск) + рекомендации ПО НЕМУ сразу следом;
    # часть 2 -- "Ваш архив" (история целиком) + диаграммы + рекомендации, накопившиеся до
    # этого пополнения, в конце. Решение пользователя 2026-07-20 (второй заход): держать
    # рекомендации физически рядом с той половиной отчёта, к которой они относятся, а не
    # одним общим блоком в хвосте -- иначе про "это только что произошедшее" читателю
    # приходится вспоминать уже после того, как рассказ ушёл в архив целиком.
    elif checklist_new is None and checklist_before is None:
        body = (_render_this_run(run_stats, level) + _render_sheet1(model, level) + _render_sheet2(model)
                + _render_exact_dup_examples(model, "Точные повторы — примеры", intro=EXACT_DUP_INTRO,
                                              verify_link=verify_link if level == "target" else None)
                + _render_sheet3_single(model, level))
    else:
        # verify_link -- только у "этого пополнения" (2026-07-26): страница целиком покрывает
        # архив, но у карточки "Накопилось раньше" ссылка читалась бы как дубль/расхождение
        # ("почему опять эта же ссылка"), пользователь уже видел её выше на этой же странице.
        body = (
            _render_this_run(run_stats, level)
            + _render_recommendations(checklist_new, "Новое в этом пополнении")
            + _render_exact_dup_examples(
                checklist_new, "Точные повторы этого пополнения — примеры", intro=EXACT_DUP_INTRO,
                verify_link=verify_link if level == "target" else None)
            + _render_sheet1(model) + _render_sheet2(model)
            + _render_recommendations(
                checklist_before, "Накопилось до этого пополнения",
                intro="Было в архиве уже до этого пополнения — не появилось из-за него, просто ещё не разобрано.",
            )
            + _render_exact_dup_examples(
                checklist_before, "Точные повторы, накопленные раньше — примеры", intro=EXACT_DUP_INTRO)
        )
    if found_archives:
        top_level, nested = found_archives
        body += _render_found_archives(top_level, nested, program_name)
    # REVIEW-HANDOFF.md, Раунд 36: секция "Рекомендации" -- только level=="analyze"
    # (target/workdir уже строят своё "что дальше" другими карточками) -- ровно перед CTA
    # блоком, тем же принципом, что и он: рекомендации, потом призыв к действию, не наоборот.
    if level == "analyze":
        body += _render_analyze_recommendations(model)
    # 4.7/4.8: CTA-блок + совет про бэкап -- ровно в конце, после последнего чек-листа/части 2.
    # model передаётся только для level=="analyze" (Раунд 33, REVIEW-HANDOFF.md) -- target/
    # workdir не используют его в _render_cta_block(), передавать безвредно и для них.
    body += _render_cta_block(level, target_path, model=model)
    # 4.1/4.3 (PROMPT_report_marketing.md): баннер доверия + компактный чек-лист -- в самом
    # начале ЛЮБОГО отчёта (все уровни), не только report.generate_report()/generate_report_
    # from_analyze_stats() по отдельности -- один общий хук здесь проще, чем дублировать вызов
    # в обоих публичных входах.
    body = _render_trust_block(level) + body
    # Ctrl+C-пакет: баннер прерывания -- ПЕРЕД баннером доверия (самая первая строка отчёта
    # целиком, по прямой просьбе пользователя), не после него.
    if interrupted:
        body = _render_interrupted_banner() + body
    _write(out_path, _page_shell(f"{program_name} — отчёт архива", body))


def generate_report(data: dict, out_path: str, level: str = "target",
                     program_name: str = "PhotoArchive", run_stats: dict = None,
                     run_start: str = None, target_path: str = None,
                     interrupted: bool = False, full_workdir: bool = False) -> None:
    """level: "target" (полный archive-прогон) | "workdir" ([2]/--dry-run) — оба читают
    dict[str, list[dict]] (CSV TARGET или CollectingRunLogs.rows). Для
    analyze/analyze-full/analyze-quick см. generate_report_from_analyze_stats().

    run_stats: сумма RunResult.stats по всем SOURCE этого вызова (см. photosort_win.py:
    _bare_launch_run_build/_bare_launch_run_dryrun/_main) -- тот же словарь, что уже питает
    консольный build_final_summary(), просто не выбрасывается после печати. None/{} --
    секция "Пополнение архива"/"Пробный прогон" не рендерится вообще (старые вызовы без
    этого параметра).

    run_start: момент начала ЭТОГО вызова ("%Y-%m-%d %H:%M:%S", тот же формат, что
    RunLogs._ts() -- см. _split_rows_by_time()) -- делит Лист 3 на "новое в этом
    пополнении"/"накопилось раньше". None -- Лист 3 не делится (один список, как раньше).
    level=="workdir" без full_workdir=True -- используется ТОЛЬКО "новое" (см.
    _generate_from_model()), "раньше" вычисляется, но сознательно не рендерится (CLI --dry-run
    пишет реальные CSV TARGET без реального копирования файла -- история там может быть
    засорена фантомными записями прошлых --dry-run, см. _generate_from_model()).

    full_workdir (REVIEW-HANDOFF.md, Раунд 38): level=="workdir" И data уже содержит
    смёржженную реальную историю Target (parse_target_logs) с гипотетическими строками этого
    прогона (photosort_win.py:_bare_launch_run_dryrun, интерактивный [2] на непустом Target,
    suppress_logs=True гарантирует отсутствие фантомных записей ЭТОГО режима в самой истории)
    -- показывает полноценные "Ваш архив"/диаграммы вместо урезанного чек-листа, требует
    run_start (иначе no-op, см. _generate_from_model()). CLI --dry-run это НЕ передаёт --
    там та же засорённость историей, от которой предостерегает предыдущий абзац.

    target_path (4.7, PROMPT_report_marketing.md): абсолютный путь TARGET -- используется
    только при level=="target", для ссылки "Открыть папку с архивом" в CTA-блоке в конце
    отчёта (_render_cta_block()). None -- ссылка не рендерится, остаётся только текст.

    interrupted (Ctrl+C-пакет): работа прервана пользователем (KeyboardInterrupt) во время
    [3]/CLI archive -- см. photosort_win.py _run_impl()/_RunState.interrupted. Данные в data
    в этом случае неполные (только то, что успело записаться в CSV до прерывания) -- баннер
    в начале отчёта (_render_interrupted_banner()) делает это явным, не молчаливым."""
    model = build_model_from_rows(data)
    checklist_new = checklist_before = None
    if run_start:
        data_new, data_before = _split_rows_by_time(data, run_start)
        checklist_new = _build_checklist_fields(data_new)
        checklist_before = _build_checklist_fields(data_before)
    # 2026-07-26: только level=="target" -- реальный архив на диске, единственный случай,
    # где "полная сверка дублей" (путь+имя каждого файла) вообще что-то значит для
    # пользователя (workdir/analyze -- in-memory прогон, файлы ещё не скопированы).
    verify_link = generate_dedup_verification_page(data, out_path, program_name) if level == "target" else None
    _generate_from_model(model, out_path, level, program_name, run_stats=run_stats,
                          checklist_new=checklist_new, checklist_before=checklist_before,
                          target_path=target_path, interrupted=interrupted,
                          full_workdir=full_workdir, verify_link=verify_link)


def generate_report_from_analyze_stats(stats, out_path: str, level: str = "analyze",
                                        program_name: str = "PhotoArchive",
                                        found_archives: tuple = None) -> None:
    """found_archives: (top_level: list[str], nested: dict[str, list[str]]) -- уже
    классифицированные photosort_win.classify_found_archives() пути найденных архивов внутри
    просканированного SOURCE (ROADMAP.md, analyze как "2 части"). None/([], {}) -- часть 2 не
    рендерится вообще (ничего не найдено, либо старые вызовы без этого параметра)."""
    model = build_model_from_analyze_stats(stats)
    _generate_from_model(model, out_path, level, program_name, found_archives=found_archives)
