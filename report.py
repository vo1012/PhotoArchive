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


def parse_target_logs(logs_dir: str) -> dict:
    """TARGET-уровень (PROMPT_archive_report.md, 1.1): разбор существующих CSV-логов
    целиком. Отсутствующий файл (near_dup_edges.csv на архивах, собранных до этой фичи) —
    пустой список, не ошибка."""
    data = {}
    for name in CSV_NAMES:
        path = os.path.join(logs_dir, f"{name}.csv")
        rows = []
        try:
            with open(_winlong(path), newline="", encoding="utf-8") as f:
                rows = list(csv.DictReader(f))
        except OSError:
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
        "disputes_by_folder": Counter(_win_dirname(r.get("source", "")) for r in disputes),
        "disputes_total": len(disputes),
        "dates_review_by_folder": Counter(
            _win_dirname(r.get("source", "")) for r in dates_review if r.get("tier") in ("B", "C")
        ),
        "dates_review_bc_total": sum(1 for r in dates_review if r.get("tier") in ("B", "C")),
        "unreadable": data.get("unreadable", []),
        # Флаг из appended.csv (RunLogs.appended()) -- раньше виден был только агрегатом в
        # диаграмме Листа 2 ("Качество кадров"), без указания, что с этим делать.
        "quality_flags": Counter(r.get("flags", "") or "" for r in appended),
        # Tier D (undated_media.csv) -- "даты нет вообще", не путать с Tier B/C выше
        # ("дата есть, но приблизительная") -- разные по природе находки, разные пункты.
        "undated_total": len(data.get("undated_media", [])),
    }


def _split_rows_by_time(data: dict, run_start: str) -> tuple:
    """Делит CSV-строки (только категории Листа 3) на "этот прогон" (timestamp >= run_start)
    и "раньше" -- по первой колонке timestamp, которая уже есть у каждого CSV-лога
    (RunLogs._ts(), формат "%Y-%m-%d %H:%M:%S", лексикографически сравнимый). `run_start` --
    тот же формат, захваченный в photosort_win.py ДО начала обработки источников -- см.
    generate_report(). "appended"/"undated_media" нужны здесь для флагов качества/Tier D
    в _build_checklist_fields() -- сами по себе не категории Листа 3, но их разбивка по
    времени строится по тому же timestamp, тем же способом."""
    names = ("near_dup_edges", "disputes", "dates_review", "unreadable", "appended", "undated_media")
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
    oldest = None  # (sort_key, source_path, place_or_none)
    total_bytes = 0

    for row in appended:
        dest = row.get("dest", "") or ""
        kind = _media_kind(dest)
        size = _row_size(row, size_cache)
        counts[kind] += 1
        bytes_by_kind[kind] += size
        total_bytes += size

        album = _parse_album(dest)
        if album:
            albums_bytes[album] += size
            albums_count[album] += 1

        if kind not in ("image", "video"):
            continue  # RAW-зеркало/прочее не участвует во временной/гео-статистике
        parsed = _parse_bydate_segment(dest)
        if parsed is None:
            continue
        year, month, day, place = parsed
        years[year] += 1
        if month:
            year_months[f"{year}-{month:02d}"] += 1
        if place:
            cities[place] += 1
        sort_key = (year, month or 0, day or 0)
        if oldest is None or sort_key < oldest[0]:
            oldest = (sort_key, row.get("source", ""), place or album)

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
        "total_media": counts["image"] + counts["video"] + counts["raw"],
        "years": years,
        "year_months": year_months,
        "cities": cities,
        "oldest": oldest,
        "bytes_saved": bytes_saved,
        "exact_dupes": len(skipped),
        "decisions": decisions,
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


# ============================================================================
# 3. SVG-графики (инлайн, без внешних библиотек)
# ============================================================================


def _svg_bar_chart(counter: Counter, width=680, height=220, color=COLOR_ACCENT) -> str:
    items = sorted(counter.items())
    if not items:
        return ""
    max_v = max(v for _, v in items) or 1
    n = len(items)
    margin_left, margin_bottom, margin_top, margin_right = 8, 26, 20, 8
    plot_w = width - margin_left - margin_right
    plot_h = height - margin_bottom - margin_top
    gap = plot_w / n
    bar_w = max(gap * 0.6, 4)
    parts = []
    for i, (label, v) in enumerate(items):
        bar_h = plot_h * (v / max_v)
        x = margin_left + i * gap + (gap - bar_w) / 2
        y = margin_top + (plot_h - bar_h)
        parts.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_w:.1f}" height="{bar_h:.1f}" '
                      f'fill="{color}" rx="2"/>')
        parts.append(f'<text x="{x + bar_w / 2:.1f}" y="{height - margin_bottom + 16:.1f}" '
                      f'font-size="11" text-anchor="middle" fill="{COLOR_TEXT_MUTED}">{html.escape(str(label))}</text>')
        parts.append(f'<text x="{x + bar_w / 2:.1f}" y="{max(y - 4, 12):.1f}" '
                      f'font-size="11" text-anchor="middle" fill="{COLOR_TEXT}">{v}</text>')
    return (f'<svg viewBox="0 0 {width} {height}" width="100%" height="{height}" role="img" '
            f'aria-label="Фото по годам">' + "".join(parts) + "</svg>")


def _svg_pie(segments: list, size=170, value_fmt=str) -> tuple:
    """segments: [(label, value, color), ...]. Возвращает (svg, legend_html) -- полный круг
    с секторами от центра (не кольцо/донат) -- площадь сектора на глаз сравнить проще, чем
    толщину дугового кольца, а аудитория отчёта нетехническая (RULES.md/
    PROMPT_archive_report.md: 45-70 лет, ценит простоту, не дашборд).

    value_fmt -- как показать value в легенде (по умолчанию как есть, для счётчиков файлов);
    _fmt_bytes -- для диаграмм по объёму (см. "Объём по категориям" в _render_sheet2)."""
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
        legend.append(f'<div class="legend-row"><span class="swatch" style="background:{color}"></span>'
                       f'{html.escape(label)} — {value_fmt(v)} ({frac * 100:.0f}%)</div>')
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
.stat .label {{ font-size: 13px; color: var(--muted); }}
.legend-row {{ font-size: 13px; margin: 4px 0; }}
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
.city-list li {{ background: var(--bg); border: 1px solid var(--line); border-radius: 999px; padding: 3px 12px; font-size: 13px; }}
.checklist {{ list-style: none; padding: 0; margin: 0; }}
.checklist li {{ padding: 10px 0; border-bottom: 1px solid var(--line); }}
.checklist li:last-child {{ border-bottom: none; }}
.checklist .title {{ font-weight: 600; }}
.checklist .detail {{ color: var(--muted); font-size: 13px; margin-top: 2px; }}
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
.muted {{ color: var(--muted); font-size: 13px; }}
.footer {{ text-align: center; color: #8a8a7c; font-size: 12px; margin: 16px 0 24px; }}
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


def generate_placeholder_report(reason: str, out_path: str, program_name: str = "PhotoArchive") -> None:
    body = f"""
<div class="card">
  <h1>{html.escape(program_name)}</h1>
  <p class="subtitle">{time.strftime("%Y-%m-%d %H:%M")}</p>
  <p>{html.escape(reason)}</p>
  <p class="muted">Подробности — в консоли программы и summary.txt.</p>
</div>
"""
    _write(out_path, _page_shell(f"{program_name} — отчёт", body))


# ============================================================================
# 6. Листы 1-3
# ============================================================================


def _render_sheet1(model: dict) -> str:
    total_media = model["total_media"]
    years = model["years"]
    stats = [f'<div class="stat"><div class="value">{total_media}</div>'
             f'<div class="label">фото и видео в архиве</div></div>']

    if years:
        span = max(years) - min(years) + 1
        stats.append(f'<div class="stat"><div class="value">{span}</div>'
                      f'<div class="label">{"год" if span == 1 else "лет"} истории</div></div>')

    if model["bytes_saved"]:
        stats.append(f'<div class="stat"><div class="value">{_fmt_bytes(model["bytes_saved"])}</div>'
                      f'<div class="label">сэкономлено на точных повторах</div></div>')

    # Все данные отчёта -- из CSV-логов TARGET, которые копятся с первого прогона
    # программы на этом архиве и никогда не очищаются между прогонами (см.
    # _finalize_target_report/PROMPT_archive_report.md) -- без этой строки цифры читались
    # бы как "результат вот этого пополнения", а на самом деле это история архива целиком.
    parts = ['<div class="card">', "<h1>Ваш архив</h1>",
             '<p class="subtitle">Цифры — по архиву целиком, с учётом только что добавленного '
             'в этом пополнении, за всё время, что вы пользуетесь программой с этим архивом.</p>',
             '<div class="stat-row">'] + stats + ["</div>"]

    oldest = model["oldest"]
    if oldest:
        (year, month, day), source, place = oldest
        if day and month:
            date_str = f"{day:02d}.{month:02d}.{year}"
        elif month:
            date_str = f"{month:02d}.{year}"
        else:
            date_str = str(year)
        place_str = f" ({html.escape(place)})" if place else ""
        parts.append(f'<p><b>Самое старое фото:</b> {date_str}{place_str}</p>')

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

    parts.append('<p class="bridge">Дальше — ваш архив в цифрах.</p>')
    parts.append("</div>")
    return "".join(parts)


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
    n_new_total = n_appended_images + n_appended_videos
    n_near_dup = (run_stats.get("appended_near_dup", 0) + run_stats.get("appended_better", 0)
                  + run_stats.get("appended_crop", 0))
    n_skipped = run_stats.get("skipped_present", 0)
    n_disputed = run_stats.get("disputed", 0)
    n_unreadable = run_stats.get("unreadable_count", 0)

    if not any((n_new_total, n_skipped, n_disputed, n_unreadable)):
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
    added_label = "новых фото и видео было бы добавлено" if preview else "новых фото и видео добавлено"
    saved_label = "было бы сэкономлено на дублях" if preview else "сэкономлено на дублях в этот раз"

    stats_html = [f'<div class="stat"><div class="value">{n_new_total}</div>'
                  f'<div class="label">{added_label}</div></div>']
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

    parts = [
        '<div class="card">', f"<h2>{html.escape(heading)}</h2>",
        f'<p class="muted">{intro}</p>',
        '<div class="stat-row">',
    ] + stats_html + ["</div>"]

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


def _render_sheet2(model: dict) -> str:
    parts = ['<div class="card">', "<h2>Фото по годам</h2>"]
    years_svg = _svg_bar_chart(model["years"])
    if years_svg:
        parts.append(years_svg)
    else:
        parts.append('<p class="muted">Недостаточно данных для графика.</p>')
    parts.append("</div>")

    pie_charts = [
        ("Тип медиа", [
            ("Фото", model["counts"]["image"], CATEGORY_PALETTE[0]),
            ("Видео", model["counts"]["video"], CATEGORY_PALETTE[1]),
            ("RAW", model["counts"]["raw"], CATEGORY_PALETTE[2]),
        ], str),
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
        ], _fmt_bytes),
        ("Итог решений программы", [
            ("Новые файлы", model["decisions"]["appended"], CATEGORY_PALETTE[0]),
            ("Точные повторы", model["decisions"]["skipped_present"], CATEGORY_PALETTE[1]),
            ("Похожие кадры сохранены", model["decisions"]["near_dup"], CATEGORY_PALETTE[2]),
            ("Не прочитано", model["decisions"]["unreadable"], CATEGORY_PALETTE[3]),
            ("Спорные", model["decisions"]["disputed"], CATEGORY_PALETTE[4]),
        ], str),
        ("Надёжность дат", [
            ("Точная (EXIF)", model["tier_counts"].get("A", 0), CATEGORY_PALETTE[0]),
            ("Высокая", model["tier_counts"].get("B", 0), CATEGORY_PALETTE[1]),
            ("Оценочная", model["tier_counts"].get("C", 0), CATEGORY_PALETTE[2]),
            ("Низкая", model["tier_counts"].get("D", 0), CATEGORY_PALETTE[3]),
        ], str),
        # Флаг из appended.csv (small_image/low_confidence_photo, см. RunLogs.appended()) --
        # раньше нигде не визуализировался, был доступен только тем, кто откроет сам CSV.
        # 2026-07-20, по запросу пользователя: отчёт должен закрывать это без похода в logs\.
        ("Качество кадров", [
            ("Обычные", model["quality_flags"].get("", 0), CATEGORY_PALETTE[0]),
            ("Маленькие фото", model["quality_flags"].get("small_image", 0), CATEGORY_PALETTE[1]),
            ("Низкая уверенность", model["quality_flags"].get("low_confidence_photo", 0), CATEGORY_PALETTE[3]),
        ], str),
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
        pie_charts.append(("География", geo_segments, str))

    # Раскладка по типу подачи, не просто "рядом чтобы компактно" (решение пользователя
    # 2026-07-20): круговые диаграммы -- у всех подпись сбоку (circle+legend, одна визуальная
    # мелодия) -- своя группа (grid-3, авто-число колонок), включая "Объём по категориям"
    # теперь тоже сектор. "Топ альбомов" -- никакой легенды нет вообще, цифры прямо у
    # полосы -- другая мелодия, полная ширина отдельно (см. её же комментарий ниже).
    pie_cells = []
    for title, segments, value_fmt in pie_charts:
        svg, legend = _svg_pie(segments, value_fmt=value_fmt)
        if not svg:
            continue
        pie_cells.append(
            f'<div class="card"><h2>{html.escape(title)}</h2>'
            f'<div class="chart-block">{svg}<div class="legend">{legend}</div></div></div>'
        )
    if pie_cells:
        parts.append(f'<div class="grid-3">{"".join(pie_cells)}</div>')

    # Полная ширина, не пара с чем-либо в grid-2 -- пробовали пару с "Объём по категориям"
    # (2026-07-20), текст съёживался вдвое вместе с шириной колонки (viewBox фиксирован под
    # ~680px) и переставал читаться. hbar-графику ширина нужна по-настоящему, не для симметрии.
    if model["top_albums"]:
        hbar = _svg_hbar_chart([(name, b, _fmt_bytes(b)) for name, b in model["top_albums"]])
        parts.append(f'<div class="card"><h2>Топ альбомов по размеру</h2>{hbar}</div>')

    return "".join(parts)


def _folder_label(path: str) -> str:
    """_win_dirname() файла прямо в корне SOURCE даёт "" -- _win_basename("") тоже
    "", без этого получалась бы пустая метка перед счётчиком ("  (2)")."""
    return _win_basename(path) or path or "корень источника"


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
        # analyze-уровень (build_model_from_analyze_stats) не отслеживает разбивку по папкам
        # -- только итоговое число; TARGET/dry-run уровень отслеживает (см. build_model_from_rows).
        folders = fields["disputes_by_folder"].most_common(TOP_N)
        folder_detail = "; ".join(f"{html.escape(_folder_label(f))} ({n})" for f, n in folders)
        # "где искать" и "какие папки-источники" -- две разные мысли, отдельные строки.
        detail = "Лежат в _Unsorted."
        if folder_detail:
            detail += f"<br>Сгруппированы по исходной папке: {folder_detail}."
        items.append(_li(f"{_n_files(fields['disputes_total'])} не удалось однозначно распознать", detail))

    if fields["dates_review_bc_total"]:
        folders = fields["dates_review_by_folder"].most_common(TOP_N)
        folder_detail = "; ".join(f"{html.escape(_folder_label(f))} ({n})" for f, n in folders)
        detail = "Стоит перепроверить при желании."
        if folder_detail:
            detail += f"<br>Папки-источники: {folder_detail}."
        items.append(_li(f"{_n_files(fields['dates_review_bc_total'])} получили дату приблизительно", detail))

    if fields["undated_total"]:
        # Tier D -- дата отсутствует вообще (ни EXIF, ни имя файла, ни соседи по папке), не
        # путать с Tier B/C выше ("дата есть, но приблизительная") -- разные находки.
        items.append(_li(
            f"{_n_files(fields['undated_total'])} вообще без даты",
            "Дата не определилась ни по EXIF, ни по имени файла, ни по соседям в папке — "
            "стоит проставить вручную при желании.",
        ))

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


def _render_sheet3_single(model: dict, level: str) -> str:
    """WORKDIR/analyze/старые вызовы без run_start -- один неразделённый список, ОБЯЗАТЕЛЬНО
    кумулятивный за всю историю архива (для TARGET-уровня с run_start Лист 3 физически
    разнесён на две части отчёта -- см. _render_recommendations()/_generate_from_model())."""
    items = _build_checklist_items(model)
    if level == "analyze":
        items.append(_li(
            "Эта часть рекомендаций дорабатывается",
            "Отдельный чек-лист для «источник для будущей сборки» / «уже готовый архив» "
            "появится в одной из следующих версий.",
        ))
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
        "total_bytes": stats.predicted_unique_bytes,
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
    }


# ============================================================================
# 7. Публичный вход
# ============================================================================


def _generate_from_model(model: dict, out_path: str, level: str, program_name: str,
                          run_stats: dict = None, checklist_new: dict = None,
                          checklist_before: dict = None, found_archives: tuple = None) -> None:
    # level=="workdir" (CLI --dry-run/интерактивный [2], решение пользователя 2026-07-20,
    # третий заход) -- ТОЛЬКО часть 1 ("Пробный прогон" + рекомендации по нему), без "Ваш
    # архив"/диаграмм: и содержательно нечего показывать (для [2] данные чисто in-memory,
    # архива в этом смысле не существует), и для CLI --dry-run опасно -- он пишет
    # персистентные CSV TARGET по-настоящему (RunLogs, не CollectingRunLogs), но БЕЗ
    # реального копирования файла (place_file() пропущен) -- повторные --dry-run на один
    # TARGET накапливают в этих CSV фантомные "appended"-строки, которые никогда не станут
    # архивом. checklist_before (если вообще посчитан -- см. generate_report()) СОЗНАТЕЛЬНО
    # не рендерится по той же причине; checklist_new (если run_start передан) уже
    # отфильтрован по времени -- используем его, а не полную (потенциально засорённую) model.
    if level == "workdir":
        fields = checklist_new if checklist_new is not None else model
        body = _render_this_run(run_stats, level) + _render_sheet3_single(fields, level)
    # Часть 1 -- "Пополнение архива" (только этот запуск) + рекомендации ПО НЕМУ сразу следом;
    # часть 2 -- "Ваш архив" (история целиком) + диаграммы + рекомендации, накопившиеся до
    # этого пополнения, в конце. Решение пользователя 2026-07-20 (второй заход): держать
    # рекомендации физически рядом с той половиной отчёта, к которой они относятся, а не
    # одним общим блоком в хвосте -- иначе про "это только что произошедшее" читателю
    # приходится вспоминать уже после того, как рассказ ушёл в архив целиком.
    elif checklist_new is None and checklist_before is None:
        body = (_render_this_run(run_stats, level) + _render_sheet1(model) + _render_sheet2(model)
                + _render_sheet3_single(model, level))
    else:
        body = (
            _render_this_run(run_stats, level)
            + _render_recommendations(checklist_new, "Новое в этом пополнении")
            + _render_sheet1(model) + _render_sheet2(model)
            + _render_recommendations(
                checklist_before, "Накопилось до этого пополнения",
                intro="Было в архиве уже до этого пополнения — не появилось из-за него, просто ещё не разобрано.",
            )
        )
    if found_archives:
        top_level, nested = found_archives
        body += _render_found_archives(top_level, nested, program_name)
    _write(out_path, _page_shell(f"{program_name} — отчёт архива", body))


def generate_report(data: dict, out_path: str, level: str = "target",
                     program_name: str = "PhotoArchive", run_stats: dict = None,
                     run_start: str = None) -> None:
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
    level=="workdir" -- используется ТОЛЬКО "новое" (см. _generate_from_model()), "раньше"
    вычисляется, но сознательно не рендерится (CLI --dry-run пишет реальные CSV TARGET без
    реального копирования файла -- история там может быть засорена фантомными записями
    прошлых --dry-run, см. _generate_from_model())."""
    model = build_model_from_rows(data)
    checklist_new = checklist_before = None
    if run_start:
        data_new, data_before = _split_rows_by_time(data, run_start)
        checklist_new = _build_checklist_fields(data_new)
        checklist_before = _build_checklist_fields(data_before)
    _generate_from_model(model, out_path, level, program_name, run_stats=run_stats,
                          checklist_new=checklist_new, checklist_before=checklist_before)


def generate_report_from_analyze_stats(stats, out_path: str, level: str = "analyze",
                                        program_name: str = "PhotoArchive",
                                        found_archives: tuple = None) -> None:
    """found_archives: (top_level: list[str], nested: dict[str, list[str]]) -- уже
    классифицированные photosort_win.classify_found_archives() пути найденных архивов внутри
    просканированного SOURCE (ROADMAP.md, analyze как "2 части"). None/([], {}) -- часть 2 не
    рендерится вообще (ничего не найдено, либо старые вызовы без этого параметра)."""
    model = build_model_from_analyze_stats(stats)
    _generate_from_model(model, out_path, level, program_name, found_archives=found_archives)
