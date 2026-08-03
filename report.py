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

# Пункт D ("большой разбор report.html", SESSION-HANDOFF.txt): ISO 3166-1 alpha-2 -> русское
# название страны -- для остального мира на диаграмме "География" ("Город, CC" -> "Город
# (Страна)"). place_for_gps() (photosort_win.py) отдаёт код именно в этом регистре/формате
# (reverse_geocoder). Не тянуть pycountry ради этого (та библиотека даёт только английские
# названия) -- статическая таблица, весь стандартный список ISO 3166-1, код без перевода
# (редкий/устаревший/спорный) просто показывается как есть в _country_name_ru()."""
COUNTRY_NAMES_RU = {
    "AD": "Андорра", "AE": "ОАЭ", "AF": "Афганистан", "AG": "Антигуа и Барбуда",
    "AI": "Ангилья", "AL": "Албания", "AM": "Армения", "AO": "Ангола", "AQ": "Антарктида",
    "AR": "Аргентина", "AS": "Американское Самоа", "AT": "Австрия", "AU": "Австралия",
    "AW": "Аруба", "AX": "Аландские острова", "AZ": "Азербайджан",
    "BA": "Босния и Герцеговина", "BB": "Барбадос", "BD": "Бангладеш", "BE": "Бельгия",
    "BF": "Буркина-Фасо", "BG": "Болгария", "BH": "Бахрейн", "BI": "Бурунди", "BJ": "Бенин",
    "BL": "Сен-Бартелеми", "BM": "Бермуды", "BN": "Бруней", "BO": "Боливия",
    "BQ": "Бонэйр, Синт-Эстатиус и Саба", "BR": "Бразилия", "BS": "Багамы", "BT": "Бутан",
    "BV": "остров Буве", "BW": "Ботсвана", "BY": "Беларусь", "BZ": "Белиз", "CA": "Канада",
    "CC": "Кокосовые острова", "CD": "ДР Конго", "CF": "ЦАР", "CG": "Республика Конго",
    "CH": "Швейцария", "CI": "Кот-д'Ивуар", "CK": "острова Кука", "CL": "Чили",
    "CM": "Камерун", "CN": "Китай", "CO": "Колумбия", "CR": "Коста-Рика", "CU": "Куба",
    "CV": "Кабо-Верде", "CW": "Кюрасао", "CX": "остров Рождества", "CY": "Кипр",
    "CZ": "Чехия", "DE": "Германия", "DJ": "Джибути", "DK": "Дания", "DM": "Доминика",
    "DO": "Доминиканская Республика", "DZ": "Алжир", "EC": "Эквадор", "EE": "Эстония",
    "EG": "Египет", "EH": "Западная Сахара", "ER": "Эритрея", "ES": "Испания",
    "ET": "Эфиопия", "FI": "Финляндия", "FJ": "Фиджи", "FK": "Фолклендские острова",
    "FM": "Микронезия", "FO": "Фарерские острова", "FR": "Франция", "GA": "Габон",
    "GB": "Великобритания", "GD": "Гренада", "GE": "Грузия", "GF": "Французская Гвиана",
    "GG": "Гернси", "GH": "Гана", "GI": "Гибралтар", "GL": "Гренландия", "GM": "Гамбия",
    "GN": "Гвинея", "GP": "Гваделупа", "GQ": "Экваториальная Гвинея", "GR": "Греция",
    "GS": "Южная Георгия и Южные Сандвичевы острова", "GT": "Гватемала", "GU": "Гуам",
    "GW": "Гвинея-Бисау", "GY": "Гайана", "HK": "Гонконг",
    "HM": "острова Херд и Макдональд", "HN": "Гондурас", "HR": "Хорватия", "HT": "Гаити",
    "HU": "Венгрия", "ID": "Индонезия", "IE": "Ирландия", "IL": "Израиль", "IM": "Остров Мэн",
    "IN": "Индия", "IO": "Британская территория в Индийском океане", "IQ": "Ирак",
    "IR": "Иран", "IS": "Исландия", "IT": "Италия", "JE": "Джерси", "JM": "Ямайка",
    "JO": "Иордания", "JP": "Япония", "KE": "Кения", "KG": "Киргизия", "KH": "Камбоджа",
    "KI": "Кирибати", "KM": "Коморы", "KN": "Сент-Китс и Невис", "KP": "КНДР",
    "KR": "Южная Корея", "KW": "Кувейт", "KY": "Каймановы острова", "KZ": "Казахстан",
    "LA": "Лаос", "LB": "Ливан", "LC": "Сент-Люсия", "LI": "Лихтенштейн", "LK": "Шри-Ланка",
    "LR": "Либерия", "LS": "Лесото", "LT": "Литва", "LU": "Люксембург", "LV": "Латвия",
    "LY": "Ливия", "MA": "Марокко", "MC": "Монако", "MD": "Молдова", "ME": "Черногория",
    "MF": "Сен-Мартен", "MG": "Мадагаскар", "MH": "Маршалловы острова",
    "MK": "Северная Македония", "ML": "Мали", "MM": "Мьянма", "MN": "Монголия",
    "MO": "Макао", "MP": "Северные Марианские острова", "MQ": "Мартиника",
    "MR": "Мавритания", "MS": "Монтсеррат", "MT": "Мальта", "MU": "Маврикий",
    "MV": "Мальдивы", "MW": "Малави", "MX": "Мексика", "MY": "Малайзия", "MZ": "Мозамбик",
    "NA": "Намибия", "NC": "Новая Каледония", "NE": "Нигер", "NF": "Остров Норфолк",
    "NG": "Нигерия", "NI": "Никарагуа", "NL": "Нидерланды", "NO": "Норвегия", "NP": "Непал",
    "NR": "Науру", "NU": "Ниуэ", "NZ": "Новая Зеландия", "OM": "Оман", "PA": "Панама",
    "PE": "Перу", "PF": "Французская Полинезия", "PG": "Папуа — Новая Гвинея",
    "PH": "Филиппины", "PK": "Пакистан", "PL": "Польша", "PM": "Сен-Пьер и Микелон",
    "PN": "Питкэрн", "PR": "Пуэрто-Рико", "PS": "Палестина", "PT": "Португалия",
    "PW": "Палау", "PY": "Парагвай", "QA": "Катар", "RE": "Реюньон", "RO": "Румыния",
    "RS": "Сербия", "RU": "Россия", "RW": "Руанда", "SA": "Саудовская Аравия",
    "SB": "Соломоновы острова", "SC": "Сейшелы", "SD": "Судан", "SE": "Швеция",
    "SG": "Сингапур", "SH": "Остров Святой Елены", "SI": "Словения",
    "SJ": "Шпицберген и Ян-Майен", "SK": "Словакия", "SL": "Сьерра-Леоне", "SM": "Сан-Марино",
    "SN": "Сенегал", "SO": "Сомали", "SR": "Суринам", "SS": "Южный Судан",
    "ST": "Сан-Томе и Принсипи", "SV": "Сальвадор", "SX": "Синт-Мартен", "SY": "Сирия",
    "SZ": "Эсватини", "TC": "острова Тёркс и Кайкос", "TD": "Чад",
    "TF": "Французские Южные и Антарктические территории", "TG": "Того", "TH": "Таиланд",
    "TJ": "Таджикистан", "TK": "Токелау", "TL": "Восточный Тимор", "TM": "Туркменистан",
    "TN": "Тунис", "TO": "Тонга", "TR": "Турция", "TT": "Тринидад и Тобаго", "TV": "Тувалу",
    "TW": "Тайвань", "TZ": "Танзания", "UA": "Украина", "UG": "Уганда",
    "UM": "Внешние малые острова США", "US": "США", "UY": "Уругвай", "UZ": "Узбекистан",
    "VA": "Ватикан", "VC": "Сент-Винсент и Гренадины", "VE": "Венесуэла",
    "VG": "Британские Виргинские острова", "VI": "Виргинские острова США", "VN": "Вьетнам",
    "VU": "Вануату", "WF": "Уоллис и Футуна", "WS": "Самоа", "YE": "Йемен",
    "YT": "Майотта", "ZA": "ЮАР", "ZM": "Замбия", "ZW": "Зимбабве",
}


def _country_name_ru(cc: str) -> str:
    """Код без перевода в таблице (редкий/устаревший/спорный территориальный код) -- сам код
    как есть, не молчаливая потеря/исключение (тот же принцип "log it, keep going", что и у
    остального report.py)."""
    return COUNTRY_NAMES_RU.get(cc, cc)

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
    функцией 2026-07-20, изначально чтобы её можно было вызвать дважды на разных
    подмножествах строк ("новое из этого пополнения" и "накопилось раньше", см.
    _split_rows_by_time()/generate_report()). REVIEW-HANDOFF.md, Раунд 44: "раньше"-половина
    убрана 2026-07-31 (729a2de, кумулятивное "Ваш архив" из обычного отчёта) -- сегодня
    вызывается один раз, на отобранном по времени "новое" (generate_report()) либо на полной
    model целиком (build_model_from_rows(), level=="analyze"/полный кумулятивный скан) --
    не на паре подмножеств одного и того же прогона.

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
        # Пункт B.3 ("большой разбор report.html", SESSION-HANDOFF.txt): переиспользует
        # _cluster_exact_dup_full() (раньше только для полной страницы сверки) вместо
        # отдельной "облегчённой" _cluster_exact_dup() -- превью-карточке тоже нужен origin
        # ("скопировано из ‹origin›"), которого у старой облегчённой версии не было вообще.
        "exact_dup_groups": _cluster_exact_dup_full(data),
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
        # Задача 5: та же группировка/превью, что Tier B/C (dates_review_detail) -- уже
        # отфильтрована до ByDate/0000-undated/ (см. _cluster_undated()). Ключ намеренно
        # ОТСУТСТВУЕТ в build_model_from_analyze_stats() (analyze не отслеживает undated_media
        # поштучно) -- _build_checklist_items() различает "нет detail вообще" (analyze,
        # .get() без ключа -> None) от "detail есть, но все файлы в Albums/" ([] после
        # фильтра) -- разное поведение рендера для каждого случая.
        "undated_detail": _cluster_undated(data.get("undated_media", [])),
        # Пункт B.5 ("большой разбор report.html", SESSION-HANDOFF.txt): интро-фраза "сохранены
        # ВСЕ N файлов, включая M спорных" -- N должно включать M (спорные физически тоже
        # сохранены, в _Unsorted, просто не идут через run_logs.appended()/appended.csv,
        # см. photosort_win.py:_process_decided_item() -- disputed пишется отдельным логом,
        # run_logs.disputed()/disputes.csv), иначе M выглядел бы НЕ подмножеством N.
        "total_new": len(appended) + len(disputes),
    }


def _split_rows_by_time(data: dict, run_start: str) -> dict:
    """Отбирает CSV-строки (только категории Листа 3) на "этот прогон" (timestamp >=
    run_start) -- по первой колонке timestamp, которая уже есть у каждого CSV-лога
    (RunLogs._ts(), формат "%Y-%m-%d %H:%M:%S", лексикографически сравнимый). `run_start` --
    тот же формат, захваченный в photosort_win.py ДО начала обработки источников -- см.
    generate_report(). "appended"/"undated_media" нужны здесь для флагов качества/Tier D
    в _build_checklist_fields() -- сами по себе не категории Листа 3, но их разбивка по
    времени строится по тому же timestamp, тем же способом. "skipped" (Раунд 31,
    REVIEW-HANDOFF.md) -- та же логика для exact_dup_groups, отдельная от Листа 3 карточка
    (_render_exact_dup_examples()), но нуждается в том же отборе по времени.

    REVIEW-HANDOFF.md, Раунд 44: раньше возвращала пару (new, before) -- "before"-половина
    (всё СТАРШЕ run_start) с 2026-07-31 (коммит 729a2de, убрана кумулятивная "Ваш архив")
    нигде не рендерится и не читается ни одним вызывающим кодом -- вычислялась впустую на
    каждом обычном отчёте. Строит и возвращает ТОЛЬКО отобранное "новое"."""
    names = ("near_dup_edges", "disputes", "dates_review", "unreadable", "appended",
              "undated_media", "skipped")
    new = {}
    for name in names:
        rows = data.get(name, [])
        new[name] = [r for r in rows if (r.get("timestamp") or "") >= run_start]
    return new


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
            # _render_sheet1() показывает папку+имя тем же способом, что и near-dup/
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
    # 2026-07-26, по просьбе пользователя: "Дубли" на диаграмме "Итог решений
    # программы" не показывали разбивку по типу файла -- та же классификация по расширению
    # (_media_kind()), что уже используется для "Тип медиа"/"Объём по категориям" выше по
    # модулю, применённая к matched_with (реальный путь в архиве, decisions["skipped_present"]
    # считает те же строки skipped -- ЛЮБАЯ причина, не только already_present, см. коммент у
    # _cluster_exact_dup_full() про осознанное расхождение с карточкой "Дубли — примеры").
    skipped_present_by_type = Counter(_media_kind(r.get("matched_with", "")) for r in skipped)

    # Пункт E ("большой разбор report.html", SESSION-HANDOFF.txt): "camera" -- новая колонка
    # appended.csv (photosort_win.py:RunLogs.appended(), rec.camera/camera_from_tags()),
    # файлы без определённой камеры (скриншоты, интернет-картинки, сканы) -- пустая строка,
    # не входят сюда вообще (не искусственная категория "неизвестно").
    cameras = Counter(r.get("camera") for r in appended if r.get("camera"))

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
        "cameras": cameras,
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


def _cluster_exact_dup_full(data: dict) -> list:
    """2026-07-26, обсуждение с пользователем: недоверчивый пользователь, который не
    принимает описание алгоритма как аргумент, хочет проверить дедуп САМ в файловой
    системе -- для каждого заархивированного файла увидеть, откуда он взят, и какие именно
    файлы источника были признаны его дублями (путь+имя, не просто число). Питает и превью
    "Дубли — примеры" (Лист 3, урезано топ-N/"Показать ещё"), и отдельную полную
    страницу сверки (generate_dedup_verification_page(), без урезания) -- см. пункт B.3 ниже.

    reason=="already_present" ТОЛЬКО -- реальный "файл уже есть в архиве" (пул дедупа,
    decide()), не raw_skipped_has_jpeg (RAW осознанно не зеркалирован при MIRROR_RAW=false --
    решение конфига, не совпадение содержимого) и не identical_at_destination (коллизия имён
    при записи, редкий отдельный случай) -- иначе число здесь разошлось бы с тем, что реально
    означает "дубль" по сути, а не просто со всем, что когда-либо попало в
    skipped.csv. Диаграмма "Итог решений программы" (model["decisions"]["skipped_present"])
    по-прежнему считает все три причины -- та цифра осталась как есть, разошлась с этой
    осознанно (см. обсуждение с пользователем).

    Группировка по папке (matched_with, через _friendly_target_dir()) -- каждая запись несёт
    origin (source из appended.csv для этого же dest -- "откуда скопирован") и полный список
    source всех найденных дублей (пункт B.3, "большой разбор report.html",
    SESSION-HANDOFF.txt -- единственный источник данных теперь и для превью-карточки
    _exact_dup_checklist_item(), и для полной страницы сверки, раньше это были две отдельные,
    разошедшиеся по возможностям функции).

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
    _cluster_exact_dup_full() -- по исходной папке (_win_dirname), не плоским списком.

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
    Раунда 32. Та же форма группировки, что _cluster_disputes()/_cluster_exact_dup_full().

    Группировка по dest (папка в АРХИВЕ, через _friendly_target_dir), не по source -- в
    отличие от _cluster_disputes() (файлы уходят в _Unsorted, зеркалируя структуру источника,
    там что source, что dest дают одно и то же дерево), файлы с приблизительной датой лежат
    как обычно в Albums/ByDate -- "где искать СЕЙЧАС" однозначно только через dest (тот же
    принцип, что уже применён к undated_media/Tier D выше).

    2026-08-02, прямое замечание пользователя (тот же принцип, что уже применён к Tier D в
    _cluster_undated() ниже): по RULES.md (блок UNDATED) точность даты определяет МЕСТО файла
    только внутри ByDate -- Albums/ раскладывается по структуре исходных папок независимо от
    даты, там неважно, точная дата, приблизительная или отсутствует вовсе. Файлы в Albums/
    отфильтрованы здесь же -- "стоит перепроверить" для них по факту бесполезно (место файла
    от даты не зависит), тот же довод, что уже был у Tier D.

    Возвращает [(folder, [(name, tier), ...]), ...], отсортировано по убыванию размера
    группы."""
    # Пункт B.9 ("большой разбор report.html", SESSION-HANDOFF.txt): группировка теперь по
    # АБСОЛЮТНОЙ папке (_win_dirname(dest)), не по уже "офрендленному" _friendly_target_dir()
    # -- та лишена корня TARGET, из неё нельзя было бы построить file://-ссылку обратно.
    # Friendly-текст для отображения по-прежнему строится на стороне рендера
    # (_dates_review_checklist_item()), сам абсолютный путь используется только для href.
    by_folder = defaultdict(list)
    for r in dates_review_rows:
        if r.get("tier") not in ("B", "C"):
            continue
        dest = r.get("dest", "")
        if "Albums" in dest.split("\\"):
            continue
        name = _win_basename(dest) or dest
        by_folder[_win_dirname(dest)].append((name, r.get("tier", "")))
    groups = [(folder, items) for folder, items in by_folder.items()]
    groups.sort(key=lambda g: -len(g[1]))
    return groups


def _cluster_undated(undated_rows: list) -> list:
    """Задача 5 (SESSION-HANDOFF.txt, пакет "боевой прогон D:\\"): тот же паттерн
    группировки, что уже применён к Tier B/C выше (_cluster_dates_review()) -- на боевом
    прогоне 274 файла Tier D сплошным абзацем через запятую (без группировки) оказались
    нечитаемы, старый комментарий ошибочно предполагал, что Tier D "их всегда мало".

    По RULES.md (блок UNDATED) отсутствие даты определяет МЕСТО файла только внутри ByDate
    (недатированное уходит в ByDate/0000-undated/) -- Albums/ раскладывается по структуре
    исходных папок независимо от даты, там отсутствие даты ни на что не влияет. Живая
    проверка на данных того же боевого прогона: все 274 файла лежали в Albums/, ни одного в
    ByDate/0000-undated/ -- совет "стоит проставить дату" был для них по факту бесполезен
    (место файла от даты не зависит). Файлы вне ByDate/0000-undated/ отфильтрованы здесь же,
    ДО подсчёта и рендера -- по решению пользователя не показывать их вообще, не только
    смягчить тон.

    Возвращает [(folder, [name, ...]), ...], отсортировано по убыванию размера группы."""
    by_folder = defaultdict(list)
    for r in undated_rows:
        dest = r.get("dest", "")
        if "0000-undated" not in dest.split("\\"):
            continue
        name = _win_basename(dest) or dest
        by_folder[_win_dirname(dest)].append(name)
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


def _fmt_run_duration(total_seconds: float) -> str:
    """Длительность ЭТОГО прогона (секция "Пополнение архива", run_stats["duration_seconds"],
    см. photosort_win.py:_run_impl()) -- в отличие от _fmt_video_duration() выше (округление
    до целых часов, огрубляет масштаб архива), здесь читатель хочет знать, сколько реально
    длился именно этот запуск -- часы и минуты словами, не ЧЧ:ММ:СС: отчёт читают один раз
    постфактум, а не следят за живым таймером (тот формат -- у статус-строки Фазы 2,
    ProgressReporter._build_two_line_status(), другая аудитория/момент)."""
    total_seconds = max(int(total_seconds), 0)
    hours, rem = divmod(total_seconds, 3600)
    minutes = rem // 60
    if hours:
        parts = [f"{hours} {_plural(hours, 'час', 'часа', 'часов')}"]
        if minutes:
            parts.append(f"{minutes} {_plural(minutes, 'минута', 'минуты', 'минут')}")
        return " ".join(parts)
    if minutes:
        return f"{minutes} {_plural(minutes, 'минута', 'минуты', 'минут')}"
    return "меньше минуты"


# ============================================================================
# 3. SVG-графики (инлайн, без внешних библиотек)
# ============================================================================


# SESSION-HANDOFF.txt, "большой разбор report.html" (2026-07-31), пункт B.7 -- живой пример
# пользователя нашёл: архив с несколькими годами вперемешку (1973 + пробел + 2003-2019)
# зеркально возможен и с искажённым EXIF (одна битая дата в 1902 среди прочих 2020-х) --
# заполнять весь диапазон нулевыми годами в ЭТОМ случае раздуло бы график на сотни пустых
# строк ради одного выброса, а не показало бы реальный провал в истории архива. Порог
# консервативный (реалистичный "старый диск за много десятилетий", FAQ.md) -- выше него
# _svg_year_hbar_chart() ниже не пытается угадать, какой конец диапазона выброс, просто не
# заполняет пропуски (те же годы, что и раньше, без нулевых строк).
_YEAR_HBAR_MAX_SPAN = 80


def _svg_year_hbar_chart(counter: Counter, width=680, bar_h=22, gap=8, color=COLOR_ACCENT) -> str:
    """Горизонтальные столбики "Медиафайлы по годам" (SESSION-HANDOFF.txt, "большой разбор
    report.html", пункт B.7) -- та же форма, что "Топ альбомов" (_svg_hbar_chart), но
    хронологический порядок (не по убыванию значения) и явные нулевые строки для каждого года
    БЕЗ снимков внутри диапазона архива (см. _YEAR_HBAR_MAX_SPAN про исключение). Раньше
    вертикальная _svg_bar_chart() просто пропускала отсутствующий год в списке столбцов --
    неотличимо на глаз от "программа потеряла данные за этот год", хотя снимков за него
    действительно нет. Замена прежней _svg_bar_chart() -- та же единственная область
    применения (эта диаграмма и в обычном report.html Sheet2, и в passport.html), горизонтальная
    форма не нуждается в прореживании подписей (как у вертикальной при большом n) -- у каждого
    года и так своя строка."""
    if not counter:
        return ""
    min_y, max_y = min(counter), max(counter)
    if max_y - min_y + 1 <= _YEAR_HBAR_MAX_SPAN:
        items = [(str(y), counter.get(y, 0)) for y in range(min_y, max_y + 1)]
    else:
        items = [(str(y), v) for y, v in sorted(counter.items())]
    max_v = max(v for _, v in items) or 1
    margin_left, margin_right = 54, 68
    plot_w = width - margin_left - margin_right
    height = len(items) * (bar_h + gap) + gap
    parts = []
    y = gap
    for label, v in items:
        w = plot_w * (v / max_v) if v else 0.0
        parts.append(f'<text x="{margin_left - 8}" y="{y + bar_h * 0.68:.1f}" font-size="12" '
                      f'text-anchor="end" fill="{COLOR_TEXT}">{label}</text>')
        if w:
            parts.append(f'<rect x="{margin_left}" y="{y:.1f}" width="{w:.1f}" height="{bar_h}" '
                          f'fill="{color}" rx="3"/>')
        parts.append(f'<text x="{margin_left + w + 6:.1f}" y="{y + bar_h * 0.68:.1f}" font-size="12" '
                      f'fill="{COLOR_TEXT_MUTED}">{_n_files(v)}</text>')
        y += bar_h + gap
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
.card {{ background: #fff; border: 1px solid var(--line); border-radius: 12px; padding: 20px 22px; margin-bottom: 16px; overflow-wrap: anywhere; }}
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
.checklist {{ list-style: none; padding: 0; margin: 0; }}
.checklist li {{ padding: 10px 0; border-bottom: 1px solid var(--line); }}
.checklist li:last-child {{ border-bottom: none; }}
.checklist .title {{ font-weight: 600; }}
.checklist .detail {{ color: var(--muted); font-size: 14px; margin-top: 2px; overflow-wrap: anywhere; }}
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
/* [4] Паспорт архива -- "проблем нет" так же заметно, как "проблема есть" (design-сессия
   2026-07-31), в отличие от .checklist/.trust-list выше, которые вообще не рендерятся при
   пустой категории -- здесь ВСЕГДА один пункт на каждую проверку, .ok/.attn просто разные
   маркеры/цвет одного и того же списка. */
.integrity-list {{ list-style: none; padding: 0; margin: 0; }}
.integrity-list li {{ padding: 8px 0; border-bottom: 1px solid var(--line); }}
.integrity-list li:last-child {{ border-bottom: none; }}
.integrity-list li.ok::before {{ content: "✓  "; color: var(--accent); font-weight: 700; }}
.integrity-list li.attn::before {{ content: "⚠  "; color: var(--accent2); font-weight: 700; }}
/* Дерево структуры архива (SESSION-HANDOFF.txt, "большой разбор report.html", пункт A;
   речь пользователя 2026-08-02, задача 2 -- "без поворотных уголков смотрится непонятно",
   сделать как на лендинге). Старый вариант ставил border-left на весь вложенный <ul>
   одной непрерывной высоты -- линия не останавливалась у последнего элемента списка, ничто
   не отличало "├" (есть ещё соседи ниже) от "└" (последний, ветка кончилась). Новый вариант --
   тот же приём, что уже на лендинге (index.html/assets/site.css, "Соединительные линии
   дерева", Design.md): line на КАЖДОМ <li> (не на <ul>), :last-child укорачивает её до
   середины строки -- это и даёт настоящий "└". Иконка папки -- inline-SVG через CSS mask
   (та же техника, что и остальные line-иконки сайта, Design.md 4.5), не эмодзи. */
.tree, .tree ul {{ list-style: none; margin: 0; padding: 0; line-height: 1.85; }}
.tree ul {{ padding-left: 1.35rem; margin-left: .4rem; }}
.tree li {{ position: relative; margin: 0; }}
.tree-name {{ font-weight: 600; }}
.tree > li > .tree-name {{ color: var(--accent); }}
.tree-stat {{ color: var(--muted); font-size: 13px; margin-left: 8px; }}
.tree ul li {{ padding-left: 1.15rem; }}
.tree ul li::before {{
  content: ""; position: absolute; left: 0; top: 0; width: 0; height: 100%;
  border-left: 1px solid var(--line);
}}
.tree ul li:last-child::before {{ height: 1em; }}
.tree ul li::after {{
  content: ""; position: absolute; left: 0; top: 1em; width: .9rem; height: 0;
  border-top: 1px solid var(--line);
}}
.tree-ico {{
  display: inline-block; width: .9rem; height: .78rem; margin-right: .35rem;
  vertical-align: -0.1em; background-color: var(--muted);
  -webkit-mask: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'%3E%3Cpath d='M2 5a2 2 0 0 1 2-2h4.5l1.7 2H20a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5Z'/%3E%3C/svg%3E") center/contain no-repeat;
  mask: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'%3E%3Cpath d='M2 5a2 2 0 0 1 2-2h4.5l1.7 2H20a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5Z'/%3E%3C/svg%3E") center/contain no-repeat;
}}
.tree > li > .tree-ico {{ background-color: var(--accent); }}
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
                      f'<div class="label">сэкономлено на дублях</div></div>')

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
        # файл. Тот же способ отображения, что у near-dup/дублей (папка + имя, не
        # сырой путь целиком). level=="analyze" (build_model_from_analyze_stats()) кладёт сюда
        # origin_display (путь в ИСТОЧНИКЕ, ByDate/Albums там не бывает) -- folder тогда пусто,
        # деградирует до одного имени файла, не ошибка.
        file_str = ""
        if oldest_path:
            name = html.escape(_win_basename(oldest_path))
            folder = _friendly_target_dir(oldest_path)
            file_text = f'{html.escape(folder)}\\{name}' if folder else name
            # Пункт B.8: file://-ссылка прямо на файл (не на папку), открывает его напрямую --
            # oldest_path сам абсолютный (level=="target", реальный dest в TARGET), не friendly-
            # усечённая folder-строка выше (та только для отображения).
            file_str = f' — {_file_link_or_text(file_text, oldest_path)}'
        parts.append(f'<p><b>Самый старый файл:</b> {date_str}{place_str}{file_str}</p>')

    if model["year_months"]:
        busiest_ym, busiest_n = model["year_months"].most_common(1)[0]
        parts.append(f'<p><b>Самый насыщенный месяц:</b> {busiest_ym} — {busiest_n} файлов</p>')
    elif years:
        busiest_y, busiest_n = years.most_common(1)[0]
        parts.append(f'<p><b>Самый насыщенный год:</b> {busiest_y} — {busiest_n} файлов</p>')

    # Пункт D ("большой разбор report.html", SESSION-HANDOFF.txt): блок географии убран из
    # шапки -- переехал на Лист 2 отдельной диаграммой (_geo_hbar()), город-теги без счётчика
    # здесь дублировали то же самое менее информативно.

    if model["video_duration_seconds"]:
        parts.append(f'<p><b>Видео в архиве:</b> суммарно '
                      f'{_fmt_video_duration(model["video_duration_seconds"])} отснятого материала</p>')

    parts.append('<p class="bridge">Дальше — ваш архив в цифрах.</p>')
    parts.append("</div>")
    return "".join(parts)


def _render_trust_block(level: str, unreadable_count: int = 0) -> str:
    """4.1/4.3 (PROMPT_report_marketing.md): баннер доверия -- самая частая рекомендация всех
    шести источников маркетингового ТЗ, полностью отсутствовала в HTML (фраза была только в
    консоли, photosort_win.py:6484/6209). Одна строка, НЕ карточка целиком (раздел 4.1 явно
    просит не раздувать плотный первый экран) + компактный чек-лист из уже существующих фактов
    рядом с ней (раздел 4.3, perplexity-источник) -- задача снять тревогу за первые секунды
    просмотра, не сообщить что-то новое. В самом начале ЛЮБОГО отчёта (все level), включая
    заглушку (см. generate_placeholder_report()).

    unreadable_count (пункт B.1, "большой разбор report.html", SESSION-HANDOFF.txt): пункт про
    ошибки чтения раньше выводился безусловно -- на чистом архиве без единого нечитаемого
    файла звучал как ответ на вопрос, которого никто не задавал ("а что, были ошибки?")."""
    items = [
        "Файлы не удалялись.",
        "Оригиналы сохранены на своих местах.",
    ]
    if unreadable_count:
        items.append("Ошибки чтения показаны отдельно, не смешаны с остальным.")
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
        "Только то, что сделал именно этот запуск программы. Чтобы проверить весь архив "
        "целиком — используйте «Паспорт архива» отдельно (см. совет в конце отчёта)."
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
    # Живой репорт пользователя (2026-08-01): сколько реально заняло время -- duration_seconds
    # считается в photosort_win.py:_run_impl() (run_start уже существовал для тайминг-логов
    # Фазы 0, просто раньше не доживал до stats). Не "было бы" в preview -- сканирование/анализ
    # в [2]/--dry-run РЕАЛЬНО заняли это время (в отличие от копирования/дедупа выше, которые
    # в dry-run гипотетические) -- меняется только существительное (проверка/сборка), не модальность.
    duration_seconds = run_stats.get("duration_seconds", 0)
    if duration_seconds:
        duration_label = "заняла проверка" if preview else "заняла сборка"
        stats_html.append(f'<div class="stat"><div class="value">{_fmt_run_duration(duration_seconds)}</div>'
                           f'<div class="label">{duration_label}</div></div>')

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

    # SESSION-HANDOFF.txt, пакет "боевой прогон D:\", задача 2: диаграмма без пояснения не
    # давала понять, что физически легло в архив, а что нет (напр. "Спорные" копируются в
    # _Unsorted, а "Дубли" -- вообще нет, но оба не входят в счётчик "Итоговый архив") --
    # статус теперь виден прямо в подписи легенды, не только в отдельном тексте карточки.
    new_label = "Новые файлы — были бы в архиве" if preview else "Новые файлы — в архиве"
    dup_label = ("Дубли — не копировались бы, уже есть в архиве" if preview else
                 "Дубли — не копировались, уже есть в архиве")
    near_dup_label = ("Похожие кадры — были бы в архиве (сохранены рядом)" if preview else
                       "Похожие кадры — в архиве (сохранены рядом)")
    unreadable_label = ("Не прочитано — не было бы скопировано (ошибка чтения)" if preview else
                         "Не прочитано — не скопировано (ошибка чтения)")
    disputed_label = ("Спорные — были бы сохранены отдельно, не в архиве (_Unsorted)" if preview else
                       "Спорные — сохранены отдельно, не в архиве (_Unsorted)")
    segments = [
        (new_label, max(n_new_total - n_near_dup, 0), CATEGORY_PALETTE[0]),
        (dup_label, n_skipped, CATEGORY_PALETTE[1]),
        (near_dup_label, n_near_dup, CATEGORY_PALETTE[2]),
        (unreadable_label, n_unreadable, CATEGORY_PALETTE[3]),
        (disputed_label, n_disputed, CATEGORY_PALETTE[4]),
    ]
    svg, legend = _svg_pie(segments)
    if svg:
        parts.append(f'<div class="chart-block">{svg}<div class="legend">{legend}</div></div>')
        # Итоговая строка с явным двоичным итогом -- легло физически на диск (новые, включая
        # near-dup, которые уже входят в n_new_total, см. photosort_win.py:5713 + спорные,
        # физически копируемые в _Unsorted) против не скопировано вообще (дубли + нечитаемое).
        landed = n_new_total + n_disputed
        not_copied = n_skipped + n_unreadable
        if landed or not_copied:
            landed_verb = "легло бы физически" if preview else "легло физически"
            not_copied_verb = "не было бы скопировано" if preview else "не скопировано"
            parts.append(
                f'<p class="muted">Итого: {_n_files(landed)} {landed_verb} '
                f'(новые + похожие + спорные), {_n_files(not_copied)} {not_copied_verb} '
                f'(дубли + не прочитано).</p>'
            )

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
        }), "Дубли"),
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
    if n_disputed:
        # Пункт B.6 ("большой разбор report.html", SESSION-HANDOFF.txt): "Спорные — N файлов"
        # раньше оставалось голым числом в легенде диаграммы -- ни разу не объяснялось, ПОЧЕМУ
        # файл спорный и что он не потерян. Общие категории причин (не по каждому файлу --
        # детали по каждому уже даёт чек-лист "Новое в этом пополнении" ниже, если он есть,
        # см. _dispute_checklist_item()), одна фраза-объяснение на всю карточку.
        parts.append(
            '<p class="muted">Спорные — маленькие/похожие на иконку изображения, '
            'анимированные GIF, файлы с повреждёнными данными или нечитаемыми метаданными. '
            'Они не потеряны — лежат в _Unsorted для проверки вручную.</p>'
        )

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
            # Пункт B.4 ("большой разбор report.html", SESSION-HANDOFF.txt): "куда" -- путь от
            # корня архива ("Albums\дедушка"), не голое имя альбома -- однозначно отличимо от
            # "откуда" (полный путь от корня SOURCE, уже показан справа, не трогаем).
            parts.append(f'<p class="muted">«Albums\\{html.escape(album)}» ← {sources}</p>')

    # Пункт B.2 ("большой разбор report.html", SESSION-HANDOFF.txt): список запароленных
    # архивов с полным путём -- раньше был только счётчик (см. print_analyze_report()/
    # build_final_summary() в консоли), путь реально уже есть в walker.archive_logs.
    encrypted = run_stats.get("encrypted_archives") or []
    if encrypted:
        parts.append('<p><b>Архивы, защищённые паролем, — не распакованы:</b></p>')
        paths = "; ".join(_file_link_or_text(html.escape(p), p) for p in sorted(encrypted)[:TOP_N])
        more = (f" и ещё {len(encrypted) - TOP_N}"
                if len(encrypted) > TOP_N else "")
        parts.append(f'<p class="muted">{paths}{more}. Программа не подбирает пароли -- '
                      'распакуйте вручную и запустите сборку ещё раз, чтобы попало и содержимое.</p>')

    # Живой репорт пользователя (2026-08-01): классическая DVD-Video-структура (VIDEO_TS/*.vob)
    # не распознаётся программой как медиа вообще (см. SESSION-HANDOFF.txt, отложенная идея про
    # полноценную поддержку) -- не молчим про это, тот же паттерн, что и у запароленных архивов
    # чуть выше.
    dvd_folders = run_stats.get("dvd_folders") or []
    if dvd_folders:
        parts.append('<p><b>DVD-видео (VIDEO_TS) — не скопировано:</b></p>')
        dvd_paths = "; ".join(_file_link_or_text(html.escape(p), p) for p in sorted(set(dvd_folders))[:TOP_N])
        dvd_more = (f" и ещё {len(set(dvd_folders)) - TOP_N}"
                    if len(set(dvd_folders)) > TOP_N else "")
        parts.append(f'<p class="muted">{dvd_paths}{dvd_more}. Формат DVD-Video сейчас не '
                      'поддерживается -- при желании скопируйте видео в архив вручную.</p>')

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


def _split_home_and_foreign(cities: Counter) -> tuple:
    """Пункт D ("большой разбор report.html", SESSION-HANDOFF.txt): place_for_gps()
    (photosort_win.py) отдаёт "Город" для домашней страны (cfg.home_country) и "Город, CC"
    (ISO alpha-2) для остального мира -- код страны нигде дальше по конвейеру отдельно не
    хранится (ни в appended.csv, ни в модели отчёта), поэтому разбирается здесь по наличию
    ", " -- самый дешёвый путь, без изменения схемы CSV/photosort_win.py. Хрупкость: если
    когда-либо у названия города в данных reverse_geocoder появится собственная запятая,
    разбор сломается -- маловероятно (стандартные англоязычные топонимы geocoder'а), но
    затронутые записи в этом случае просто уйдут в "домашние" (не наоборот, безопаснее).

    Возвращает (home_cities: Counter[город -> N], foreign_countries: Counter[страна -> N])
    -- страна уже переведена на русский (_country_name_ru()), несколько городов одной страны
    суммируются в одну запись."""
    home, foreign = Counter(), Counter()
    for place, n in cities.items():
        if ", " in place:
            _city, cc = place.rsplit(", ", 1)
            foreign[_country_name_ru(cc)] += n
        else:
            home[place] += n
    return home, foreign


def _geo_hbar(cities: Counter, width: int = 680) -> str:
    """Горизонтальные столбики "География" (пункт D) -- заменяет прежнюю круговую диаграмму
    (Sheet2 обычного report.html и Паспорт архива, общий хелпер для обеих, тот же принцип,
    что и у _svg_year_hbar_chart()). Без "прочих": домашние города и зарубежные страны --
    два отдельных, самостоятельно подписанных списка (топ-N каждый), не общая диаграмма с
    одним смешанным "остальное"-сектором, как было у круговой версии."""
    home, foreign = _split_home_and_foreign(cities)
    if not home and not foreign:
        return ""
    parts = []
    if home:
        items = [(city, n, _n_files(n)) for city, n in home.most_common(TOP_N)]
        parts.append('<p class="muted"><b>По вашим местам</b></p>'
                      + _svg_hbar_chart(items, width=width, aria_label="География — свои места"))
    if foreign:
        items = [(country, n, _n_files(n)) for country, n in foreign.most_common(TOP_N)]
        parts.append('<p class="muted"><b>Остальной мир</b></p>'
                      + _svg_hbar_chart(items, width=width, color=COLOR_ACCENT_SECONDARY,
                                        aria_label="География — остальной мир"))
    return "".join(parts)


_MIN_DISTINCT_CAMERAS = 3  # пункт E: "одного-двух пунктов" из ТЗ -- меньше 3 разных камер не
# складывается в осмысленный "топ" (нечего ранжировать), диаграмма не рендерится совсем.


def _top_cameras_chart(cameras: Counter, width: int = 680) -> str:
    """Пункт E ("большой разбор report.html", SESSION-HANDOFF.txt): "Топ камер/устройств
    съёмки" -- горизонтальные столбики (не pie: проценты считались бы от файлов-с-известной-
    камерой, не от всего архива, а многие архивы почти сплошь без EXIF-камеры -- вводящее в
    заблуждение "100%" на диаграмме, где на самом деле известна камера у трети файлов). Файлы
    без определённой камеры -- не искусственная категория "неизвестно", просто не в
    диаграмме."""
    if len(cameras) < _MIN_DISTINCT_CAMERAS:
        return ""
    items = [(cam, n, _n_files(n)) for cam, n in cameras.most_common(TOP_N)]
    return _svg_hbar_chart(items, width=width, aria_label="Топ камер/устройств съёмки")


def _render_geo_card(cities: Counter) -> str:
    """[4] Паспорт архива (2026-07-31): "География" как отдельная карточка, не часть grid-3
    _render_sheet2() -- паспорт не строит остальные диаграммы того листа (тип медиа/объём/
    качество и т.д.), только целостность+сводка+года, географии там пока не было вовсе."""
    hbar = _geo_hbar(cities)
    if not hbar:
        return ""
    return f'<div class="card"><h2>География</h2>{hbar}</div>'


# SESSION-HANDOFF.txt, "большой разбор report.html", пункт A (дерево структуры архива) --
# нужен и для [4] Паспорт архива (`generate_passport_report()`, реальная структура TARGET),
# и для analyze/analyze-quick/analyze-full (`generate_report_from_analyze_stats()`,
# предсказание БУДУЩЕЙ структуры SOURCE) -- оба получают Counter'ы с одинаковой формой ключей
# (photosort_win.py:run_analyze(), AnalyzeStats.tree_folder_counts/tree_folder_bytes,
# "Albums/Свадьба", "ByDate/2024/2024-07 [PhotoArchive]", "RAW", "_Unsorted", ...), эта пара
# функций строит из них рендер, ничего не зная про TARGET/SOURCE. dry-run ([2]/CLI --dry-run)
# сознательно НЕ подключён -- отдельный код-путь (run_for_source()/decide(), не
# run_analyze()), нужна отдельная сессия по проводке статистики, не решение "заодно".
_TREE_TOP_ORDER = ["Albums", "ByDate", "RAW", "_Unsorted"]


def _build_archive_tree(tree_folder_counts: Counter, tree_folder_bytes: Counter) -> dict:
    """Плоский Counter (ключ — "/"-путь бакета) -> вложенный dict для рендера. own — (n, bytes)
    ИМЕННО этого узла БЕЗ вложенных (дословно из ТЗ) — суммировать по поддереву решили не
    делать (см. дизайн-обсуждение с пользователем), узел без собственных файлов, но с
    непустыми children — обычная промежуточная папка (сам "ByDate", год "2024" и т.д.)."""
    root = {"own": (0, 0), "children": {}}
    for key, n in tree_folder_counts.items():
        node = root
        for part in key.split("/"):
            node = node["children"].setdefault(part, {"own": (0, 0), "children": {}})
        node["own"] = (n, tree_folder_bytes.get(key, 0))
    return root


def _render_tree_children(children: dict, order: list = None) -> str:
    if not children:
        return ""
    names = list(order) if order else []
    names += sorted(n for n in children if n not in names)
    names = [n for n in names if n in children]
    items = []
    for name in names:
        node = children[name]
        own_n, own_bytes = node["own"]
        stat = (f'<span class="tree-stat">{_n_files(own_n)}, {_fmt_bytes(own_bytes)}</span>'
                if own_n else "")
        nested = _render_tree_children(node["children"])
        items.append(f'<li><span class="tree-ico"></span>'
                      f'<span class="tree-name">{html.escape(name)}</span>{stat}'
                      f'{f"<ul>{nested}</ul>" if nested else ""}</li>')
    return "".join(items)


def _render_archive_tree_card(tree_folder_counts: Counter, tree_folder_bytes: Counter) -> str:
    if not tree_folder_counts:
        return ""
    root = _build_archive_tree(tree_folder_counts, tree_folder_bytes)
    body = _render_tree_children(root["children"], order=_TREE_TOP_ORDER)
    if not body:
        return ""
    return f'<div class="card"><h2>Структура архива</h2><ul class="tree">{body}</ul></div>'


def _render_sheet2(model: dict) -> str:
    parts = ['<div class="card">', "<h2>Медиафайлы по годам</h2>"]
    years_svg = _svg_year_hbar_chart(model["years"])
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

    # 2026-07-26, по просьбе пользователя: "Дубли" на диаграмме "Итог решений
    # программы" не показывали разбивку по типу файла -- см. model["skipped_present_by_type"]
    # (build_model_from_rows(), классификация matched_with тем же _media_kind(), что и "Тип
    # медиа"/"Объём по категориям" выше). Подпись только ненулевых категорий, "" если считать
    # нечего (analyze-уровень: build_model_from_analyze_stats() не строит эту разбивку вообще,
    # .get() выше по функции даёт пустой Counter).
    dup_type_caption = _type_breakdown_caption(
        model.get("skipped_present_by_type", Counter()), "Дубли")

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
            ("Дубли", model["decisions"]["skipped_present"], CATEGORY_PALETTE[1]),
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

    # Пункт D ("большой разбор report.html", SESSION-HANDOFF.txt): круговая диаграмма
    # географии заменена на горизонтальные столбики (_geo_hbar()) -- та же полноширинная
    # карточка, что и "Топ альбомов" выше (hbar-графику нужна настоящая ширина, не колонка
    # grid-3).
    geo_hbar = _geo_hbar(model["cities"])
    if geo_hbar:
        parts.append(f'<div class="card"><h2>География</h2>{geo_hbar}</div>')

    # Пункт E: освободившийся слот на Листе 2 (см. коммент у "География" выше) -- "Топ камер/
    # устройств съёмки".
    cameras_hbar = _top_cameras_chart(model.get("cameras", Counter()))
    if cameras_hbar:
        parts.append(f'<div class="card"><h2>Топ камер/устройств съёмки</h2>{cameras_hbar}</div>')

    return "".join(parts)


def _folder_label(path: str) -> str:
    """_win_dirname() файла прямо в корне SOURCE даёт "" -- _win_basename("") тоже
    "", без этого получалась бы пустая метка перед счётчиком ("  (2)")."""
    return _win_basename(path) or path or "корень источника"


_ABS_WIN_PATH_RE = re.compile(r"^[A-Za-z]:\\")


def _file_link_or_text(display_html: str, path: str) -> str:
    """Пункты B.8/B.9 ("большой разбор report.html", SESSION-HANDOFF.txt): file://-ссылка на
    реальный абсолютный Windows-путь -- тот же паттерн, что уже есть у "Открыть папку с
    архивом" в _render_cta_block(). Работает только когда path реально абсолютный (диск+буква,
    ":\\") -- level=="analyze" кладёт сюда origin_display (путь В ИСТОЧНИКЕ, но БЕЗ корня
    SOURCE, см. SourceItem.origin_display) -- ссылка туда вела бы в никуда, лучше оставить
    обычным текстом, чем показать нерабочую ссылку."""
    if not path or not _ABS_WIN_PATH_RE.match(path):
        return display_html
    href = "file:///" + path.replace("\\", "/")
    return f'<a href="{html.escape(href)}" target="_blank" rel="noopener">{display_html}</a>'


def _unsorted_link(target_path: str = None) -> str:
    """Пункт B.5 ("большой разбор report.html", SESSION-HANDOFF.txt): адрес спорных файлов
    ВСЕГДА один и тот же (_Unsorted, не варьируется) -- безусловная file://-ссылка на неё, тот
    же паттерн, что и "Открыть папку с архивом" в _render_cta_block(). target_path=None
    (analyze-уровень -- нет реального TARGET) -- откатывается на голый текст, как раньше."""
    if not target_path:
        return "_Unsorted"
    href = "file:///" + os.path.join(target_path, "_Unsorted").replace("\\", "/")
    return f'<a href="{html.escape(href)}" target="_blank" rel="noopener">_Unsorted</a>'


def _dispute_checklist_item(group: tuple, target_path: str = None) -> tuple:
    folder, items = group
    labels = [f"{html.escape(name)} ({html.escape(_dispute_reason_label(reason))})"
              for name, reason in items[:5]]
    more = f" и ещё {len(items) - 5} {_plural(len(items) - 5, 'файл', 'файла', 'файлов')}" if len(items) > 5 else ""
    # Пункт B.9: folder -- уже абсолютный путь-источник (_win_dirname(row["source"]), см.
    # _cluster_disputes()), _folder_label() -- только display-текст (базовое имя).
    folder_line = (f"Папка: {_file_link_or_text(html.escape(_folder_label(folder)), folder)}."
                   if folder else "")
    action_line = f"Лежат в {_unsorted_link(target_path)}: {', '.join(labels)}{more}."
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
    # folder -- абсолютная папка в TARGET (_cluster_dates_review()); friendly-текст для показа
    # строится тем же _friendly_target_dir(), что и у near-dup -- фиктивное имя файла на конце,
    # функция всё равно отбрасывает последний сегмент (см. её докстринг).
    friendly = _friendly_target_dir(folder + "\\x") if folder else ""
    folder_line = f"Папка: {_file_link_or_text(html.escape(friendly), folder)}." if friendly else ""
    action_line = f"Стоит перепроверить при желании: {', '.join(labels)}{more}."
    detail = f"{folder_line}<br>{action_line}" if folder_line else action_line
    return f"{_n_files(len(items))} получили дату приблизительно", detail


def _undated_checklist_item(group: tuple) -> tuple:
    """Задача 5: тот же паттерн, что _dates_review_checklist_item() -- превью на 5 файлов +
    folder_line (кликабельная ссылка на саму папку под ByDate/0000-undated/, по решению
    пользователя -- та же ссылка, что уже даёт Tier B/C, здесь раньше не было вообще).
    group -- уже отфильтрован до ByDate/0000-undated/ (см. _cluster_undated())."""
    folder, items = group
    names = [html.escape(n) for n in items[:5]]
    more = f" и ещё {len(items) - 5} {_plural(len(items) - 5, 'файл', 'файла', 'файлов')}" if len(items) > 5 else ""
    friendly = _friendly_target_dir(folder + "\\x") if folder else ""
    folder_line = f"Папка: {_file_link_or_text(html.escape(friendly), folder)}." if friendly else ""
    action_line = f"Стоит проставить дату вручную при желании: {', '.join(names)}{more}."
    detail = f"{folder_line}<br>{action_line}" if folder_line else action_line
    return f"{_n_files(len(items))} вообще без даты", detail


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


def _cluster_checklist_item(cluster: list, verify_link: str = None) -> tuple:
    """verify_link (задача 6, SESSION-HANDOFF.txt, пакет "боевой прогон D:\\"): страница
    "Полная сверка" (см. generate_dedup_verification_page()/_render_near_dup_verification_
    section()) -- при len(dirs)>1 (разные папки, до этой задачи расползался на построчный
    список путей одного файла на строку, live-примеры на боевом прогоне дали разнобой с
    компактной однопапочной веткой ниже) теперь ссылка на полный список вместо построчного
    повтора здесь. None (level!="target", страница не строится вовсе, см. generate_report())
    -- откатывается на старый построчный список, ссылаться некуда."""
    names = [_win_basename(p) for p in cluster[:5]]
    more = f" и ещё {len(cluster) - 5}" if len(cluster) > 5 else ""
    dirs = {_win_dirname(p) for p in cluster}
    # Кластер почти всегда лежит в одной папке (near-dup совпал с уже размещённым соседом по
    # своей же дате/месту) -- один путь один раз, не на каждое имя файла. Разные папки --
    # редкий случай (даты разошлись по краю месяца/при рубеже bydate_granularity) -- тогда
    # путь при каждом имени.
    if len(dirs) == 1:
        folder = _friendly_target_dir(cluster[0])
        # Пункт B.9 ("большой разбор report.html", SESSION-HANDOFF.txt): file://-ссылка на
        # папку -- _win_dirname(cluster[0]), не friendly-строку выше (та только для показа).
        # REVIEW-HANDOFF.md, Раунд 45 [БЛОКЕР]: раньше здесь был os.path.dirname() -- на
        # ubuntu-latest (os.path==posixpath, не понимает "\\") тот отдаёт "" для Windows-пути
        # целиком, ссылка молча пропадала, не считая пустую строку по всему проекту (кроме
        # этого места) -- _win_dirname() уже используется двумя строками выше в этой же функции.
        folder_line = (f"Папка: {_file_link_or_text(html.escape(folder), _win_dirname(cluster[0]))}."
                        if folder else "")
        files = ", ".join(html.escape(n) for n in names)
        action_line = "Стоит вручную выбрать лучший: " + files + more
        # Папка и список файлов -- две разные мысли (где искать / что сравнить), раздельные
        # строки читаются, склеенные в одну через точку -- нет.
        detail = f"{folder_line}<br>{action_line}" if folder_line else action_line
    elif verify_link:
        detail = (
            "Кадры лежат в разных папках. Полный список — в «Полной сверке»: "
            f'<a href="{html.escape(verify_link)}" target="_blank" rel="noopener">'
            "полная сверка похожих серий →</a>."
        )
    else:
        # Разные папки, полной сверки нет (level!="target") -- ссылка на каждый файл
        # индивидуально (не на общую папку, её нет), тот же принцип file://, что и "Самый
        # старый файл" (B.8).
        files = ", ".join(
            _file_link_or_text(
                html.escape((_friendly_target_dir(p) + "\\" if _friendly_target_dir(p) else "") + n), p)
            for p, n in zip(cluster[:5], names, strict=True)
        )
        detail = "Стоит вручную выбрать лучший: " + files + more
    return f"Похожая серия из {len(cluster)} кадров", detail


def _build_checklist_items(fields: dict, target_path: str = None, verify_link: str = None) -> list:
    """Строит список готовых <li>...</li> Листа 3 из полей _build_checklist_fields() --
    вынесено отдельно от рендера 2026-07-20, чтобы вызывать на "новом" и "старом"
    подмножестве раздельно (см. _generate_from_model()). Каждая категория с несколькими
    находками (сейчас только near-dup-серии) сворачивается независимо от других -- превью
    CHECKLIST_PREVIEW_N + <details> на оставшееся, БЕЗ отсылки к CSV (пользователь отчёт
    открывает вместо логов -- решение пользователя 2026-07-20).

    target_path (пункт B.5): доходит до _dispute_checklist_item()/_unsorted_link() -- None
    (analyze-уровень, нет реального TARGET) откатывается на голый текст "_Unsorted".

    verify_link (задача 6): доходит до _cluster_checklist_item() -- None (level!="target",
    страница "Полная сверка" не строится) откатывается на старый построчный список путей."""
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
        cluster_lis = [_li(*_cluster_checklist_item(c, verify_link)) for c in clusters]
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
            dispute_lis = [_li(*_dispute_checklist_item(g, target_path)) for g in dispute_groups]
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
            detail = f"Лежат в {_unsorted_link(target_path)}."
            if folder_detail:
                detail += f"<br>Сгруппированы по исходной папке: {folder_detail}."
            items.append(_li(f"{_n_files(fields['disputes_total'])} не удалось однозначно распознать", detail))

    if fields["dates_review_bc_total"]:
        # 2026-08-02: dates_review_detail -- None означает "detail вообще недоступен"
        # (analyze-уровень, build_model_from_analyze_stats не отслеживает source/dest на
        # файл поштучно) -- отличается от [] ("detail доступен, но ПОСЛЕ фильтра до
        # ByDate-файлов ничего не осталось", см. _cluster_dates_review()) -- тот же паттерн,
        # что уже применён к undated_detail/Tier D ниже.
        review_groups = fields.get("dates_review_detail")
        if review_groups is None:
            # analyze-уровень (build_model_from_analyze_stats) не отслеживает source/dest на
            # файл -- только итоговое число, тот же асимметричный охват, что у disputes выше.
            folders = fields["dates_review_by_folder"].most_common(TOP_N)
            folder_detail = "; ".join(f"{html.escape(_folder_label(f))} ({n})" for f, n in folders)
            detail = "Стоит перепроверить при желании."
            if folder_detail:
                detail += f"<br>Папки-источники: {folder_detail}."
            items.append(_li(f"{_n_files(fields['dates_review_bc_total'])} получили дату приблизительно", detail))
        elif review_groups:
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
        # review_groups == [] -- все Tier B/C файлы лежат в Albums/, точность даты не влияет
        # на их место -- по тому же решению пользователя, что и у Tier D, пункт не рендерится
        # вообще, ни в каком виде.

    if fields["undated_total"]:
        # Tier D -- дата отсутствует вообще (ни EXIF, ни имя файла, ни соседи по папке), не
        # путать с Tier B/C выше ("дата есть, но приблизительная") -- разные находки.
        # Задача 5 (SESSION-HANDOFF.txt, пакет "боевой прогон D:\\"): undated_detail -- None
        # означает "detail вообще недоступен" (analyze-уровень, build_model_from_analyze_stats
        # не отслеживает undated_media поштучно) -- отличается от [] ("detail доступен, но
        # ПОСЛЕ фильтра до ByDate/0000-undated/ ничего не осталось", см. _cluster_undated()) --
        # разное поведение: [] -- пункт вообще не рендерится (Albums/ не показываем никогда),
        # None -- откат на старый общий текст, единственный доступный сигнал на этом уровне.
        undated_groups = fields.get("undated_detail")
        if undated_groups is None:
            items.append(_li(
                f"{_n_files(fields['undated_total'])} вообще без даты",
                "Дата не определилась ни по EXIF, ни по имени файла, ни по соседям в папке — "
                "стоит проставить вручную при желании.",
            ))
        elif undated_groups:
            # Группировка по папке + превью на 5 файлов + "Показать ещё N папок" -- тот же
            # <details>-паттерн, что Tier B/C выше (живой репорт пользователя: 274 файла
            # сплошным абзацем через запятую без группировки читались нечитаемо).
            undated_lis = [_li(*_undated_checklist_item(g)) for g in undated_groups]
            items.extend(undated_lis[:CHECKLIST_PREVIEW_N])
            rest = undated_lis[CHECKLIST_PREVIEW_N:]
            if rest:
                n = len(rest)
                label = f"Показать ещё {n} {_plural(n, 'папку', 'папки', 'папок')}"
                items.append(
                    f'<li class="expand"><details><summary>{html.escape(label)}</summary>'
                    f'<ul class="checklist nested">{"".join(rest)}</ul></details></li>'
                )
        # undated_groups == [] -- все Tier D файлы лежат в Albums/, дата не влияет на их
        # место -- по решению пользователя пункт не рендерится вообще, ни в каком виде.

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
    """group -- (folder, [(matched, origin, [dup_source, ...]), ...]), та же форма, что
    _cluster_exact_dup_full() (пункт B.3, "большой разбор report.html", SESSION-HANDOFF.txt):
    раньше превью-карточка не показывала origin ("откуда сам файл-эталон в архиве"), только
    имя + счётчик -- этого не хватало по прямому замечанию ТЗ ("этой карточке такого сегодня
    не хватает"). Список источников-дублей здесь по-прежнему компактным счётчиком (×N), не
    построчно -- построчный список для этого и существует отдельная "Полная сверка дублей"
    (_render_dedup_verification_page(), см. ссылку ниже по карточке)."""
    folder, items = group
    total = sum(len(sources) for _, _, sources in items)

    def _label(matched, origin, sources):
        name = html.escape(_win_basename(matched))
        count = f" (×{len(sources)})" if len(sources) > 1 else ""
        origin_part = f" — скопировано из {html.escape(origin)}" if origin else ""
        return f"{name}{count}{origin_part}"

    labels = [_label(*it) for it in items[:5]]
    more = f" и ещё {len(items) - 5} {_plural(len(items) - 5, 'файл', 'файла', 'файлов')}" if len(items) > 5 else ""
    files = "<br>".join(labels)
    folder_line = f"Папка: {html.escape(folder)}." if folder else ""
    action_line = f"Уже в архиве:<br>{files}{more}."
    detail = f"{folder_line}<br>{action_line}" if folder_line else action_line
    return f"{_n_files(total)} — дубли файлов из этой папки", detail


EXACT_DUP_PREVIEW_N = 2  # тот же порядок превью, что CHECKLIST_PREVIEW_N.
EXACT_DUP_INTRO = "Ничего делать не нужно — показано для тех, кто хочет убедиться сам."


def _build_exact_dup_items(fields: dict) -> list:
    """REVIEW-HANDOFF.md, Раунд 31: паттерн прогрессивного раскрытия -- буквально тот же
    приём, что _build_checklist_items() использует для near_dup_clusters, но отдельная
    функция: тон здесь другой (см. _render_exact_dup_examples()) и категория живёт вне Листа 3
    (не "стоит проверить" -- действие не требуется вообще, см. докстринг _cluster_exact_dup_full())."""
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
    проверить/исправить". fields -- checklist_new/model (REVIEW-HANDOFF.md, Раунд 44:
    checklist_before как параметр убран 2026-07-31 вместе с кумулятивным "Ваш архив" -- любой
    оставшийся dict с ключом "exact_dup_groups", см. _build_checklist_fields()).

    verify_link (2026-07-26): ссылка на отдельную страницу "Полная сверка дублей" (см.
    generate_dedup_verification_page()) -- сразу ПОД этой же карточкой, не в хвосте всей
    страницы отчёта (живая находка пользователя: раньше ссылка была в конце body, физически
    оторвана от карточки "Дубли — примеры", к которой относится по смыслу -- "почему
    это примеры, если рядом полная информация" читалось необъяснимо без видимой связи)."""
    if fields is None:
        return ""
    card = _render_checklist_card(heading, _build_exact_dup_items(fields), intro=intro)
    if card and verify_link:
        card += (
            '<p class="muted">Показаны только первые несколько — '
            f'<a href="{html.escape(verify_link)}" target="_blank" rel="noopener">полная сверка построчно, по каждому файлу →</a>.</p>'
        )
    return card


DEDUP_VERIFICATION_FILENAME = "dedup_verification.html"


def _render_near_dup_verification_section(clusters: list) -> str:
    """Задача 6 (SESSION-HANDOFF.txt, пакет "боевой прогон D:\\"): случай "разные папки" в
    самом отчёте (_cluster_checklist_item()) больше не расписывает построчный список путей на
    месте -- ссылается сюда, на полный, не обрезанный по топ-N список каждой серии. Та же
    карточка-на-группу форма, что и exact-dup секция выше на этой же странице.

    REVIEW-HANDOFF.md, Раунд 52 (придирка 2): только кластеры, реально лежащие в разных
    папках (len(dirs)>1, та же проверка, что уже определяет ветку в _cluster_checklist_item())
    -- однопапочные кластеры уже показаны полностью прямо в основном отчёте (компактная ветка
    той же функции), дублировать их здесь ещё раз не нужно, страница обещает именно "разные
    папки" (см. CHANGELOG.md/её же докстринг у _render_dedup_verification_page())."""
    clusters = [c for c in clusters if len({_win_dirname(p) for p in c}) > 1]
    if not clusters:
        return ""
    cards = []
    for cluster in clusters:
        names = [_win_basename(p) for p in cluster]
        rows = "<br>".join(
            html.escape((_friendly_target_dir(p) + "\\" if _friendly_target_dir(p) else "") + n)
            for p, n in zip(cluster, names, strict=True)
        )
        cards.append(
            f'<div class="card"><h2>Похожая серия из {len(cluster)} кадров</h2>'
            f'<div class="detail">{rows}</div></div>'
        )
    n = len(clusters)
    header = (
        '<div class="card"><h1>Полная сверка похожих серий</h1>'
        f'<p class="subtitle">{n} {_plural(n, "серия", "серии", "серий")} похожих кадров — '
        'полный список файлов каждой серии, без сокращения.</p></div>'
    )
    return header + "".join(cards)


def _render_dedup_verification_page(data: dict) -> str:
    """2026-07-26: тело отдельной страницы "Полная сверка дублей" -- построчно, без
    сворачивания "и ещё N" (в отличие от _render_exact_dup_examples()/Листа 3, здесь весь
    смысл страницы -- ничего не урезать). Группировка по папке архива визуально разделяет
    находки (отдельная карточка на папку, тот же .card/h2, что и везде в отчёте) -- по
    прямой просьбе пользователя не гнать всё сплошным потоком.

    2026-08-02, задача 6: страница расширена второй секцией -- похожие серии (near-dup) в
    разных папках, та же цель "полный список без обрезки", что и у дублей ниже.

    Возвращает "" если показывать нечего (нет ни дублей, ни похожих серий) -- вызывающая
    сторона (generate_dedup_verification_page()) тогда не пишет файл и не даёт на него
    ссылку из основного отчёта."""
    groups = _cluster_exact_dup_full(data)
    near_dup_clusters = _cluster_near_dup(data.get("near_dup_edges", []))
    # Раунд 52 (придирка 2): та же многопапочная фильтрация, что теперь применяет
    # _render_near_dup_verification_section() -- иначе гейт "есть что показать" мог бы
    # считать страницу непустой из-за однопапочных кластеров, которые сама секция потом всё
    # равно отфильтрует, и странице неоткуда взять контент, кроме "назад к отчёту".
    near_dup_multi_folder = [c for c in near_dup_clusters if len({_win_dirname(p) for p in c}) > 1]
    if not groups and not near_dup_multi_folder:
        return ""
    parts = []
    if groups:
        cards = []
        for folder, items in groups:
            rows = []
            for matched, origin, sources in items:
                name = html.escape(_win_basename(matched))
                origin_line = f" — скопировано из {html.escape(origin)}" if origin else ""
                n = len(sources)
                dup_word = _plural(n, "дубль", "дубля", "дублей")
                verb = "отклонён" if n == 1 else "отклонены"
                # Пункт B.3 ("большой разбор report.html", SESSION-HANDOFF.txt): каждый
                # путь-дубль с новой строки, визуально отделены -- раньше был один сплошной
                # comma-separated список, на большом числе дублей (типично для целиком
                # задублированной папки) превращался в нечитаемую простыню.
                dup_list = "<br>".join(html.escape(s) for s in sources)
                rows.append(
                    f'<li><div class="title">{name}</div>'
                    f'<div class="detail">В архиве{origin_line}.<br>'
                    f'{n} {dup_word} {verb}:<br>{dup_list}</div></li>'
                )
            cards.append(
                f'<div class="card"><h2>{html.escape(folder or "Корень архива")}</h2>'
                f'<ul class="checklist">{"".join(rows)}</ul></div>'
            )
        total_files = sum(len(items) for _, items in groups)
        total_dups = sum(len(sources) for _, items in groups for _, _, sources in items)
        header = (
            '<div class="card">'
            '<h1>Полная сверка дублей</h1>'
            f'<p class="subtitle">{_n_files(total_files)} в архиве имеют хотя бы один дубль '
            f'в источнике — {_n_files(total_dups)} отклонено как дубли и не попало в '
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
            '</div>'
        )
        parts.append(header)
        parts += cards
    parts.append(_render_near_dup_verification_section(near_dup_clusters))
    parts.append('<div class="card"><p class="muted">'
                  '<a href="report.html" target="_blank" rel="noopener">← назад к отчёту</a></p></div>')
    return "".join(parts)


def generate_dedup_verification_page(data: dict, report_out_path: str,
                                      program_name: str = "PhotoArchive") -> str:
    """Пишет файл-сосед report_out_path (тот же каталог, DEDUP_VERIFICATION_FILENAME) --
    полная построчная сверка "какой файл в архиве откуда, какие файлы источника были его
    дублями" (см. _cluster_exact_dup_full()), для пользователя, который не принимает
    описание алгоритма и хочет проверить дедуп сам в файловой системе (2026-07-26,
    обсуждение с пользователем), плюс (2026-08-02) полный список похожих серий в разных
    папках. Возвращает имя файла (относительный href для ссылки из основного отчёта) или
    None, если показывать нечего вообще -- тогда ничего не пишется и ссылка не появляется."""
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


def _render_recommendations(fields: dict, heading: str, intro: str = "", target_path: str = None,
                             verify_link: str = None) -> str:
    """checklist_new -- рекомендации по ЭТОМУ прогону, сразу после "Пополнение архива" (см.
    _generate_from_model()). 2026-07-31: раньше был парный вызов для checklist_before
    ("накопилось до этого пополнения", кумулятивная история) -- убран вместе с "Ваш архив"
    (см. _generate_from_model()), функция сама не изменилась, только второй вызов исчез.
    None -- рекомендации не сформированы (например, level=="workdir", туда run_start не
    передаётся вовсе).

    intro (пункт B.5, "большой разбор report.html", SESSION-HANDOFF.txt) -- по умолчанию
    вызывающий код (_generate_from_model()) строит "сохранены ВСЕ N файлов, включая M
    спорных", когда disputes_total>0 -- явно не намекает, что со спорными что-то не так/
    потеряно, сами файлы просто внизу списком с причиной (см. _dispute_checklist_item()).

    verify_link (задача 6): доходит до _build_checklist_items() -- ссылка на "Полную сверку"
    для похожих серий в разных папках, None если страница не строится (level!="target")."""
    if fields is None:
        return ""
    return _render_checklist_card(heading, _build_checklist_items(fields, target_path, verify_link),
                                   intro=intro)


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
        + _render_exact_dup_examples(model, "Дубли — примеры", intro=EXACT_DUP_INTRO)
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
    "разногласий"/приблизительных дат по папкам) — пустые Counter/None, соответствующая
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
        # 2026-07-31: analyze/analyze-quick/analyze-full теперь тоже резолвят GPS -> место
        # (photosort_win.py:run_analyze(), тот же кэш place_for_gps(), что и у реальной сборки)
        # -- пусто здесь означает "в этом скане не нашлось GPS-тегов", не "не считалось вовсе".
        "cities": Counter(stats.cities),
        # Пункт E ("большой разбор report.html", SESSION-HANDOFF.txt): та же логика, что
        # cities выше -- rec.camera уже читается той же exiftool-пачкой, что и GPS/дата.
        "cameras": Counter(stats.cameras),
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
        # Пункт B.2 ("большой разбор report.html", SESSION-HANDOFF.txt): полные пути
        # запароленных архивов, не только счётчик выше.
        "encrypted_archive_paths": list(stats.encrypted_archive_paths),
        # REVIEW-HANDOFF.md, Раунд 36: секция "Рекомендации" (_render_analyze_recommendations)
        # -- нужен только факт "на источнике уже есть собранный архив", уже посчитан
        # unconditionally в run_analyze() (classify_found_archives()) -- 2026-07-31, пункт I:
        # раньше тот же список питал found_archives-параметр generate_report_from_analyze_stats()
        # для отдельного блока "На этом диске найден архив", теперь этот блок для analyze не
        # рендерится вообще (см. photosort_win.py:_finalize_analyze_report()) -- поле здесь
        # используется только для рекомендации ниже.
        "found_archive_count": len(stats.found_archive_top_level),
        # 2026-07-31, пункт I: (root_path, n_files) -- реально исключено из ЭТОЙ статистики
        # (SourceWalker.excluded_found_archives) -- пусто, если cfg.
        # include_found_archives_in_analyze включён явно (тогда используется found_archive_count
        # выше, старая формулировка).
        "excluded_found_archives": list(stats.excluded_found_archives),
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

    # 2026-07-31, пункт I: excluded_found_archives -- по умолчанию (cfg.
    # include_found_archives_in_analyze не включён явно) содержимое найденного архива
    # исключается из этой статистики, старая формулировка ("дублирования не будет") тогда
    # вводила бы в заблуждение -- эта статистика его вообще не видела. Взаимоисключающе с
    # found_archive_count ниже -- одно и то же событие, разное объяснение в зависимости от
    # того, что реально произошло с этим содержимым.
    excluded = model.get("excluded_found_archives") or []
    if excluded:
        n_files = sum(n for _, n in excluded)
        if len(excluded) == 1:
            where = f" ({html.escape(excluded[0][0])})"
        else:
            where = f" в {len(excluded)} {_plural(len(excluded), 'месте', 'местах', 'местах')}"
        items.append(_li(
            f"На источнике уже есть собранный архив{where} — исключён из этой статистики",
            f"{_n_files(n_files)} не учтены в числах этого анализа, чтобы не искажать картину "
            "по тому, что реально анализируется. Чтобы проверить сам этот архив — "
            "используйте «Паспорт архива» отдельно.",
        ))
    elif model.get("found_archive_count", 0):
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

    # Пункт B.2: полные пути запароленных архивов, не только счётчик.
    encrypted = model.get("encrypted_archive_paths") or []
    if encrypted:
        paths = "; ".join(_file_link_or_text(html.escape(p), p) for p in sorted(encrypted)[:TOP_N])
        more = f" и ещё {len(encrypted) - TOP_N}" if len(encrypted) > TOP_N else ""
        items.append(_li(
            f"{len(encrypted)} {_plural(len(encrypted), 'архив защищён', 'архива защищены', 'архивов защищены')} паролем",
            f"{paths}{more}. Программа не подбирает пароли — распакуйте вручную перед сборкой, "
            "чтобы попало и содержимое.",
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
            parts.append(f'<p><a href="{html.escape(href)}" target="_blank" rel="noopener">Открыть папку с архивом</a> '
                          f'— {html.escape(target_path)}</p>')
        parts.append('<p class="muted">Хотите проверить ещё один диск или флешку — запустите '
                      'программу снова с новым источником.</p>')
        parts.append(
            '<p><b>Совет:</b> теперь, когда архив собран, стоит сделать его резервную копию '
            'на другом диске или в облаке — так воспоминания не будут зависеть от одного '
            'носителя.</p>'
        )
        # 2026-07-31, по прямой просьбе пользователя: этот отчёт теперь показывает только
        # результат текущего прогона (см. _generate_from_model()/_render_this_run()) -- полная
        # проверка архива целиком (дубли/даты/спорные с нуля, не из истории CSV-логов) --
        # отдельное явное действие, не побочный эффект каждого обычного прогона.
        parts.append(
            '<p class="muted">Хотите проверить архив целиком (не только этот прогон) — '
            'например, если что-то в нём переносили или удаляли руками — запустите отдельно '
            '«Паспорт архива» из главного меню программы.</p>'
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
                          found_archives: tuple = None,
                          target_path: str = None, interrupted: bool = False,
                          full_workdir: bool = False, verify_link: str = None) -> None:
    # level=="workdir" (CLI --dry-run/интерактивный [2], решение пользователя 2026-07-20,
    # третий заход) -- по умолчанию ТОЛЬКО часть 1 ("Пробный прогон" + рекомендации по нему),
    # без "Ваш архив"/диаграмм: и содержательно нечего показывать (для [2] данные чисто
    # in-memory, архива в этом смысле не существует), и для CLI --dry-run опасно -- он пишет
    # персистентные CSV TARGET по-настоящему (RunLogs, не CollectingRunLogs), но БЕЗ
    # реального копирования файла (place_file() пропущен) -- повторные --dry-run на один
    # TARGET накапливают в этих CSV фантомные "appended"-строки, которые никогда не станут
    # архивом. checklist_new (если run_start передан) уже отфильтрован по времени --
    # используем его, а не полную (потенциально засорённую) model. REVIEW-HANDOFF.md, Раунд
    # 44: до 2026-07-31 здесь же упоминался checklist_before -- параметр убран вместе с
    # кумулятивным "Ваш архив" (729a2de), _split_rows_by_time() больше не вычисляет и не
    # возвращает "раньше"-половину вовсе, упоминать как несостоявшуюся альтернативу уже нечего.
    #
    # REVIEW-HANDOFF.md, Раунд 38: интерактивный [2] на уже существующем Target -- другой
    # случай, безопасный (suppress_logs=True там всегда, никаких фантомных записей своей же
    # истории быть не может). full_workdir=True (см. photosort_win.py:_bare_launch_run_dryrun) --
    # явный сигнал вызывающего кода "я смёржил настоящую историю Target с гипотетическими
    # строками этого прогона и посчитал run_start" -- отдельный флаг, снаружи неотличимый от
    # обычного checklist_new (оба -- тот же тип/форма), поэтому не выводится неявно из его
    # значения, а передаётся явным параметром.
    if level == "workdir" and not full_workdir:
        fields = checklist_new if checklist_new is not None else model
        body = _render_this_run(run_stats, level) + _render_sheet3_single(fields, level)
    # level=="analyze" (никогда не передаёт run_start, checklist_new всегда None здесь) --
    # единственный оставшийся потребитель полной кумулятивной картины (Sheet1/Sheet2, "Что
    # нашлось на этом диске") -- это ОДНОразовый скан SOURCE, не история архива, "паспорт"
    # (см. ниже) её не заменяет.
    elif checklist_new is None:
        body = (_render_this_run(run_stats, level) + _render_sheet1(model, level) + _render_sheet2(model)
                + _render_exact_dup_examples(model, "Дубли — примеры", intro=EXACT_DUP_INTRO,
                                              verify_link=verify_link if level == "target" else None)
                + _render_sheet3_single(model, level))
    else:
        # 2026-07-31, по прямой просьбе пользователя: level=="target" (и full_workdir=True --
        # превью [2] на уже существующем Target, тот же код-путь) больше НЕ показывает
        # кумулятивную "Ваш архив"/диаграммы/"накопилось раньше" -- отчёт теперь только про
        # ЭТОТ прогон. Полная картина архива целиком -- отдельное явное действие ([4] Паспорт
        # архива), не побочный эффект каждого обычного отчёта (см. _render_cta_block()).
        new_intro = ""
        if checklist_new and checklist_new.get("disputes_total"):
            n_total = checklist_new.get("total_new", 0)
            n_disp = checklist_new["disputes_total"]
            new_intro = (
                f"Сохранены ВСЕ {_n_files(n_total)}, включая {n_disp} "
                f"{_plural(n_disp, 'спорный', 'спорных', 'спорных')} — ничего не потеряно."
            )
        body = (
            _render_this_run(run_stats, level)
            + _render_recommendations(checklist_new, "Новое в этом пополнении", intro=new_intro,
                                       target_path=target_path,
                                       verify_link=verify_link if level == "target" else None)
            + _render_exact_dup_examples(
                checklist_new, "Дубли этого пополнения — примеры", intro=EXACT_DUP_INTRO,
                verify_link=verify_link if level == "target" else None)
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
    body = _render_trust_block(level, model.get("decisions", {}).get("unreadable", 0)) + body
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
    RunLogs._ts() -- см. _split_rows_by_time()) -- фильтрует Лист 3/чек-лист "Новое в этом
    пополнении" до записей именно этого запуска. None -- Лист 3 не делится (один список, как
    раньше).

    2026-07-31, по прямой просьбе пользователя: кумулятивная "Ваш архив"/диаграммы/"накопилось
    до этого пополнения" (история архива целиком) больше не рендерится ни для level=="target",
    ни для full_workdir=True -- отчёт после обычного прогона теперь только про сам этот прогон,
    полная картина архива -- отдельное действие [4] Паспорт архива (см. run_passport()),
    упомянутое в _render_cta_block(). full_workdir (REVIEW-HANDOFF.md, Раунд 38) по-прежнему
    отличает [2] на непустом Target (смёрженная реальная история + гипотетические строки этого
    прогона, photosort_win.py:_bare_launch_run_dryrun) от CLI --dry-run (которому мержить
    нечего, checklist_new без него не строится вовсе) -- разница теперь только в ЭТОМ, не в
    объёме показанного.

    target_path (4.7, PROMPT_report_marketing.md): абсолютный путь TARGET -- используется
    только при level=="target", для ссылки "Открыть папку с архивом" в CTA-блоке в конце
    отчёта (_render_cta_block()). None -- ссылка не рендерится, остаётся только текст.

    interrupted (Ctrl+C-пакет): работа прервана пользователем (KeyboardInterrupt) во время
    [3]/CLI archive -- см. photosort_win.py _run_impl()/_RunState.interrupted. Данные в data
    в этом случае неполные (только то, что успело записаться в CSV до прерывания) -- баннер
    в начале отчёта (_render_interrupted_banner()) делает это явным, не молчаливым."""
    model = build_model_from_rows(data)
    checklist_new = None
    if run_start:
        data_new = _split_rows_by_time(data, run_start)
        checklist_new = _build_checklist_fields(data_new)
    # 2026-07-26: только level=="target" -- реальный архив на диске, единственный случай,
    # где "полная сверка дублей" (путь+имя каждого файла) вообще что-то значит для
    # пользователя (workdir/analyze -- in-memory прогон, файлы ещё не скопированы).
    verify_link = generate_dedup_verification_page(data, out_path, program_name) if level == "target" else None
    _generate_from_model(model, out_path, level, program_name, run_stats=run_stats,
                          checklist_new=checklist_new,
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


# ============================================================================
# 7. Паспорт архива ([4], photosort_win.py:run_passport())
# ============================================================================


def _addition_date_range(target_path: str) -> tuple:
    """Пункт B.10 ("большой разбор report.html", SESSION-HANDOFF.txt): дата первого и
    последнего АВТОМАТИЧЕСКОГО пополнения -- по timestamp-колонке appended.csv (формат
    RunLogs._ts(), "%Y-%m-%d %H:%M:%S" -- лексикографическая сортировка совпадает с
    хронологической, парсить в datetime не нужно). В отличие от остального паспорта (числа
    "проверены заново" самим self_scan-обходом TARGET) -- это ЕДИНСТВЕННОЕ поле, которое
    честно берётся из истории логов, не может быть перепроверено self_scan'ом (сам факт "когда
    именно программа что-то добавила" в содержимом файлов не записан). Возвращает (first, last)
    ("%Y-%m-%d") или (None, None), если appended.csv нет/пуст (архив собран до этой колонки,
    либо ротация унесла всю историю)."""
    logs_dir = os.path.join(target_path, "__служебные_файлы", "logs")
    rows = parse_target_logs(logs_dir).get("appended", [])
    timestamps = sorted(r["timestamp"] for r in rows if r.get("timestamp"))
    if not timestamps:
        return None, None
    return timestamps[0][:10], timestamps[-1][:10]


def _render_passport_summary(stats, target_path: str = None) -> str:
    parts = ['<div class="card">', '<h1>Архив сейчас</h1>']
    where = html.escape(target_path) if target_path else "архива"
    parts.append(f'<p class="subtitle">Полная проверка {where} заново, с нуля — не из истории '
                 'прошлых прогонов программы.</p>')
    stat_items = [f'<div class="stat"><div class="value">{stats.total_files}</div>'
                  f'<div class="label">файлов в архиве</div></div>']
    if stats.total_bytes:
        stat_items.append(f'<div class="stat"><div class="value">{_fmt_bytes(stats.total_bytes)}</div>'
                           f'<div class="label">занимает архив</div></div>')
    if stats.n_albums_detected:
        n = stats.n_albums_detected
        stat_items.append(f'<div class="stat"><div class="value">{n}</div>'
                           f'<div class="label">{_plural(n, "альбом", "альбома", "альбомов")}</div></div>')
    years = stats.dates_by_year
    if years:
        span = max(years) - min(years) + 1
        stat_items.append(f'<div class="stat"><div class="value">{span}</div>'
                           f'<div class="label">{_plural(span, "год", "года", "лет")} истории</div></div>')
    parts.append('<div class="stat-row">' + "".join(stat_items) + '</div>')
    breakdown = _type_breakdown_caption(
        Counter({"image": stats.n_images, "raw": stats.n_raw, "video": stats.n_videos}))
    if breakdown:
        parts.append(f'<p class="muted">{html.escape(breakdown)}</p>')
    if stats.oldest_date is not None:
        d = stats.oldest_date
        date_str = f"{d.day:02d}.{d.month:02d}.{d.year}" if d.day else f"{d.year}"
        # stats.oldest_display -- SourceItem.origin_display, всегда posix-style ("/", см.
        # SourceWalker._walk_dir()), даже когда паспорт сканирует TARGET -- реальный Windows-
        # путь с настоящими Albums/ByDate-маркерами. _win_basename()/_friendly_target_dir()
        # написаны под dest-пути реальной сборки ("\\" явно, см. их же докстринги) -- без
        # нормализации разделителя маркер не находится, вся строка "Albums/Папка/файл.jpg"
        # ошибочно показывается как одно "имя файла" без папки (живая находка при первом
        # реальном прогоне [4] на Windows, 2026-07-31).
        oldest_win_path = (stats.oldest_display or "").replace("/", "\\")
        name = html.escape(_win_basename(oldest_win_path))
        folder = _friendly_target_dir(oldest_win_path)
        file_text = f'{html.escape(folder)}\\{name}' if folder else name
        # Пункт B.8: oldest_win_path здесь -- путь ОТНОСИТЕЛЬНО TARGET (см. коммент выше),
        # не абсолютный сам по себе (в отличие от Sheet1) -- нужен target_path, чтобы собрать
        # реальный абсолютный путь для file://-ссылки.
        abs_path = os.path.join(target_path, oldest_win_path) if target_path and oldest_win_path else None
        file_str = f' — {_file_link_or_text(file_text, abs_path)}' if (file_text) else ""
        parts.append(f'<p><b>Самый старый файл:</b> {date_str}{file_str}</p>')
    if target_path:
        first, last = _addition_date_range(target_path)
        if first:
            # Речь пользователя, 2026-08-02 (задача 6): старая формулировка "Автоматических
            # пополнений программой: {span}" не объясняла, ЧТО значит {span} (диапазон дат?
            # число пополнений?) -- переформулировано явно как диапазон дат, с честной
            # оговоркой источника (см. докстринг _addition_date_range() -- ЕДИНСТВЕННОЕ поле
            # паспорта, не перепроверяемое собственным self_scan-сканированием).
            when = f'с {first} по {last}' if first != last else f'{first}'
            parts.append(
                f'<p class="muted">Программа пополняла этот архив {when} — по записям в '
                'журналах прошлых запусков (эту дату нельзя установить повторным '
                'сканированием, только по истории программы).</p>'
            )
    parts.append('</div>')
    return "".join(parts)


def _passport_check(n: int, ok_text: str, attn_text) -> str:
    """Один пункт "Целостности архива" -- в отличие от остального report.html (пустая
    категория скрывается целиком), здесь ПРОБЛЕМ НЕТ показывается так же явно, как проблема
    ЕСТЬ (SESSION-HANDOFF.txt, design-сессия 2026-07-31, прямое решение пользователя) --
    паспорт существует именно для того, чтобы ответить "всё цело?", молчание не отвечает на
    этот вопрос. attn_text -- callable(n) -> str, т.к. текст обычно склоняется по числу."""
    if n:
        return f'<li class="attn">{html.escape(attn_text(n))}</li>'
    return f'<li class="ok">{html.escape(ok_text)}</li>'


_DEEP_ALBUM_MIN_SUBPATH = 3  # Живое обсуждение с пользователем (2026-08-01): "вложенность
# больше 2" -- число ПОДпапок внутри альбома (Albums/Альбом/A/B -- уже 2, не триггерит;
# Albums/Альбом/A/B/C -- 3, триггерит), не общая глубина пути от Albums.


def _deep_nested_albums(tree_folder_counts: Counter) -> list:
    """Ключи tree_folder_counts (см. run_analyze()/AnalyzeStats.tree_folder_counts) --
    "Albums/<альбом>/<sub1>/<sub2>/...", subpath -- та же вложенность, что find_album()
    возвращает для реального размещения (подпапки самого альбома, dump-сегменты уже
    схлопнуты). Возвращает [(альбом, макс_глубина), ...] для альбомов с глубиной >=
    _DEEP_ALBUM_MIN_SUBPATH -- максимум по всем бакетам альбома (один альбом может иметь
    несколько разных по глубине подпапок), отсортировано по убыванию глубины.

    REVIEW-HANDOFF.md, Раунд 46, замечание 1: RAW-файлы в tree_folder_counts всегда
    уплощены до отдельного бакета "RAW" независимо от реальной глубины пути (тот же
    осознанный компромисс карточки "Структура архива", run_analyze()) -- если глубокая
    подпапка альбома существует ТОЛЬКО за счёт RAW-файлов без JPEG-партнёра на той же
    глубине, этот альбом здесь не найдётся (ложноотрицательный результат для 8-го пункта
    "Целостности"). Узкий случай, дешёвого закрытия без дублирования RAW-логики размещения
    нет -- решение то же, что уже принято для дерева-диаграммы: сознательно не покрывать."""
    max_depth = {}
    for key in tree_folder_counts:
        parts = key.split("/")
        if len(parts) < 2 or parts[0] != "Albums":
            continue
        album = parts[1]
        depth = len(parts) - 2
        if depth > max_depth.get(album, 0):
            max_depth[album] = depth
    deep = [(album, d) for album, d in max_depth.items() if d >= _DEEP_ALBUM_MIN_SUBPATH]
    deep.sort(key=lambda t: -t[1])
    return deep


PASSPORT_VERIFICATION_FILENAME = "passport_verification.html"


def _passport_normalize_dest(p: str) -> str:
    """AnalyzeStats.near_dup_edges/exact_dup_edges хранят item.origin_display -- всегда
    posix-style ("/"), даже когда паспорт сканирует TARGET (тот же класс находки, что уже
    исправлен для oldest_display в _render_passport_summary(): без нормализации
    _win_dirname()/_friendly_target_dir() не находят маркер ByDate/Albums вообще, вся строка
    ошибочно читается как один "файл без папки")."""
    return (p or "").replace("/", "\\")


def _passport_file_link(win_rel_path: str, target_path: str) -> str:
    """win_rel_path -- уже нормализован (_passport_normalize_dest), относительно TARGET.
    Склеивает с target_path для настоящей file://-ссылки -- тот же приём, что уже применяет
    _render_passport_summary() для oldest_display."""
    name = html.escape(_win_basename(win_rel_path))
    folder = _friendly_target_dir(win_rel_path)
    text = f'{html.escape(folder)}\\{name}' if folder else name
    abs_path = os.path.join(target_path, win_rel_path) if target_path and win_rel_path else None
    return _file_link_or_text(text, abs_path)


def _cluster_passport_edges(edges: list) -> list:
    """Те же рёбра (dest/matched_dest), что и у обычного _cluster_near_dup() -- сам union-find
    не знает и не заботится, near-dup это или точный дубль, переиспользуется как есть, только
    с нормализованными (см. _passport_normalize_dest()) путями."""
    normalized = [
        {"dest": _passport_normalize_dest(e.get("dest")),
         "matched_dest": _passport_normalize_dest(e.get("matched_dest"))}
        for e in edges
    ]
    return _cluster_near_dup(normalized)


def _render_passport_dup_li(clusters: list, target_path: str, noun: tuple, note: str,
                             verify_link: str = None) -> str:
    """Один <li class="attn"> для секции "Целостность архива" -- превью первых
    CHECKLIST_PREVIEW_N групп (папка + файлы, та же форма, что _cluster_checklist_item()
    обычного отчёта), "и ещё N" на остальное, ссылка на "Полную сверку" (см.
    generate_passport_verification_page()), если хотя бы одна группа лежит в разных папках.
    noun -- (один, немного, много) для склонения "дубль"/"похожий кадр"."""
    # REVIEW-HANDOFF.md, Раунд 57 [ЗАМЕЧАНИЕ]: n -- число РЕАЛЬНО лишних (удаляемых) копий, не
    # общее число файлов во всех кластерах -- каждый кластер всегда содержит один "оригинал",
    # который никто не собирается удалять (T файлов в G группах -> T-G лишних копий), та же
    # семантика, что и у stats.n_exact_dupes/n_near_dupes до коммита af50df1. Пример: 1 файл +
    # 1 ручная копия того же файла в другой папке -> один кластер из 2 файлов -> 2-1=1 лишняя
    # копия ("1 дубль"), не 2 (раньше эта функция считала T, не T-G, завышая число ровно на
    # количество групп).
    total = sum(len(c) for c in clusters)
    lines = [_passport_cluster_line(c, target_path) for c in clusters[:CHECKLIST_PREVIEW_N]]
    more_n = len(clusters) - CHECKLIST_PREVIEW_N
    more = f" (и ещё {more_n} {_plural(more_n, 'группу', 'группы', 'групп')})" if more_n > 0 else ""
    detail = "<br>".join(lines) + more
    multi_folder = any(len({_win_dirname(p) for p in c}) > 1 for c in clusters)
    if multi_folder and verify_link:
        detail += (f'<br><a href="{html.escape(verify_link)}" target="_blank" rel="noopener">'
                   "полная сверка →</a>")
    n = total - len(clusters)
    return (f'<li class="attn">{n} {_plural(n, *noun)} {note}<br>{detail}</li>')


def _passport_cluster_line(cluster: list, target_path: str) -> str:
    """Одна строка превью группы: папка (если все файлы в одной) + сами файлы, каждый --
    кликабельная file://-ссылка (см. _passport_file_link())."""
    dirs = {_win_dirname(p) for p in cluster}
    files_html = ", ".join(_passport_file_link(p, target_path) for p in cluster)
    if len(dirs) == 1:
        folder = _friendly_target_dir(cluster[0])
        folder_prefix = f"{html.escape(folder)}: " if folder else ""
    else:
        folder_prefix = "разные папки: "
    return folder_prefix + files_html


def _render_passport_integrity(stats, target_path: str = None, verify_link: str = None) -> str:
    exact_clusters = _cluster_passport_edges(stats.exact_dup_edges)
    near_clusters = _cluster_passport_edges(stats.near_dup_edges)
    if exact_clusters:
        exact_li = _render_passport_dup_li(
            exact_clusters, target_path, ("дубль", "дубля", "дублей"),
            "внутри архива — один и тот же файл сохранён более одного раза. Не удаляйте "
            "лишние копии вручную: вытащите папку(и) с дублями в отдельное место и "
            "назначьте её источником (SOURCE) для обычной процедуры добавления — программа "
            "сама отличит уже архивированное; либо пересоберите архив целиком, указав сам "
            "архив источником для нового архива (дольше, но надёжнее для больших расхождений).",
            verify_link)
    else:
        exact_li = '<li class="ok">Дублей внутри архива нет.</li>'
    if near_clusters:
        near_li = _render_passport_dup_li(
            near_clusters, target_path, ("похожий кадр", "похожих кадра", "похожих кадров"),
            "сохранено рядом с оригиналом — обычно не ошибка, стоит проверить вручную, если "
            "важна экономия места.",
            verify_link)
    else:
        near_li = '<li class="ok">Похожих кадров/возможных кропов не найдено.</li>'
    items = [
        exact_li,
        near_li,
        _passport_check(
            stats.n_broken_or_zero, "Повреждённых или пустых файлов нет.",
            lambda n: f"{n} {_plural(n, 'файл повреждён или пуст', 'файла повреждены или пусты', 'файлов повреждены или пусты')} "
                      "(0 байт)."),
        _passport_check(
            stats.n_signature_mismatch, "У всех файлов расширение совпадает с содержимым.",
            lambda n: f"У {n} {_plural(n, 'файла', 'файлов', 'файлов')} расширение не совпадает с "
                      "реальным содержимым — признак повреждения или подмены файла."),
        _passport_check(
            stats.n_archives_found, "Посторонних архивов (zip/rar) внутри не осталось.",
            lambda n: f"Внутри архива {'найден' if n == 1 else 'найдено'} {n} "
                      f"{_plural(n, 'архив', 'архива', 'архивов')} (zip/rar) — стоит разобрать "
                      "и удалить, иначе он не защищён общей проверкой этой программы."),
        _passport_check(
            stats.n_dump_items, "Все файлы лежат внутри признанных альбомов/дат.",
            lambda n: f"{n} {_plural(n, 'файл лежит', 'файла лежат', 'файлов лежат')} не внутри "
                      "конкретного альбома или папки по дате — похоже на файлы, добавленные или "
                      "перенесённые вручную, в обход программы."),
    ]
    # Речь пользователя, 2026-08-02 (задача 3): старая версия просто складывала tier C+D в
    # одно число, не объясняя, что оно значит -- по RULES.md (блок UNDATED) точность даты
    # решает, в какую подпапку попадёт файл, ТОЛЬКО внутри ByDate; в Albums дата ни на что не
    # влияет (место определяет структура исходных папок), перепроверять её там бессмысленно.
    # Тот же принцип, что уже применён к report.py's _cluster_dates_review()/_cluster_undated()
    # для обычного пополнения (2026-08-02, прямое замечание пользователя) -- здесь используем
    # stats.n_tier_cd_bydate (AnalyzeStats, тот же фильтр "не Albums", посчитан заодно с
    # tier_counts в run_analyze()) вместо сырой суммы, чтобы ok/attn-статус самой проверки
    # тоже отражал только действительно значимую часть, не общий счёт.
    n_approx_or_missing = stats.tier_counts.get("C", 0) + stats.tier_counts.get("D", 0)
    n_actionable = stats.n_tier_cd_bydate
    n_in_albums = max(n_approx_or_missing - n_actionable, 0)
    albums_note = (
        f" Ещё {_n_files(n_in_albums)} с такой же неточной или отсутствующей датой лежат в "
        "Albums — там дата ни на что не влияет (место файла определяет структура исходных "
        "папок, не дата съёмки), действие не требуется."
    ) if n_in_albums else ""
    ok_text = ("Все файлы в ByDate имеют точную дату съёмки." + albums_note if n_in_albums else
               "У всех файлов есть точная или приблизительная дата съёмки.")
    items.append(_passport_check(
        n_actionable, ok_text,
        lambda n: (
            f"У {n} {_plural(n, 'файла', 'файлов', 'файлов')} в ByDate дата определена лишь "
            "приблизительно или не определена вовсе — точность даты решает, в какую подпапку "
            f"попадёт файл, стоит перепроверить при желании.{albums_note}"
        )))
    # Живое обсуждение с пользователем (2026-08-01): альбом с глубокой вложенностью подпапок
    # часто означает, что реальный отдельный альбом "спрятан" внутри более общего родителя
    # (find_album() взял верхний непустой сегмент, а не тот, что пользователь интуитивно
    # считает альбомом) -- стоит подсказать перенести такие подпапки на верхний уровень.
    deep_albums = _deep_nested_albums(stats.tree_folder_counts)
    if deep_albums:
        names = ", ".join(f"«{html.escape(a)}» ({d} {_plural(d, 'подпапка', 'подпапки', 'подпапок')})"
                           for a, d in deep_albums[:TOP_N])
        n = len(deep_albums)
        # REVIEW-HANDOFF.md, Раунд 46, замечание 2: при >TOP_N число расходилось со списком без
        # оговорки -- тот же паттерн "и ещё N", что уже использует B.2 (запароленные архивы).
        more = f" и ещё {n - TOP_N}" if n > TOP_N else ""
        items.append(
            f'<li class="attn">{n} {_plural(n, "альбом имеет", "альбома имеют", "альбомов имеют")} '
            f'глубокую вложенность подпапок: {names}{more} — стоит перенести глубокие подпапки на '
            f'верхний уровень, чтобы они стали отдельными альбомами.</li>'
        )
    else:
        items.append('<li class="ok">Глубоко вложенных альбомов нет.</li>')
    return (
        '<div class="card"><h1>Целостность архива</h1>'
        '<p class="subtitle">Каждый пункт проверен заново, прямо сейчас — не как отчёт о том, '
        'что программа когда-то сделала, а как факт о текущем состоянии.</p>'
        f'<ul class="integrity-list">{"".join(items)}</ul></div>'
    )


def _render_passport_dup_group_card(heading: str, clusters: list, target_path: str) -> str:
    """Одна карточка на группу (та же форма, что уже использует
    _render_near_dup_verification_section() для обычного пополнения) -- полный список файлов
    без обрезки по CHECKLIST_PREVIEW_N, каждый файл кликабельная file://-ссылка."""
    cards = []
    for c in clusters:
        rows = "<br>".join(_passport_file_link(p, target_path) for p in c)
        cards.append(
            f'<div class="card"><h2>{heading} из {len(c)} файлов</h2>'
            f'<div class="detail">{rows}</div></div>'
        )
    return "".join(cards)


def _render_passport_verification_page(exact_multi: list, near_multi: list, target_path: str) -> str:
    """Тело страницы "Полная сверка" паспорта -- те же две секции (точные дубли + похожие
    серии), что и у обычного пополнения (_render_dedup_verification_page()), но построена из
    exact_dup_edges/near_dup_edges self_scan'а, не из CSV-логов прошлых прогонов -- паспорт
    вообще не пишет CSV (read-only). Только группы, реально лежащие в разных папках (тот же
    фильтр, что и у обычного пополнения, Раунд 52 (придирка 2)) -- однопапочные уже полностью
    показаны в самой "Целостности архива" (превью там не обрезает файлы внутри одной папки)."""
    if not exact_multi and not near_multi:
        return ""
    parts = ['<div class="card"><h1>Полная сверка — Паспорт архива</h1>'
             '<p class="subtitle">Полный список файлов каждой группы, без сокращения — '
             'построено этим же сканированием архива, не из истории прошлых прогонов.</p></div>']
    parts.append(_render_passport_dup_group_card("Точные дубли", exact_multi, target_path))
    parts.append(_render_passport_dup_group_card("Похожая серия", near_multi, target_path))
    parts.append('<div class="card"><p class="muted">'
                  '<a href="passport.html" target="_blank" rel="noopener">← назад к паспорту</a></p></div>')
    return "".join(parts)


def generate_passport_verification_page(stats, out_path: str, target_path: str = None,
                                         program_name: str = "PhotoArchive") -> str:
    """Пишет файл-сосед out_path (PASSPORT_VERIFICATION_FILENAME) -- полная построчная сверка
    дублей/похожих серий, найденных внутри архива этим сканированием. Возвращает имя файла
    (относительный href для ссылки из паспорта) или None, если показывать нечего (только
    однопапочные группы или групп нет вовсе) -- тогда ничего не пишется, ссылка не появляется,
    та же семантика, что у generate_dedup_verification_page() обычного пополнения."""
    exact_clusters = _cluster_passport_edges(stats.exact_dup_edges)
    near_clusters = _cluster_passport_edges(stats.near_dup_edges)
    exact_multi = [c for c in exact_clusters if len({_win_dirname(p) for p in c}) > 1]
    near_multi = [c for c in near_clusters if len({_win_dirname(p) for p in c}) > 1]
    body = _render_passport_verification_page(exact_multi, near_multi, target_path)
    if not body:
        return None
    verify_out_path = os.path.join(os.path.dirname(out_path), PASSPORT_VERIFICATION_FILENAME)
    _write(verify_out_path, _page_shell(f"{program_name} — полная сверка паспорта", body))
    return PASSPORT_VERIFICATION_FILENAME


def _render_passport_charts(stats) -> str:
    """Задача 1, речь пользователя 2026-08-02: раньше отчёт о пополнении показывал "часть 2"
    (Sheet1/Sheet2) -- кумулятивные диаграммы всего архива, убранные из обычного report.html
    2026-07-31 в пользу отдельного действия ("Паспорт архива", см. _generate_from_model()).
    Паспорт до этой правки не наследовал ни одну диаграмму Sheet2 -- переносим уместное:
    "Тип медиа" (уже есть текстовой подписью в _render_passport_summary(), здесь диаграммой),
    "Итог проверки" (self_scan-аналог "Итога решений программы" Sheet2 -- сколько файлов
    архива уникальны/дублируются/повреждены при сверке САМОГО С СОБОЙ), "Надёжность дат" (уже
    есть как чек-лист-пункт в "Целостности", диаграмма даёт распределение по всем 4 уровням
    разом, не только по актуальному для ByDate срезу). Переиспользует
    build_model_from_analyze_stats() -- та же модель, что уже питает Sheet1/Sheet2 для
    CLI-режима analyze, никакой отдельной агрегации.

    НЕ перенесены: "Объём по категориям" (байты по типу медиа), "Топ альбомов" (байты по
    альбому), "Качество кадров" (small_image/low_confidence-флаги) -- AnalyzeStats физически
    не считает эти величины для analyze-режимов (см. те же пустые поля в
    build_model_from_analyze_stats()), перенести можно только заведя отдельную задачу по
    проводке новой статистики в run_analyze()."""
    model = build_model_from_analyze_stats(stats)
    pie_charts = [
        ("Тип медиа", [
            ("Фото", model["counts"]["image"], CATEGORY_PALETTE[0]),
            ("Видео", model["counts"]["video"], CATEGORY_PALETTE[1]),
            ("RAW", model["counts"]["raw"], CATEGORY_PALETTE[2]),
        ]),
        ("Итог проверки", [
            ("Уникальные", model["decisions"]["appended"], CATEGORY_PALETTE[0]),
            ("Точные дубли", model["decisions"]["skipped_present"], CATEGORY_PALETTE[1]),
            ("Похожие кадры", model["decisions"]["near_dup"], CATEGORY_PALETTE[2]),
            ("Повреждены/пусты", model["decisions"]["unreadable"], CATEGORY_PALETTE[3]),
        ]),
        ("Надёжность дат", [
            ("Точная (EXIF)", model["tier_counts"].get("A", 0), CATEGORY_PALETTE[0]),
            ("Высокая", model["tier_counts"].get("B", 0), CATEGORY_PALETTE[1]),
            ("Оценочная", model["tier_counts"].get("C", 0), CATEGORY_PALETTE[2]),
            ("Низкая", model["tier_counts"].get("D", 0), CATEGORY_PALETTE[3]),
        ]),
    ]
    pie_cells = []
    for title, segments in pie_charts:
        svg, legend = _svg_pie(segments)
        if not svg:
            continue
        pie_cells.append(
            f'<div class="card"><h2>{html.escape(title)}</h2>'
            f'<div class="chart-block">{svg}<div class="legend">{legend}</div></div></div>'
        )
    parts = [f'<div class="grid-3">{"".join(pie_cells)}</div>'] if pie_cells else []
    cameras_hbar = _top_cameras_chart(model.get("cameras", Counter()))
    if cameras_hbar:
        parts.append(f'<div class="card"><h2>Топ камер/устройств съёмки</h2>{cameras_hbar}</div>')
    return "".join(parts)


def generate_passport_report(stats, out_path: str, target_path: str = None,
                              program_name: str = "PhotoArchive") -> None:
    """[4] Паспорт архива (photosort_win.py:run_passport()) -- отдельный формат "с нуля", НЕ
    наследует _generate_from_model()/Sheet1-3 (SESSION-HANDOFF.txt, design-сессия 2026-07-31):
    паспорт не про "что сделал этот прогон" (нечего делать, read-only), а про "насколько цел
    архив прямо сейчас" -- явное "проблем нет" так же заметно, как "проблем 201", в отличие от
    остального report.html, где пустая категория скрывается целиком.

    stats -- AnalyzeStats из run_passport() (source=TARGET, mode="analyze") -- та же форма,
    что уже питает generate_report_from_analyze_stats(), здесь просто другой рендер поверх тех
    же данных, без found_archives/checklist-инфраструктуры обычного отчёта (архивы, найденные
    ВНУТРИ TARGET, у паспорта — сами по себе строка целостности, см.
    _render_passport_integrity(), не отдельная "часть 2")."""
    body = _render_trust_block("target", stats.n_broken_or_zero)
    body += _render_passport_summary(stats, target_path)
    # Задачи 4/5, речь пользователя 2026-08-02: страница "Полная сверка" пишется ДО основного
    # тела (тот же порядок, что уже использует _finalize_target_report() для обычного
    # пополнения, photosort_win.py) -- verify_link должен быть уже готов к моменту рендера
    # "Целостности архива" ниже, не задним числом.
    verify_link = generate_passport_verification_page(stats, out_path, target_path, program_name)
    body += _render_passport_integrity(stats, target_path, verify_link)
    if stats.dates_by_year:
        svg = _svg_year_hbar_chart(Counter(stats.dates_by_year))
        if svg:
            body += f'<div class="card"><h2>Медиафайлы по годам</h2>{svg}</div>'
    body += _render_passport_charts(stats)
    # 2026-07-31: раньше "География" пропадала для архива целиком вместе с убранным Sheet2 --
    # place_for_gps() данные считались (place-колонка в appended.csv), но нигде не
    # показывались; run_analyze() теперь резолвит GPS -> место сам (см. AnalyzeStats.cities),
    # паспорт получает диаграмму без похода в историю CSV-логов, тем же принципом "с нуля".
    body += _render_geo_card(stats.cities)
    body += _render_archive_tree_card(stats.tree_folder_counts, stats.tree_folder_bytes)
    _write(out_path, _page_shell(f"{program_name} — паспорт архива", body))
