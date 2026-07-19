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

# Донат-палитра для категориальных срезов (Лист 2) — производные от акцентных цветов
# буклета, не новые случайные цвета: тёплый/холодный/приглушённый ряд той же гаммы.
DONUT_PALETTE = [COLOR_ACCENT, COLOR_ACCENT_SECONDARY, "#6E8C74", "#C9A063", "#9AA593"]

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


_MONTH_FOLDER_RE = re.compile(r"^(\d{4})-(\d{2})(?:-(\d{2}))?")


def _parse_bydate_segment(dest: str):
    """Достаёт (year, month, day, place) из пути вида
    ...\\ByDate\\<year>\\<YYYY-MM[-DD]> [место][ [PhotoArchive]]\\file — под любую
    bydate_granularity (day/month/year/flat). Возвращает None, если по пути нельзя
    восстановить хотя бы год (flat-раскладка, либо 0000-undated)."""
    parts = dest.split(os.sep)
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
    parts = dest.split(os.sep)
    if "Albums" not in parts:
        return None
    idx = parts.index("Albums")
    if idx + 1 >= len(parts):
        return None
    return parts[idx + 1]


def build_model_from_rows(data: dict) -> dict:
    """Общая агрегация для TARGET-уровня (parse_target_logs) и WORKDIR
    [2]/--dry-run-уровня (CollectingRunLogs.rows) — обе формы идентичны по структуре
    (PROMPT_archive_report.md, раздел 3), эта функция не знает, откуда пришли данные."""
    appended = data.get("appended", [])
    near_dup = data.get("near_dup_edges", [])
    skipped = data.get("skipped", [])
    disputes = data.get("disputes", [])
    dates_review = data.get("dates_review", [])
    albums_merged = data.get("albums_merged", [])
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

    disputes_by_folder = Counter(os.path.dirname(r.get("source", "")) for r in disputes)
    dates_review_by_folder = Counter(
        os.path.dirname(r.get("source", "")) for r in dates_review if r.get("tier") in ("B", "C")
    )

    return {
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
        "near_dup_clusters": _cluster_near_dup(near_dup),
        "disputes_by_folder": disputes_by_folder,
        "disputes_total": len(disputes),
        "albums_merged": albums_merged,
        "dates_review_by_folder": dates_review_by_folder,
        "dates_review_bc_total": sum(1 for r in dates_review if r.get("tier") in ("B", "C")),
        "unreadable": unreadable,
        "rejected_noise_total": len(rejected_noise),
    }


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


def _svg_donut(segments: list, size=170, stroke=26) -> tuple:
    """segments: [(label, value, color), ...]. Возвращает (svg, legend_html)."""
    segments = [(label, v, c) for label, v, c in segments if v > 0]
    total = sum(v for _, v, _ in segments)
    if total <= 0:
        return "", ""
    r = (size - stroke) / 2
    cx = cy = size / 2
    circumference = 2 * math.pi * r
    offset = 0.0
    circles, legend = [], []
    for label, v, color in segments:
        frac = v / total
        dash = frac * circumference
        circles.append(
            f'<circle cx="{cx}" cy="{cy}" r="{r:.2f}" fill="none" stroke="{color}" '
            f'stroke-width="{stroke}" stroke-dasharray="{dash:.2f} {circumference - dash:.2f}" '
            f'stroke-dashoffset="{-offset:.2f}" transform="rotate(-90 {cx} {cy})"/>'
        )
        offset += dash
        legend.append(f'<div class="legend-row"><span class="swatch" style="background:{color}"></span>'
                       f'{html.escape(label)} — {v} ({frac * 100:.0f}%)</div>')
    svg = (f'<svg viewBox="0 0 {size} {size}" width="{size}" height="{size}" role="img" '
           f'aria-label="Диаграмма">' + "".join(circles) + "</svg>")
    return svg, "".join(legend)


def _svg_hbar_chart(items: list, width=680, bar_h=22, gap=8, color=COLOR_ACCENT) -> str:
    """items: [(label, value, display_str), ...], уже отсортированные по убыванию."""
    if not items:
        return ""
    max_v = max(v for _, v, _ in items) or 1
    margin_left, margin_right = 170, 70
    plot_w = width - margin_left - margin_right
    height = len(items) * (bar_h + gap) + gap
    parts = []
    y = gap
    for label, v, disp in items:
        w = plot_w * (v / max_v)
        short_label = label if len(label) <= 26 else label[:23] + "…"
        parts.append(f'<text x="{margin_left - 8}" y="{y + bar_h * 0.68:.1f}" font-size="12" '
                      f'text-anchor="end" fill="{COLOR_TEXT}">{html.escape(short_label)}</text>')
        parts.append(f'<rect x="{margin_left}" y="{y}" width="{w:.1f}" height="{bar_h}" '
                      f'fill="{color}" rx="3"/>')
        parts.append(f'<text x="{margin_left + w + 6:.1f}" y="{y + bar_h * 0.68:.1f}" font-size="12" '
                      f'fill="{COLOR_TEXT_MUTED}">{html.escape(disp)}</text>')
        y += bar_h + gap
    return (f'<svg viewBox="0 0 {width} {height}" width="100%" height="{height}" role="img" '
            f'aria-label="Топ альбомов по размеру">' + "".join(parts) + "</svg>")


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
.sheet {{ max-width: 780px; margin: 0 auto; padding: 32px 20px 12px; }}
.card {{ background: #fff; border: 1px solid var(--line); border-radius: 12px; padding: 24px; margin-bottom: 20px; }}
h1 {{ color: var(--accent); font-size: 28px; margin: 0 0 6px; }}
h2 {{ color: var(--accent); font-size: 19px; margin: 0 0 16px; border-bottom: 1px solid var(--line); padding-bottom: 8px; }}
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
.city-list {{ display: flex; flex-wrap: wrap; gap: 6px 10px; padding: 0; margin: 0; list-style: none; }}
.city-list li {{ background: var(--bg); border: 1px solid var(--line); border-radius: 999px; padding: 3px 12px; font-size: 13px; }}
.checklist {{ list-style: none; padding: 0; margin: 0; }}
.checklist li {{ padding: 12px 0; border-bottom: 1px solid var(--line); }}
.checklist li:last-child {{ border-bottom: none; }}
.checklist .title {{ font-weight: 600; }}
.checklist .detail {{ color: var(--muted); font-size: 13px; margin-top: 2px; }}
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

    parts = ['<div class="card">', "<h1>Ваш архив</h1>", '<div class="stat-row">'] + stats + ["</div>"]

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

    parts.append('<p class="bridge">Дальше — цифры архива красиво.</p>')
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

    donuts = [
        ("Тип медиа", [
            ("Фото", model["counts"]["image"], DONUT_PALETTE[0]),
            ("Видео", model["counts"]["video"], DONUT_PALETTE[1]),
            ("RAW", model["counts"]["raw"], DONUT_PALETTE[2]),
        ]),
        ("Итог решений программы", [
            ("Новые файлы", model["decisions"]["appended"], DONUT_PALETTE[0]),
            ("Точные повторы", model["decisions"]["skipped_present"], DONUT_PALETTE[1]),
            ("Похожие кадры сохранены", model["decisions"]["near_dup"], DONUT_PALETTE[2]),
            ("Не прочитано", model["decisions"]["unreadable"], DONUT_PALETTE[3]),
            ("Спорные", model["decisions"]["disputed"], DONUT_PALETTE[4]),
        ]),
        ("Надёжность дат", [
            ("Точная (EXIF)", model["tier_counts"].get("A", 0), DONUT_PALETTE[0]),
            ("Высокая", model["tier_counts"].get("B", 0), DONUT_PALETTE[1]),
            ("Оценочная", model["tier_counts"].get("C", 0), DONUT_PALETTE[2]),
            ("Низкая", model["tier_counts"].get("D", 0), DONUT_PALETTE[3]),
        ]),
    ]
    for title, segments in donuts:
        svg, legend = _svg_donut(segments)
        if not svg:
            continue
        parts.append('<div class="card">')
        parts.append(f"<h2>{html.escape(title)}</h2>")
        parts.append(f'<div class="chart-block">{svg}<div class="legend">{legend}</div></div>')
        parts.append("</div>")

    if model["top_albums"]:
        hbar = _svg_hbar_chart([(name, b, _fmt_bytes(b)) for name, b in model["top_albums"]])
        parts.append('<div class="card">')
        parts.append("<h2>Топ альбомов по размеру</h2>")
        parts.append(hbar)
        parts.append("</div>")

    if model["total_bytes"]:
        cat_items = [(label, model["bytes_by_kind"][key])
                     for key, label in (("image", "Фото"), ("video", "Видео"), ("raw", "RAW"))
                     if model["bytes_by_kind"][key]]
        if cat_items:
            parts.append('<div class="card"><h2>Объём по категориям</h2><div class="stat-row">')
            for label, b in cat_items:
                parts.append(f'<div class="stat"><div class="value">{_fmt_bytes(b)}</div>'
                              f'<div class="label">{html.escape(label)}</div></div>')
            parts.append("</div></div>")

    return "".join(parts)


def _folder_label(path: str) -> str:
    """os.path.dirname() файла прямо в корне SOURCE даёт "" -- os.path.basename("") тоже
    "", без этого получалась бы пустая метка перед счётчиком ("  (2)")."""
    return os.path.basename(path) or path or "корень источника"


def _render_sheet3(model: dict, level: str) -> str:
    items = []

    clusters = model["near_dup_clusters"][:TOP_N]
    if clusters:
        for cluster in clusters:
            names = [os.path.basename(p) for p in cluster[:5]]
            more = f" и ещё {len(cluster) - 5}" if len(cluster) > 5 else ""
            items.append((
                f"Похожая серия из {len(cluster)} кадров",
                "Стоит вручную выбрать лучший: " + ", ".join(html.escape(n) for n in names) + more,
            ))
        if len(model["near_dup_clusters"]) > TOP_N:
            rest_n = len(model["near_dup_clusters"]) - TOP_N
            items.append((
                f"И ещё {rest_n} {_plural(rest_n, 'похожая серия', 'похожие серии', 'похожих серий')}",
                "Полный список — в near_dup_edges.csv (папка __служебные_файлы\\logs).",
            ))

    if model["disputes_total"]:
        folders = model["disputes_by_folder"].most_common(TOP_N)
        detail = "; ".join(f"{html.escape(_folder_label(f))} ({n})" for f, n in folders)
        # analyze-уровень (build_model_from_analyze_stats) не отслеживает разбивку по папкам
        # -- только итоговое число; TARGET/dry-run уровень отслеживает (см. build_model_from_rows).
        tail = f", сгруппированы по исходной папке: {detail}." if detail else "."
        items.append((
            f"{_n_files(model['disputes_total'])} не удалось однозначно распознать",
            f"Лежат в _Unsorted{tail}",
        ))

    if model["albums_merged"]:
        names = sorted({r.get("album", "") for r in model["albums_merged"] if r.get("album")})[:TOP_N]
        items.append((
            f"{_n_files(len(model['albums_merged']))} пополнили уже существующие альбомы",
            "Альбомы: " + ", ".join(html.escape(n) for n in names) + (" и другие" if len(names) == TOP_N else ""),
        ))

    if model["dates_review_bc_total"]:
        folders = model["dates_review_by_folder"].most_common(TOP_N)
        detail = "; ".join(f"{html.escape(_folder_label(f))} ({n})" for f, n in folders)
        detail_text = f" Стоит перепроверить при желании: {detail}." if detail else " Стоит перепроверить при желании."
        items.append((
            f"{_n_files(model['dates_review_bc_total'])} получили дату приблизительно",
            detail_text.strip(),
        ))

    if model["unreadable"]:
        items.append((
            f"{_n_files(len(model['unreadable']))} не прочитано",
            "Обычно помогает закрыть программу, которая могла держать файл открытым, и "
            "запустить тот же прогон ещё раз (см. FAQ).",
        ))

    if level == "analyze":
        items.append((
            "Эта часть рекомендаций дорабатывается",
            "Отдельный чек-лист для «источник для будущей сборки» / «уже готовый архив» "
            "появится в одной из следующих версий.",
        ))

    if not items:
        return ""

    rows = "".join(f'<li><div class="title">{html.escape(t)}</div><div class="detail">{d}</div></li>'
                    for t, d in items)
    return f'<div class="card"><h2>Что стоит проверить</h2><ul class="checklist">{rows}</ul></div>'


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
        "albums_merged": [],  # analyze ничего не пишет физически -- "слияние" не происходит
        "dates_review_by_folder": Counter(),
        "dates_review_bc_total": stats.tier_counts.get("B", 0) + stats.tier_counts.get("C", 0),
        "unreadable": [{}] * stats.n_broken_or_zero,
        "rejected_noise_total": 0,
    }


# ============================================================================
# 7. Публичный вход
# ============================================================================


def _generate_from_model(model: dict, out_path: str, level: str, program_name: str) -> None:
    body = _render_sheet1(model) + _render_sheet2(model) + _render_sheet3(model, level)
    _write(out_path, _page_shell(f"{program_name} — отчёт архива", body))


def generate_report(data: dict, out_path: str, level: str = "target",
                     program_name: str = "PhotoArchive") -> None:
    """level: "target" (полный archive-прогон) | "workdir" ([2]/--dry-run) — оба читают
    dict[str, list[dict]] (CSV TARGET или CollectingRunLogs.rows). Для
    analyze/analyze-full/analyze-quick см. generate_report_from_analyze_stats()."""
    model = build_model_from_rows(data)
    _generate_from_model(model, out_path, level, program_name)


def generate_report_from_analyze_stats(stats, out_path: str, level: str = "analyze",
                                        program_name: str = "PhotoArchive") -> None:
    model = build_model_from_analyze_stats(stats)
    _generate_from_model(model, out_path, level, program_name)
