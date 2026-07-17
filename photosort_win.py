#!/usr/bin/env python3
"""
photosort_win.py -- сборка фото/видео архива (portable Windows-версия).

Полная спецификация правил: RULES.md рядом с этим файлом (адаптация оригинального
photo-archive-prompt.md для локального запуска на Windows, без SMB). Бизнес-логика
(дедуп, тиры дат, hybrid-раскладка) идентична Linux-версии photo-sort; отличия — см.
RULES.md, раздел "Отличия от оригинала".

Запуск (portable .exe, собранный PyInstaller -- см. README-BUILD.md):
    PhotoArchive.exe --source "D:\\Фото" --target "D:\\Архив фото"
    PhotoArchive.exe                      # спросит source/target интерактивно
    PhotoArchive.exe --source "D:\\Фото" --target "D:\\Архив фото" --dry-run --sample-limit 200

Запуск из исходников (разработка/тестирование, требует Python 3 + зависимости из
requirements -- см. README-BUILD.md):
    python3 photosort_win.py --source <SOURCE> --target <TARGET>
"""

import argparse
import contextlib
import csv
import ctypes
import errno
import fnmatch
import hashlib
import inspect
import itertools
import json
import multiprocessing
import os
import re
import shutil
import sqlite3
import statistics
import subprocess
import sys
import tarfile
import tempfile
import textwrap
import time
import traceback
import warnings
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime

from PIL import Image
import pillow_heif
import imagehash
import yaml
from tqdm import tqdm as _tqdm

# 2026-07-11 live-run finding: Pillow prints "Palette images with Transparency expressed in
# bytes should be converted to RGBA images" (UserWarning, via the `warnings` module -> stderr
# by default) on ordinary real-world palette+transparency PNGs/GIFs during normal analysis --
# purely advisory (about how Pillow itself might change behavior in a future version, not
# about anything wrong with the file or our processing), with no coordination with our own
# tqdm progress bar, so it interleaves mid-line on a real console (same class of problem as
# reverse_geocoder's verbose print, see place_for_gps()). Silenced by exact message, not a
# blanket ignore of all warnings, so any other future PIL/library warning still surfaces.
warnings.filterwarnings("ignore", message="Palette images with Transparency.*", category=UserWarning)

__version__ = "0.1.0"           # версия ПРОГРАММЫ (тег/релиз, см. RELEASING.md) -- НЕ путать
                                 # с RULES_VERSION ниже (та про совместимость архива, а не exe)
RULES_VERSION = "2026-07-12"   # дата последнего изменения бизнес-правил -- см. RULES.md;
                                # менять руками при изменении логики раскладки/дедупа/дат
__copyright__ = "© 2026 Vladimir Oleynikov"  # держим строку короткой и везде идентичной
                                              # LICENSE, а не только там, куда мало кто
                                              # заглядывает; заодно закрывает обязательство
                                              # держать копирайт-уведомление легкодоступным при
                                              # распространении .exe, бандлящего сторонние
                                              # GPL/LGPL/Artistic-компоненты (см.
                                              # THIRD_PARTY_LICENSES.md)
__license__ = "Apache License 2.0"  # см. LICENSE/NOTICE -- ЕДИНСТВЕННОЕ место, которое
                      # меняется, если лицензия программы сменится. Финальное решение
                      # 2026-07-13 (после MIT -> PolyForm Noncommercial -> это, см. историю
                      # обсуждения в ROADMAP.md/CHANGELOG.md): коммерческой ценности,
                      # которую защищал бы source-available-вариант, всё равно нет (техника не
                      # уникальна) -- а для репутации/портфолио важнее узнаваемая OSI-лицензия.
                      # Название уже включает слово "License" -- места, которые его печатают,
                      # не должны добавлять его ещё раз следом.

GITHUB_URL = "https://github.com/vo1012/PhotoArchive"  # 2026-07-15: публичный репо
    # (пока приватный, откроют позже) -- исходный код + пользовательская документация + exe,
    # ОТДЕЛЬНЫЙ от репозитория разработки (photo-sort-win), в который не должны попасть
    # внутренние файлы вроде ROADMAP.md/SESSION-HANDOFF.txt. При смене адреса поменять только
    # здесь -- print_welcome_banner()/build_arg_parser() ссылаются на эту же константу.

DONATION_TEXT = (  # тот же текст, дословно, что и P.S. в PhotoArchive_ot_avtora.md -- при
    # правке одного менять и другой. Решение 2026-07-15: полный текст, не короткая строка,
    # т.к. --help может быть единственным местом, где пользователь вообще видит эту программу
    # (exe пересылается мессенджерами без README/письма от автора).
    "Этот проект не преследует получение коммерческой выгоды -- программа бесплатна и\n"
    "останется такой для всех, вне зависимости от того, воспользуетесь вы этим предложением\n"
    "или нет. Но если PhotoArchive оказалась полезной и вы хотите поддержать её разработку --\n"
    "буду искренне благодарен:\n"
    "\n"
    "  Перевод на карту Сбербанка. Номер карты: 2202 2092 9796 4578.\n"
    "  Получатель: Владимир Александрович О.\n"
    "\n"
    "Если в переводе есть поле комментария -- пожалуйста, укажите «подарок» или «донат»."
)


def _app_dir() -> str:
    """Папка рядом с исполняемым файлом: frozen PyInstaller exe -> папка с .exe;
    иначе -- папка со скриптом. work.db/photoarchive_config.yaml живут здесь же (portable: переносится
    вместе с .exe одной папкой)."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def resource_path(rel: str) -> str:
    """Путь к бандленному ресурсу (внешний бинарник и т.п.): под frozen PyInstaller --
    onefile ресурсы, добавленные через --add-binary, распаковываются во временную
    sys._MEIPASS; при обычном запуске .py -- берём ./bin/ рядом со скриптом."""
    base = getattr(sys, "_MEIPASS", _app_dir())
    return os.path.join(base, rel)


def tool_binary(name: str, exe_name: str) -> str:
    """Резолвит путь к внешнему бинарнику (exiftool/ffprobe/ffmpeg/7z/unrar):
    1) бандленный в bin/ (frozen exe или dev-дерево рядом со скриптом) -- всегда в приоритете,
       если файл реально на месте;
    2) frozen exe БЕЗ файла в bin/ (сломанная сборка -- забыли --add-binary для этого файла) --
       Security audit finding #2 (2026-07-10): всё равно возвращаем ожидаемый (хоть и
       отсутствующий) АБСОЛЮТНЫЙ путь, а не голое имя. На portable frozen exe голое имя
       означает поиск по PATH, где на машине пользователя этого инструмента гарантированно
       нет (portable exe специально не требует системных установок) -- раньше это давало ту
       же самую строку, что и осознанный dev-fallback ниже, и check_bundled_tools() (которая
       отличает "отсутствует" именно по os.path.isabs()) не могла увидеть разницу между
       "сломанная сборка" и "нормальный dev-запуск на Linux с системными пакетами" -- сломанная
       сборка проходила молча, без единого предупреждения, и весь прогон архивации тихо
       деградировал (например, без exiftool -- каждая дата в архиве получала Tier C/эвристику
       вместо реального EXIF, без единой строки об этом в логах);
    3) НЕ frozen (dev-запуск python3 photosort_win.py) без файла в bin/ -- голое имя, в
       расчёте на PATH (dev-запуск на Linux, где эти утилиты уже стоят системно -- см. README
       оригинального photo-sort)."""
    candidate = resource_path(os.path.join("bin", exe_name))
    if os.path.exists(candidate) or getattr(sys, "frozen", False):
        return candidate
    return name


def winlong(path: str) -> str:
    """Extended-length ('\\\\?\\') form of an absolute path for raw filesystem calls
    (open/os.stat/os.listdir/shutil.copy2/os.replace/...), so deeply nested ByDate/Albums
    destinations and deeply nested source trees survive the 260-character Windows MAX_PATH
    limit. Works unconditionally (no admin rights, no registry LongPathsEnabled policy
    needed) -- unlike the manifest-based longPathAware opt-in, which only helps if the
    machine's policy is already on and which a portable, no-install exe cannot set for the
    user. No-op on non-Windows (dev/test on Linux) and on UNC (\\\\server\\share) paths,
    which need a different \\\\?\\UNC\\ form not constructed here -- not needed for local
    SOURCE/TARGET paths, which this portable version requires anyway (see RULES.md).
    Only wraps calls we make directly in Python; exiftool/ffmpeg/ffprobe/7z/unrar are
    separate subprocesses invoked with the plain path and are not covered by this."""
    if os.name != "nt":
        return path
    if path.startswith("\\\\"):
        return path
    return "\\\\?\\" + os.path.abspath(path)


def _makedirs_iterative(path: str):
    """Non-recursive equivalent of `os.makedirs(path, exist_ok=True)`. `os.makedirs()` itself
    recurses once per missing path component -- mirroring SourceWalker's own now-fixed depth
    limit (see ROADMAP.md "RecursionError на очень глубоком дереве папок SOURCE") on the
    TARGET side (a dest_dir built from a deep SOURCE subpath) would otherwise still hit
    Python's OWN RecursionError here, on write instead of on enumeration -- found while
    testing that fix. Walk up to the nearest already-existing ancestor, then create the
    missing suffix top-down in a plain loop -- no recursion at any depth. Expects an
    already-`winlong()`-formed path, same as every direct `os.makedirs()` call it replaces."""
    if os.path.isdir(path):
        return
    missing = []
    cur = path
    while cur and not os.path.isdir(cur):
        missing.append(cur)
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    for d in reversed(missing):
        try:
            os.mkdir(d)
        except FileExistsError:
            pass


def _strip_winlong(path: str) -> str:
    """Undo winlong()'s '\\\\?\\' prefix, so paths coming out of a long-path-safe walk
    stay canonical plain strings for DB storage / CSV logs / display -- the prefix is only
    ever needed at the point of the actual filesystem call, re-added there via winlong()."""
    if path.startswith("\\\\?\\"):
        return path[4:]
    return path


WORKDIR = _app_dir()
SCRIPT_PATH = os.path.abspath(sys.executable if getattr(sys, "frozen", False) else __file__)

EXIFTOOL_BIN = tool_binary("exiftool", "exiftool.exe")
FFPROBE_BIN = tool_binary("ffprobe", "ffprobe.exe")
FFMPEG_BIN = tool_binary("ffmpeg", "ffmpeg.exe")
SEVENZIP_BIN = tool_binary("7z", "7z.exe")
UNRAR_BIN = tool_binary("unrar", "UnRAR.exe")

# ============================================================================
# PROGRESS  (А.4/Задача 4: экранная индикация длительных фаз, без файлового heartbeat)
# ============================================================================

_ACTIVE_BARS = []  # стек открытых ProgressReporter с реальным tqdm-баром -- см. log_line()


def log_line(msg, log=print):
    """log() совместимая обёртка: если сейчас активен хотя бы один экранный прогресс-бар,
    печать идёт БЕЗ порчи строки бара (строка бара очищается, сообщение печатается на
    отдельной строке, бар перерисовывается заново ниже), иначе -- как обычно через log().
    Передаётся как `log=` в run()/run_analyze() ОДИН раз в main() -- дальше все вложенные
    log() по всему конвейеру (SourceWalker, analyze_batch, index_archive, ...) уже вызывают
    именно этот же переданный им callable, так что оборачивать нужно только тут, без правки
    отдельных мест вызова.

    2026-07-11 (live user report): раньше здесь был `_tqdm.write(str(msg), file=sys.stderr)`
    -- штатный способ tqdm напечатать что-то, не сломав активный бар. НЕ РАБОТАЕТ с нашим
    баром: `_tqdm.write()` внутри себя решает, какие активные бары нужно очистить перед
    печатью, СРАВНИВАЯ их поток вывода с переданным `file=` -- а наш бар создан с
    `file=_RussianRateStream(...)` (см. её докстроку), собственным прокси-объектом, а не
    голым `sys.stderr`, который сюда передан. Сравнение не совпадает, tqdm не узнаёт свой же
    бар, ничего не очищает -- сообщение просто дописывается ПРЯМО В КОНЕЦ текущей строки бара
    без разделителя (живой пример: "...обработано  [archive] X.zip: archive_no_media", без
    переноса строки ни до, ни нормально после). Вместо того чтобы полагаться на этот
    внутренний tqdm-механизм сопоставления потоков, работаем напрямую с уже отслеживаемыми
    нами `_ACTIVE_BARS` -- та же последовательность действий (очистить/напечатать/
    перерисовать), которую сделал бы `tqdm.write()`, если бы правильно узнал бар."""
    if _ACTIVE_BARS:
        for b in _ACTIVE_BARS:
            if b._bar is not None:
                b._bar.clear()
        print(str(msg), file=sys.stderr)
        for b in _ACTIVE_BARS:
            if b._bar is not None:
                b._bar.refresh()
    else:
        log(msg)


def _terminal_wrap_width(fraction: float = 2 / 3, min_width: int = 40) -> int:
    """2026-07-12, интерфейс: пользователь пожаловался, что длинные строки в терминальном
    окне смотрятся некрасиво -- ограничиваем СВОИ переносы 2/3 реальной ширины терминала,
    а не даём тексту растягиваться во всю доступную ширину/заворачиваться терминалом как
    попало."""
    columns = shutil.get_terminal_size(fallback=(80, 24)).columns
    return max(min_width, int(columns * fraction))


def _wrap_console_text(text: str, width: int) -> str:
    """Построчный word-wrap с висячим отступом под уже имеющийся ведущий отступ строки
    (пункты меню продолжают визуально выделяться, а не съезжают к левому краю). Пустые
    строки и строки короче width не трогаем. break_long_words/break_on_hyphens=False --
    "слово" без пробелов длиннее width (путь, хеш, декоративная рамка из "=") остаётся на
    своей строке как есть, а не режется посередине."""
    out_lines = []
    for line in str(text).split("\n"):
        if not line.strip() or len(line) <= width:
            out_lines.append(line)
            continue
        indent = line[: len(line) - len(line.lstrip(" "))]
        wrapped = textwrap.wrap(
            line.strip(), width=width,
            initial_indent=indent, subsequent_indent=indent + "  ",
            break_long_words=False, break_on_hyphens=False,
        )
        out_lines.append("\n".join(wrapped) if wrapped else line)
    return "\n".join(out_lines)


def console_log(msg):
    """Однопараметровый log()-callable для передачи как log= в run_for_source()/
    run_analyze_for_source() из main() -- единственное место, где нужно завернуть print в
    log_line(); все вложенные log() ниже по конвейеру уже используют именно этот переданный
    им callable, так что оборачивать нужно только тут (включая перенос длинных строк -- см.
    _wrap_console_text()). isatty()-гейт: перенос -- забота о реальном терминальном окне
    пользователя, поэтому включается только когда stdout реально tty, тем же паттерном, что
    уже использует ProgressReporter.is_tty выше -- при перенаправлении в файл/пайп текст
    остаётся как есть, ничем не отличаясь от поведения до этой правки."""
    text = str(msg)
    if sys.stdout.isatty():
        text = _wrap_console_text(text, _terminal_wrap_width())
    log_line(text, log=print)


class _RussianRateStream:
    """Прокси над реальным stderr для tqdm (см. ProgressReporter, 2026-07-11 live-run
    finding): tqdm вычисляет rate_fmt/rate_noinv_fmt сам и БЕЗУСЛОВНО приклеивает английский
    суффикс "/s" (harcoded в tqdm.std.format_meter), а не наш unit="файл"/"с" -- получается
    смешение языков "файл/s". Нет публичного параметра tqdm, чтобы это переопределить;
    перехватываем на уровне write() и подменяем ТОЛЬКО этот конкретный суффикс."""

    def __init__(self, real_stream, unit):
        self._real = real_stream
        self._old = f"{unit}/s"
        self._new = f"{unit}/с"

    def write(self, s):
        return self._real.write(s.replace(self._old, self._new))

    def __getattr__(self, name):
        return getattr(self._real, name)


class ProgressReporter:
    """Живой прогресс одной длительной фазы (индексация/хеширование/распаковка/копирование).

    На терминале (stderr -- tty) -- самообновляющаяся строка tqdm: фаза, обработано/всего
    (если total известен), %, скорость (файлов/с; МБ/с -- см. unit="Б"), ETA (только когда
    total известен -- у tqdm это само получается из total). update(note=...) меняет
    описание бара на текущее длительное действие ("Распаковка family_2015.zip (4.2 ГБ)…"),
    чтобы легитимная пауза не читалась как зависание.

    Не на терминале (файл/пайп) -- это НЕ дефолтное поведение tqdm (оно всё равно шлёт \\r),
    поэтому реализовано явно: периодические обычные строки без ANSI, раз в log_interval_sec
    секунд ИЛИ log_interval_n файлов (что раньше).

    2026-07-11 live-run finding: при total=None (indeterminate) сам tqdm рисовал двойное
    двоеточие ("Фаза N — текст: : 7файл [...]") независимо от того, что в desc -- его
    ДЕФОЛТНЫЙ no-total шаблон безусловно добавляет ": " поверх уже добавленного
    set_description()'ом, а WITH-total шаблон эту дубликацию сам же и проверяет (баг именно
    в tqdm, не в нашем desc). Фикс -- явный bar_format только для total=None (см. __init__).
    Разделяй части desc через " — " (тире) просто для стиля, не для обхода этого бага.

    Использование:
        with ProgressReporter(total=n, desc="Фаза 3 — хеширование") as bar:
            for item in items:
                ...
                bar.update(1, note=short_desc)
    """

    def __init__(self, total=None, desc="", unit="файл",
                 log_interval_sec=5.0, log_interval_n=200):
        self.desc = desc
        self.unit = unit
        self.total = total
        self.count = 0
        self.is_tty = bool(getattr(sys.stderr, "isatty", lambda: False)())
        self._t0 = time.time()
        self._last_log_t = self._t0
        self._last_log_n = 0
        self.log_interval_sec = log_interval_sec
        self.log_interval_n = log_interval_n
        self._context_note = None  # see set_context()
        # 2026-07-11 live-run finding: tqdm's own DEFAULT no-total rendering path
        # unconditionally appends its own ": " after `desc`, even though set_description()
        # already appended one itself -- renders as "Фаза N — текст: : 7файл [...]" (doubled
        # colon). The WITH-total default path already guards against this (checks whether
        # desc already ends in ": " first) -- only the no-total (indeterminate) case needs
        # an explicit bar_format override to sidestep tqdm's buggy branch. Also reworded the
        # bare "{n_fmt}{unit}" counter into "всего обработано N файл" per user feedback, then
        # 2026-07-11 (this session): "N файл" is ungrammatical for most N (needs declension by
        # count -- 1 файл/2 файла/5 файлов) -- reworded to a fixed genitive-plural phrase
        # ("обработано файлов: N") that reads correctly for any N without needing to compute
        # the declension. Only unit="файл" is used anywhere in this codebase today (see
        # ProgressReporter call sites) so hardcoding "файлов" here is safe.
        tqdm_kwargs = ({"bar_format": "{desc}всего обработано файлов: {n_fmt} [{elapsed}, {rate_fmt}{postfix}]"}
                       if total is None else {})
        stream = _RussianRateStream(sys.stderr, unit)
        # 2026-07-11 (live user report, later the same session): tqdm's set_description()
        # (called from update()/set_context() below) appends its OWN trailing ": " to its
        # internal desc attribute (confirmed empirically -- NOT documented tqdm behavior worth
        # trusting from memory alone) -- but passing desc= directly to the _tqdm(...)
        # CONSTRUCTOR does NOT go through set_description(), so IT renders one frame with the
        # raw string and no trailing ": " at all, the instant the object is constructed (tqdm
        # refreshes on init). An archive with 0 files (the live report's exact case) never
        # calls update() even once, so its bar rendered ONLY that raw pre-update() frame for
        # its entire lifetime -- "Фаза 1 — индексация архива" glued directly onto the literal
        # "всего обработано..." with zero separator ("архивавсего"). Fix: construct with an
        # EMPTY desc (nothing for the bar_format's {desc} to glue onto, however briefly) and
        # set the real one immediately after via update(0) -- which runs set_description()
        # and gets the correct trailing ": ", so even a zero-file run's only render is already
        # correctly separated from its very first frame, not just from the second one on.
        self._bar = _tqdm(total=total, desc="", unit=unit, file=stream,
                           dynamic_ncols=True, leave=True, **tqdm_kwargs) if self.is_tty else None
        self.update(0)

    def __enter__(self):
        if self._bar is not None:
            _ACTIVE_BARS.append(self)
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False

    def set_context(self, note):
        """2026-07-11 (this session), user feedback: an archive being extracted already shows
        a "текущее действие" note (see update()'s docstring finding below) so a slow archive
        never reads as a hang -- but plain folders being walked show nothing at all, so on a
        slow network drive/huge directory tree there is no way to tell where the program is
        currently digging. Unlike update()'s per-call `note` (a ONE-OFF that update() itself
        clears back to blank the moment the next file has no note of its own -- see the
        2026-07-11 finding just below), this is a PERSISTENT baseline: SourceWalker calls this
        once per directory entered, and it stays the fallback description until the next
        directory replaces it, surviving any number of ordinary per-file update(note=None)
        calls in between. Refreshes the display immediately (no update() call needed) since
        being inside a slow os.listdir() -- before any file in that directory has been
        processed yet -- is exactly the case this exists for.

        2026-07-12, живой репорт пользователя (строка при распаковке архива обрезалась
        терминалом даже во всю ширину экрана): вызывающий код (SourceWalker) уже обрезает
        note по общему, довольно грубому бюджету (_progress_note_budget(), не знает точную
        длину self.desc). Здесь, зная self.desc ТОЧНО, обрезаем ещё раз по более точному
        бюджету -- реальная ширина терминала минус фактическая длина desc минус запас под
        хвостовые tqdm-счётчики. _truncate_progress_note() идемпотентна для уже обрезанного
        текста (берёт символы с конца, второго "…" не появляется)."""
        if note and self._bar is not None and sys.stderr.isatty():
            note = _truncate_progress_note(note, maxlen=self._context_note_budget())
        self._context_note = note
        if self._bar is not None:
            self._bar.set_description(f"{self.desc} — {note}" if note else self.desc)

    def _context_note_budget(self, tail_reserve: int = 58, min_width: int = 15) -> int:
        """2026-07-12: бюджет под note, ТОЧНО учитывающий длину self.desc (в отличие от
        _progress_note_budget()'s общей оценки "самый длинный реальный префикс"). tail_reserve
        -- запас под хвостовые tqdm-счётчики этого bar_format ("всего обработано файлов: N
        [MM:SS, X.Xфайл/с]"), с большим запасом на случай многочасового прогона с 6-значным
        счётчиком файлов."""
        columns = shutil.get_terminal_size(fallback=(80, 24)).columns
        overhead = len(self.desc) + len(" — ") + tail_reserve
        return max(min_width, columns - overhead)

    def update(self, n=1, note=None):
        self.count += n
        effective_note = note or self._context_note
        if self._bar is not None:
            # 2026-07-11 finding (live production run): previously only set_description()
            # when note was truthy -- tqdm's description is sticky, so a note from one large
            # video ("хеширование большого видео") stayed on screen for every subsequent
            # photo with note=None, falsely suggesting the run was stuck processing video.
            # Always (re)set it, falling back to the persistent context note (see
            # set_context()) or the plain phase description if neither is set.
            self._bar.set_description(f"{self.desc} — {effective_note}" if effective_note else self.desc)
            self._bar.update(n)
            return
        now = time.time()
        if (now - self._last_log_t >= self.log_interval_sec
                or self.count - self._last_log_n >= self.log_interval_n):
            self._emit_plain_line(effective_note)
            self._last_log_t = now
            self._last_log_n = self.count

    def _emit_plain_line(self, note=None):
        elapsed = max(time.time() - self._t0, 1e-6)
        rate = self.count / elapsed
        pct = f"{100 * self.count / self.total:.0f}%" if self.total else "?"
        total_part = f"/{self.total}" if self.total else ""
        extra = f" -- {note}" if note else ""
        print(f"[{self.desc}] {self.count}{total_part} ({pct}), {rate:.1f} {self.unit}/с{extra}",
              file=sys.stderr)

    def close(self):
        if self._bar is not None:
            self._bar.close()
            if self in _ACTIVE_BARS:
                _ACTIVE_BARS.remove(self)
        elif self.count:
            self._emit_plain_line(note="фаза завершена")


# ============================================================================
# CONFIG  (from pipeline/config.py)
# ============================================================================


IMAGE_EXTS = {"jpg", "jpeg", "png", "heic", "heif", "tif", "tiff", "bmp", "webp", "gif"}
RAW_EXTS = {"cr2", "cr3", "nef", "arw", "dng"}
VIDEO_EXTS = {"mp4", "mov", "m4v", "avi", "mkv", "3gp", "mts", "m2ts", "wmv", "flv", "webm", "mod", "tod"}
ARCHIVE_EXTS = {"zip", "rar", "7z", "tar", "gz", "tgz", "bz2"}
ARCHIVE_MULTI_EXTS = {"tar.gz", "tar.bz2"}

# Security audit finding #4: the archive-bomb check below only looks at total_size vs.
# compressed_size ratio -- an archive with millions of near-empty entries (e.g. 2M files of
# 0-1 bytes each) reports a tiny total_size, sails straight past that check with almost no
# free-space requirement, and only THEN grinds the per-file pipeline (SHA256, work.db insert,
# 7 CSV log rows per entry) for hours, while bloating NTFS metadata far past what total_size
# suggested (many tiny files cost real disk space in cluster/MFT overhead). A real family
# archive of any format essentially never has this many entries in one file.
MAX_ARCHIVE_ENTRIES = 200_000


def format_formats_report() -> str:
    """Печатает те же множества, что реально использует file_type() для классификации
    -- в отличие от таблицы расширений в RULES.md, не может разойтись с кодом."""
    groups = [
        ("Изображения", IMAGE_EXTS),
        ("RAW", RAW_EXTS),
        ("Видео", VIDEO_EXTS),
        ("Архивы", ARCHIVE_EXTS | ARCHIVE_MULTI_EXTS),
    ]
    lines = ["Расширения файлов, которые PhotoArchive распознаёт (без точки, регистр не важен):", ""]
    for title, exts in groups:
        lines.append(f"  {title}: {', '.join(sorted(exts))}")
    lines.append("")
    lines.append('Всё остальное -- тип "other", не медиафайл.')
    return "\n".join(lines)


HARD_EXCLUDE_DIRS = {
    # 2026-07-11 finding (live production run, SOURCE=C:\): "Default"/"Default User" are
    # Windows' hidden template profiles (used to create new user accounts), never a real
    # user -- every subfolder under them is permission-locked for everyone by OS design, so
    # a whole-disk scan hit a wall of scary "Отказано в доступе" messages for a profile that
    # was never going to have photos in it. "Мои видеозаписи"/"Моя музыка"/"мои рисунки" (and
    # the English-locale equivalents) are legacy compatibility JUNCTIONS Windows creates
    # under every real user's Documents\, pointing at that same user's real Videos\/Music\/
    # Pictures\ -- Windows itself refuses to enumerate through them (Access Denied) to avoid
    # double-walking; the real folders are already reached directly and unaffected by this.
    "system volume information",
    "default", "default user",
    "мои видеозаписи", "моя музыка", "мои рисунки",
    "my videos", "my music", "my pictures",
    # 2026-07-11 finding (live production run, SOURCE=C:\, second round): four more
    # top-level Windows folders that are always Access-Denied for any real user account --
    # "Documents and Settings" is a legacy Windows XP-era junction pointing at "Users" (same
    # double-walking protection as the Мои видеозаписи junctions above), "MSOCache" is
    # Microsoft Office's installer cache (SYSTEM/admin only), "PerfLogs" and "Recovery" are
    # Windows' own performance-log and recovery-partition-mirror folders (also SYSTEM/admin
    # only) -- none of these can ever contain a real user photo.
    "documents and settings", "msocache", "perflogs", "recovery",
    # 2026-07-11 (session on managing this list): "__служебные_файлы" is a different kind of
    # entry than the ones above -- not about OS access, about not letting the tool re-ingest
    # its OWN logs/tmp_extract as a "new" source. Kept unconditional for the same reason
    # SKIP_PHOTOSORT.txt is (see below) -- a quick name-based gate in addition to the marker.
    "__служебные_файлы",
}

# 2026-07-11 (session on managing this list): unlike HARD_EXCLUDE_DIRS above, none of these
# are actually inaccessible -- they're a heuristic ("almost certainly not a photo"), not an OS
# restriction. $RECYCLE.BIN in particular is readable by its owning user (each drive's
# \$RECYCLE.BIN\<SID>\ subfolder belongs to that user, no elevation needed) -- a user wanting
# to recover deleted photos may legitimately want it scanned. Kept as a sensible, user-editable
# DEFAULT (see Config.default_exclude_dirs/photoarchive_config.yaml.example) rather than hardcoded, so
# removing an entry here doesn't require touching code.
DEFAULT_EXCLUDE_DIR_NAMES = ["node_modules", ".git", "$recycle.bin"]

# Слой 1 (гейт обхода): "настоящие" системные папки резолвятся через переменные окружения,
# а не хардкодом имён -- переносимо между версиями/языками Windows. Обход внутрь них
# гейтится Config.scan_system_dirs (по умолчанию False -- не заходить). Применяется только
# при рекурсии (SourceWalker._walk_dir, is_root=False); явно указанный SOURCE внутрь такой
# папки всегда обрабатывается (см. RULES.md "ПРАВИЛО ЯВНОГО УКАЗАНИЯ").
SYSTEM_DIR_ENV_VARS = (
    "WINDIR", "ProgramFiles", "ProgramFiles(x86)", "ProgramData",
    "LOCALAPPDATA", "APPDATA", "TEMP",
)


def _resolve_system_dirs():
    dirs = []
    for var in SYSTEM_DIR_ENV_VARS:
        v = os.environ.get(var)
        if v:
            dirs.append(os.path.normcase(os.path.realpath(v)))
    return dirs


SYSTEM_DIRS = _resolve_system_dirs()


def is_under_system_dir(path: str) -> bool:
    real = os.path.normcase(os.path.realpath(path))
    return any(real == d or real.startswith(d + os.sep) for d in SYSTEM_DIRS)


# Слой 2 (маршрутизация сомнительного внутри того, что реально обходится): "шумные" зоны
# определяются по имени СЕГМЕНТА пути, могут встретиться где угодно (не только в системных
# папках) -- поэтому это НЕ повод пропускать обход (в отличие от HARD_EXCLUDE_DIRS/
# default_exclude_dirs выше), а повод строже классифицировать. Раньше temp/tmp/.cache/
# .thumbnails были в EXCLUDE_DIRS (жёсткий
# скип, фото внутри терялись бы молча); теперь это зона noisy -- см. classify_zone().
NOISE_SEGMENT_NAMES = {
    "cache", "code cache", "gpucache", "temp", "tmp", ".cache", ".thumbnails",
    "thumbnailcache",
}


def classify_zone(path: str) -> str:
    for seg in path.split(os.sep):
        if seg.strip().lower() in NOISE_SEGMENT_NAMES:
            return "noisy"
    return "normal"

EXCLUDE_FILES_PATTERNS = [
    "thumbs.db", "desktop.ini", ".ds_store", "*.tmp", "*.part",
    "hiberfil.sys", "pagefile.sys", "swapfile.sys",  # locked Windows system files, never readable
    "ntuser.dat", "ntuser.dat.log*",
]
SIDECAR_PATTERNS = ["*.xmp", "*.aae"]
SKIP_MARKER = "SKIP_PHOTOSORT.txt"

# Security audit finding #1 (2026-07-10): TMP_EXTRACT_DIR is user-configurable
# (photoarchive_config.yaml, no path validation) and its contents get shutil.rmtree'd at the start of
# EVERY run, including --dry-run, to clean up after a crashed previous run. If tmp_extract_dir
# is ever misconfigured (typo, or a maliciously "helpful" photoarchive_config.yaml) to point at an existing
# unrelated folder -- e.g. the user's Desktop -- that folder's entire contents were silently
# deleted on the very first launch. Fix: only ever delete entries that look like OUR OWN
# extraction dirs (named after the archive's sha256 hex digest, see _handle_archive() below) --
# anything else is left untouched and just logged as a warning, regardless of what
# tmp_extract_dir turns out to be.
_OWN_TMP_EXTRACT_ENTRY_RE = re.compile(r"^[0-9a-f]{64}$")

# 2026-07-11: a sensible, user-editable DEFAULT (see Config.dump_segment_names/
# photoarchive_config.yaml.example) -- real heuristic "probably not an album" names, safe to let a user
# add/remove entries in photoarchive_config.yaml. Deliberately does NOT include the self-protection names
# below (DUMP_SEGMENT_NAMES_PROTECTED) -- those must never be removable via config.
DEFAULT_DUMP_SEGMENT_NAMES = [
    "dcim", "camera", "camera uploads", "фотокамера", "photostream", "моменты",
    "screenshots", "скриншоты", "downloads", "загрузки", "saved pictures",
    "pictures", "изображения", "фотопленка",
    "users", "home",
    "desktop", "рабочий стол",  # 2026-07-11 finding: universal Windows profile folder, same
                                # category as downloads/pictures above -- a loose photo dropped
                                # on the Desktop is not a deliberately-named album
    "camera roll",  # 2026-07-11 finding: standard Windows/OneDrive phone-sync folder name,
                    # observed on a real archive alongside "camera"/"camera uploads" above
    # NB: no generic "archive"/"архив" entry here (added then reverted 2026-07-11) -- that
    # word is too plausible as someone's real, deliberately-named album folder to blanket-
    # whitelist. The actual problem (a Yandex.Disk export's every zip unpacking into an
    # internal folder literally called "archive") is fixed at the root instead: an archive's
    # OWN filename now anchors the album whenever nothing meaningful exists on the disk-side
    # path leading to it, and folder names found INSIDE any archive are never trusted on
    # their own to name an album -- see find_album()'s archive_boundary_idx.
]
# 2026-07-11: our OWN archive's top-level segments (p.5.2б) -- if SOURCE is pointed at the
# root of an already-built archive ("cascade" re-run), these must never themselves be
# swallowed whole as one giant "album". UNLIKE DEFAULT_DUMP_SEGMENT_NAMES above, this is NOT
# exposed as a photoarchive_config.yaml override -- if a user removed "albums" from an editable list
# (accidentally or not) and then pointed SOURCE at an already-built TARGET, the "Albums"
# segment would stop being recognised as internal scaffolding and get swallowed as a real
# album. Always unioned into the effective dump-name set inside is_dump_segment(), on top of
# whatever the user configured.
DUMP_SEGMENT_NAMES_PROTECTED = frozenset({
    "bydate", "albums", "raw",
    "_unsorted",  # disputed files' top-level home (see Config.dispute) -- same self-eating
                  # protection as bydate/albums/raw above
})
DEFAULT_DUMP_SEGMENT_PREFIXES = ["whatsapp", "telegram"]
# 2026-07-11, по запросу пользователя: ручной способ пометить конкретную папку-источник как
# "не альбом, сортировать по дате", даже если её имя иначе выглядело бы как настоящий альбом
# (например, папка облачной синхронизации "Яндекс_диск"). Пользователь переименовывает СВОЮ
# папку вручную ("~Яндекс_диск") -- программа исходники никогда не переименовывает сама, это
# однозначно read-only сигнал. См. is_dump_segment().
FORCE_DUMP_PREFIX = "~"
DUMP_SEGMENT_REGEXES = [
    re.compile(r"^\d{3}[A-Za-z]+$"),
    # 2026-07-11 finding: Windows' own default name for an unrenamed new folder (seen THREE
    # times on one real archive, including a numbered "Новая папка (2)" sibling -- Windows
    # appends " (N)" for each further unnamed folder in the same place) -- unambiguous, nobody
    # deliberately leaves a real photo album named this.
    re.compile(r"^новая папка(\s\(\d+\))?$"),
    re.compile(r"^new folder(\s\(\d+\))?$"),
]
# 2026-07-11: a bare 6-8 digit folder name (YYYYMMDD/YYMMDD-shaped, e.g. "20240802") is dump
# ONLY when deciding which segment gets to NAME the album -- an album literally called
# "20240802" is never wanted. But once a real album has already been found further up the
# path, that same folder, dragged in unrenamed straight from a camera/phone export, very
# plausibly represents a deliberate day-grouping the user wants kept -- the same reasoning
# that already exempts a date WITH separators ("2015-08-20") from being dump at all. Any OTHER
# bare digit sequence (short like "101", or 9+ digits) has no such exemption -- see
# is_dump_segment()'s n.isdigit() branch, which handles ALL pure-digit segments together so
# a blanket "^\d+$" pattern here can no longer silently shadow this 6-8-digit exemption
# (that shadowing was a real bug caught by test_bare_digit_date_folder_kept_inside_album_but_not_as_album_name).
DUMP_SEGMENT_DATE_REGEX = re.compile(r"^\d{6,8}$")

# Public name (see __version__ banner / PhotoArchive.exe), deliberately NOT the internal
# "photosort" codename -- an end user re-ingesting their own already-built archive as a new
# SOURCE (p.5.2) has only ever seen "PhotoArchive". Appended verbatim to the end of every
# day/month/month-unknown folder name build_bydate_dest_dir() generates.
DUMP_TAG = " [PhotoArchive]"

# Precomputed defaults used whenever is_dump_segment()/find_album() are called WITHOUT an
# explicit cfg (e.g. bare calls in ci/windows_ci_test.py) -- keeps those call sites working
# unmodified. PROTECTED names are unioned in here too, same as Config.dump_segment_names_lower
# does for the configurable path (see Config.__post_init__).
_DEFAULT_DUMP_SEGMENT_NAMES_LOWER = frozenset(
    n.lower() for n in DEFAULT_DUMP_SEGMENT_NAMES) | DUMP_SEGMENT_NAMES_PROTECTED
_DEFAULT_DUMP_SEGMENT_PREFIXES_TUPLE = tuple(DEFAULT_DUMP_SEGMENT_PREFIXES)


def is_dump_segment(name: str, for_subpath: bool = False, *,
                     dump_names=None, dump_prefixes=None) -> bool:
    """for_subpath=True (2026-07-11): called while deciding which folders survive as
    subpath UNDERNEATH an already-found album (find_album()'s second pass), not while
    searching for the album name itself -- a bare 6-8 digit folder there is presumed to be a
    deliberate day-grouping the user carried over unrenamed, not noise, so it is NOT
    collapsed (see DUMP_SEGMENT_DATE_REGEX).

    dump_names/dump_prefixes (2026-07-11, photoarchive_config.yaml exposure): the effective, already-
    lowered set/tuple to check against -- production call sites pass
    cfg.dump_segment_names_lower/cfg.dump_segment_prefixes_tuple (user config ∪
    DUMP_SEGMENT_NAMES_PROTECTED, see Config.__post_init__). Left at their default (None) this
    falls back to the module defaults (DEFAULT_DUMP_SEGMENT_NAMES ∪ PROTECTED,
    DEFAULT_DUMP_SEGMENT_PREFIXES) -- callers with no cfg in scope (tests) keep working as-is."""
    if dump_names is None:
        dump_names = _DEFAULT_DUMP_SEGMENT_NAMES_LOWER
    if dump_prefixes is None:
        dump_prefixes = _DEFAULT_DUMP_SEGMENT_PREFIXES_TUPLE
    stripped = name.strip()
    if stripped.endswith(DUMP_TAG):
        # A day/month folder we generated ourselves (build_bydate_dest_dir) -- unambiguous,
        # no user ever types this tag by hand. This is the ONLY thing that marks a
        # date-shaped segment as dump -- see below, "looks like a date" alone is no longer
        # sufficient (p.5.2): a real user album can legitimately be named "2000-10-10".
        return True
    if stripped.startswith(FORCE_DUMP_PREFIX):
        # 2026-07-11, по просьбе пользователя: ручной способ заставить программу считать
        # конкретную папку НЕ альбомом, даже если её имя иначе выглядело бы как настоящее
        # (пример: папка синхронизации облака "Яндекс_диск" -- реальное, осмысленное на вид
        # имя, но пользователь хочет, чтобы её содержимое раскладывалось по дате). Пользователь
        # переименовывает СВОЮ папку-источник вручную (программа исходники не трогает) --
        # "~Яндекс_диск". Безусловно, как и обычные dump-имена -- не сохраняется даже
        # подпапкой (for_subpath не важен), сегмент с тильдой просто стирается из пути.
        return True
    n = stripped.lower()
    if n in dump_names:
        return True
    if n.startswith(dump_prefixes):
        return True
    for rx in DUMP_SEGMENT_REGEXES:
        if rx.match(n):
            return True
    if n.isdigit():
        if DUMP_SEGMENT_DATE_REGEX.match(n):
            return not for_subpath
        return True  # any other bare digit run (short like "101", or 9+ digits) -- no exemption
    return False


def _clean_str_set(items) -> set:
    """Нормализует пользовательский список из photoarchive_config.yaml в множество строк
    в нижнем регистре -- элементы, которые НЕ являются строкой (в частности, вложенный
    список/словарь), молча отбрасываются, а не приводятся через str().

    Причина именно отбрасывать, а не str()-ить что попало: YAML-якоря/алиасы (`&a`/`*a`)
    позволяют СЖАТО описать экспоненциально большую вложенную структуру ("billion laughs") --
    yaml.safe_load() сам по себе парсит такой файл мгновенно (алиасы -- это просто ссылки на
    один и тот же Python-объект, не копии), но str()/repr() на такой ссылке рекурсивно
    разворачивает её ЦЕЛИКОМ в строку. Крошечный (несколько сотен байт) файл конфига с
    десятком уровней вложенности разворачивался в этой функции в сотни МБ строк и держал
    Config(...) занятым ~15 секунд -- найдено адверсариальным тестированием, не гипотетически.
    Обычный корректный конфиг (список строк) этот фильтр не меняет никак."""
    return {item.strip().lower() for item in (items or []) if isinstance(item, str)}


@dataclass
class Config:
    source: str
    target: str
    workdir: str = None  # None -> WORKDIR (папка рядом с exe/скриптом), см. __post_init__
    layout: str = "hybrid"
    date_mode: str = "best_guess"
    place_lookup: str = "offline"
    home_country: str = "RU"
    archive_hash_cache: bool = True
    max_archive_depth: int = 8
    max_dest_path: int = 240
    small_image_px: int = 640
    free_space_margin_gb: float = 10.0
    dry_run: bool = False
    sample_limit: int = 0
    read_retry_count: int = 3
    read_retry_delay: float = 5.0
    bydate_granularity: str = "month"  # day | month | year | flat -- гранулярность папок ByDate
        # 2026-07-11: дефолт сменён с "day" на "month" по прямой просьбе пользователя --
        # архивы, уже собранные под "day" (append-only), НЕ переименовываются задним числом;
        # смена дефолта касается только НОВЫХ TARGET (или явных photoarchive_config.yaml/CLI-переопределений).
    scan_system_dirs: bool = False  # заходить ли в системные папки (см. SYSTEM_DIR_ENV_VARS)
    default_exclude_dirs: list = field(default_factory=lambda: list(DEFAULT_EXCLUDE_DIR_NAMES))
        # 2026-07-11: редактируемый (в отличие от HARD_EXCLUDE_DIRS) список папок, пропускаемых
        # по умолчанию -- эвристика "скорее всего не фото", не защита ОС. Пользователь может
        # убрать любое имя в photoarchive_config.yaml (например "$recycle.bin", если хочет вытащить
        # удалённые файлы) -- см. DEFAULT_EXCLUDE_DIR_NAMES/photoarchive_config.yaml.example.
    extra_exclude_dirs: list = field(default_factory=list)  # доп. исключения ПОВЕРХ default_exclude_dirs
    dump_segment_names: list = field(default_factory=lambda: list(DEFAULT_DUMP_SEGMENT_NAMES))
        # 2026-07-11: тот же паттерн default_+extra_, что default_exclude_dirs выше --
        # редактируемая эвристика "не альбом, скорее всего мусорное имя" (dcim, camera,
        # downloads, ...). НЕ включает DUMP_SEGMENT_NAMES_PROTECTED (bydate/albums/raw/
        # _unsorted) -- те самозащита архива от самопоедания при каскадном прогоне и никогда
        # не отдаются в конфиг, см. dump_segment_names_lower ниже.
    extra_dump_segment_names: list = field(default_factory=list)  # доп. имена ПОВЕРХ dump_segment_names
    dump_segment_prefixes: list = field(default_factory=lambda: list(DEFAULT_DUMP_SEGMENT_PREFIXES))
    extra_dump_segment_prefixes: list = field(default_factory=list)  # доп. префиксы ПОВЕРХ dump_segment_prefixes
    mirror_raw: bool = True  # False = избыточный RAW (есть парный JPEG) не мирроить; одинокий RAW спасается всегда
    tmp_extract_dir: str = None  # None -> тот же физический том, что TARGET (см. __post_init__)
    raw_layout: str = "mirror"  # mirror (по умолчанию) | sibling -- см. raw_dest_dir()
    debug: bool = False  # p.5.3: подробные [DEBUG]-строки в actions.log (причины решений,
                          # полный traceback на ошибках) -- для тестеров/разбора багов между
                          # релизами, НЕ ротируется отдельно от остального actions.log
    suppress_logs: bool = False  # ТЗ-меню 2026-07-10, раздел 5: интерактивный "пробный
        # прогон" из голого меню репетирует archive dry_run=True, НО не создаёт __служебные_файлы\
        # и не пишет CSV/summary.txt в TARGET -- результат только на экране. НЕ выставляется
        # ни из CLI-флагов, ни из photoarchive_config.yaml (сознательно нет argparse/yaml ручки) -- только
        # интерактивный слой (run_bare_launch()) конструирует Config с этим флагом напрямую.
        # CLI --dry-run продолжает писать логи как раньше (suppress_logs там всегда False).

    def __post_init__(self):
        if self.bydate_granularity not in ("day", "month", "year", "flat"):
            raise ValueError(
                f"bydate_granularity должен быть day/month/year/flat, получено: {self.bydate_granularity!r}"
            )
        if self.raw_layout not in ("mirror", "sibling"):
            raise ValueError(
                f"raw_layout должен быть mirror/sibling, получено: {self.raw_layout!r} "
                f"(flat сознательно не реализован -- см. ROADMAP.md/RULES.md)"
            )
        # Security audit finding #7: photoarchive_config.yaml is user-editable and none of these numeric
        # fields were range-checked. free_space_margin_gb in particular is finding #2 --
        # a negative value silently defeats every free-space check in the program (see
        # _handle_archive()/atomic_copy()), letting a run fill the disk to literally 0 bytes
        # free before an unhandled OSError finally stops it.
        if self.free_space_margin_gb < 0:
            raise ValueError(
                f"free_space_margin_gb не может быть отрицательным (получено "
                f"{self.free_space_margin_gb!r}) -- отрицательный запас отключает защиту от "
                f"заполнения диска."
            )
        if self.max_archive_depth < 1:
            raise ValueError(
                f"max_archive_depth должен быть не меньше 1 (получено {self.max_archive_depth!r})"
            )
        if self.max_dest_path < 10:
            raise ValueError(
                f"max_dest_path должен быть не меньше 10 символов, иначе не остаётся места даже "
                f"на короткое имя с расширением (получено {self.max_dest_path!r})"
            )
        if self.small_image_px < 0:
            raise ValueError(
                f"small_image_px не может быть отрицательным (получено {self.small_image_px!r})"
            )
        if self.sample_limit < 0:
            raise ValueError(
                f"sample_limit не может быть отрицательным -- 0 значит «без ограничения» "
                f"(получено {self.sample_limit!r})"
            )
        if self.read_retry_count < 0:
            raise ValueError(
                f"read_retry_count не может быть отрицательным (получено {self.read_retry_count!r})"
            )
        if self.read_retry_delay < 0:
            raise ValueError(
                f"read_retry_delay не может быть отрицательным (получено {self.read_retry_delay!r})"
            )
        if not os.path.isabs(self.source):
            raise ValueError(
                f"SOURCE ({self.source}) не является полным путём -- укажите полный путь, "
                f"начиная с буквы диска (D:\\...) или \\\\сервер\\ресурс\\..."
            )
        if not os.path.isabs(self.target):
            raise ValueError(
                f"TARGET ({self.target}) не является полным путём -- укажите полный путь, "
                f"начиная с буквы диска (D:\\...) или \\\\сервер\\ресурс\\..."
            )
        self.source = os.path.abspath(self.source)
        self.target = os.path.abspath(self.target)
        source_real = os.path.normcase(os.path.realpath(self.source))
        target_real = os.path.normcase(os.path.realpath(self.target))
        if source_real == target_real:
            raise ValueError(
                f"SOURCE и TARGET указывают на один и тот же путь ({self.target}) -- "
                f"архив читал бы сам себя как источник. Укажите разные пути."
            )
        # На native Windows нет физической защиты ro-mount, которая раньше не давала
        # процессу писать в источник -- источники read-only теперь только потому, что код
        # НИКОГДА не формирует путь записи внутри дерева источника. TARGET подпапкой внутри
        # SOURCE -- ПОДДЕРЖИВАЕМЫЙ, документированный сценарий (например, SOURCE=D:\,
        # TARGET=D:\Архив фото), от самопоедания в этом случае защищает
        # SourceWalker._walk_dir (пропускает TARGET целиком при обходе) -- см. RULES.md.
        # А вот обратное -- SOURCE внутри TARGET -- ничем не защищено: walk() не сравнивает
        # с target_real свой собственный корень (проверка только "not is_root"), так что
        # SOURCE, указывающий вглубь TARGET, читал бы (и мог бы повторно поглощать) файлы,
        # которые этот же прогон только что сам туда записал. Явно отклоняем такой запуск.
        if source_real.startswith(target_real + os.sep):
            raise ValueError(
                f"SOURCE ({self.source}) находится внутри TARGET ({self.target}) -- "
                f"прогон мог бы повторно поглощать файлы, только что записанные им же самим. "
                f"Укажите SOURCE вне дерева TARGET."
            )
        self.workdir = self.workdir or WORKDIR
        self.index_db = os.path.join(self.workdir, "work.db")
        self.albums_root = os.path.join(self.target, "Albums")
        self.bydate_root = os.path.join(self.target, "ByDate")
        self.raw_root = os.path.join(self.target, "RAW")
        self.undated_root = os.path.join(self.bydate_root, "0000-undated")  # см. RULES.md
        # "УМБРЕЛЛА" __служебные_файлы\ (переименована из "_photosort\" 2026-07-11 -- то имя
        # было внутренним старым названием проекта до ребрендинга в PhotoArchive и ничего не
        # говорило пользователю о назначении папки; всё остальное в интерактивном слое и так
        # уже целиком на русском, см. RULES.md): все служебные (не медиа-) папки архива живут
        # под одним корнем, а не разбросаны по TARGET как раньше (_disputed, _logs, _prompt,
        # _tmp_extract у самого TARGET) -- проще один раз объяснить пользователю и один раз
        # защитить маркером SKIP_PHOTOSORT.txt (см. ensure_target_layout).
        self.photosort_dir = os.path.join(self.target, "__служебные_файлы")
        # 2026-07-11 finding: disputed files are REAL photos (just not confidently
        # classified), not disposable metadata like logs/tmp_extract -- burying them one
        # level inside a folder that otherwise holds only safe-to-delete service data invites
        # exactly the "looks technical, must be safe to delete" mistake. Moved to a TOP-LEVEL
        # sibling of Albums/ByDate/RAW, same standing as those, and deliberately NOT under the
        # SKIP_PHOTOSORT.txt umbrella -- same reasoning as ByDate\0000-undated: if TARGET is
        # later reused as SOURCE, a disputed file should be free to "graduate" into a real
        # place once rules/evidence improve, not be walled off. "_unsorted" renamed to
        # "_Unsorted" same session, same consistency reasoning as the umbrella rename above.
        self.dispute = os.path.join(self.target, "_Unsorted")
        self.logs = os.path.join(self.photosort_dir, "logs")
        self.prompt_dir = os.path.join(self.photosort_dir, "prompt")
        # TMP_EXTRACT_DIR: конфигурируемый (см. photoarchive_config.yaml.example). Дефолт -- НЕ системный
        # %TEMP%, а {TARGET}\__служебные_файлы\tmp_extract\ -- уже гарантированно на том же
        # физическом ТОМЕ, что TARGET (это подпапка самого TARGET), так что финализация
        # файлов из архива по умолчанию всегда получает быстрый rename (см. place_file()/
        # same_volume() в блоке IO_COPY), без специальной логики поиска "корня тома".
        # Причина держать распаковку вообще НЕ внутри дерева Albums/ByDate/RAW: source может
        # быть read-only носителем (CD/DVD, смонтированный ISO), а класть временные файлы
        # прямо в архивные корни нельзя (append-only, никаких временных артефактов в них).
        # Явный tmp_extract_dir в конфиге -- всегда в приоритете (пользователь может указать
        # другой том, например быстрый SSD -- тогда финализация деградирует до copy, см.
        # report_environment()).
        if self.tmp_extract_dir:
            self.tmp_extract = os.path.abspath(self.tmp_extract_dir)
        else:
            self.tmp_extract = os.path.join(self.photosort_dir, "tmp_extract")
        self.default_exclude_dirs_lower = _clean_str_set(self.default_exclude_dirs)
        self.extra_exclude_dirs_lower = _clean_str_set(self.extra_exclude_dirs)
        # 2026-07-11: эффективный набор dump-имён/префиксов для is_dump_segment()/find_album()
        # -- ВСЕГДА объединяет пользовательский photoarchive_config.yaml с DUMP_SEGMENT_NAMES_PROTECTED
        # (bydate/albums/raw/_unsorted), даже если пользователь их не указывал или случайно
        # убрал -- эти четыре не редактируемы в принципе, см. поле dump_segment_names выше.
        self.dump_segment_names_lower = (
            _clean_str_set(self.dump_segment_names)
            | _clean_str_set(self.extra_dump_segment_names)
            | DUMP_SEGMENT_NAMES_PROTECTED
        )
        self.dump_segment_prefixes_tuple = tuple(
            _clean_str_set(self.dump_segment_prefixes)
            | _clean_str_set(self.extra_dump_segment_prefixes)
        )

# ============================================================================
# DB  (from pipeline/db.py)
# ============================================================================


SCHEMA = """
CREATE TABLE IF NOT EXISTS archive (
    path TEXT PRIMARY KEY,
    root TEXT,
    size INTEGER,
    mtime REAL,
    sha256 TEXT,
    phash TEXT,
    duration REAL,
    type TEXT,
    width INTEGER,
    height INTEGER,
    bitrate INTEGER
);
CREATE INDEX IF NOT EXISTS idx_archive_sha ON archive(sha256);
CREATE INDEX IF NOT EXISTS idx_archive_size ON archive(size);

CREATE TABLE IF NOT EXISTS source (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    read_path TEXT,            -- actual on-disk path usable for opening the file right now
    origin_display TEXT,       -- human path for logs, e.g. "Foto2015.zip -> 2015/Crimea/IMG_1234.jpg"
    rel_path TEXT,             -- path relative to the walked root (source root or archive internal root),
                               -- used for album/dump segment detection
    size INTEGER,
    mtime REAL,
    ext TEXT,
    type TEXT,                 -- image / raw / video / archive / other
    sha256 TEXT,
    phash TEXT,
    width INTEGER,
    height INTEGER,
    aspect REAL,
    duration REAL,
    exif_dt TEXT,
    camera TEXT,
    gps_lat REAL,
    gps_lon REAL,
    place TEXT,
    is_media INTEGER,
    media_note TEXT,
    raw_pair_read_path TEXT,   -- for RAW: paired jpeg read_path (if any); for JPEG: paired RAW read_path
    date_value TEXT,
    date_tier TEXT,
    date_conf TEXT,
    date_evidence TEXT,
    decision TEXT,
    dest_path TEXT,
    note TEXT
);
CREATE INDEX IF NOT EXISTS idx_source_sha ON source(sha256);
CREATE INDEX IF NOT EXISTS idx_source_size ON source(size);

CREATE TABLE IF NOT EXISTS archive_cache (
    path TEXT PRIMARY KEY,
    size INTEGER,
    mtime REAL,
    sha256 TEXT,
    phash TEXT,
    duration REAL,
    width INTEGER,
    height INTEGER,
    bitrate INTEGER
);
"""


def connect(index_db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(index_db_path)
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def db_reset(index_db_path: str) -> sqlite3.Connection:
    """work.db is ephemeral: rebuilt fresh every run (except archive_cache table,
    which is intentionally preserved across runs when ARCHIVE_HASH_CACHE=1)."""
    conn = sqlite3.connect(index_db_path)
    conn.executescript(SCHEMA)
    conn.execute("DELETE FROM archive")
    conn.execute("DELETE FROM source")
    conn.commit()
    return conn

# ============================================================================
# HASHING  (from pipeline/hashing.py)
# ============================================================================


pillow_heif.register_heif_opener()


def sha256_file(path: str, chunk_size: int = 4 * 1024 * 1024) -> str:
    h = hashlib.sha256()
    with open(winlong(path), "rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def image_phash_and_size(path: str):
    """Returns (phash_hex, width, height) or (None, None, None) if unreadable."""
    try:
        with Image.open(winlong(path)) as im:
            im = im.convert("L")
            w, h = im.size
            ph = imagehash.phash(im)
            return str(ph), w, h
    except Exception:
        return None, None, None


def image_size_only(path: str):
    """Returns (width, height) or (None, None) if unreadable -- используется analyze-quick
    (skip_hash=True в analyze_batch): PIL лениво декодирует заголовок для .size без разбора
    полных пиксельных данных, поэтому это на порядок дешевле, чем image_phash_and_size()
    (там ещё и convert("L") + imagehash.phash -- полное декодирование + DCT)."""
    try:
        with Image.open(winlong(path)) as im:
            return im.size
    except Exception:
        return None, None


def hamming(hash_a: str, hash_b: str) -> int:
    if hash_a is None or hash_b is None:
        return 999
    try:
        ha = imagehash.hex_to_hash(hash_a)
        hb = imagehash.hex_to_hash(hash_b)
        return ha - hb
    except Exception:
        return 999


def ffprobe_json(path: str) -> dict:
    try:
        out = subprocess.run(
            [
                FFPROBE_BIN, "-v", "quiet", "-print_format", "json",
                "-show_format", "-show_streams", path,
            ],
            capture_output=True, timeout=60,
        )
        import json
        return json.loads(out.stdout.decode("utf-8", "replace") or "{}")
    except Exception:
        return {}


def video_duration_and_resolution(path: str):
    info = ffprobe_json(path)
    duration = None
    width = height = None
    bitrate = None
    fmt = info.get("format", {})
    if fmt.get("duration"):
        try:
            duration = float(fmt["duration"])
        except Exception:
            pass
    if fmt.get("bit_rate"):
        try:
            bitrate = int(fmt["bit_rate"])
        except Exception:
            pass
    for s in info.get("streams", []):
        if s.get("codec_type") == "video":
            width = s.get("width")
            height = s.get("height")
            if duration is None and s.get("duration"):
                try:
                    duration = float(s["duration"])
                except Exception:
                    pass
            break
    return duration, width, height, bitrate


def video_phash_3frames(path: str, duration: float):
    """Extract frames at 10/50/90% and phash each. Returns list of up to 3 hex phashes."""
    if not duration or duration <= 0:
        offsets = [0.5, 1.0, 1.5]
    else:
        offsets = [duration * 0.10, duration * 0.50, duration * 0.90]
    hashes = []
    with tempfile.TemporaryDirectory() as tmpdir:
        for i, off in enumerate(offsets):
            frame_path = os.path.join(tmpdir, f"frame_{i}.jpg")
            try:
                subprocess.run(
                    [
                        FFMPEG_BIN, "-y", "-ss", str(max(off, 0)), "-i", path,
                        "-frames:v", "1", "-q:v", "3", frame_path,
                    ],
                    capture_output=True, timeout=30,
                )
                if os.path.exists(frame_path):
                    ph, _, _ = image_phash_and_size(frame_path)
                    if ph:
                        hashes.append(ph)
            except Exception:
                continue
    return hashes


def video_hashes_match(hashes_a, hashes_b, threshold=6) -> bool:
    if not hashes_a or not hashes_b:
        return False
    n = min(len(hashes_a), len(hashes_b))
    if n == 0:
        return False
    for i in range(n):
        if hamming(hashes_a[i], hashes_b[i]) > threshold:
            return False
    return True

# ============================================================================
# METADATA  (from pipeline/metadata.py)
# ============================================================================


EXIF_TAGS = [
    "-DateTimeOriginal", "-CreateDate", "-GPSDateStamp",
    "-QuickTime:CreateDate", "-MediaCreateDate", "-TrackCreateDate",
    "-XMP:DateCreated", "-IPTC:DateCreated",
    "-Make", "-Model",
    "-ImageWidth", "-ImageHeight",
    "-GPSLatitude", "-GPSLongitude",
    "-FileType",
]

_DATE_RE = re.compile(r"^(\d{4}):(\d{2}):(\d{2})[ T](\d{2}):(\d{2}):(\d{2})")


def parse_exif_date(s):
    if not s or not isinstance(s, str):
        return None
    m = _DATE_RE.match(s.strip())
    if not m:
        return None
    y, mo, d, h, mi, se = (int(x) for x in m.groups())
    try:
        return datetime(y, mo, d, h, mi, se)
    except ValueError:
        return None


def exiftool_batch(paths, batch_size=200):
    """Returns dict: path -> tag dict (raw exiftool JSON entry).
    Paths go through an -@ argfile, not raw argv: exiftool.exe on Windows does its own
    wildcard-expansion of command-line arguments (no shell globbing on Windows, so exiftool
    does it itself) and mis-parses non-ASCII bytes in the process -- any path with Cyrillic
    (or other non-Latin1) characters, e.g. a typical album name, fails with "Wildcards don't
    work in the directory specification" / "No matching files" and silently yields no EXIF
    data at all, downgrading the file straight to Tier B/C dates. -@ argfile reads paths
    from a file instead of argv, bypassing that layer entirely."""
    results = {}
    for i in range(0, len(paths), batch_size):
        chunk = paths[i:i + batch_size]
        if not chunk:
            continue
        argfile_path = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".args", delete=False, encoding="utf-8"
            ) as argfile:
                argfile_path = argfile.name
                for p in chunk:
                    argfile.write(p + "\n")
            out = subprocess.run(
                [EXIFTOOL_BIN, "-j", "-n", "-charset", "filename=utf8"] + EXIF_TAGS
                + ["-@", argfile_path],
                capture_output=True, timeout=120,
            )
            data = json.loads(out.stdout.decode("utf-8", "replace") or "[]")
            # Match by position, not by the echoed SourceFile string: exiftool's JSON
            # output preserves -@ argfile input order, and on Windows the SourceFile it
            # echoes back can differ from the original path string (separator style /
            # Unicode normalization), which would silently break a string-keyed lookup and
            # downgrade every match to Tier B/C dates without any error. Positional zip is
            # exact when counts line up; fall back to string matching only if they don't
            # (e.g. exiftool dropped an unreadable file from its output).
            if len(data) == len(chunk):
                for p, entry in zip(chunk, data, strict=True):
                    results[p] = entry
            else:
                for entry in data:
                    sf = entry.get("SourceFile")
                    if sf:
                        results[sf] = entry
        except Exception:
            continue
        finally:
            if argfile_path:
                try:
                    os.unlink(argfile_path)
                except OSError:
                    pass
    return results


def best_exif_datetime(tags: dict):
    """Tier A candidate date from EXIF/QuickTime/XMP/IPTC tags, in priority order."""
    for key in (
        "DateTimeOriginal", "CreateDate", "QuickTime:CreateDate",
        "MediaCreateDate", "TrackCreateDate", "XMP:DateCreated",
        "IPTC:DateCreated", "GPSDateStamp",
    ):
        dt = parse_exif_date(tags.get(key))
        if dt:
            return dt, key
    return None, None


def gps_from_tags(tags: dict):
    lat = tags.get("GPSLatitude")
    lon = tags.get("GPSLongitude")
    if lat is None or lon is None:
        return None, None
    try:
        return float(lat), float(lon)
    except Exception:
        return None, None


def camera_from_tags(tags: dict):
    make = (tags.get("Make") or "").strip()
    model = (tags.get("Model") or "").strip()
    if make and model:
        if make.lower() in model.lower():
            return model
        return f"{make} {model}"
    return make or model or None

# ============================================================================
# CLASSIFY  (from pipeline/classify.py)
# ============================================================================


def ext_of(path: str) -> str:
    name = os.path.basename(path).lower()
    if name.endswith(".tar.gz"):
        return "tar.gz"
    if name.endswith(".tar.bz2"):
        return "tar.bz2"
    return name.rsplit(".", 1)[-1] if "." in name else ""


def file_type(path: str) -> str:
    e = ext_of(path)
    if e in IMAGE_EXTS:
        return "image"
    if e in RAW_EXTS:
        return "raw"
    if e in VIDEO_EXTS:
        return "video"
    if e in ARCHIVE_EXTS or e in ("tar.gz", "tar.bz2"):
        return "archive"
    return "other"


def classify_image(path: str, width, height, camera, size_bytes: int, small_image_px: int = 640):
    """Returns (is_media: bool, note: str|None).
    Two-tier minimum size rule:
      max side < 256px            -> not media (probable icon), always disputed.
      256px <= max side < SMALL_IMAGE_PX and no camera EXIF -> media, but flagged 'small_image'
                                      (kept, not lost -- for later batch review).
      max side < SMALL_IMAGE_PX with camera EXIF, or max side >= SMALL_IMAGE_PX -> ordinary photo, no flag.
    """
    e = ext_of(path)
    if e in ("ico", "svg"):
        return False, "icon_or_svg"
    if e == "gif":
        try:
            with Image.open(winlong(path)) as im:
                if getattr(im, "is_animated", False):
                    return False, "animated_gif"
        except Exception:
            pass

    if width is None or height is None:
        return True, "low_confidence_photo"

    max_side = max(width, height)
    if max_side < 256:
        return False, "tiny_image"

    if max_side < small_image_px and not camera:
        return True, "small_image"

    return True, None


# --- А.2 (analyze-режимы): грубая проверка "расширение соответствует сигнатуре файла" ---
# Не претендует на криминалистическую точность -- задача чисто диагностическая (найти
# явно переименованные/битые-по-контейнеру файлы в источнике до сборки архива), не часть
# основного конвейера классификации "медиа/не медиа" (тот не меняется).
_SIGNATURE_TABLE = [
    (b"\xff\xd8\xff", "image"),                    # JPEG
    (b"\x89PNG\r\n\x1a\n", "image"),                # PNG
    (b"GIF87a", "image"), (b"GIF89a", "image"),     # GIF
    (b"BM", "image"),                               # BMP
    (b"II*\x00", "image"), (b"MM\x00*", "image"),   # TIFF / многие RAW-контейнеры
    (b"PK\x03\x04", "archive"), (b"PK\x05\x06", "archive"),  # ZIP
    (b"Rar!\x1a\x07", "archive"),                   # RAR
    (b"7z\xbc\xaf\x27\x1c", "archive"),             # 7z
    (b"\x1f\x8b", "archive"),                       # GZIP (в т.ч. tar.gz)
]


def sniff_signature(path: str):
    """Читает первые байты файла и грубо определяет РЕАЛЬНЫЙ тип по сигнатуре (magic bytes),
    независимо от расширения в имени. Возвращает "image"/"video"/"archive"/None
    (None = сигнатура не распознана вообще -- сравнивать не с чем, не считается
    несоответствием). Используется только в analyze-режимах (RULES.md, "несоответствие
    расширения и сигнатуры") -- не влияет на решения обычной сборки архива."""
    try:
        with open(winlong(path), "rb") as f:
            head = f.read(32)
    except OSError:
        return None
    if len(head) >= 12 and head[4:8] == b"ftyp":
        brand = head[8:12]
        if brand in (b"heic", b"heix", b"heim", b"heis", b"mif1", b"msf1", b"hevc", b"hevx"):
            return "image"
        return "video"  # остальные ftyp-бренды (isom/mp42/qt  /M4V ...) -- видео-контейнеры
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image"
    for sig, kind in _SIGNATURE_TABLE:
        if head.startswith(sig):
            return kind
    return None


def _coarse_kind(ftype: str) -> str:
    """Огрубление file_type() до трёх категорий сравнения с sniff_signature(): raw трактуем
    как image-подобный (TIFF-based контейнеры RAW дают ту же сигнатуру II*/MM*, что и TIFF --
    точная проверка конкретного RAW-вендора вне разумного объёма диагностической эвристики)."""
    if ftype in ("image", "raw"):
        return "image"
    if ftype == "video":
        return "video"
    if ftype == "archive":
        return "archive"
    return "other"

# ============================================================================
# ARCHIVES  (from pipeline/archives.py)
# ============================================================================


def detect_archive_format(path: str):
    e = ext_of(path)
    if e == "zip":
        return "zip"
    if e == "7z":
        return "7z"
    if e == "rar":
        return "rar"
    if e == "tar":
        return "tar"
    if e in ("tar.gz", "tgz"):
        return "tar.gz"
    if e in ("tar.bz2",):
        return "tar.bz2"
    if e == "gz" and path.lower().endswith(".tar.gz"):
        return "tar.gz"
    if e == "bz2" and path.lower().endswith(".tar.bz2"):
        return "tar.bz2"
    return None


class ArchiveInfo:
    def __init__(self, total_size=0, encrypted=False, entries=0, ok=True, path_traversal=False,
                 has_media_candidate=True):
        self.total_size = total_size
        self.encrypted = encrypted
        self.entries = entries
        self.ok = ok
        self.path_traversal = path_traversal
        # 2026-07-11 finding (live production run): a whole-disk scan hits plenty of
        # installers/backups/configs zipped up with zero photos inside -- every one of them
        # was being FULLY extracted just to discover that afterwards (see media_count in
        # _handle_archive()). The archive's own listing (already parsed for the size
        # estimate/zip-slip check above) already names every member -- default True (unknown
        # format/parse failure) so nothing is ever skipped on a guess, only when the listing
        # was actually readable and genuinely contains no plausible media/nested-archive name.
        self.has_media_candidate = has_media_candidate


def _member_name_is_media_candidate(name: str) -> bool:
    ext = os.path.splitext(name)[1].lstrip(".").lower()
    return ext in IMAGE_EXTS or ext in RAW_EXTS or ext in VIDEO_EXTS or ext in ARCHIVE_EXTS


def _looks_like_path_traversal(member_name: str) -> bool:
    """Security audit finding #8: a zip/7z/rar member name containing a literal '..'
    segment, or that is itself an absolute path, could make the extracting tool write
    outside dest_dir ("zip-slip") -- unlike tar (extracted here member-by-member with
    sanitize_windows_component() + filter="data", see extract_archive()), zip/7z/rar
    extraction is fully delegated to the external 7z.exe/UnRAR.exe subprocess, whose own
    path-traversal protection this code has no way to verify. Checked against the archive's
    OWN listing (already fetched for the free-space estimate) before extraction ever starts,
    independent of whatever the external binary would or wouldn't have done."""
    normalized = member_name.replace("\\", "/")
    if normalized.startswith("/"):
        return True
    if re.match(r"^[A-Za-z]:", normalized):
        return True
    return any(seg == ".." for seg in normalized.split("/"))


def _list_7z(path: str) -> ArchiveInfo:
    try:
        out = subprocess.run(
            [SEVENZIP_BIN, "l", "-slt", path],
            capture_output=True, timeout=120,
        )
        text = out.stdout.decode("utf-8", "replace")
    except Exception:
        return ArchiveInfo(ok=False)

    sep_idx = text.find("----------")
    if sep_idx == -1:
        return ArchiveInfo(ok=False)
    remainder = text[sep_idx + len("----------"):]
    blocks = re.split(r"\n\s*\n", remainder)

    total = 0
    encrypted = False
    entries = 0
    path_traversal = False
    has_media_candidate = False
    for block in blocks:
        if "Path =" not in block:
            continue
        is_folder = False
        size = 0
        block_encrypted = False
        member_path = None
        for line in block.splitlines():
            line = line.strip()
            if line.startswith("Path ="):
                member_path = line.split("=", 1)[1].strip()
            elif line.startswith("Folder ="):
                is_folder = line.split("=", 1)[1].strip() == "+"
            elif line.startswith("Size ="):
                try:
                    size = int(line.split("=", 1)[1].strip())
                except ValueError:
                    size = 0
            elif line.startswith("Encrypted ="):
                block_encrypted = line.split("=", 1)[1].strip() == "+"
        if not is_folder:
            total += size
            entries += 1
            if member_path and _member_name_is_media_candidate(member_path):
                has_media_candidate = True
        if block_encrypted:
            encrypted = True
        if member_path and _looks_like_path_traversal(member_path):
            path_traversal = True
    return ArchiveInfo(total_size=total, encrypted=encrypted, entries=entries, ok=True,
                        path_traversal=path_traversal, has_media_candidate=has_media_candidate)


def _list_rar(path: str) -> ArchiveInfo:
    try:
        out = subprocess.run(
            [UNRAR_BIN, "lt", "-p-", path],
            capture_output=True, timeout=120,
        )
        text = out.stdout.decode("utf-8", "replace")
    except Exception:
        return ArchiveInfo(ok=False)

    total = 0
    entries = 0
    encrypted = "encrypted" in text.lower() or "password" in text.lower()
    for m in re.finditer(r"^\s*Size:\s*(\d+)", text, re.MULTILINE):
        total += int(m.group(1))
        entries += 1
    if entries == 0 and "Type: File" not in text and "is not RAR archive" in text:
        return ArchiveInfo(ok=False)
    member_names = [m.group(1).strip() for m in re.finditer(r"^\s*Name:\s*(.+)$", text, re.MULTILINE)]
    path_traversal = any(_looks_like_path_traversal(n) for n in member_names)
    has_media_candidate = any(_member_name_is_media_candidate(n) for n in member_names)
    return ArchiveInfo(total_size=total, encrypted=encrypted, entries=entries, ok=True,
                        path_traversal=path_traversal, has_media_candidate=has_media_candidate)


def _list_tar(path: str, mode: str) -> ArchiveInfo:
    try:
        with tarfile.open(winlong(path), mode) as tf:
            total = 0
            entries = 0
            has_media_candidate = False
            for m in tf.getmembers():
                if m.isfile():
                    total += m.size
                    entries += 1
                    if _member_name_is_media_candidate(m.name):
                        has_media_candidate = True
            return ArchiveInfo(total_size=total, encrypted=False, entries=entries, ok=True,
                                has_media_candidate=has_media_candidate)
    except Exception:
        return ArchiveInfo(ok=False)


TAR_MODES = {"tar": "r:", "tar.gz": "r:gz", "tar.bz2": "r:bz2"}

# TarFile.extract(..., filter="data") -- the path-traversal-safe extraction filter (PEP 706)
# -- only exists on Python >=3.9.17/3.10.12/3.11.4/3.12. Detected once at import time instead
# of assuming the build machine is new enough: on an older interpreter, passing filter="data"
# unconditionally would raise TypeError for every single tar member and silently make
# tar/tar.gz/tar.bz2 sources non-functional (each member logged as "failed to extract").
_TAR_EXTRACT_SUPPORTS_DATA_FILTER = "filter" in inspect.signature(tarfile.TarFile.extract).parameters


def list_archive(path: str, fmt: str) -> ArchiveInfo:
    if fmt in ("zip", "7z"):
        return _list_7z(path)
    if fmt == "rar":
        return _list_rar(path)
    if fmt in TAR_MODES:
        return _list_tar(path, TAR_MODES[fmt])
    return ArchiveInfo(ok=False)


def extract_archive(path: str, fmt: str, dest_dir: str, log=print) -> bool:
    _makedirs_iterative(winlong(dest_dir))
    try:
        if fmt in ("zip", "7z"):
            out = subprocess.run(
                [SEVENZIP_BIN, "x", f"-o{dest_dir}", "-y", path],
                capture_output=True, timeout=1800,
            )
            return out.returncode == 0
        if fmt == "rar":
            out = subprocess.run(
                [UNRAR_BIN, "x", "-y", path, dest_dir + os.sep],
                capture_output=True, timeout=1800,
            )
            return out.returncode == 0
        if fmt in TAR_MODES:
            # Member-by-member (не extractall целиком): tar может быть собран не на Windows
            # и содержать имя с символом, который NTFS не примет (':', '?', ...) -- одно
            # такое имя иначе роняло бы исключение и обрывало распаковку ВСЕГО архива.
            # Здесь -- как и для имени назначения при копировании -- сегменты санитизируются
            # заранее, а один нераспаковавшийся файл просто логируется и пропускается.
            if not _TAR_EXTRACT_SUPPORTS_DATA_FILTER:
                log("  ВНИМАНИЕ: интерпретатор Python, которым собран .exe, слишком старый для "
                    "filter=\"data\" (защита от path traversal при распаковке tar) -- "
                    "распаковка продолжится БЕЗ этой защиты. Пересоберите на Python >=3.11.4/3.12.")
            extract_kwargs = {"filter": "data"} if _TAR_EXTRACT_SUPPORTS_DATA_FILTER else {}
            with tarfile.open(winlong(path), TAR_MODES[fmt]) as tf:
                for member in tf.getmembers():
                    safe_name = "/".join(
                        sanitize_windows_component(p) for p in member.name.split("/") if p
                    )
                    if not safe_name:
                        continue
                    member.name = safe_name
                    try:
                        tf.extract(member, winlong(dest_dir), **extract_kwargs)
                    except Exception as e:
                        log(f"  пропущен файл в архиве (не удалось распаковать) {member.name}: {e}")
            return True
    except Exception as e:
        log(f"  ошибка распаковки {path}: {e}")
        return False
    return False


FILE_ATTRIBUTE_REPARSE_POINT = 0x400


def _is_reparse_point(path: str) -> bool:
    """True если path -- Windows reparse point (symlink ИЛИ junction). Определяется через
    st_file_attributes (тот же приём, что is_hidden_path() для DOS_ATTR_HIDDEN_BIT) вместо
    os.path.islink(), который на Windows исторически распознаёт только IO_REPARSE_TAG_SYMLINK,
    но НЕ IO_REPARSE_TAG_MOUNT_POINT (junction) -- os.path.isjunction() существует только с
    Python 3.12, а поддерживаемый минимум интерпретатора для сборки этого exe ниже (см.
    _TAR_EXTRACT_SUPPORTS_DATA_FILTER выше). st_file_attributes ловит оба вида reparse point
    единообразно на любой версии Windows-Python. os.lstat (не stat) -- не следует за самим
    reparse point при проверке. Fallback на os.path.islink() на не-Windows (dev/test на
    Linux, где junction как явления не существует)."""
    try:
        st = os.lstat(winlong(path))
    except OSError:
        return False
    if hasattr(st, "st_file_attributes"):
        return bool(st.st_file_attributes & FILE_ATTRIBUTE_REPARSE_POINT)
    return os.path.islink(path)


def count_extracted_files(root: str) -> int:
    """Post-extraction defense in depth (Phase 2 audit finding 7): `_looks_like_path_traversal()`
    parses the human-readable text output of `7z l -slt`/`unrar lt` with regexes -- not a
    fully trusted layer (no concrete bypass is known, but it's still string-parsing external
    tool output, not a structured API). A zip/7z/rar member whose traversal the pre-extraction
    regex check missed would get written by 7z.exe/UnRAR.exe OUTSIDE extract_dir -- which means
    it simply won't be found by walking extract_dir afterwards, so a LOWER file count than the
    archive's own listing claimed is the detectable symptom of a successful escape. Counts only
    regular files (not directories) under `root`, followlinks=False like
    find_reparse_point_in_tree() (a reparse point itself is caught by that separate check, not
    walked into here)."""
    total = 0
    for _dirpath, _dirnames, filenames in os.walk(winlong(root), followlinks=False):
        total += len(filenames)
    return total


def find_reparse_point_in_tree(root: str):
    """Post-extraction defense in depth (2026-07-10 Phase 2 audit): zip/7z/rar extraction is
    fully delegated to the external 7z.exe/UnRAR.exe subprocess -- unlike tar, which is
    protected at extraction time by tarfile's filter="data" (PEP 706, see extract_archive()),
    which explicitly refuses to extract a symlink member whose target would resolve outside
    the destination directory. If 7z.exe/UnRAR.exe ever materializes a symlink/junction member
    from a zip/7z/rar archive as a real Windows reparse point inside extract_dir, walking that
    tree afterwards (SourceWalker._walk_dir) could follow it to an arbitrary location elsewhere
    on disk -- the walker's cycle-detection (ancestors=...) only catches a loop back onto an
    already-open ancestor of the CURRENT walk, not a one-way escape to an unrelated directory.
    Worst case: a booby-trapped "family photos.zip" silently pulls unrelated files (e.g. the
    victim's Documents folder) into the resulting archive. Whether 7z.exe/UnRAR.exe actually do
    this on a real Windows machine is UNCONFIRMED (needs verification on real hardware, see
    SESSION-HANDOFF.txt) -- this check costs nothing when it never triggers, and closes the gap
    outright if it does. os.walk(followlinks=False) does not descend into a reparse point
    (so this scan itself can't be tricked into walking outside `root`), but still lists it once
    at its parent level -- enough to detect and reject it. Returns the first reparse point path
    found, or None if the tree is clean."""
    for dirpath, dirnames, filenames in os.walk(winlong(root), followlinks=False):
        for name in dirnames + filenames:
            full = os.path.join(dirpath, name)
            if _is_reparse_point(full):
                return _strip_winlong(full)
    return None


def free_space_bytes(path: str) -> int:
    """Free space of the volume containing `path`. `path` itself (or its TARGET/
    tmp_extract ancestors) may not exist yet -- e.g. analyze-* modes never create TARGET,
    and tmp_extract is created lazily on first archive extraction -- so walk up to the
    nearest existing ancestor (worst case: the drive root, which always exists) rather than
    calling disk_usage() directly on a path shutil can't stat."""
    p = os.path.abspath(path)
    while p and not os.path.isdir(winlong(p)):
        parent = os.path.dirname(p)
        if parent == p:
            break
        p = parent
    usage = shutil.disk_usage(winlong(p))
    return usage.free


def cleanup_dir(path: str):
    if os.path.isdir(winlong(path)):
        shutil.rmtree(winlong(path), ignore_errors=True)

# ============================================================================
# WALKER  (from pipeline/walker.py)
# ============================================================================


@dataclass
class SourceItem:
    read_path: str
    origin_display: str
    rel_path: str          # posix-style path used for album/dump-segment detection
    size: int
    mtime: float
    ftype: str              # image / raw / video / other
    sibling_path: str = None  # same-basename sibling (RAW<->image pair) in the same directory, if any
    zone: str = "normal"    # "normal" | "noisy" -- см. classify_zone()
    archive_no_crc: bool = False  # True only for items extracted from tar/tar.gz/tar.bz2 --
                                   # those formats carry no per-file content checksum at all
                                   # (unlike zip/7z/rar, whose extractors verify CRC and fail
                                   # the whole extraction on mismatch), so place_file() must
                                   # not take its CRC-trusted same-volume rename shortcut for
                                   # these -- see place_file() docstring.
    archive_boundary_idx: int = None  # 2026-07-11: index (in rel_path.split("/")) of the
        # OUTERMOST archive's own filename segment, for items that came from inside any
        # archive; None for items found directly on disk. See find_album() -- an archive's
        # internal folder names are never trusted as an album name on their own (a generic
        # word like "archive" inside a zip must not silently become Albums\archive\); if no
        # real album exists on the disk-side portion of the path, the archive's OWN filename
        # becomes the album instead, and everything inside it becomes that album's subpath.


def _matches_any(name: str, patterns) -> bool:
    lname = name.lower()
    return any(fnmatch.fnmatch(lname, pat) for pat in patterns)


def _strip_trailing_arrow(s: str) -> str:
    """Removes exactly one trailing " → " separator (used to join nested archive names) --
    NOT str.rstrip(" → "), which strips any trailing run of space/→ characters and can eat
    into a nested archive's own name if it happens to end in one of those characters (ruff
    B005: misleading multi-character strip, found 2026-07-17)."""
    return s[:-len(" → ")] if s.endswith(" → ") else s


class SourceWalker:
    def __init__(self, cfg: Config, log=print, progress_cb=None):
        self.cfg = cfg
        self.log = log
        # 2026-07-11, user feedback: an archive being extracted already shows a "текущее
        # действие" note (see _handle_archive()) so a slow archive never reads as a hang --
        # plain folders showed nothing at all, no way to tell where the program is currently
        # digging on a slow network drive/huge tree. Optional callback (typically
        # ProgressReporter.set_context(), see its docstring) -- default None keeps every
        # caller that doesn't pass one (or constructs SourceWalker without a live progress
        # bar at all) working exactly as before.
        self._progress_cb = progress_cb
        self.archive_logs = []   # list of (archive_display, status, note)
        self.sidecar_logs = []   # list of (display_path,)
        self.skipped_marker_logs = []  # list of (display_path,)
        # 2026-07-11 (session on managing the exclude-dir list): pропуски по имени папки
        # (hard/default/extra) считаются, а не печатаются построчно -- на полном скане диска
        # node_modules/.git может встретиться сотни раз, построчный print был бы спамом.
        # Ключ -- (reason, name), см. excluded_dir_summary(). Гейт системных папок (см.
        # is_under_system_dir ниже) срабатывает максимум по разу на каждый реально
        # присутствующий SYSTEM_DIR_ENV_VARS-корень (рекурсия сразу останавливается), поэтому
        # для него достаточно списка путей, не агрегации.
        self._excluded_dir_hits = {}
        self.system_dir_skips = []  # list of (dirpath,)
        self._target_real = os.path.realpath(cfg.target)

    def _record_excluded_dir(self, name: str, reason: str):
        key = (reason, name)
        self._excluded_dir_hits[key] = self._excluded_dir_hits.get(key, 0) + 1

    def excluded_dir_summary(self):
        """list of (name, reason, count), one row per distinct (reason, name) pair."""
        return [(name, reason, count) for (reason, name), count in self._excluded_dir_hits.items()]

    def _log_archive(self, display, status, note=""):
        self.archive_logs.append((display, status, note))
        self.log(f"  [archive] {display}: {status} {note}".rstrip())

    def walk(self):
        source = self.cfg.source
        if os.path.isfile(winlong(source)):
            # SOURCE is a single archive file (or a folder-of-parts handled by caller)
            fmt = detect_archive_format(source)
            if fmt:
                yield from self._handle_archive(source, rel_prefix="", origin_prefix="", depth=1,
                                                 archive_boundary_idx=0)
                return
            # a single plain media file given directly as SOURCE
            t = file_type(source)
            if t in ("image", "raw", "video"):
                st = os.stat(winlong(source))
                yield SourceItem(source, source, os.path.basename(source), st.st_size, st.st_mtime, t,
                                  zone=classify_zone(source))
            return

        # ПРАВИЛО ЯВНОГО УКАЗАНИЯ (RULES.md): если сам SOURCE уже лежит внутри системной
        # папки (например, SOURCE=C:\Users\X\AppData\Local\SomeApp), пользователь явно
        # выбрал это дерево целиком -- гейт системных папок ниже не должен повторно
        # срабатывать на КАЖДОМ вложенном уровне (иначе всё глубже первого подкаталога
        # молча терялось бы без единой строки в лог, т.к. is_under_system_dir() истинен
        # для любого потомка системного корня). Фиксируем это один раз для всего обхода.
        self._root_under_system_dir = is_under_system_dir(source)
        root_real = os.path.normcase(os.path.realpath(source))
        yield from self._walk_dir(source, rel_prefix="", origin_prefix="", depth=0, is_root=True,
                                   ancestors=(root_real,))

    def _walk_dir(self, dirpath, rel_prefix, origin_prefix, depth, is_root=False, ancestors=(),
                  archive_no_crc=False, archive_boundary_idx=None):
        # ROADMAP.md "RecursionError на очень глубоком дереве папок SOURCE": до этой правки
        # descent в подпапки шёл через `yield from self._walk_dir(...)` -- дерево глубиной
        # ~1000+ уровней (путь при этом всего пара КБ, ничего экстремального для
        # Windows-длинных-путей) роняло RecursionError и обрывало ВЕСЬ прогон, даже независимые
        # файлы вне глубокой ветки. Явный стек вместо рекурсии по подпапкам -- единственный
        # рекурсивный вызов был в самом САМОМ КОНЦЕ метода, без обработки результата после
        # (ничего не делается с yield'нутыми элементами здесь же), поэтому эквивалентная замена
        # прямая: pending-подпапки кладутся в стек вместо рекурсивного спуска. origin_prefix/
        # depth/archive_no_crc/archive_boundary_idx остаются константами на весь вызов (как и
        # раньше передавались в рекурсию без изменений) -- по стеку путешествуют только то, что
        # реально менялось на каждом уровне: dirpath/rel_prefix/is_root/ancestors.
        # Архивная рекурсия (_handle_archive ниже, вложенные архивы) НЕ трогается -- она уже
        # ограничена max_archive_depth и не растёт с глубиной папок SOURCE, отдельный, гораздо
        # более мелкий источник глубины стека.
        stack = [(dirpath, rel_prefix, is_root, ancestors)]
        while stack:
            cur_dirpath, cur_rel_prefix, cur_is_root, cur_ancestors = stack.pop()

            if not cur_is_root:
                if os.path.realpath(cur_dirpath) == self._target_real:
                    continue  # self-eating protection: never descend into TARGET
                base = os.path.basename(cur_dirpath)
                base_lower = base.lower()
                if base_lower in HARD_EXCLUDE_DIRS:
                    self._record_excluded_dir(base_lower, "защищено программой, не настраивается")
                    continue
                if base_lower in self.cfg.default_exclude_dirs_lower:
                    self._record_excluded_dir(base_lower, "по умолчанию -- настраивается через default_exclude_dirs")
                    continue
                if base_lower in self.cfg.extra_exclude_dirs_lower:
                    self._record_excluded_dir(base_lower, "добавлено пользователем через extra_exclude_dirs")
                    continue
                if (not self.cfg.scan_system_dirs and not self._root_under_system_dir
                        and is_under_system_dir(cur_dirpath)):
                    self.system_dir_skips.append(cur_dirpath)
                    continue

            if self._progress_cb is not None:
                # 2026-07-11, user feedback: set BEFORE the (potentially slow, e.g. network
                # drive/huge directory) os.listdir() call below, not after -- being inside a
                # slow listdir with nothing processed yet is exactly the case this exists to
                # make visible. origin_prefix is set only while walking an archive's extracted
                # temp dir (a meaningless hash-named path on disk) -- shown as
                # "archive.zip → subdir" instead, same convention as every other
                # archive-nested display elsewhere.
                disp = f"{origin_prefix}{cur_rel_prefix}" if origin_prefix else cur_dirpath
                self._progress_cb(_truncate_progress_note(disp))

            try:
                entries = sorted(os.listdir(winlong(cur_dirpath)))
            except OSError as e:
                self.log(f"  не удалось прочитать директорию {cur_dirpath}: {e}")
                continue

            if not cur_is_root:
                if SKIP_MARKER in entries:
                    disp = origin_prefix + cur_rel_prefix
                    self.skipped_marker_logs.append(disp)
                    self.log(f"  [skip_marker] {disp}")
                    continue

            subdirs = []
            files = []
            for name in entries:
                full = os.path.join(cur_dirpath, name)
                if os.path.isdir(winlong(full)):
                    subdirs.append(name)
                else:
                    files.append(name)

            # same-basename RAW<->image sibling pairing (scoped to this directory)
            sibling_by_base = {}
            for name in files:
                t = file_type(os.path.join(cur_dirpath, name))
                if t not in ("image", "raw"):
                    continue
                base_noext = os.path.splitext(name)[0].lower()
                sibling_by_base.setdefault(base_noext, {})[t] = os.path.join(cur_dirpath, name)

            def _defer_raw_with_sibling(name, _dirpath=cur_dirpath, _sibling_by_base=sibling_by_base):
                t = file_type(os.path.join(_dirpath, name))
                if t != "raw":
                    return 0
                base_noext = os.path.splitext(name)[0].lower()
                return 1 if "image" in _sibling_by_base.get(base_noext, {}) else 0

            files.sort(key=_defer_raw_with_sibling)

            for name in files:
                full = os.path.join(cur_dirpath, name)
                if _matches_any(name, EXCLUDE_FILES_PATTERNS) or name == SKIP_MARKER:
                    continue
                if _matches_any(name, SIDECAR_PATTERNS):
                    self.sidecar_logs.append(origin_prefix + cur_rel_prefix + "/" + name if cur_rel_prefix else origin_prefix + name)
                    continue

                rel = f"{cur_rel_prefix}/{name}" if cur_rel_prefix else name
                disp = f"{origin_prefix}{rel}" if origin_prefix else rel

                fmt = detect_archive_format(full)
                if fmt:
                    base_no_ext = name[: -(len(ext_of(name)) + 1)] if ext_of(name) else name
                    new_rel_prefix = f"{cur_rel_prefix}/{base_no_ext}" if cur_rel_prefix else base_no_ext
                    new_origin_prefix = f"{origin_prefix}{name} → "
                    # 2026-07-11: record the OUTERMOST archive's own name-segment index the
                    # first time we cross into any archive -- a nested archive-inside-archive
                    # keeps the outer one's boundary (see find_album()), not its own.
                    this_boundary = archive_boundary_idx
                    if this_boundary is None:
                        this_boundary = cur_rel_prefix.count("/") + 1 if cur_rel_prefix else 0
                    yield from self._handle_archive(full, new_rel_prefix, new_origin_prefix, depth + 1,
                                                     archive_boundary_idx=this_boundary)
                    continue

                t = file_type(full)
                if t == "other":
                    # Files with no plausible photo/video relevance (.exe, .docx, .pdf, ...)
                    # are silently ignored: not copied, not disputed, not logged. Only
                    # image/raw/video/archive extensions enter the pipeline at all; borderline
                    # cases within those (icons, tiny images, broken files) are still routed to
                    # _disputed later via the is_media classification.
                    continue

                try:
                    st = os.stat(winlong(full))
                except OSError:
                    continue

                sibling_path = None
                if t in ("image", "raw"):
                    base_noext = os.path.splitext(name)[0].lower()
                    other_type = "raw" if t == "image" else "image"
                    sibling_path = sibling_by_base.get(base_noext, {}).get(other_type)

                yield SourceItem(full, disp, rel, st.st_size, st.st_mtime, t, sibling_path,
                                  zone=classify_zone(full), archive_no_crc=archive_no_crc,
                                  archive_boundary_idx=archive_boundary_idx)

            # LIFO стек -- пушим в ОБРАТНОМ sorted-порядке, чтобы pop() отдавал подпапки в том
            # же порядке (по возрастанию имени), в каком их раньше обходила рекурсия; порядок
            # обхода влияет на то, какой из дублей "выигрывает" имя при дедупе (см. RULES.md).
            for name in reversed(subdirs):
                full = os.path.join(cur_dirpath, name)
                # Security audit finding #3: a directory junction/symlink can point back at an
                # ancestor of itself (deliberately, as a booby trap on a hostile SOURCE, or by
                # accident) -- os.path.isdir()/os.listdir() both follow reparse points on
                # Windows, and neither is guarded anywhere else in this walk, so without this
                # check such a loop recurses forever (now: grows the stack forever) instead of
                # terminating. realpath() resolves the reparse point; if it matches anything
                # already open on this branch of the walk, it's a cycle -- skip it.
                full_real = os.path.normcase(os.path.realpath(full))
                if full_real in cur_ancestors:
                    self.log(f"  [symlink_loop] пропущена зацикленная папка (junction/symlink "
                             f"ведёт на себя или предка по дереву): {full}")
                    continue
                rel = f"{cur_rel_prefix}/{name}" if cur_rel_prefix else name
                stack.append((full, rel, False, cur_ancestors + (full_real,)))

    def _handle_archive(self, archive_path, rel_prefix, origin_prefix, depth, archive_boundary_idx=None):
        display_name = _strip_trailing_arrow(origin_prefix) if origin_prefix else os.path.basename(archive_path)
        full_display = f"{origin_prefix}" if origin_prefix else os.path.basename(archive_path)

        if depth > self.cfg.max_archive_depth:
            self._log_archive(_strip_trailing_arrow(full_display), "archive_bomb_suspected", "превышена глубина вложенности")
            return

        fmt = detect_archive_format(archive_path)
        info = list_archive(archive_path, fmt)

        try:
            compressed_size = os.path.getsize(winlong(archive_path))
        except OSError as e:
            # 2026-07-11 (live user report): the user deleted this archive file WHILE
            # PhotoArchive was still scanning it -- this generator method runs during the
            # enumeration phase (Phase 2), a completely different code path from
            # place_file()/resolve_dest_path()'s already-guarded copy phase (see
            # _log_write_failure()'s docstring for that earlier fix). This raw os.path.getsize()
            # was never wrapped, so a vanished-mid-scan file raised OSError straight out of the
            # generator, through _walk_dir()/walk(), all the way to main() -- which only catches
            # KeyboardInterrupt/EOFError -- crashing the entire run with a raw traceback instead
            # of skipping just this one archive and continuing with everything else.
            self._log_archive(_strip_trailing_arrow(full_display), "archive_read_error",
                               f"файл исчез или недоступен во время обработки: {e}")
            return

        if info.ok:
            if info.encrypted:
                self._log_archive(_strip_trailing_arrow(full_display), "archive_password_protected")
                return
            if info.path_traversal:
                self._log_archive(_strip_trailing_arrow(full_display), "archive_path_traversal_suspected",
                                   "член архива содержит '..' или абсолютный путь -- не распаковываю")
                return
            if info.total_size > 2 * 1024**3 and compressed_size > 0 and info.total_size > compressed_size * 100:
                self._log_archive(_strip_trailing_arrow(full_display), "archive_bomb_suspected",
                                   f"ratio={info.total_size / max(compressed_size,1):.0f}x")
                return
            if info.entries > MAX_ARCHIVE_ENTRIES:
                self._log_archive(_strip_trailing_arrow(full_display), "archive_bomb_suspected",
                                   f"entries={info.entries} (лимит {MAX_ARCHIVE_ENTRIES})")
                return
            required = info.total_size + int(self.cfg.free_space_margin_gb * 1024**3)
        else:
            # Листинг архива не читается -- реальный распакованный размер неизвестен, и
            # угадывать его коэффициентом (compressed_size*3) не даёт настоящей защиты: архив,
            # специально сконструированный ломать листинг, с тем же успехом может распаковаться
            # в тысячи раз больше заявленного и заполнить весь том прямо во время распаковки
            # (мимо этой предполётной проверки). Раз надёжной оценки места нет -- считаем
            # такой архив подозрительным и не распаковываем, а не гадаем с потолка.
            self._log_archive(_strip_trailing_arrow(full_display), "archive_bomb_suspected",
                               "листинг архива не читается, распакованный размер неизвестен")
            return

        free = free_space_bytes(self.cfg.tmp_extract if os.path.isdir(winlong(self.cfg.tmp_extract)) else self.cfg.target)
        if required > free:
            self._log_archive(_strip_trailing_arrow(full_display), "archive_skipped_no_space",
                               f"нужно ~{required/1024**3:.1f}ГБ, свободно {free/1024**3:.1f}ГБ")
            return

        # 2026-07-11 finding (live production run): a whole-disk scan runs into plenty of
        # installers/backups/configs zipped up with zero photos inside -- the listing already
        # parsed above (info.has_media_candidate) already names every member, so there is no
        # need to actually extract anything just to discover that afterwards. Same log status
        # ("archive_no_media") as the post-extraction empty-result case below, just reached
        # without ever touching tmp_extract for this archive.
        if not info.has_media_candidate:
            self._log_archive(_strip_trailing_arrow(full_display), "archive_no_media")
            return

        try:
            archive_hash = sha256_file(archive_path)
        except OSError as e:
            # Same race as the os.path.getsize() guard above, just later -- this reads the
            # WHOLE archive to hash it (real wall-clock time on a multi-GB file, exactly the
            # window the live user report happened in: "программа его продолжала распаковывать,
            # а потом срубилась"). Same fix, same reasoning.
            self._log_archive(_strip_trailing_arrow(full_display), "archive_read_error",
                               f"файл исчез или недоступен во время обработки: {e}")
            return
        extract_dir = os.path.join(self.cfg.tmp_extract, archive_hash)

        # Задача 4: распаковка может занять минуты на большом архиве без собственного
        # прогресса (7z/unrar/tarfile не отдают построчный процент сюда) -- явная строка
        # "текущее действие", чтобы легитимная пауза не читалась как зависание. Через self.log
        # (= log_line, если вызывающий передал его) -- не рвёт строку активного бара.
        size_gb = compressed_size / 1024**3
        self.log(f"  Распаковка {display_name} ({size_gb:.1f} ГБ)…")
        ok = extract_archive(archive_path, fmt, extract_dir, log=self.log)
        if not ok:
            self._log_archive(_strip_trailing_arrow(full_display), "archive_extract_failed")
            cleanup_dir(extract_dir)
            return

        if fmt not in TAR_MODES:
            # tar/tar.gz/tar.bz2 already refuses (at extraction time, via filter="data") any
            # symlink member whose target would resolve outside dest_dir -- an in-bounds tar
            # symlink is legitimate content, not a reason to reject the whole archive. zip/7z/
            # rar extraction has no such built-in check (see find_reparse_point_in_tree()
            # docstring), so any reparse point found there is treated as suspicious outright.
            reparse = find_reparse_point_in_tree(extract_dir)
            if reparse:
                self._log_archive(_strip_trailing_arrow(full_display), "archive_symlink_suspected",
                                   f"извлечённое дерево содержит symlink/junction ({reparse}) -- "
                                   f"содержимое архива не читаю")
                cleanup_dir(extract_dir)
                return

            # Finding 7: если часть членов архива ушла за пределы extract_dir (traversal,
            # который не поймал текстовый парсер листинга -- см. count_extracted_files()),
            # здесь физически найдётся МЕНЬШЕ файлов, чем заявлено в листинге архива.
            extracted_count = count_extracted_files(extract_dir)
            if extracted_count < info.entries:
                self._log_archive(_strip_trailing_arrow(full_display), "archive_path_traversal_suspected",
                                   f"после распаковки найдено {extracted_count} файлов, "
                                   f"в листинге архива было {info.entries} -- похоже, часть "
                                   f"содержимого вышла за пределы extract_dir")
                cleanup_dir(extract_dir)
                return

        media_count = 0
        extract_dir_real = os.path.normcase(os.path.realpath(extract_dir))
        try:
            for item in self._walk_dir(extract_dir, rel_prefix, origin_prefix, depth, is_root=True,
                                        ancestors=(extract_dir_real,), archive_no_crc=(fmt in TAR_MODES),
                                        archive_boundary_idx=archive_boundary_idx):
                if item.ftype in ("image", "raw", "video"):
                    media_count += 1
                yield item
        finally:
            cleanup_dir(extract_dir)

        if media_count == 0:
            self._log_archive(_strip_trailing_arrow(full_display), "archive_no_media")
        else:
            self._log_archive(_strip_trailing_arrow(full_display), "archive_extracted", f"{media_count} медиафайлов")

# ============================================================================
# PROCESS  (from pipeline/process.py)
# ============================================================================


class ReadError(Exception):
    """Raised when the source file itself could not be read (lock, permission, disk I/O
    hiccup) after retries -- distinct from a file that reads fine but is corrupt/
    unrecognisable media (which stays a classification concern, not a read concern)."""


@dataclass
class SourceRecord:
    item: SourceItem
    sha256: str = None
    phash: str = None            # image: single; video: "|"-joined 3 frame hashes
    width: int = None
    height: int = None
    aspect: float = None
    duration: float = None
    bitrate: int = None
    exif_dt = None                # datetime or None
    exif_dt_source: str = None
    camera: str = None
    gps_lat: float = None
    gps_lon: float = None
    is_media: bool = True
    media_note: str = None
    broken: bool = False          # ffprobe failed / unreadable image / 0 bytes
    is_hidden: bool = False
    read_error: bool = False
    read_error_msg: str = None


DOS_ATTR_HIDDEN_BIT = 0x2  # FILE_ATTRIBUTE_HIDDEN


def is_hidden_path(read_path: str) -> bool:
    """Hidden-file detection. On native Windows (the portable target platform), the DOS
    hidden attribute is available directly from the filesystem via os.stat() --
    st_file_attributes only exists on Windows builds of Python. Falls back to the Unix
    dotfile convention when st_file_attributes is unavailable (dev-testing this script
    directly on Linux, where there is no DOS attribute to read)."""
    try:
        st = os.stat(winlong(read_path))
        if hasattr(st, "st_file_attributes"):
            return bool(st.st_file_attributes & DOS_ATTR_HIDDEN_BIT)
    except OSError:
        pass
    name = os.path.basename(read_path)
    return name.startswith(".")


def sha256_file_with_retry(path: str, retries: int, delay: float) -> str:
    last_err = None
    for attempt in range(retries):
        try:
            return sha256_file(path)
        except OSError as e:
            last_err = e
            if attempt < retries - 1:
                time.sleep(delay)
    raise ReadError(str(last_err))


def analyze_batch(items: list, retries: int = 3, retry_delay: float = 5.0,
                   small_image_px: int = 640, log=print, skip_hash: bool = False) -> list:
    """Phase 3: compute hashes/metadata/classification for a batch of SourceItem.
    Returns list of SourceRecord in the same order as items.
    A record with read_error=True means the file could not be read at all (locked /
    permission / disk I/O hiccup) after `retries` attempts -- the caller decides whether to
    defer it for an end-of-run retry or log it straight to unreadable.csv.

    skip_hash=True (используется ТОЛЬКО режимом analyze-quick, см. run_analyze()): не
    считать sha256 (chunked-чтение всего файла) и не считать pHash (imagehash.phash --
    решающая доля затрат для изображений; video_phash_3frames -- три отдельных ffmpeg-вызова
    на файл для видео). Экономит именно самое дорогое в конвейере, оставляя дешёвые вещи
    (exiftool-даты, размеры кадра, базовая проверка "открывается ли файл") включёнными --
    этого достаточно для быстрой метаданными-диагностики источника. Не даёт read_error-
    ретраев (те опираются на попытку sha256_file_with_retry) -- любой файл, который не
    открылся, в quick-режиме сразу считается broken, без отложенного повтора в конце
    прогона; это осознанное упрощение read-only диагностического режима, не влияющее на
    поведение обычной сборки (skip_hash по умолчанию False, здесь ничего не меняется).
    """
    image_video_paths = [it.read_path for it in items if it.ftype in ("image", "raw", "video")]
    tags_by_path = exiftool_batch(image_video_paths) if image_video_paths else {}

    records = []
    for it in items:
        rec = SourceRecord(item=it)
        rec.is_hidden = is_hidden_path(it.read_path)

        if it.size == 0:
            rec.broken = True
            rec.is_media = False
            rec.media_note = "empty_file"
            records.append(rec)
            continue

        if not skip_hash:
            try:
                rec.sha256 = sha256_file_with_retry(it.read_path, retries, retry_delay)
            except ReadError as e:
                rec.read_error = True
                rec.read_error_msg = str(e)
                records.append(rec)
                continue

        tags = tags_by_path.get(it.read_path, {})

        if tags:
            dt, src = best_exif_datetime(tags)
            rec.exif_dt, rec.exif_dt_source = dt, src
            rec.camera = camera_from_tags(tags)
            rec.gps_lat, rec.gps_lon = gps_from_tags(tags)

        if it.ftype == "raw":
            # RAW formats (CR2/NEF/ARW/DNG) aren't decodable by Pillow; dedup for RAW is
            # SHA-256-only (see dedup.py), so no phash/aspect is needed. Always camera output.
            try:
                w = tags.get("ImageWidth")
                h = tags.get("ImageHeight")
                rec.width, rec.height = w, h
            except Exception:
                pass
            rec.is_media = True

        elif it.ftype == "image":
            if skip_hash:
                w, h = image_size_only(it.read_path)
                ph = "-" if w is not None else None  # заглушка: не None -> "не broken"
            else:
                ph, w, h = image_phash_and_size(it.read_path)
            if ph is None:
                rec.broken = True
                rec.is_media = False
                rec.media_note = "unreadable_image"
                records.append(rec)
                continue
            rec.phash, rec.width, rec.height = (None if skip_hash else ph), w, h
            rec.aspect = (w / h) if h else None
            is_media, note = classify_image(it.read_path, w, h, rec.camera, it.size,
                                                       small_image_px)
            rec.is_media, rec.media_note = is_media, note

        elif it.ftype == "video":
            duration, w, h, bitrate = video_duration_and_resolution(it.read_path)
            if duration is None and w is None:
                rec.broken = True
                rec.is_media = False
                rec.media_note = "unreadable_video"
                records.append(rec)
                continue
            rec.duration, rec.width, rec.height, rec.bitrate = duration, w, h, bitrate
            if not skip_hash:
                frames = video_phash_3frames(it.read_path, duration or 1.0)
                rec.phash = "|".join(frames) if frames else None
            rec.is_media = True

        records.append(rec)

    return records

# ============================================================================
# DEDUP  (from pipeline/dedup.py)
# ============================================================================


@dataclass
class PoolEntry:
    sha256: str
    ftype: str                 # image / video / raw
    dest_path: str
    size: int
    aspect: float = None
    width: int = None
    height: int = None
    phash: str = None          # image: single hex phash; video: "|"-joined 3 frame hashes
    duration: float = None
    has_camera: bool = False
    bitrate: int = None


def _aspect_bucket(aspect: float) -> int:
    return round(aspect * 50)  # ~2% grid


class Pool:
    def __init__(self):
        self.by_sha = {}
        self.by_aspect_bucket = defaultdict(list)
        self.by_duration_bucket = defaultdict(list)

    def add(self, entry: PoolEntry):
        self.by_sha[entry.sha256] = entry
        if entry.ftype in ("image",) and entry.aspect and entry.phash:
            self.by_aspect_bucket[_aspect_bucket(entry.aspect)].append(entry)
        elif entry.ftype == "video" and entry.duration is not None:
            self.by_duration_bucket[int(entry.duration)].append(entry)

    def find_exact(self, sha256: str):
        return self.by_sha.get(sha256)

    def find_near_dup_image(self, aspect: float, phash: str, threshold=6):
        """Returns (entry, aspect_matches, hamming_distance) for the best-quality near-dup
        within threshold, or (None, None, None). Among all cluster matches, the entry is
        chosen by _quality_key (same criterion as image_is_strictly_better) rather than the
        nearest by Hamming distance -- otherwise appended_better/appended_near_dup could be
        decided against the wrong cluster member. The distance of the chosen entry is still
        surfaced (p.5.7) so the caller can log how close the match was, now that near-dups
        are appended, not skipped."""
        bucket = _aspect_bucket(aspect)
        candidates = []
        for b in (bucket - 1, bucket, bucket + 1):
            for entry in self.by_aspect_bucket.get(b, []):
                d = hamming(entry.phash, phash)
                if d <= threshold:
                    rel_diff = abs(entry.aspect - aspect) / max(entry.aspect, 1e-6)
                    if rel_diff <= 0.02:
                        candidates.append((entry, d))
        if candidates:
            best, best_dist = max(candidates, key=lambda pair: _quality_key(pair[0]))
            return best, True, best_dist

        # Fallback: a crop can have a different aspect (so it never lands in the buckets
        # above) but a similar phash. Scan every image entry in the pool -- by_aspect_bucket
        # already holds all of them, no separate phash-prefix index needed.
        candidates = []
        for entry in itertools.chain(*self.by_aspect_bucket.values()):
            d = hamming(entry.phash, phash)
            if d <= threshold:
                candidates.append((entry, d))
        if candidates:
            best, best_dist = max(candidates, key=lambda pair: _quality_key(pair[0]))
            return best, False, best_dist
        return None, None, None

    def find_near_dup_video(self, duration: float, frame_hashes, threshold=6, max_delta=1.0):
        """Returns (entry, hamming_distance) for the closest near-dup, or (None, None) --
        distance is the worst (max) per-frame Hamming distance among the matched frames,
        i.e. the one closest to the threshold (see p.5.7 note on find_near_dup_image)."""
        if not frame_hashes:
            return None, None
        buckets = set()
        base = int(duration) if duration else 0
        for delta in (-1, 0, 1):
            buckets.add(base + delta)
        for b in buckets:
            for entry in self.by_duration_bucket.get(b, []):
                if entry.duration is None or abs(entry.duration - duration) > max_delta:
                    continue
                entry_hashes = entry.phash.split("|") if entry.phash else []
                if video_hashes_match(entry_hashes, frame_hashes, threshold):
                    n = min(len(entry_hashes), len(frame_hashes))
                    max_dist = max((hamming(entry_hashes[i], frame_hashes[i]) for i in range(n)), default=0)
                    return entry, max_dist
        return None, None


def _quality_key(entry: PoolEntry):
    """Ordering used to pick the best of several near-dup candidates: pixel area, then file
    size, then EXIF camera presence -- same criterion as image_is_strictly_better below."""
    return ((entry.width or 0) * (entry.height or 0), entry.size, entry.has_camera)


def image_is_strictly_better(candidate: PoolEntry, existing: PoolEntry) -> bool:
    return _quality_key(candidate) > _quality_key(existing)


def video_is_strictly_better(candidate: PoolEntry, existing: PoolEntry) -> bool:
    cand_px = (candidate.width or 0) * (candidate.height or 0)
    exist_px = (existing.width or 0) * (existing.height or 0)
    if cand_px != exist_px:
        return cand_px > exist_px
    cand_br = candidate.bitrate or 0
    exist_br = existing.bitrate or 0
    if cand_br != exist_br:
        return cand_br > exist_br
    if candidate.size != existing.size:
        return candidate.size > existing.size
    return False


def _pct_vs(candidate_value, existing_value) -> str:
    """p.5.3б: relative diff for a [DEBUG] near_dup criterion string, e.g. 'area+12%'.
    existing_value==0 has no meaningful percentage -- report the raw delta instead."""
    if not existing_value:
        return f"{candidate_value - existing_value:+d}"
    return f"{(candidate_value - existing_value) / existing_value * 100:+.0f}%"


def _image_compare_debug(candidate: PoolEntry, existing: PoolEntry) -> str:
    cand_px = (candidate.width or 0) * (candidate.height or 0)
    exist_px = (existing.width or 0) * (existing.height or 0)
    return (f"image_is_strictly_better(area{_pct_vs(cand_px, exist_px)}, "
            f"size{_pct_vs(candidate.size, existing.size)}, "
            f"exif_camera={str(bool(candidate.has_camera)).lower()})")


def _video_compare_debug(candidate: PoolEntry, existing: PoolEntry) -> str:
    cand_px = (candidate.width or 0) * (candidate.height or 0)
    exist_px = (existing.width or 0) * (existing.height or 0)
    return (f"video_is_strictly_better(area{_pct_vs(cand_px, exist_px)}, "
            f"bitrate{_pct_vs(candidate.bitrate or 0, existing.bitrate or 0)}, "
            f"size{_pct_vs(candidate.size, existing.size)})")

# ============================================================================
# DECIDE  (from pipeline/decide.py)
# ============================================================================


@dataclass
class Decision:
    decision: str
    matched_dest: str = None
    note: str = None
    debug_detail: str = None  # p.5.3б: criterion string for [DEBUG] near_dup lines -- pure
                               # data (decide() stays a pure function, no logging inside it)


def decide(pool: Pool, rec: SourceRecord, mirror_raw: bool = True) -> Decision:
    if rec.broken or not rec.is_media:
        return Decision("disputed", note=rec.media_note or "not_media")

    if rec.item.ftype == "raw":
        existing = pool.find_exact(rec.sha256)
        if existing:
            return Decision("skipped_present", matched_dest=existing.dest_path, note="already_present")
        has_jpeg = bool(rec.item.sibling_path)
        # MIRROR_RAW управляет только избыточным RAW (есть парный JPEG). Одинокий RAW --
        # единственный носитель кадра -- мирроится ВСЕГДА независимо от флага (см. RULES.md).
        if has_jpeg and not mirror_raw:
            return Decision("raw_skipped", note="raw_skipped_has_jpeg")
        if not has_jpeg and not mirror_raw:
            return Decision("raw_mirrored", note="raw_lone_mirrored")
        return Decision("raw_mirrored", note="raw_with_jpeg" if has_jpeg else "raw_without_jpeg")

    if rec.item.ftype == "image":
        existing = pool.find_exact(rec.sha256)
        if existing:
            return Decision("skipped_present", matched_dest=existing.dest_path, note="already_present")

        if not rec.phash or not rec.aspect:
            return Decision("appended_uncertain", note="no_phash_available")

        entry, aspect_ok, dist = pool.find_near_dup_image(rec.aspect, rec.phash)
        if entry is None:
            return Decision("appended_new")

        if aspect_ok:
            candidate = PoolEntry(
                sha256=rec.sha256, ftype="image", dest_path=None, size=rec.item.size,
                aspect=rec.aspect, width=rec.width, height=rec.height, phash=rec.phash,
                has_camera=bool(rec.camera),
            )
            detail = _image_compare_debug(candidate, entry)
            if image_is_strictly_better(candidate, entry):
                return Decision("appended_better", matched_dest=entry.dest_path, note="better_quality_appended",
                                 debug_detail=detail)
            # p.5.7: near-dup no longer excludes the file from the archive -- a burst-shot
            # sequence can have one frame that matters (a bird mid-flight) that perceptual
            # hashing can't tell apart from its neighbors on technical metrics alone. Append
            # both, log which existing file it's close to and by how much, and let a human
            # clean up duplicates later if they want to (source: user).
            return Decision("appended_near_dup", matched_dest=entry.dest_path,
                             note=f"near_dup_of={os.path.basename(entry.dest_path)}_hamming={dist}",
                             debug_detail=detail)

        return Decision("appended_crop", matched_dest=entry.dest_path, note="kept_both_possible_crop")

    if rec.item.ftype == "video":
        existing = pool.find_exact(rec.sha256)
        if existing:
            return Decision("skipped_present", matched_dest=existing.dest_path, note="already_present")

        if not rec.phash:
            return Decision("appended_uncertain", note="no_phash_available")

        frame_hashes = rec.phash.split("|")
        entry, dist = pool.find_near_dup_video(rec.duration or 0.0, frame_hashes)
        if entry is None:
            return Decision("appended_new")

        candidate = PoolEntry(
            sha256=rec.sha256, ftype="video", dest_path=None, size=rec.item.size,
            width=rec.width, height=rec.height, duration=rec.duration, bitrate=rec.bitrate,
        )
        detail = _video_compare_debug(candidate, entry)
        if video_is_strictly_better(candidate, entry):
            return Decision("appended_better", matched_dest=entry.dest_path, note="better_quality_appended",
                             debug_detail=detail)
        # p.5.7: same reasoning as the image branch above -- append instead of skip.
        return Decision("appended_near_dup", matched_dest=entry.dest_path,
                         note=f"near_dup_of={os.path.basename(entry.dest_path)}_hamming={dist}",
                         debug_detail=detail)

    return Decision("appended_uncertain", note="unknown_type")

# ============================================================================
# DATES  (from pipeline/dates.py)
# ============================================================================


_MIN_YEAR = 1900  # семейный архив может включать оцифрованные плёночные фото старше 1990


def _valid(y, mo=1, d=1):
    if y < _MIN_YEAR or y > datetime.now().year:
        return False
    try:
        datetime(y, mo, d)
        return True
    except ValueError:
        return False


# strict filename date patterns (counters like IMG_1234 are intentionally NOT matched)
_FNAME_PATTERNS = [
    (re.compile(r"(?<!\d)(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})(\d{2})(?!\d)"),
     lambda m: (int(m[1]), int(m[2]), int(m[3]))),
    (re.compile(r"IMG[_-](\d{4})(\d{2})(\d{2})(?!\d)"),
     lambda m: (int(m[1]), int(m[2]), int(m[3]))),
    (re.compile(r"IMG-(\d{4})(\d{2})(\d{2})-WA\d+"),
     lambda m: (int(m[1]), int(m[2]), int(m[3]))),
    (re.compile(r"Screenshot_(\d{4})-(\d{2})-(\d{2})"),
     lambda m: (int(m[1]), int(m[2]), int(m[3]))),
    (re.compile(r"PXL[_-](\d{4})(\d{2})(\d{2})(?!\d)"),
     lambda m: (int(m[1]), int(m[2]), int(m[3]))),
    (re.compile(r"VID[_-](\d{4})(\d{2})(\d{2})(?!\d)"),
     lambda m: (int(m[1]), int(m[2]), int(m[3]))),
    (re.compile(r"(?<!\d)(\d{4})-(\d{2})-(\d{2})(?!\d)"),
     lambda m: (int(m[1]), int(m[2]), int(m[3]))),
    (re.compile(r"(?<!\d)(\d{4})_(\d{2})(?!\d)"),
     lambda m: (int(m[1]), int(m[2]), 1)),
]

_FOLDER_YEAR_RE = re.compile(r"(?<!\d)(19\d{2}|20\d{2})(?!\d)")  # 1900..2099; _valid() сузит верх/низ


def date_from_filename(name: str):
    base = os.path.splitext(name)[0]
    for rx, extract in _FNAME_PATTERNS:
        m = rx.search(base)
        if m:
            try:
                y, mo, d = extract(m)
                if _valid(y, mo, d):
                    return datetime(y, mo, d), "filename_pattern"
            except ValueError:
                continue
    return None, None


def date_from_folder_name(rel_path: str):
    """Year found in an ancestor folder segment of rel_path; year must be valid."""
    parts = rel_path.split("/")[:-1]
    for part in reversed(parts):
        m = _FOLDER_YEAR_RE.search(part)
        if m:
            y = int(m.group(1))
            if _valid(y):
                return datetime(y, 1, 1), "folder_name_year"
    return None, None


def mtime_is_copy_artifact(mtimes: list, window_seconds=5) -> bool:
    """Many files in a folder sharing an identical/narrow mtime window => copy event, not a
    reliable date signal."""
    if len(mtimes) < 3:
        return False
    mtimes = sorted(mtimes)
    span = mtimes[-1] - mtimes[0]
    return span <= window_seconds


def folder_cluster_median(tier_ab_dates: list):
    if not tier_ab_dates:
        return None
    ts = sorted(d.timestamp() for d in tier_ab_dates)
    med = statistics.median(ts)
    return datetime.fromtimestamp(med)


class DateContext:
    """Accumulates per-directory evidence across the run so later files in the
    same folder can borrow tier A/B dates from earlier siblings (folder-cluster
    inference) and so copy-artifact mtimes can be recognised."""

    def __init__(self):
        self.dir_tier_ab_dates = defaultdict(list)
        self.dir_mtimes = defaultdict(list)

    def record(self, dirname, dt, tier, mtime):
        if tier in ("A", "B"):
            self.dir_tier_ab_dates[dirname].append(dt)
        self.dir_mtimes[dirname].append(mtime)


def resolve_date(ctx: DateContext, rel_path: str, mtime: float, exif_dt=None, exif_source=None):
    """Phase 4.5: returns (date_value, tier, confidence, evidence, precision).
    precision is 'day' (full date known) or 'year' (only the year is reliable,
    e.g. a bare year found in a folder name) -> routes to the month-unknown bucket.
    """
    dirname = os.path.dirname(rel_path)
    name = os.path.basename(rel_path)

    if exif_dt:
        ctx.record(dirname, exif_dt, "A", mtime)
        return exif_dt, "A", "high", exif_source, "day"

    dt, ev = date_from_filename(name)
    if dt:
        ctx.record(dirname, dt, "B", mtime)
        return dt, "B", "medium", ev, "day"

    dt, ev = date_from_folder_name(rel_path)
    if dt:
        ctx.record(dirname, dt, "B", mtime)
        return dt, "B", "medium", ev, "year"

    neighbors = ctx.dir_tier_ab_dates.get(dirname, [])
    if neighbors:
        med = folder_cluster_median(neighbors)
        ctx.record(dirname, med, "C", mtime)
        return med, "C", "low", "inferred_from_folder_cluster", "day"

    sibling_mtimes = ctx.dir_mtimes.get(dirname, [])
    if not mtime_is_copy_artifact(sibling_mtimes + [mtime]):
        dt = datetime.fromtimestamp(mtime)
        ctx.record(dirname, dt, "C", mtime)
        return dt, "C", "low", "mtime", "day"

    ctx.record(dirname, None, "D", mtime)
    return None, "D", "none", "no_signal", None

# ============================================================================
# PLACEMENT  (from pipeline/placement.py)
# ============================================================================


def _has_letters(s: str) -> bool:
    return bool(re.search(r"[A-Za-zА-Яа-яЁё]", s))


_PROFILE_ROOT_NAMES = {"users", "home"}


def find_album(rel_path: str, archive_boundary_idx: int = None, *,
                dump_names=None, dump_prefixes=None):
    """АЛЬБОМ = самый верхний (ближайший к корню SOURCE конкретного прогона) не-dump сегмент
    пути с буквами; всё глубже сохраняется как есть (см. subpath ниже).
    Returns (album_name, subpath_segments) or (None, None).
    subpath_segments = non-dump folder segments deeper than the album (dump ones collapsed).

    dump_names/dump_prefixes: forwarded as-is to every is_dump_segment() call below (see that
    function's docstring) -- production call sites pass cfg.dump_segment_names_lower/
    cfg.dump_segment_prefixes_tuple, bare calls (tests) fall back to module defaults.

    A segment that is itself a Windows/Unix profile username (i.e. sits directly under a
    "Users"/"Home" root, e.g. Users/HTPC/...) is treated as dump too: a personal profile
    folder name is not a meaningful album, so photos loose inside it (Pictures/Downloads/...)
    fall through to date-based ByDate placement instead of being lumped under the username.

    archive_boundary_idx (2026-07-11, real-archive finding): the index of the OUTERMOST
    archive's own filename segment, for an item that came from inside a zip/rar/7z/tar (None
    for a plain on-disk file -- see SourceItem.archive_boundary_idx). A folder name INSIDE an
    archive is never trusted on its own to name an album -- an archive is somebody's
    deliberately assembled bundle, but its internal folder names can just as easily be
    generic/auto-generated (a real case: a Yandex.Disk export's every zip unpacks into a
    folder plainly called "archive", which would otherwise silently merge unrelated exports
    into one pile). So the search for a real album is restricted to the DISK-SIDE portion of
    the path (strictly before archive_boundary_idx) first -- if the file sits inside an
    already-meaningful album folder on disk (e.g. "Свадьба\\photos.zip"), that still wins and
    archive-internal folders remain available as subpath underneath it, same as before. Only
    when the disk side has no real album does the archive's OWN name become the album --
    subject to the SAME is_dump_segment()/_has_letters() check as any other candidate (an
    archive literally named "20200701.zip" still isn't a meaningful album name and still
    falls through to ByDate, exactly as before archives got this special handling at all).
    When a real album WAS found outside the archive, the archive's own name segment is not
    re-used as a redundant subpath level either -- only what's genuinely inside the archive
    survives as subpath.

    Subpath segments use is_dump_segment(for_subpath=True) (2026-07-11), not the plain call
    used for finding the album itself: a bare 6-8 digit folder (e.g. a camera/phone export
    folder "20240802" carried over unrenamed) is dump when deciding what NAMES the album
    (nobody wants an album literally called "20240802"), but once a real album already exists
    further up the path, that same folder plausibly represents a deliberate day-grouping and
    is kept as a subpath segment instead of collapsing away.
    """
    segments = rel_path.split("/")[:-1]
    disk_side_limit = archive_boundary_idx if archive_boundary_idx is not None else len(segments)
    album_idx = None
    for i in range(0, disk_side_limit):
        seg = segments[i]
        is_profile_username = i > 0 and segments[i - 1].strip().lower() in _PROFILE_ROOT_NAMES
        if (not is_dump_segment(seg, dump_names=dump_names, dump_prefixes=dump_prefixes)
                and not is_profile_username and _has_letters(seg)):
            album_idx = i
            break
    if album_idx is None and archive_boundary_idx is not None:
        candidate = segments[archive_boundary_idx]
        if (not is_dump_segment(candidate, dump_names=dump_names, dump_prefixes=dump_prefixes)
                and _has_letters(candidate)):
            album_idx = archive_boundary_idx
    if album_idx is None:
        return None, None, None
    subpath = [
        s for i, s in enumerate(segments[album_idx + 1:], start=album_idx + 1)
        # The archive's own filename segment never becomes a subpath level on its own --
        # either it WAS just chosen as the album (i == archive_boundary_idx == album_idx,
        # already excluded by starting the slice at album_idx + 1), or a real album was found
        # OUTSIDE the archive, in which case re-showing the archive's filename one level
        # deeper would just be redundant noise (see docstring).
        if i != archive_boundary_idx and not is_dump_segment(
            s, for_subpath=True, dump_names=dump_names, dump_prefixes=dump_prefixes)
    ]
    # album_prefix (2026-07-11, по запросу пользователя): путь ОТ КОРНЯ SOURCE ДО И ВКЛЮЧАЯ
    # сам сегмент-альбом -- используется только для __ВНИМАНИЕ_объединённая_папка.txt
    # (см. _note_album_source()), чтобы понимать, из какого физического места на диске
    # реально пришло содержимое альбома. Единообразно работает и для альбома-из-архива
    # (album_idx == archive_boundary_idx -- сегмент это имя самого архивного файла).
    album_prefix = "/".join(segments[:album_idx + 1])
    return segments[album_idx], subpath, album_prefix


# 2026-07-11, по запросу пользователя: см. _note_album_source() -- прозрачность для случая,
# когда альбом фактически собирается из НЕСКОЛЬКИХ разных физических мест в дереве источника
# (дедуп по содержимому глобален и не привязан к альбому, см. RULES.md/обсуждение сессии).
MERGED_ALBUM_MARKER_FILENAME = "__ВНИМАНИЕ_объединённая_папка.txt"


def _read_known_album_sources(album_dir: str) -> set:
    """Разбирает уже существующий MERGED_ALBUM_MARKER_FILENAME (если есть) -- достаёт только
    сами пути-источники (после "— "), без временных меток, чтобы не дублировать запись про
    уже известный источник при повторном прогоне. Читается ВСЕГДА, даже при dry_run (само
    чтение безвредно, не нарушает "пробный прогон ничего не пишет") -- см. _note_album_source()."""
    known = set()
    try:
        with open(winlong(os.path.join(album_dir, MERGED_ALBUM_MARKER_FILENAME)),
                  "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and " — " in line:
                    known.add(line.split(" — ", 1)[1])
    except OSError:
        pass
    return known


def _note_album_source(cfg, st, stats, album, album_prefix, album_dir):
    """2026-07-11, по запросу пользователя: если альбом реально пополняется содержимым из
    ДРУГОГО физического места в дереве источника (album_prefix отличается от уже известных
    для этого альбома -- будь то дописанный уникальный файл ИЛИ обнаруженный дубль, см. оба
    места вызова), дописывает строку в MERGED_ALBUM_MARKER_FILENAME в корне альбома -- чтобы
    пользователь мог потом понять, из каких физических мест реально собран этот альбом, и
    решить, как его в итоге назвать/разобрать. Файл появляется ТОЛЬКО когда объединение
    реально произошло -- самый первый увиденный источник альбома просто запоминается как
    базовый, без записи (иначе файл появлялся бы в каждом альбоме без исключения, без пользы).
    Обнаружение считается ВСЕГДА (попадает в stats["album_merge_events"] для человекочитаемой
    сводки, в том числе пробного прогона), но сама запись в файл -- НЕ при dry_run, как и
    обычное копирование файлов (см. RULES.md, "пробный прогон ничего не пишет")."""
    if album_prefix is None:
        return
    known = st.album_known_sources.get(album)
    if known is None:
        known = _read_known_album_sources(album_dir)
        st.album_known_sources[album] = known
    if album_prefix in known:
        return
    is_first_ever = len(known) == 0
    known.add(album_prefix)
    if is_first_ever:
        return
    stats.setdefault("album_merge_events", []).append((album, album_prefix))
    if cfg.dry_run:
        return
    marker_path = os.path.join(album_dir, MERGED_ALBUM_MARKER_FILENAME)
    need_separator = (
        album not in st.album_marker_separator_done
        and os.path.exists(winlong(marker_path))
    )
    _makedirs_iterative(winlong(album_dir))
    with open(winlong(marker_path), "a", encoding="utf-8") as f:
        if need_separator:
            f.write("\n")
        f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} — {album_prefix}\n")
    st.album_marker_separator_done.add(album)


def place_for_gps(lat, lon, home_country="RU"):
    if lat is None or lon is None:
        return None
    try:
        import reverse_geocoder as rg
        # 2026-07-11 live-run finding: verbose=True (its default) prints "Loading formatted
        # geocoded file..." etc. straight to stdout on its one-time lazy-load, with no
        # coordination with our own tqdm progress bar (writes to stderr) -- the two interleave
        # mid-line on a real console, producing garbled output like "обработка источника: :
        # 7файл [00:02, 3.62файл/s]Loading formatted geocoded file...". Silencing it here is a
        # supported library parameter, not a workaround.
        result = rg.search([(lat, lon)], verbose=False)
        if not result:
            return None
        r = result[0]
        city = r.get("name")
        cc = r.get("cc")
        if not city:
            return None
        if cc == home_country:
            return city
        return f"{city}, {cc}"
    except Exception:
        return None


_FS_MAX_COMPONENT_BYTES = 255  # hard filesystem limit per path component (ext4 etc.)

_WINDOWS_INVALID_CHARS_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WINDOWS_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)), "COM¹", "COM²", "COM³",
    *(f"LPT{i}" for i in range(1, 10)), "LPT¹", "LPT²", "LPT³",
}


def sanitize_windows_component(name: str) -> str:
    """Делает один сегмент пути (имя файла или папки) легальным на Windows/NTFS, независимо
    от того, откуда он взялся -- имя альбома из папки источника, город из reverse_geocoder,
    или файл, распакованный из архива (zip/tar могли быть собраны не на Windows и содержать
    символы вроде ':' или '?', которые NTFS не примет). На native Windows нет отдельного
    ro-контура, который раньше физически не давал таким именам попасть в дерево TARGET --
    единственная защита теперь в том, что мы сами никогда не формируем недопустимый путь.
    Не трогает разделители пути -- вызывается только на уже разбитых сегментах, никогда на
    целом пути."""
    if not name:
        return "_"
    cleaned = _WINDOWS_INVALID_CHARS_RE.sub("_", name)
    cleaned = cleaned.rstrip(" .")  # Windows отбрасывает хвостовые точки/пробелы у компонента
    if not cleaned:
        cleaned = "_"
    base, ext = os.path.splitext(cleaned)
    if base.upper() in _WINDOWS_RESERVED_NAMES:  # "CON.jpg" тоже зарезервировано, не только "CON"
        cleaned = base + "_" + ext
    return cleaned


def truncate_segment(name: str, max_len: int) -> str:
    """Truncate a single path segment to at most max_len characters AND, more importantly,
    at most 255 UTF-8 bytes -- multi-byte scripts (Cyrillic etc.) hit the filesystem's
    per-component byte limit long before the character-count budget, which caused real
    ENAMETOOLONG failures on long Russian filenames. Also sanitizes Windows-illegal
    characters/reserved device names first (sanitize, then truncate: replacing an invalid
    char with '_' never changes byte length, so the budget math below stays correct)."""
    name = sanitize_windows_component(name)
    byte_budget = min(max_len, _FS_MAX_COMPONENT_BYTES)
    if len(name) <= max_len and len(name.encode("utf-8")) <= byte_budget:
        return name
    root, ext = os.path.splitext(name)
    ext_bytes = ext.encode("utf-8", "ignore")
    root_byte_budget = max(1, byte_budget - len(ext_bytes))
    root_bytes = root.encode("utf-8", "ignore")[:root_byte_budget]
    while root_bytes and (root_bytes[-1] & 0xC0) == 0x80:  # don't split a multi-byte char
        root_bytes = root_bytes[:-1]
    return root_bytes.decode("utf-8", "ignore") + ext


def safe_mirror_dir(root: str, rel_dir: str, max_segment_len: int = 100) -> str:
    """Build a directory path mirroring a (possibly very long / deeply nested) source
    tree, truncating each segment so the result stays filesystem-safe (avoids
    ENAMETOOLONG on real-world Windows paths with long Cyrillic folder names)."""
    if not rel_dir or rel_dir in (".", ""):
        return root
    parts = [truncate_segment(p, max_segment_len) for p in rel_dir.split("/") if p]
    return os.path.join(root, *parts) if parts else root


def build_album_dest_dir(albums_root: str, album: str, subpath: list) -> str:
    parts = [albums_root] + [sanitize_windows_component(p) for p in [album] + list(subpath)]
    return os.path.join(*parts)


def build_bydate_dest_dir(bydate_root: str, date_value, precision: str, place: str,
                           granularity: str = "day") -> str:
    """granularity: day (по умолчанию, текущее поведение) | month | year | flat.
    precision=='year' (сама дата известна только с точностью до года) всегда даёт
    month-unknown-корзину независимо от granularity -- сузить её до month/day нечем.
    place (город из reverse_geocoder) санитизируется перед склейкой в имя папки -- он не
    сегмент пути сам по себе, а часть строки, поэтому чистить нужно ДО f-string, а не
    после (санитайзер не трогает пробелы, только Windows-запрещённые символы)."""
    place = sanitize_windows_component(place) if place else place
    if granularity == "flat":
        return bydate_root
    year = date_value.year
    if granularity == "year":
        return os.path.join(bydate_root, str(year))
    if precision == "year":
        return os.path.join(bydate_root, str(year), f"{year}-00 month-unknown{DUMP_TAG}")
    if granularity == "month":
        month_folder = date_value.strftime("%Y-%m")
        if place:
            month_folder = f"{month_folder} {place}"
        return os.path.join(bydate_root, str(year), f"{month_folder}{DUMP_TAG}")
    day_folder = date_value.strftime("%Y-%m-%d")
    if place:
        day_folder = f"{day_folder} {place}"
    return os.path.join(bydate_root, str(year), f"{day_folder}{DUMP_TAG}")


def build_mirror_dest_dir(root: str, rel_dir: str) -> str:
    if not rel_dir:
        return root
    parts = [sanitize_windows_component(p) for p in rel_dir.split("/") if p]
    return os.path.join(root, *parts) if parts else root


def raw_dest_dir(item: "SourceItem", rec: "SourceRecord", cfg: "Config",
                  dest_path_by_read_path: dict, date_ctx: "DateContext") -> str:
    """Папка назначения для RAW-кандидата (те, что реально мирроятся -- см. decide()),
    в зависимости от RAW_LAYOUT (photoarchive_config.yaml, см. RULES.md):

    mirror (по умолчанию) -- отдельный корень {TARGET}\\RAW\\, зеркалящий структуру
    основного архива (RAW\\Albums\\..., RAW\\ByDate\\YYYY\\...). Основной архив остаётся
    чистой галереей, все RAW сносятся одной отдельной папкой.

    sibling -- RAW кладётся в подпапку RAW\\ РЯДОМ с тем местом, куда лёг (или лёг бы) его
    JPEG-партнёр: Albums\\Море 2015\\RAW\\IMG.CR2, ByDate\\2019\\2019-07-15 Москва
    [PhotoArchive]\\RAW\\IMG.CR2. Удобно для сценария "фотограф хранит RAW при кадре";
    удаление альбома удаляет и его RAW заодно. Одинокий RAW (нет парного JPEG) кладётся в
    RAW-подпапку той папки,
    куда лёг бы его JPEG по обычной логике размещения (альбом/дата) -- см. RULES.md,
    правило "одинокий RAW спасается всегда" не зависит от RAW_LAYOUT.

    В обоих случаях RAW и JPEG никогда не оказываются в одной папке (гарантируется самим
    построением путей), и оба варианта одинаково участвуют в дедупе через общий pool.
    """
    sibling_dest = dest_path_by_read_path.get(item.sibling_path) if item.sibling_path else None

    if cfg.raw_layout == "sibling":
        if sibling_dest:
            return os.path.join(os.path.dirname(sibling_dest), "RAW")
        album, subpath, _ = find_album(item.rel_path, item.archive_boundary_idx,
                                        dump_names=cfg.dump_segment_names_lower,
                                        dump_prefixes=cfg.dump_segment_prefixes_tuple)
        if album:
            return os.path.join(build_album_dest_dir(cfg.albums_root, album, subpath), "RAW")
        date_value, tier, conf, evidence, precision = resolve_date(
            date_ctx, item.rel_path, item.mtime, rec.exif_dt, rec.exif_dt_source)
        if date_value is None:
            return os.path.join(safe_mirror_dir(cfg.undated_root, os.path.dirname(item.rel_path)), "RAW")
        return os.path.join(
            build_bydate_dest_dir(cfg.bydate_root, date_value, precision, None, cfg.bydate_granularity),
            "RAW",
        )

    # mirror (по умолчанию) -- прежняя логика, без изменений
    if sibling_dest:
        for src_root, dst_root in ((cfg.albums_root, os.path.join(cfg.raw_root, "Albums")),
                                    (cfg.bydate_root, os.path.join(cfg.raw_root, "ByDate"))):
            if sibling_dest.startswith(src_root + os.sep):
                rel_dir = os.path.dirname(os.path.relpath(sibling_dest, src_root))
                return os.path.join(dst_root, rel_dir) if rel_dir != "." else dst_root
        return os.path.join(cfg.raw_root, "ByDate", "_misc")
    album, subpath, _ = find_album(item.rel_path, item.archive_boundary_idx,
                                    dump_names=cfg.dump_segment_names_lower,
                                    dump_prefixes=cfg.dump_segment_prefixes_tuple)
    if album:
        return build_album_dest_dir(os.path.join(cfg.raw_root, "Albums"), album, subpath)
    date_value, tier, conf, evidence, precision = resolve_date(
        date_ctx, item.rel_path, item.mtime, rec.exif_dt, rec.exif_dt_source)
    if date_value is None:
        # Симметрично основному дереву: RAW_ROOT/ByDate/0000-undated/<дерево источника> --
        # см. RULES.md, тот же принцип, что и "0000-undated" в основном ByDate.
        return safe_mirror_dir(os.path.join(cfg.raw_root, "ByDate", "0000-undated"),
                                os.path.dirname(item.rel_path))
    return build_bydate_dest_dir(os.path.join(cfg.raw_root, "ByDate"), date_value, precision, None,
                                  cfg.bydate_granularity)


def resolve_dest_path(dest_dir: str, filename: str, candidate_sha256: str, sha256_of_file_fn, max_len: int,
                       stats: dict = None):
    """Handle name collisions: identical content at an occupied name => duplicate (skip);
    otherwise append _1, _2, ... Returns (final_path, is_duplicate).
    stats (p.5.3а, optional): if given, counts a "warn_path_truncated" occurrence whenever
    the filename actually had to be shortened -- surfaced in summary.txt."""
    _makedirs_iterative(winlong(dest_dir))
    name = truncate_segment(filename, max_len)
    if stats is not None and name != sanitize_windows_component(filename):
        # Compare against the SANITIZED (not raw) filename -- sanitize_windows_component()
        # alone (illegal-char replacement) is a different, already-documented concern than
        # length truncation; only count it here if the name was actually shortened.
        stats["warn_path_truncated"] = stats.get("warn_path_truncated", 0) + 1
    root, ext = os.path.splitext(name)
    candidate_path = os.path.join(dest_dir, name)
    n = 0
    while os.path.exists(winlong(candidate_path)):
        try:
            existing_sha = sha256_of_file_fn(candidate_path)
        except OSError:
            existing_sha = None
        if existing_sha == candidate_sha256:
            return candidate_path, True
        n += 1
        candidate_path = os.path.join(dest_dir, f"{root}_{n}{ext}")
    return candidate_path, False

# ============================================================================
# IO_COPY  (from pipeline/io_copy.py)
# ============================================================================


class InsufficientSpace(Exception):
    pass


class TargetLocked(Exception):
    pass


LOCK_STALE_SECONDS = 12 * 3600


class TargetLock:
    """p.5.4б: защита от ДВУХ ОДНОВРЕМЕННЫХ прогонов archive на один TARGET -- аудитом кода
    найдена единственная реальная дыра в гарантии "чужое содержимое не затирается":
    resolve_dest_path() проверяет занятость имени через os.path.exists() (TOCTOU), а
    os.replace() на Windows безусловно перезаписывает существующий файл. Два случайных
    параллельных прогона (двойной клик мимо, забыли про уже работающий) могут независимо
    счесть один и тот же dest_path свободным.

    Простой exclusive-create lock-файл, НЕ полноценный распределённый лок: детект "устарел"
    -- по времени (mtime > 12ч), а не по реальной проверке "жив ли процесс с этим PID"
    (ненадёжно кроссплатформенно без доп. зависимостей вроде psutil). Этого достаточно для
    реалистичного сценария ("забыл, что прогон уже идёт"), не для состязания с кем-то, кто
    специально хочет обойти защиту -- как и остальная "защита от дурака" в этом файле."""

    def __init__(self, target: str, log=print):
        self.lock_path = os.path.join(target, "__служебные_файлы", "LOCK")
        self.log = log
        self._acquired = False

    def __enter__(self):
        real_path = winlong(self.lock_path)
        _makedirs_iterative(winlong(os.path.dirname(self.lock_path)))
        try:
            fd = os.open(real_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                age = time.time() - os.path.getmtime(real_path)
            except OSError:
                age = LOCK_STALE_SECONDS + 1
            if age <= LOCK_STALE_SECONDS:
                raise TargetLocked(
                    f"похоже, другой прогон PhotoArchive уже работает с этим TARGET -- "
                    f"файл {self.lock_path} создан {age:.0f} сек назад. Если это не так "
                    f"(прошлый прогон аварийно завершился только что) -- удалите файл "
                    f"вручную и запустите снова."
                ) from None
            self.log(f"ВНИМАНИЕ: обнаружен устаревший LOCK-файл ({age / 3600:.1f}ч) -- "
                     f"похоже, прошлый прогон был прерван аварийно (питание/крэш). "
                     f"Удаляю и продолжаю.")
            try:
                os.remove(real_path)
            except OSError:
                pass
            fd = os.open(real_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode("ascii"))
        os.close(fd)
        self._acquired = True
        return self

    def __exit__(self, exc_type, exc, tb):
        if self._acquired:
            try:
                os.remove(winlong(self.lock_path))
            except OSError:
                pass
        return False


def atomic_copy(src_path: str, dest_path: str, expected_sha256: str, margin_bytes: int):
    """Copy src -> temp file in the same directory as dest -> verify hash matches source
    -> atomic rename to dest_path. A broken/partial copy can never end up at dest_path.
    Raises InsufficientSpace if free space (after the copy) would dip below margin.
    """
    dest_dir = os.path.dirname(dest_path)
    _makedirs_iterative(winlong(dest_dir))

    size = os.path.getsize(winlong(src_path))
    free = shutil.disk_usage(winlong(dest_dir)).free
    if free - size < margin_bytes:
        raise InsufficientSpace(
            f"свободно {free/1024**3:.2f}ГБ, нужно {size/1024**3:.2f}ГБ + запас {margin_bytes/1024**3:.1f}ГБ"
        )

    # dir= уже в extended-length форме -> tmp_path, который вернёт mkstemp, тоже будет с
    # префиксом \\?\ (winlong() ниже это распознаёт и не удваивает префикс).
    fd, tmp_path = tempfile.mkstemp(prefix=".photosort_tmp_", dir=winlong(dest_dir))
    os.close(fd)
    try:
        shutil.copy2(winlong(src_path), tmp_path)
        actual_sha = sha256_file(tmp_path)
        if actual_sha != expected_sha256:
            raise IOError(f"hash mismatch after copy: {src_path} -> {tmp_path} "
                           f"(expected {expected_sha256}, got {actual_sha})")
        os.replace(tmp_path, winlong(dest_path))
    except OSError as e:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        # Security audit finding #2: the margin check above is the intended guard, but a
        # misconfigured/zero/negative free_space_margin_gb (see Config.__post_init__
        # validation) could let it through -- if the OS itself then runs out of space
        # mid-copy, surface it as the same friendly InsufficientSpace stop instead of an
        # unhandled OSError traceback (which would leave the disk sitting at 0 bytes free).
        if e.errno == errno.ENOSPC:
            raise InsufficientSpace(f"диск заполнился во время копирования: {e}") from e
        raise
    except Exception:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise


def same_volume(path_a: str, path_b: str) -> bool:
    """True, если оба пути физически на одном томе (сравнение st_dev -- на Windows это
    серийный номер тома, надёжнее парсинга буквы диска: работает и для путей без буквы
    диска и не путается с монтированными точками). Оба пути должны существовать на момент
    вызова. Любая ошибка stat -- считаем "не один том" (безопасный дефолт: тогда используется
    обычное копирование с hash-verify, а не быстрый rename)."""
    try:
        return os.stat(winlong(path_a)).st_dev == os.stat(winlong(path_b)).st_dev
    except OSError:
        return False


def place_file(item: "SourceItem", dest_path: str, expected_sha256: str, cfg: "Config", run_logs,
                stats: dict = None) -> None:
    """Финализация одного файла на его месте в TARGET (Фаза 5).

    Файл ИЗ архива-источника (лежит внутри TMP_EXTRACT), если TMP_EXTRACT и место
    назначения физически на одном томе -- атомарный rename (os.replace), без повторного
    чтения байт: целостность уже подтверждена CRC-проверкой распаковщика при извлечении
    (успешная распаковка zip/7z/rar = файл цел), повторный sha256-verify избыточен.
    Rename не потребляет свободное место (файл просто меняет родителя на том же томе),
    поэтому проверка FREE_SPACE_MARGIN_GB здесь не нужна -- она уже была бы сделана перед
    распаковкой архива (см. SourceWalker._handle_archive).

    Исправлено в рамках аудита 2026-07-10 (Фаза 2, целостность данных): предыдущий
    комментарий утверждал это и про tar/tar.gz/tar.bz2 -- НЕВЕРНО. У формата tar вообще нет
    чек-суммы содержимого файла (только чек-сумма заголовка) -- tarfile.extract() "успешно"
    извлечёт член с побитыми в середине байтами без единой ошибки, в отличие от zip/7z/rar,
    где 7z.exe/UnRAR.exe реально сверяют CRC и извлечение целиком проваливается (см.
    extract_archive()) при несовпадении. item.archive_no_crc (выставляется SourceWalker
    только для tar-источников) поэтому НИКОГДА не берёт этот rename-шорткат -- всегда идёт
    полный atomic_copy() с hash-verify ниже. Это не может обнаружить повреждение, случившееся
    ВНУТРИ самой распаковки tar (сверять tar-контент не с чем -- у формата просто нет
    контрольной суммы), но хотя бы не выдаёт rename без единой проверки за "формат с
    подтверждённой при распаковке целостностью", каким tar не является.

    Во всех остальных случаях (обычный файл источника, включая CD/DVD напрямую; файл из
    tar/tar.gz/tar.bz2; либо файл из zip/7z/rar, но TMP_EXTRACT_DIR оказался на другом томе,
    чем TARGET) -- прежняя схема без изменений: atomic_copy (temp-файл рядом с dest ->
    hash-verify -> atomic rename)."""
    dest_dir = os.path.dirname(dest_path)
    from_archive = item.read_path.startswith(cfg.tmp_extract + os.sep)
    if from_archive and not item.archive_no_crc:
        _makedirs_iterative(winlong(dest_dir))
        if same_volume(cfg.tmp_extract, dest_dir):
            os.replace(winlong(item.read_path), winlong(dest_path))
            run_logs.action(f"renamed(from_archive,same_volume): {item.read_path} -> {dest_path}")
            return
        if stats is not None:
            # p.5.3а: TMP_EXTRACT_DIR on a different volume than this file's destination --
            # already warned once at startup (report_environment) if the config-level paths
            # differ; this counts the actual number of files that degraded to copy because
            # of it, for summary.txt.
            stats["warn_cross_volume_tmp_extract"] = stats.get("warn_cross_volume_tmp_extract", 0) + 1
    elif from_archive and stats is not None:
        stats["tar_verified_copy"] = stats.get("tar_verified_copy", 0) + 1
    atomic_copy(item.read_path, dest_path, expected_sha256, int(cfg.free_space_margin_gb * 1024**3))

# ============================================================================
# LOGS  (from pipeline/logs.py)
# ============================================================================

LOG_ROTATE_MAX_BYTES = 20 * 1024 * 1024
LOG_ROTATE_KEEP = 3


def _rotate_log_if_needed(path: str):
    """p.5.3в: every file under __служебные_файлы/logs/ accumulates for the whole lifetime of the
    archive (opened "a" once per run/crash, never truncated) -- without this, a long-lived
    archive used by a non-technical user ("домохозяйки не будут чистить логи") grows these files
    without bound. At 20MB, rename to <name>-YYYYMMDD-HHMMSS.<ext> and keep only the
    LOG_ROTATE_KEEP most recent rotated copies per base name (older ones deleted). None of
    these files is ever read back by the program itself (grep-confirmed -- the real
    source of truth is work.db) -- rotation can only affect a human reading the log later,
    never program logic. Called right before a file is opened for append, so a rotated
    file's replacement always starts empty (CSV header re-written on next _init_csv call).
    Also reused for crash.log (next to the .exe, not under logs/ -- see
    _log_unexpected_crash), which has no CSV header to restore."""
    real_path = winlong(path)
    try:
        size = os.path.getsize(real_path)
    except OSError:
        return
    if size < LOG_ROTATE_MAX_BYTES:
        return
    root, ext = os.path.splitext(path)
    rotated_path = f"{root}-{time.strftime('%Y%m%d-%H%M%S')}{ext}"
    try:
        os.replace(real_path, winlong(rotated_path))
    except OSError:
        return
    base_name = os.path.basename(root)
    dirpath = os.path.dirname(path) or "."
    pattern = re.compile(r"^" + re.escape(base_name) + r"-\d{8}-\d{6}" + re.escape(ext) + r"$")
    try:
        rotated_siblings = sorted(
            (f for f in os.listdir(winlong(dirpath)) if pattern.match(f)), reverse=True,
        )
    except OSError:
        return
    for stale in rotated_siblings[LOG_ROTATE_KEEP:]:
        try:
            os.remove(winlong(os.path.join(dirpath, stale)))
        except OSError:
            pass


class RunLogs:
    def __init__(self, logs_dir: str):
        self.logs_dir = logs_dir
        _makedirs_iterative(winlong(logs_dir))
        self._files = {}
        self._writers = {}
        self._init_csv("appended", ["timestamp", "source", "dest", "reason", "flags"])
        self._init_csv("skipped", ["timestamp", "source", "matched_with", "reason"])
        self._init_csv("disputes", ["timestamp", "source", "reason", "dest", "was_hidden"])
        self._init_csv("dates_review", ["timestamp", "dest", "date", "tier", "confidence", "evidence", "source"])
        self._init_csv("albums_merged", ["timestamp", "album", "source_variant"])
        self._init_csv("unreadable", ["timestamp", "source", "error"])
        self._init_csv("rejected_noise", ["timestamp", "source", "reason"])
        actions_path = os.path.join(logs_dir, "actions.log")
        _rotate_log_if_needed(actions_path)
        self.actions_log = open(winlong(actions_path), "a", encoding="utf-8")
        archives_path = os.path.join(logs_dir, "archives.log")
        _rotate_log_if_needed(archives_path)
        self.archives_log = open(winlong(archives_path), "a", encoding="utf-8")

    def _init_csv(self, name, header):
        path = os.path.join(self.logs_dir, f"{name}.csv")
        _rotate_log_if_needed(path)
        is_new = not os.path.exists(winlong(path))
        f = open(winlong(path), "a", newline="", encoding="utf-8")
        w = csv.writer(f)
        if is_new:
            w.writerow(header)
            f.flush()
        self._files[name] = f
        self._writers[name] = w

    def _ts(self):
        return time.strftime("%Y-%m-%d %H:%M:%S")

    def appended(self, source, dest, reason, flags=""):
        self._writers["appended"].writerow([self._ts(), source, dest, reason, flags])
        self._files["appended"].flush()

    def skipped(self, source, matched_with, reason):
        self._writers["skipped"].writerow([self._ts(), source, matched_with, reason])
        self._files["skipped"].flush()

    def disputed(self, source, reason, dest, was_hidden=False):
        self._writers["disputes"].writerow([self._ts(), source, reason, dest, int(was_hidden)])
        self._files["disputes"].flush()

    def unreadable(self, source, error):
        self._writers["unreadable"].writerow([self._ts(), source, error])
        self._files["unreadable"].flush()

    def rejected_noise(self, source, reason):
        self._writers["rejected_noise"].writerow([self._ts(), source, reason])
        self._files["rejected_noise"].flush()

    def date_review(self, dest, date_value, tier, confidence, evidence, source):
        self._writers["dates_review"].writerow(
            [self._ts(), dest, date_value.isoformat() if date_value else "", tier, confidence, evidence, source]
        )
        self._files["dates_review"].flush()

    def album_merged(self, album, source_variant):
        self._writers["albums_merged"].writerow([self._ts(), album, source_variant])
        self._files["albums_merged"].flush()

    def action(self, line):
        self.actions_log.write(f"[{self._ts()}] {line}\n")
        self.actions_log.flush()

    def debug_action(self, line):
        """p.5.3б: [DEBUG]-строка в actions.log -- caller gates this on cfg.debug, this
        method itself has no opinion on whether debug mode is on (keeps RunLogs config-
        agnostic, like every other method here)."""
        self.actions_log.write(f"[{self._ts()}] [DEBUG] {line}\n")
        self.actions_log.flush()

    def archive_event(self, display, status, note=""):
        self.archives_log.write(f"[{self._ts()}] {display}: {status} {note}\n".rstrip() + "\n")
        self.archives_log.flush()

    def write_summary(self, text: str):
        path = os.path.join(self.logs_dir, "summary.txt")
        _rotate_log_if_needed(path)
        with open(winlong(path), "a", encoding="utf-8") as f:
            f.write(text)

    def close(self):
        for f in self._files.values():
            f.close()
        self.actions_log.close()
        self.archives_log.close()


class NullRunLogs:
    """ТЗ-меню 2026-07-10, раздел 5: используется вместо RunLogs, когда
    cfg.suppress_logs=True (интерактивный "пробный прогон" из голого меню) -- та же
    поверхность методов, каждый no-op, НИЧЕГО не создаёт и не открывает на диске (в отличие
    от RunLogs.__init__, который безусловно делает os.makedirs()+открывает файлы)."""

    def appended(self, *a, **kw): pass
    def skipped(self, *a, **kw): pass
    def disputed(self, *a, **kw): pass
    def unreadable(self, *a, **kw): pass
    def rejected_noise(self, *a, **kw): pass
    def date_review(self, *a, **kw): pass
    def album_merged(self, *a, **kw): pass
    def action(self, *a, **kw): pass
    def debug_action(self, *a, **kw): pass
    def archive_event(self, *a, **kw): pass
    def write_summary(self, *a, **kw): pass
    def close(self): pass

# ============================================================================
# ARCHIVE_INDEX  (from pipeline/archive_index.py)
# ============================================================================


def _walk_media_files(root: str, exclude_dirs=None):
    """Yields (path, ftype) for every image/raw/video file under root, pruning any subtree
    whose path exactly matches one of exclude_dirs (used to keep 0000-undated out of the
    dedup base -- see index_archive). Comparison is case-insensitive (NTFS) via normcase,
    same convention as is_under_system_dir()."""
    exclude_norm = {os.path.normcase(os.path.abspath(d)) for d in (exclude_dirs or [])}
    # winlong(root) so the walk itself survives deeply-nested Albums/ByDate trees (the exact
    # kind of path this tool builds) on re-indexing in later runs -- os.walk inherits the
    # extended-length prefix into every subsequent os.path.join it does internally. Stripped
    # back to a plain path immediately so DB storage/logs/comparisons stay canonical; winlong()
    # is re-applied at the point of each actual filesystem call (see index_archive).
    for dirpath, dirnames, filenames in os.walk(winlong(root)):
        stripped_dirpath = _strip_winlong(dirpath)
        if os.path.normcase(os.path.abspath(stripped_dirpath)) in exclude_norm:
            dirnames[:] = []  # prune: don't descend into this subtree either
            continue
        for fn in filenames:
            p = _strip_winlong(os.path.join(dirpath, fn))
            t = file_type(p)
            if t in ("image", "raw", "video"):
                yield p, t


def index_archive(cfg: Config, conn, log=print):
    """Phase 1: index Albums/ + ByDate/ + RAW/ into the dedup pool (archive table).
    __служебные_файлы/ (disputed/logs/prompt/tmp_extract) and ByDate/0000-undated (+ its RAW
    mirror, RAW/ByDate/0000-undated) are intentionally excluded -- not part of the dedup
    ground truth. Excluding __служебные_файлы/ needs no special-casing here: it lives outside
    albums_root/bydate_root/raw_root entirely (see Config.__post_init__), so the roots
    walked below never reach it. 0000-undated DOES sit inside bydate_root/raw_root (it's
    real archive content, re-readable as a source -- see RULES.md), so it needs an explicit
    prune: a file in the dedup base would block its own promotion once new rules manage to
    date it (see RULES.md, "КРИТИЧНО" note in the undated section).
    """
    roots = [cfg.albums_root, cfg.bydate_root, cfg.raw_root]
    excludes_by_root = {
        cfg.bydate_root: [cfg.undated_root],
        cfg.raw_root: [os.path.join(cfg.raw_root, "ByDate", "0000-undated")],
    }
    total_files = 0
    total_bytes = 0
    cur = conn.cursor()

    cache = {}
    if cfg.archive_hash_cache:
        for row in conn.execute(
            "SELECT path, size, mtime, sha256, phash, duration, width, height, bitrate FROM archive_cache"
        ):
            cache[row[0]] = row[1:]

    # Дешёвая предпосчитывающая проходка (только имена файлов, без stat/hash) -- даёт total
    # для бара с честным %/ETA; сама индексация не меняется, просто вложена в тот же цикл.
    precount = sum(1 for root in roots if os.path.isdir(winlong(root))
                   for _ in _walk_media_files(root, exclude_dirs=excludes_by_root.get(root)))

    with ProgressReporter(total=precount or None, desc="Фаза 1 — индексация архива", unit="файл") as bar:
        for root in roots:
            if not os.path.isdir(winlong(root)):
                continue
            for path, ftype in _walk_media_files(root, exclude_dirs=excludes_by_root.get(root)):
                try:
                    st = os.stat(winlong(path))
                except OSError:
                    continue
                size, mtime = st.st_size, st.st_mtime

                cached = cache.get(path)
                if cached and cached[0] == size and abs(cached[1] - mtime) < 1e-6:
                    sha, phash, duration, width, height, bitrate = cached[2], cached[3], cached[4], cached[5], cached[6], cached[7]
                else:
                    width = height = bitrate = None
                    try:
                        sha = sha256_file(path)
                    except OSError:
                        # Same class of race as the archive-scan guards in _handle_archive()
                        # (2026-07-11, live user report) -- this indexes the user's OWN existing
                        # archive (Phase 1, dedup base), so a file removed/renamed here from
                        # outside the program between the os.stat() above and this read must not
                        # crash the whole run either. Skipping it here just means Phase 1 doesn't
                        # index a file that's no longer actually there -- same effect as if it
                        # had never been stat-able in the first place (see the os.stat() guard).
                        continue
                    phash = None
                    duration = None
                    if ftype in ("image", "raw"):
                        phash, width, height = image_phash_and_size(path)
                    elif ftype == "video":
                        duration, width, height, bitrate = video_duration_and_resolution(path)
                        frames = video_phash_3frames(path, duration)
                        phash = "|".join(frames) if frames else None
                    if cfg.archive_hash_cache:
                        conn.execute(
                            "INSERT OR REPLACE INTO archive_cache"
                            "(path,size,mtime,sha256,phash,duration,width,height,bitrate) "
                            "VALUES (?,?,?,?,?,?,?,?,?)",
                            (path, size, mtime, sha, phash, duration, width, height, bitrate),
                        )

                cur.execute(
                    "INSERT OR REPLACE INTO archive(path,root,size,mtime,sha256,phash,duration,type,width,height,bitrate) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (path, root, size, mtime, sha, phash, duration, ftype, width, height, bitrate),
                )
                total_files += 1
                total_bytes += size
                note = "большое видео" if ftype == "video" and size > 200 * 1024**2 else None
                bar.update(1, note=note)

    if cfg.archive_hash_cache:
        # archive_cache -- единственная таблица work.db, персистентная между прогонами --
        # иначе росла бы бессрочно (файлы, удалённые/переименованные из архива, оставляли бы
        # в кэше вечный мусор). Только что законченный обход roots -- это полная актуальная
        # правда "что реально сейчас есть в архиве", поэтому пути, не попавшие в archive
        # этим же проходом, безопасно считать устаревшими.
        conn.execute("DELETE FROM archive_cache WHERE path NOT IN (SELECT path FROM archive)")

    conn.commit()
    log(f"Фаза 1: проиндексировано существующего архива — {total_files} файлов, "
        f"{total_bytes / (1024**3):.2f} ГБ")
    return total_files, total_bytes

# ============================================================================
# ANALYZE  (А.2: analyze-quick / analyze / analyze-full)
# Отдельные РЕЖИМЫ (не флаг DRY_RUN -- у DRY_RUN другой смысл, "что я сделаю при сборке":
# он проходит ВСЮ Фазу 4/4.5/5 и пишет обычные __служебные_файлы\logs\*.csv в TARGET, просто без физического
# копирования байт). Analyze-режимы вообще не пишут в TARGET ни файлов, ни логов -- это
# read-only диагностика ИСТОЧНИКА, единственный побочный эффект на диске -- work.db
# (эфемерный индекс, как и у обычной сборки) и, по желанию вызывающего, analyze_report.csv,
# оба в WORKDIR, а не в TARGET.
# ============================================================================


@dataclass
class AnalyzeStats:
    mode: str
    total_files: int = 0
    total_bytes: int = 0
    n_images: int = 0
    n_raw: int = 0
    n_videos: int = 0
    n_archives_found: int = 0
    n_archives_encrypted: int = 0
    n_archives_nested: int = 0
    n_no_exif_date: int = 0
    n_future_date: int = 0
    n_before_1990: int = 0
    n_tier_c_estimated: int = 0
    n_copy_artifact_mtime: int = 0
    n_broken_or_zero: int = 0
    n_signature_mismatch: int = 0
    n_raw_without_jpeg: int = 0
    n_jpeg_without_raw: int = 0
    n_albums_detected: int = 0
    n_dump_items: int = 0
    # analyze / analyze-full (полный проход хеширования -- точный/near-дедуп):
    n_exact_dupes: int = 0
    n_diff_name_same_content: int = 0
    n_near_dupes: int = 0
    predicted_unique_count: int = 0
    predicted_unique_bytes: int = 0
    # analyze-full (+ сверка с TARGET):
    already_in_archive_count: int = 0
    target_free_bytes: int = 0
    fits_on_target: bool = True


def run_analyze(cfg: Config, mode: str, log=print) -> AnalyzeStats:
    """mode: analyze-quick (метаданные, без SHA/pHash) | analyze (+ полный проход хеширования,
    точный+near-дедуп ВНУТРИ источника) | analyze-full (+ индексация существующего TARGET,
    Фаза 1 -- что уже спасено, что новое, поместится ли новое на TARGET).

    Переиспользует РЕАЛЬНЫЙ конвейер (SourceWalker, analyze_batch, resolve_date, find_album,
    decide()+Pool, index_archive) вплоть до Фазы 4.5 включительно -- решения о дедупе и дате
    считаются той же логикой, что и настоящая сборка, только результат никогда не
    материализуется на диск (Фаза 5 не вызывается вовсе)."""
    stats = AnalyzeStats(mode=mode)
    date_ctx = DateContext()
    album_names = set()

    pool = Pool()
    if mode == "analyze-full":
        conn = db_reset(cfg.index_db)
        index_archive(cfg, conn, log=log)
        pool = build_pool_from_archive_table(conn)
        conn.close()

    processed = 0
    progress_desc = {
        "analyze-quick": "analyze-quick — метаданные источника",
        "analyze": "analyze — метаданные + хеширование источника",
        "analyze-full": "analyze-full — метаданные + хеширование + сверка с TARGET",
    }.get(mode, mode)
    # Без `with`/reindent остального тела: явный close() перед return ниже (см. дальше по
    # функции) -- вызывающий печатает чек-лист сразу после возврата stats.
    bar = ProgressReporter(total=None, desc=progress_desc, unit="файл")
    bar.__enter__()
    walker = SourceWalker(cfg, log=log, progress_cb=bar.set_context)
    for item in walker.walk():
        if cfg.sample_limit and processed >= cfg.sample_limit:
            break
        processed += 1
        bar.update(1, note="большое видео" if (
            item.ftype == "video" and item.size > 200 * 1024**2) else None)
        stats.total_files += 1
        stats.total_bytes += item.size

        if item.ftype == "image":
            stats.n_images += 1
        elif item.ftype == "raw":
            stats.n_raw += 1
        elif item.ftype == "video":
            stats.n_videos += 1

        album, _, _ = find_album(item.rel_path, item.archive_boundary_idx,
                                  dump_names=cfg.dump_segment_names_lower,
                                  dump_prefixes=cfg.dump_segment_prefixes_tuple)
        if album:
            album_names.add(album)
        else:
            stats.n_dump_items += 1

        if item.ftype == "raw" and not item.sibling_path:
            stats.n_raw_without_jpeg += 1
        if item.ftype == "image" and not item.sibling_path:
            stats.n_jpeg_without_raw += 1

        real_kind = sniff_signature(item.read_path)
        if real_kind is not None and real_kind != _coarse_kind(item.ftype):
            stats.n_signature_mismatch += 1

        if item.size == 0:
            stats.n_broken_or_zero += 1
            continue
        if item.ftype not in ("image", "raw", "video"):
            continue

        recs = analyze_batch([item], retries=cfg.read_retry_count, retry_delay=cfg.read_retry_delay,
                              small_image_px=cfg.small_image_px, log=log,
                              skip_hash=(mode == "analyze-quick"))
        rec = recs[0]
        if rec.read_error or rec.broken:
            stats.n_broken_or_zero += 1
            continue

        dirname = os.path.dirname(item.rel_path)
        date_value, tier, conf, evidence, precision = resolve_date(
            date_ctx, item.rel_path, item.mtime, rec.exif_dt, rec.exif_dt_source)
        if rec.exif_dt is None:
            stats.n_no_exif_date += 1
        if date_value:
            now_year = datetime.now().year
            if date_value.year > now_year:
                stats.n_future_date += 1
            elif date_value.year < 1990:
                stats.n_before_1990 += 1
        if tier == "C":
            stats.n_tier_c_estimated += 1
        elif tier == "D" and mtime_is_copy_artifact(date_ctx.dir_mtimes.get(dirname, [])):
            stats.n_copy_artifact_mtime += 1

        if mode in ("analyze", "analyze-full") and rec.sha256:
            decision = decide(pool, rec, cfg.mirror_raw)
            if decision.decision == "skipped_present":
                stats.n_exact_dupes += 1
                existing = pool.find_exact(rec.sha256)
                if existing and existing.dest_path and \
                        os.path.basename(existing.dest_path) != os.path.basename(item.read_path):
                    stats.n_diff_name_same_content += 1
            elif decision.decision == "raw_skipped":
                pass  # MIRROR_RAW=false + есть JPEG -- осознанно не копируется, не "новое"
            else:
                # appended_new / appended_better / appended_crop / appended_near_dup /
                # appended_uncertain / raw_mirrored -- p.5.7: near-dup is appended, not
                # skipped, so it counts toward predicted_unique_* like any other appended file.
                if decision.decision in ("appended_better", "appended_crop", "appended_near_dup"):
                    stats.n_near_dupes += 1
                stats.predicted_unique_count += 1
                stats.predicted_unique_bytes += item.size
                if item.ftype == "raw":
                    entry = PoolEntry(sha256=rec.sha256, ftype="raw", dest_path=item.origin_display,
                                       size=item.size)
                else:
                    pool_ftype = "image" if item.ftype == "image" else "video"
                    entry = PoolEntry(sha256=rec.sha256, ftype=pool_ftype, dest_path=item.origin_display,
                                       size=item.size, aspect=rec.aspect, width=rec.width, height=rec.height,
                                       phash=rec.phash, duration=rec.duration, bitrate=rec.bitrate,
                                       has_camera=bool(rec.camera))
                pool.add(entry)

    bar.close()  # ДО печати чек-листа вызывающим (print_analyze_report) -- не портить формат

    for display, status, _note in walker.archive_logs:
        if status.startswith("archive_"):
            stats.n_archives_found += 1
        if status == "archive_password_protected":
            stats.n_archives_encrypted += 1
        if display.count(" → ") >= 2:
            stats.n_archives_nested += 1

    stats.n_albums_detected = len(album_names)

    if mode == "analyze-full":
        stats.already_in_archive_count = stats.n_exact_dupes
        margin = int(cfg.free_space_margin_gb * 1024**3)
        stats.target_free_bytes = (shutil.disk_usage(winlong(cfg.target)).free
                                    if os.path.isdir(winlong(cfg.target)) else 0)
        stats.fits_on_target = (stats.target_free_bytes - stats.predicted_unique_bytes) >= margin

    return stats


def print_analyze_report(stats: AnalyzeStats, log=print):
    mode_titles = {
        "analyze-quick": "analyze-quick (быстрая метаданными-диагностика, без SHA/pHash)",
        "analyze": "analyze (+ полный проход хеширования: дедуп внутри источника)",
        "analyze-full": "analyze-full (+ сверка с TARGET)",
    }
    log(f"\n=== Отчёт: {mode_titles.get(stats.mode, stats.mode)} ===")
    log(f"Просканировано: {stats.total_files} файлов, {stats.total_bytes / 1024**3:.2f} ГБ "
        f"(фото {stats.n_images}, raw {stats.n_raw}, видео {stats.n_videos})")
    log(f"✓ {stats.n_no_exif_date} файлов без EXIF-даты")
    log(f"✓ {stats.n_future_date} файлов с датой в будущем")
    log(f"✓ {stats.n_before_1990} файлов с датой до 1990")
    log(f"✓ {stats.n_tier_c_estimated} дат определены только эвристикой (Tier C, best_guess)")
    log(f"✓ {stats.n_copy_artifact_mtime} файлов с подозрительным mtime (похоже на массовое "
        f"копирование, не на съёмку)")
    log(f"✓ {stats.n_broken_or_zero} файлов повреждены / 0 байт / не открылись")
    log(f"✓ {stats.n_signature_mismatch} файлов: расширение не совпадает с сигнатурой содержимого")
    log(f"✓ {stats.n_jpeg_without_raw} JPEG без парного RAW в той же папке")
    log(f"✓ {stats.n_raw_without_jpeg} RAW без парного JPEG в той же папке")
    log(f"✓ альбомов распознано: {stats.n_albums_detected}, элементов в свалках (dump): {stats.n_dump_items}")
    log(f"✓ архивов найдено: {stats.n_archives_found} (запаролено: {stats.n_archives_encrypted}, "
        f"вложенных матрёшкой: {stats.n_archives_nested})")
    if stats.mode in ("analyze", "analyze-full"):
        log(f"✓ {stats.n_exact_dupes} точных дубликатов (по SHA-256)")
        log(f"✓ {stats.n_diff_name_same_content} файлов: разные имена, одинаковое содержимое")
        log(f"✓ {stats.n_near_dupes} near-dup / возможных кропов (по pHash)")
        log(f"✓ прогноз уникальных после дедупа: {stats.predicted_unique_count} файлов, "
            f"~{stats.predicted_unique_bytes / 1024**3:.2f} ГБ")
    if stats.mode == "analyze-full":
        log(f"✓ уже есть в TARGET (по SHA): {stats.already_in_archive_count}")
        log(f"✓ новых после сверки с TARGET: {stats.predicted_unique_count} файлов, "
            f"~{stats.predicted_unique_bytes / 1024**3:.2f} ГБ")
        fits = "ХВАТИТ" if stats.fits_on_target else "НЕ ХВАТИТ, освободите место перед сборкой"
        log(f"✓ свободно на TARGET: {stats.target_free_bytes / 1024**3:.2f} ГБ -- {fits}")


def write_analyze_report_csv(path: str, stats: AnalyzeStats):
    """Машинный аналог print_analyze_report(): metric,value. Пишется в WORKDIR (НЕ в
    TARGET -- ни один analyze-режим не пишет в TARGET), перезаписывается на каждый прогон
    (снимок текущего анализа, не append-only лог, в отличие от __служебные_файлы\\logs\\*.csv реальной сборки)."""
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["metric", "value"])
        for field_name, value in vars(stats).items():
            w.writerow([field_name, value])

# ============================================================================
# MAIN  (from pipeline/main.py)
# ============================================================================

PHOTOSORT_DIR_EXPLANATION = (
    "Служебная папка архива PhotoArchive (__служебные_файлы\\): logs (логи прогонов), "
    "prompt (версии правил сборки), tmp_extract (временная распаковка архивов-источников, "
    "чистится автоматически). Не медиа-контент, не трогать вручную во время прогона. Спорные "
    "файлы (не смогли уверенно распознать) лежат отдельно, в _Unsorted\\ рядом с "
    "Albums\\/ByDate\\/RAW\\ -- это настоящие фото, не служебные данные. Этот маркер исключает "
    "всё поддерево __служебные_файлы\\ из обхода источника, если сам архив используется как "
    "SOURCE для другого TARGET."
)


def ensure_target_layout(cfg: Config):
    # ByDate/0000-undated (undated) намеренно НЕ создаётся заранее здесь -- это часть
    # основного архивного дерева (не служебная папка), появляется лениво через
    # resolve_dest_path() при первом недатированном файле, как любая обычная ByDate/YYYY.
    for d in (cfg.albums_root, cfg.bydate_root, cfg.raw_root,
              cfg.dispute, cfg.tmp_extract, cfg.logs, cfg.prompt_dir):
        _makedirs_iterative(winlong(d))
    marker = os.path.join(cfg.photosort_dir, "SKIP_PHOTOSORT.txt")
    if not os.path.exists(winlong(marker)):
        with open(winlong(marker), "w", encoding="utf-8") as f:
            f.write(PHOTOSORT_DIR_EXPLANATION + "\n")


def check_rules_version(cfg: Config, log=print):
    """Заменяет byte-diff версионирование оригинала (которое копировало целиком
    prompt.md/photosort.py в {TARGET}/__служебные_файлы/prompt/ при изменении). Здесь скрипт --
    один onefile .exe (сотни МБ), копировать его в TARGET на каждый прогон нельзя, поэтому
    сравнивается короткая строка RULES_VERSION (см. верх файла, RULES.md), а не байты
    исполняемого файла. version.txt -- append-only история смен версий правил, как и
    раньше никогда не затирается."""
    dest = os.path.join(cfg.prompt_dir, "version.txt")
    if not os.path.exists(winlong(dest)):
        with open(winlong(dest), "w", encoding="utf-8") as f:
            f.write(f"{RULES_VERSION}\t{time.strftime('%Y-%m-%d %H:%M:%S')}\tfirst_run\n")
        log(f"Версия правил: первая — {RULES_VERSION} (записана в __служебные_файлы\\prompt\\version.txt)")
        return
    with open(winlong(dest), "r", encoding="utf-8") as f:
        lines = [line for line in f.read().splitlines() if line.strip()]
    last_version = lines[-1].split("\t", 1)[0] if lines else None
    if last_version == RULES_VERSION:
        return
    with open(winlong(dest), "a", encoding="utf-8") as f:
        f.write(f"{RULES_VERSION}\t{time.strftime('%Y-%m-%d %H:%M:%S')}\tchanged_from={last_version}\n")
    log(f"ВНИМАНИЕ: версия правил изменилась с прошлого прогона ({last_version} -> "
        f"{RULES_VERSION}). Архив мог быть собран другой версией правил (см. "
        f"__служебные_файлы\\prompt\\version.txt). appended.csv будет отмечать текущую версию.")


def build_pool_from_archive_table(conn) -> Pool:
    """Width/height/bitrate now come straight from the archive table (populated by
    index_archive) instead of re-decoding every already-archived image via PIL on every
    single run -- besides the wasted I/O, the old version never set width/height for
    *video* entries at all, which silently broke cross-run video near-dup comparison
    (video_is_strictly_better saw existing videos as 0x0 and always treated the new file as
    strictly better, re-appending near-duplicate videos across runs instead of skipping)."""
    pool = Pool()
    for row in conn.execute(
        "SELECT path, size, mtime, sha256, phash, duration, type, root, width, height, bitrate FROM archive"
    ):
        path, size, mtime, sha256, phash, duration, ftype, root, width, height, bitrate = row
        entry = PoolEntry(sha256=sha256, ftype=ftype, dest_path=path, size=size, duration=duration,
                           phash=phash, width=width, height=height, bitrate=bitrate)
        if ftype == "image" and width and height:
            entry.aspect = width / height
        pool.add(entry)
    return pool


def _iter_ancestors(path: str):
    """Yields every ancestor directory of path, from its immediate parent up to the
    filesystem/drive root (inclusive), stopping once os.path.dirname stops changing."""
    parent = os.path.dirname(path)
    prev = None
    while parent and parent != prev:
        yield parent
        prev = parent
        parent = os.path.dirname(parent)


def warn_if_target_nested_in_archive(cfg: Config, log=print) -> bool:
    """А.3: TARGET мог по ошибке быть указан подпапкой УЖЕ существующего архива photo-sort
    (например, TARGET=D:\\Архив фото\\Albums\\Свадьба вместо самого D:\\Архив фото) -- тогда
    служебные папки (__служебные_файлы\\) расплодятся по веткам вместо единого архива. НЕ блокирует
    запуск: отдельные тематические архивы верхнего уровня -- нормальный, поддерживаемый
    сценарий (RULES.md, "ЭКСПЛУАТАЦИЯ"); предупреждение реагирует только на вложенность В
    существующий архив, не на соседство с ним. Признак существующего архива у предка --
    его собственная папка __служебные_файлы\\ (умбрелла Задачи 1) ИЛИ одновременно Albums\\ и
    ByDate\\ (более старый архив/ручное дерево без __служебные_файлы). Тематические папки должны
    быть альбомами ВНУТРИ одного архива, а не отдельными TARGET. Returns True if it warned
    (p.5.3а: summary.txt counts this, see report_environment)."""
    target_real = os.path.realpath(cfg.target)
    for parent in _iter_ancestors(target_real):
        if not os.path.isdir(winlong(parent)):
            continue
        try:
            entries = {e.lower() for e in os.listdir(winlong(parent))}
        except OSError:
            continue
        if "__служебные_файлы" in entries or ("albums" in entries and "bydate" in entries):
            log(f"ВНИМАНИЕ: TARGET ({cfg.target}) похож на подпапку уже существующего "
                f"архива photo-sort в {parent} -- служебные папки (__служебные_файлы\\) могут "
                f"расплодиться по веткам вместо единого архива. Если TARGET указан веткой "
                f"существующего архива по ошибке (нужен был сам {parent} или его Albums\\-"
                f"подпапка как тема внутри него) -- поправьте TARGET. Если это осознанно "
                f"отдельный тематический архив верхнего уровня -- игнорируйте.")
            return True
    return False


def _target_has_existing_archive(target: str) -> bool:
    """True, если сам TARGET (не его предки, см. warn_if_target_nested_in_archive()) уже
    содержит структуру существующего архива photo-sort -- та же сигнатура ("__служебные_файлы" в
    listdir, либо одновременно "albums" и "bydate" -- более старый архив/ручное дерево без
    неё). Используется меню голого запуска (RULES.md, "ЗАПУСК" п.3), чтобы выбрать между
    analyze (первый раз) и analyze-full (уже есть с чем сверяться) без лишнего вопроса
    пользователю. Несуществующий/недоступный TARGET -> False (сверяться пока не с чем)."""
    real_target = winlong(target)
    if not os.path.isdir(real_target):
        return False
    try:
        entries = {e.lower() for e in os.listdir(real_target)}
    except OSError:
        return False
    return "__служебные_файлы" in entries or ("albums" in entries and "bydate" in entries)


def report_environment(cfg: Config, log=print, stats: dict = None):
    log(f"SOURCE: {cfg.source}")
    log(f"TARGET: {cfg.target}")
    log(f"WORKDIR (локально): {cfg.workdir}")
    log(f"TMP_EXTRACT_DIR: {cfg.tmp_extract}")
    # ТЗ-меню 2026-07-10, раздел 5: suppress_logs=True (интерактивный "пробный прогон")
    # сознательно НЕ создаёт TARGET заранее (ensure_target_layout() пропущен) -- на первом
    # прогоне в свежую папку disk_usage() на несуществующем пути упал бы FileNotFoundError.
    # Поднимаемся до ближайшего существующего предка (в худшем случае -- до буквы диска,
    # которая существует всегда) -- та же файловая система, тот же ответ.
    usage_probe = winlong(cfg.target)
    while not os.path.isdir(usage_probe):
        parent = os.path.dirname(usage_probe)
        if parent == usage_probe:
            break
        usage_probe = parent
    try:
        # Self-audit 2026-07-10: even the drive root can be missing (typo'd/removed drive
        # letter typed into "своя папка" with suppress_logs=True, so nothing upstream ever
        # touched the filesystem to catch it earlier) -- report_environment() is not the
        # place to invent a new fatal error for that, just say "unknown" and carry on.
        free = shutil.disk_usage(usage_probe).free
        log(f"Свободно на TARGET: {free / 1024**3:.2f} ГБ")
    except OSError:
        log(f"Свободно на TARGET: не удалось определить (диск {usage_probe} недоступен)")
    log(f"DRY_RUN={int(cfg.dry_run)}  SAMPLE_LIMIT={cfg.sample_limit}")
    nested = warn_if_target_nested_in_archive(cfg, log=log)
    # А.1: rename-финализация файлов из архива работает только на одном томе с TARGET --
    # предупредить один раз в начале прогона, если это не так, а не молча деградировать
    # к копированию без объяснения, почему сборка архивов вдруг медленнее ожидаемого.
    cross_volume_config = os.path.isdir(winlong(cfg.tmp_extract)) and not same_volume(cfg.tmp_extract, cfg.target)
    if cross_volume_config:
        log(f"ВНИМАНИЕ: TMP_EXTRACT_DIR ({cfg.tmp_extract}) на другом томе, чем TARGET "
            f"({cfg.target}) -- быстрая rename-финализация файлов из архивов недоступна, "
            f"будет использовано обычное копирование с hash-verify (медленнее). См. README.md.")
    if stats is not None:
        stats["warn_nested_target"] = stats.get("warn_nested_target", 0) + int(nested)
        # Фактическое число файлов, деградировавших до copy из-за кросс-volume tmp_extract,
        # копится в place_file() (p.5.3а) -- этот флаг только объясняет ПРИЧИНУ в консоли/логе.


class _RunState:
    def __init__(self, cfg, pool, date_ctx, run_logs, stats):
        self.cfg = cfg
        self.pool = pool
        self.date_ctx = date_ctx
        self.run_logs = run_logs
        self.stats = stats
        self.dest_path_by_read_path = {}
        self.merged_albums_seen = set()
        self.stopped_for_space = False
        # 2026-07-11, по запросу пользователя: album -> set известных "путей-источников"
        # (album_prefix из find_album()), уже отмеченных в __ВНИМАНИЕ_объединённая_папка.txt
        # этого альбома -- либо считанных из уже существующего файла (первое касание альбома
        # в этом прогоне), либо добавленных в ходе этого же прогона. См. _note_album_source().
        self.album_known_sources = {}
        # album -> True, если в этом прогоне уже вставлен пустой разделитель перед новыми
        # строками (чтобы вставить его РОВНО один раз за прогон, а не перед каждой строкой).
        self.album_marker_separator_done = set()


def _log_write_failure(item, dest_hint, e, cfg, run_logs, stats, log):
    """Security audit finding #1 (2026-07-10 follow-up): both resolve_dest_path() (its own
    os.makedirs) and place_file() can raise any OSError other than InsufficientSpace -- a file
    locked by an antivirus/indexer for a few hundred ms right after creation, a bad sector on a
    failing/scratched source disc (CD/DVD sources are an explicitly supported scenario, see
    RULES.md), a reserved device name (CON/NUL/...) that extreme truncate_segment() truncation
    happened to produce, or a destination path segment blocked by an unrelated same-named plain
    file. Before this helper existed, two of the three place_file() call sites in
    _process_record() only caught InsufficientSpace, and resolve_dest_path() itself was never
    guarded at all -- any other exception propagated all the way out of
    run()/run_for_source()/_main(), killing the ENTIRE run (and, for --source all, every
    remaining source in the batch) with a raw traceback instead of skipping the one problem file
    and continuing. Shared by all three call sites so the "log it, count it, keep going"
    behavior can't drift out of sync again. dest_hint is dest_dir, not dest_path -- dest_path
    may not exist yet if resolve_dest_path() itself is what failed."""
    log(f"  ошибка записи {item.read_path} -> {dest_hint}: {e}")
    if cfg.debug:
        run_logs.debug_action("traceback: begin")
        for line in traceback.format_exc().splitlines():
            run_logs.debug_action(line)
        run_logs.debug_action("traceback: end")
    run_logs.unreadable(item.origin_display, f"write_failed: {e}")
    stats["write_failed"] = stats.get("write_failed", 0) + 1


def _process_record(rec, st: _RunState, log=print):
    """Runs decide+date+placement+atomic-copy for one already-hashed record.
    Returns True if the run must stop (out of space)."""
    cfg, pool, date_ctx, run_logs, stats = st.cfg, st.pool, st.date_ctx, st.run_logs, st.stats
    item = rec.item

    # Зоны доверия (слой 2, см. classify_zone): в шумной зоне (кэши/temp) любое сомнение --
    # не медиа ИЛИ погранично-неуверенное (small_image/low_confidence_photo) -- уходит только
    # строкой в rejected_noise.csv, без копирования и без _disputed. Уверенно опознанное фото
    # (media_note не в этом наборе) архивируется как обычно из ЛЮБОЙ зоны, в т.ч. шумной.
    is_uncertain = (not rec.is_media) or (rec.media_note in ("small_image", "low_confidence_photo"))
    if item.zone == "noisy" and is_uncertain:
        run_logs.rejected_noise(item.origin_display, rec.media_note or "not_media")
        stats["rejected_noise"] += 1
        return False

    if not rec.is_media:
        dest_dir = safe_mirror_dir(cfg.dispute, os.path.dirname(item.rel_path))
        # rec.sha256 is only ever None for the size==0 special case (analyze_batch skips
        # hashing empty files) -- sha256_bytes(b"") is the real hash of an empty file, so
        # identical 0-byte placeholders still dedup correctly instead of comparing against
        # an empty-string sentinel that can never match anything.
        expected_sha = rec.sha256 or sha256_bytes(b"")
        try:
            dest_path, is_dup = resolve_dest_path(
                dest_dir, os.path.basename(item.rel_path),
                expected_sha, sha256_file, cfg.max_dest_path, stats=stats,
            )
            if not is_dup and not cfg.dry_run:
                # place_file: rename для файлов из архива на одном томе с dest, иначе
                # прежняя схема (temp file -> hash-verify -> atomic rename) -- см. IO_COPY.
                # _disputed всё так же внутри TARGET, так что степень защиты не меняется.
                place_file(item, dest_path, expected_sha, cfg, run_logs, stats=stats)
        except InsufficientSpace as e:
            log(f"ОСТАНОВКА: недостаточно места на TARGET ({e}). "
                f"Освободите место и запустите снова.")
            return True
        except Exception as e:
            _log_write_failure(item, dest_dir, e, cfg, run_logs, stats, log)
            return False
        if not is_dup:
            run_logs.disputed(item.origin_display, rec.media_note or "not_media", dest_path,
                               was_hidden=rec.is_hidden)
            run_logs.action(f"disputed: {item.origin_display} -> {dest_path}")
        stats["disputed"] += 1
        return False

    decision = decide(pool, rec, cfg.mirror_raw)
    if cfg.debug and decision.debug_detail:
        run_logs.debug_action(f"near_dup: source={item.origin_display} vs "
                               f"existing={decision.matched_dest} criterion={decision.debug_detail} "
                               f"-> {decision.decision}")

    if decision.decision == "skipped_present":
        run_logs.skipped(item.origin_display, decision.matched_dest, decision.note)
        stats[decision.decision] += 1
        # А.4: оценка "сэкономлено места дедупом" -- сколько байт НЕ скопировано (не "освобождено":
        # программа ничего не удаляет) благодаря тому, что содержимое уже есть в архиве/пуле
        # этого прогона. Near-dup (p.5.7) больше не сюда -- такие файлы теперь дописываются,
        # место для них не экономится.
        stats["bytes_saved_by_dedup"] += item.size
        # 2026-07-11, по запросу пользователя: если найденный дубль физически уже лежит в
        # АЛЬБОМЕ (не в ByDate/RAW), а путь ЭТОГО файла-дубля в источнике указывает на ДРУГОЕ
        # физическое место -- это ровно тот случай "альбом пополняется из другого места",
        # который стоит отметить в MERGED_ALBUM_MARKER_FILENAME уже существующего
        # (выигравшего гонку) альбома, см. _note_album_source().
        if decision.matched_dest.startswith(cfg.albums_root + os.sep):
            existing_album = os.path.relpath(decision.matched_dest, cfg.albums_root).split(os.sep)[0]
            _, _, own_prefix = find_album(item.rel_path, item.archive_boundary_idx,
                                           dump_names=cfg.dump_segment_names_lower,
                                           dump_prefixes=cfg.dump_segment_prefixes_tuple)
            if own_prefix is not None:
                existing_album_dir = os.path.join(cfg.albums_root, existing_album)
                _note_album_source(cfg, st, stats, existing_album, own_prefix, existing_album_dir)
        return False

    if decision.decision == "raw_skipped":
        # MIRROR_RAW=false + есть парный JPEG уже в основном архиве -- избыточный RAW
        # осознанно не копируется никуда, только строка в skipped.csv.
        run_logs.skipped(item.origin_display, item.sibling_path or "", decision.note)
        stats["raw_skipped"] += 1
        return False

    if decision.decision == "raw_mirrored":
        dest_dir = raw_dest_dir(item, rec, cfg, st.dest_path_by_read_path, date_ctx)

        try:
            dest_path, is_dup = resolve_dest_path(
                dest_dir, os.path.basename(item.rel_path), rec.sha256, sha256_file, cfg.max_dest_path,
                stats=stats)
            if not is_dup and not cfg.dry_run:
                place_file(item, dest_path, rec.sha256, cfg, run_logs, stats=stats)
        except InsufficientSpace as e:
            log(f"ОСТАНОВКА: недостаточно места на TARGET ({e}). "
                f"Освободите место и запустите снова.")
            return True
        except Exception as e:
            _log_write_failure(item, dest_dir, e, cfg, run_logs, stats, log)
            return False
        if not is_dup:
            pool.add(PoolEntry(sha256=rec.sha256, ftype="raw", dest_path=dest_path, size=item.size))
            st.dest_path_by_read_path[item.read_path] = dest_path
            run_logs.appended(item.origin_display, dest_path, decision.note)
            run_logs.action(f"appended(raw): {item.origin_display} -> {dest_path}")
            stats["raw_mirrored"] += 1
            stats["bytes_appended"] += item.size
        return False

    # image / video appended_*
    date_value, tier, conf, evidence, precision = resolve_date(
        date_ctx, item.rel_path, item.mtime, rec.exif_dt, rec.exif_dt_source)
    # p.5.3а: heuristic co-occurrence count, not a proven causal one -- exiftool (a separate
    # subprocess, not covered by winlong()) can silently fail to read EXIF on a source path
    # past the legacy 260-char MAX_PATH, degrading the date straight to Tier C (see
    # RULES.md/2.5г). Any other Tier-C cause (folder cluster, mtime, ...) at a long path also
    # gets counted here -- there's no separate signal distinguishing "long path caused this"
    # from "no EXIF for some other reason", so this only tells the user "look here", not "this
    # is definitely why".
    if tier == "C" and len(item.read_path) > 259:
        stats["warn_tier_c_long_path"] = stats.get("warn_tier_c_long_path", 0) + 1

    album, subpath, album_prefix = find_album(item.rel_path, item.archive_boundary_idx,
                                               dump_names=cfg.dump_segment_names_lower,
                                               dump_prefixes=cfg.dump_segment_prefixes_tuple)
    if cfg.debug:
        segments = item.rel_path.split("/")[:-1]
        if segments:
            deepest = segments[-1]
            has_tag = deepest.strip().endswith(DUMP_TAG)
            tag_flag = "да" if has_tag else "нет"
            if album:
                run_logs.debug_action(f"album_decision: segment='{deepest}' tag={tag_flag} -> album='{album}'")
            else:
                run_logs.debug_action(f"album_decision: segment='{deepest}' tag={tag_flag} -> dump")
    final_decision = decision.decision
    if album:
        album_dir = build_album_dest_dir(cfg.albums_root, album, subpath)
        if album not in st.merged_albums_seen:
            st.merged_albums_seen.add(album)
            if os.path.isdir(winlong(album_dir)):
                run_logs.album_merged(album, item.origin_display)
        # MERGED_ALBUM_MARKER_FILENAME belongs at the ALBUM ROOT (Albums/<album>/), not
        # nested inside whatever subpath THIS particular file happens to use -- a single
        # album can have many different subpath levels across its files, but the marker is
        # about the album as a whole, same root used by the skipped_present call site below.
        _note_album_source(cfg, st, stats, album, album_prefix, os.path.join(cfg.albums_root, album))
        dest_dir = album_dir
    elif date_value is None:
        dest_dir = safe_mirror_dir(cfg.undated_root, os.path.dirname(item.rel_path))
        final_decision = "undated"
    else:
        place = place_for_gps(rec.gps_lat, rec.gps_lon, cfg.home_country) if cfg.place_lookup == "offline" else None
        dest_dir = build_bydate_dest_dir(cfg.bydate_root, date_value, precision, place,
                                          cfg.bydate_granularity)

    try:
        dest_path, is_dup = resolve_dest_path(
            dest_dir, os.path.basename(item.rel_path), rec.sha256, sha256_file, cfg.max_dest_path,
            stats=stats)
        if not is_dup and not cfg.dry_run:
            place_file(item, dest_path, rec.sha256, cfg, run_logs, stats=stats)
    except InsufficientSpace as e:
        log(f"ОСТАНОВКА: недостаточно места на TARGET ({e}). Освободите место и запустите снова.")
        return True
    except Exception as e:
        _log_write_failure(item, dest_dir, e, cfg, run_logs, stats, log)
        return False

    if is_dup:
        run_logs.skipped(item.origin_display, dest_path, "identical_at_destination")
        stats["skipped_present"] += 1
        stats["bytes_saved_by_dedup"] += item.size  # А.4
        return False

    pool_ftype = "image" if item.ftype == "image" else "video"
    pool.add(PoolEntry(
        sha256=rec.sha256, ftype=pool_ftype, dest_path=dest_path, size=item.size,
        aspect=rec.aspect, width=rec.width, height=rec.height, phash=rec.phash,
        duration=rec.duration, bitrate=rec.bitrate, has_camera=bool(rec.camera),
    ))
    st.dest_path_by_read_path[item.read_path] = dest_path
    flags = rec.media_note if rec.media_note in ("small_image", "low_confidence_photo") else ""
    run_logs.appended(item.origin_display, dest_path, decision.note or decision.decision, flags=flags)
    run_logs.action(f"appended: {item.origin_display} -> {dest_path}")
    if tier != "A" and date_value is not None:
        run_logs.date_review(dest_path, date_value, tier, conf, evidence, item.origin_display)
        if cfg.debug:
            run_logs.debug_action(f"date: dest={dest_path} tier={tier} confidence={conf} "
                                   f"evidence={evidence} source={item.origin_display}")
    stats[final_decision] = stats.get(final_decision, 0) + 1
    stats["bytes_appended"] += item.size
    # А.4: разбивка "уникальных" по типу для итоговой сводки (фото vs видео)
    stats["appended_images" if pool_ftype == "image" else "appended_videos"] += 1
    # Security audit finding #5: p.5.7 made near-dup always append (never skip) -- track its
    # bytes separately so unbounded growth from a hostile/corrupted burst-shot SOURCE is
    # visible in the summary instead of hiding inside the aggregate archive size.
    if decision.decision in ("appended_near_dup", "appended_better", "appended_crop"):
        stats["bytes_near_dup"] = stats.get("bytes_near_dup", 0) + item.size
    return False


def build_final_summary(stats: dict, walker: "SourceWalker", unreadable_count: int,
                         pool: "Pool", processed_count: int) -> str:
    """А.4: человекочитаемый итог прогона поверх УЖЕ посчитанных чисел -- чистая агрегация
    существующих Фаза-4/4.5/5 решений и финального состояния pool, без новой бизнес-логики.
    "Итоговый архив" -- кумулятивное состояние ВСЕГО архива после этого прогона (то, что
    было в TARGET до старта, проиндексированное в Фазе 1, плюс дописанное сейчас), а не
    только дельта этого запуска -- см. RULES.md/README.md."""
    n_archives_extracted = sum(1 for _, status, _ in walker.archive_logs if status == "archive_extracted")
    # p.5.7: near-dup no longer means "skipped" -- appended_near_dup/appended_better/
    # appended_crop are the three near-dup outcomes, all now actually copied into the archive.
    n_near_dup = stats["appended_near_dup"] + stats["appended_better"] + stats["appended_crop"]
    n_broken_or_unreadable = stats["disputed"] + unreadable_count

    n_pool_images = sum(1 for e in pool.by_sha.values() if e.ftype == "image")
    n_pool_videos = sum(1 for e in pool.by_sha.values() if e.ftype == "video")
    pool_bytes = sum(e.size or 0 for e in pool.by_sha.values())

    # 2026-07-11: сумма пропусков по имени папки (HARD_EXCLUDE_DIRS/default_exclude_dirs/
    # extra_exclude_dirs) + по гейту системных папок -- разбивка по причинам только в
    # actions.log (см. _run_impl), тут только итоговое число, чтобы не раздувать сводку.
    n_excluded_dirs = sum(c for _, _, c in walker.excluded_dir_summary()) + len(walker.system_dir_skips)

    lines = [
        "\n--- Итог прогона ---\n",
        f"Обработано: {processed_count} файлов\n",
        f"Фотографий: {stats['appended_images']} | Видео: {stats['appended_videos']}\n",
        f"Точных дубликатов: {stats['skipped_present']} | Near-dup: {n_near_dup} "
        f"(~{stats.get('bytes_near_dup', 0) / 1024**3:.2f} ГБ, все сохранены -- похожие кадры "
        f"не удаляются, см. README)\n",
        f"Битых/нечитаемых: {n_broken_or_unreadable} | Без надёжной даты: {stats['undated']}\n",
        f"Архивов распаковано: {n_archives_extracted}\n",
        f"В _Unsorted: {stats['disputed']} | В rejected_noise: {stats['rejected_noise']}\n",
        f"Сэкономлено места при сборке (точные дубли не копировались повторно): "
        f"~{stats['bytes_saved_by_dedup'] / 1024**3:.2f} ГБ\n",
        f"Итоговый архив: {n_pool_images} уникальных фото + {n_pool_videos} видео, "
        f"размер {pool_bytes / 1024**3:.2f} ГБ\n",
    ]
    if n_excluded_dirs:
        lines.append(f"Пропущено служебных/системных папок: {n_excluded_dirs} "
                      f"(подробности -- actions.log)\n")
    # 2026-07-11, по запросу пользователя: показывать ВСЕГДА, включая dry_run (пробный
    # прогон CLI/меню) -- сама запись в MERGED_ALBUM_MARKER_FILENAME пропускается при
    # dry_run, но пользователь должен ЗАРАНЕЕ видеть, какие альбомы реально объединятся из
    # нескольких мест, до того как решит собирать по-настоящему. См. _note_album_source().
    merge_events = stats.get("album_merge_events") or []
    if merge_events:
        lines.append("Альбомы, объединённые из нескольких мест в источнике "
                      "(подробности -- в самом альбоме, __ВНИМАНИЕ_объединённая_папка.txt):\n")
        for album, prefix in merge_events:
            lines.append(f"  {album} ← {prefix}\n")
    return "".join(lines)


def run(cfg: Config, log=print):
    """p.5.4б: весь реальный прогон обёрнут TargetLock -- см. его докстринг про TOCTOU-гонку,
    единственную найденную дыру в защите TARGET от параллельных запусков. Исключение --
    cfg.suppress_logs (ТЗ-меню 2026-07-10, раздел 5): интерактивный "пробный прогон" никогда
    не пишет в TARGET (см. _run_impl), поэтому лочить нечего -- LOCK-файл сам по себе создал
    бы __служебные_файлы\\, что suppress_logs как раз обязан НЕ делать."""
    if cfg.suppress_logs:
        return _run_impl(cfg, log=log)
    with TargetLock(cfg.target, log=log):
        return _run_impl(cfg, log=log)


def _run_impl(cfg: Config, log=print):
    run_start = time.monotonic()
    # p.5.3а: stats создаётся ДО отчёта окружения -- report_environment() тоже пишет в него
    # счётчики предупреждений (вложенность TARGET, кросс-volume tmp_extract), которые потом
    # уходят в обогащённый summary.txt вместе со всем остальным.
    stats = {
        "appended_new": 0, "appended_better": 0, "appended_crop": 0, "appended_uncertain": 0,
        "appended_near_dup": 0,  # p.5.7: near-dup image/video, appended (not skipped)
        "skipped_present": 0, "disputed": 0, "raw_mirrored": 0,
        "raw_skipped": 0, "rejected_noise": 0,
        "undated": 0, "bytes_appended": 0,
        # А.4 (итоговая человекочитаемая сводка) -- чистые агрегаты поверх решений выше,
        # никакой новой бизнес-логики не добавляют:
        "appended_images": 0, "appended_videos": 0, "bytes_saved_by_dedup": 0,
        # Security audit finding #5: bytes copied specifically as near-dup (see build_final_summary)
        "bytes_near_dup": 0,
        # p.5.3а: счётчики предупреждений по типам, для обогащённого summary.txt
        "warn_nested_target": 0, "warn_cross_volume_tmp_extract": 0,
        "warn_path_truncated": 0, "warn_tier_c_long_path": 0,
    }

    log("=== Фаза 0: окружение ===")
    if os.path.isdir(winlong(cfg.tmp_extract)) and os.listdir(winlong(cfg.tmp_extract)):
        entries = [n for n in os.listdir(winlong(cfg.tmp_extract)) if n != SKIP_MARKER]
        # Only remove entries that look like our own archive_hash extraction dirs (see
        # _handle_archive()) -- see _OWN_TMP_EXTRACT_ENTRY_RE comment above for why.
        recognized = [n for n in entries if _OWN_TMP_EXTRACT_ENTRY_RE.match(n)]
        unrecognized = [n for n in entries if n not in recognized]
        if recognized:
            log(f"TMP_EXTRACT не пуст — очищаю {len(recognized)} остатков прошлого "
                f"прерванного прогона")
            for name in recognized:
                cleanup_dir(os.path.join(cfg.tmp_extract, name))
        if unrecognized:
            log(f"ВНИМАНИЕ: в TMP_EXTRACT_DIR ({cfg.tmp_extract}) есть {len(unrecognized)} "
                f"файлов/папок, не похожих на собственные временные файлы программы -- "
                f"НЕ трогаю их. Если это чужая папка (например, tmp_extract_dir в photoarchive_config.yaml "
                f"указан по ошибке) -- поправьте настройку. Первые: "
                f"{unrecognized[:5]}")

    if not cfg.suppress_logs:
        ensure_target_layout(cfg)
        check_rules_version(cfg, log=log)
    report_environment(cfg, log=log, stats=stats)
    phase0_end = time.monotonic()

    conn = db_reset(cfg.index_db)

    log("=== Фаза 1: индекс архива (база дедупа) ===")
    index_archive(cfg, conn, log=log)
    pool = build_pool_from_archive_table(conn)
    conn.close()  # не нужен дальше в этом прогоне; важно закрывать явно для --source all,
                  # где run() вызывается многократно в одном процессе на один и тот же work.db
    phase1_end = time.monotonic()

    run_logs = NullRunLogs() if cfg.suppress_logs else RunLogs(cfg.logs)
    date_ctx = DateContext()

    st = _RunState(cfg, pool, date_ctx, run_logs, stats)

    log("=== Фаза 2/2а: обход источника ===")

    processed_count = 0
    unreadable_count = 0
    pending_retry = []  # SourceItem list: read failed 3x but the file persists (not archive-tmp)
                         # -> gets one more attempt at the end of the run.

    # Задача 4: total неизвестен заранее (walker -- генератор, находит файлы и вложенные
    # архивы по ходу обхода) -- бар без total (только счётчик/скорость, без %/ETA, как и
    # предписано: ETA только там, где total известен).
    with ProgressReporter(total=None, desc="Фаза 2-5 — обработка источника", unit="файл") as bar:
        walker = SourceWalker(cfg, log=log, progress_cb=bar.set_context)
        # NB: items are analyzed and placed one at a time (no read-ahead batching).
        # Files extracted from an archive live in TMP_EXTRACT only until that archive's
        # generator scope closes (walker.py cleans up in a `finally` right after its last
        # item is yielded) -- pulling several items ahead into a batch before processing
        # them risks the physical file already being deleted by the time we hash/copy it.
        for item in walker.walk():
            if cfg.sample_limit and processed_count >= cfg.sample_limit:
                break

            note = "хеширование большого видео" if (
                item.ftype == "video" and item.size > 200 * 1024**2) else None
            records = analyze_batch([item], retries=cfg.read_retry_count, retry_delay=cfg.read_retry_delay,
                                     small_image_px=cfg.small_image_px, log=log)

            for rec in records:
                processed_count += 1

                if rec.read_error:
                    if item.read_path.startswith(cfg.tmp_extract + os.sep):
                        # physical file will vanish once this archive's TMP_EXTRACT is cleaned up
                        # -- no later retry is possible, log it now.
                        run_logs.unreadable(item.origin_display, rec.read_error_msg)
                        unreadable_count += 1
                    else:
                        pending_retry.append(item)
                    bar.update(1, note=note)
                    continue

                if _process_record(rec, st, log=log):
                    st.stopped_for_space = True
                    bar.update(1, note=note)
                    break
                bar.update(1, note=note)

            if st.stopped_for_space:
                break

        # Архивные события (extracted/no_media/password_protected/bomb_suspected/...) копятся
        # в walker.archive_logs по ходу walk() -- по завершении обхода переносим их в
        # archives.log (иначе файл существовал бы, но всегда оставался пустым).
        for display, status, note in walker.archive_logs:
            run_logs.archive_event(display, status, note)
        # ТЗ-меню 2026-07-10, раздел 5: "Загляну внутрь: N сжатых файлов" в человеческой
        # сводке пробного прогона -- чистая инструментация поверх уже собранного списка,
        # никакой новой бизнес-логики.
        stats["archives_seen"] = len(walker.archive_logs)

        # 2026-07-11 (сессия про управляемый список служебных папок): раньше пропуски по
        # HARD_EXCLUDE_DIRS/default_exclude_dirs/extra_exclude_dirs и по гейту системных
        # папок не попадали в actions.log вообще (только мимолётный print), а
        # skipped_marker_logs/sidecar_logs копились, но ни разу не читались -- даже
        # промаркированные пропуски терялись безвозвратно. Переносим все четыре источника в
        # actions.log тем же способом, что и archive_logs выше.
        for name, reason, count in walker.excluded_dir_summary():
            run_logs.action(f"[EXCLUDE] {name}: пропущено {count} раз ({reason})")
        for path in walker.system_dir_skips:
            run_logs.action(f"[SYSTEM_DIR] {path}: пропущено (scan_system_dirs=false)")
        for disp in walker.skipped_marker_logs:
            run_logs.action(f"[skip_marker] {disp}")
        for disp in walker.sidecar_logs:
            run_logs.action(f"[sidecar] {disp}")

        if pending_retry and not st.stopped_for_space:
            log(f"Повторное чтение {len(pending_retry)} отложенных файлов в конце прогона...")
            for item in pending_retry:
                records = analyze_batch([item], retries=1, retry_delay=cfg.read_retry_delay,
                                         small_image_px=cfg.small_image_px, log=log)
                rec = records[0]
                if rec.read_error:
                    run_logs.unreadable(item.origin_display, rec.read_error_msg)
                    unreadable_count += 1
                    continue
                if _process_record(rec, st, log=log):
                    st.stopped_for_space = True
                    bar.update(1, note="повтор чтения (диск может быть медленным)")
                    break
                bar.update(1, note="повтор чтения (диск может быть медленным)")

    phase2_end = time.monotonic()

    summary_lines = []
    summary_lines.append(f"\n===== Прогон {time.strftime('%Y-%m-%d %H:%M:%S')} =====\n")
    # p.5.3а: версия/инструменты/тайминги -- ВСЕГДА, без флага debug (для сравнения между
    # тестерами с разными бинарниками в bin/ и разбора багов между релизами). Сырой английский
    # дамп stats ниже по функции -- уже ТОЛЬКО под debug (см. 2026-07-11).
    summary_lines.append(f"PhotoArchive {__version__} (rules {RULES_VERSION}) {__copyright__}, "
                         f"{__license__}\n")
    tool_versions = detect_tool_versions()
    summary_lines.append("Инструменты: " + ", ".join(f"{k}={v}" for k, v in tool_versions.items()) + "\n")
    summary_lines.append(
        f"Тайминги: Фаза 0={phase0_end - run_start:.1f}с, Фаза 1={phase1_end - phase0_end:.1f}с, "
        f"Фаза 2-5={phase2_end - phase1_end:.1f}с, всего={phase2_end - run_start:.1f}с\n"
    )
    summary_lines.append(f"SOURCE: {cfg.source}\n")
    summary_lines.append(f"TARGET: {cfg.target}\n")
    summary_lines.append(f"Обработано элементов источника: {processed_count}\n")
    for k, v in stats.items():
        if k == "bytes_appended":
            summary_lines.append(f"  объём дописанного: {v / 1024**3:.2f} ГБ\n")
        elif cfg.debug:
            # 2026-07-11, по замечанию пользователя: сырой дамп английских ключей
            # внутреннего словаря статистики (appended_new, skipped_present, warn_* и т.п.)
            # был безусловным (без debug) с самого начала (p.5.3а) -- задумывался для
            # сравнения между тестерами/разбора багов, но обычному пользователю смешивал
            # английские имена с русским текстом вокруг, при том что те же цифры уже есть в
            # человекочитаемом "Итог прогона" ниже (build_final_summary). Теперь только под
            # debug=true -- как и остальные [DEBUG]-подробности в actions.log.
            summary_lines.append(f"  {k}: {v}\n")
    if unreadable_count:
        summary_lines.append(f"{unreadable_count} файлов не прочитано — см. unreadable.csv\n")
    free = shutil.disk_usage(winlong(cfg.target)).free
    summary_lines.append(f"Свободно на TARGET по завершении: {free / 1024**3:.2f} ГБ\n")
    if st.stopped_for_space:
        summary_lines.append("ОСТАНОВЛЕНО: недостаточно места на TARGET. Освободите место и запустите снова.\n")
    summary_lines.append(build_final_summary(stats, walker, unreadable_count, pool, processed_count))
    summary_text = "".join(summary_lines)
    run_logs.write_summary(summary_text)
    run_logs.close()

    log(summary_text)
    return stats, processed_count, st.stopped_for_space

# ============================================================================
# STARTUP: конфиг, проверка бандленных бинарников, интерактивный ввод, CLI
# ============================================================================

CONFIG_YAML_PATH = os.path.join(WORKDIR, "photoarchive_config.yaml")

# 2026-07-11: содержимое ДОЛЖНО совпадать с photoarchive_config.yaml.example в корне репозитория --
# ДЕРЖАТЬ ДВА ТЕКСТА В СИНХРОНЕ ВРУЧНУЮ при правке одного из них. photoarchive_config.yaml.example
# остаётся в репозитории как есть (для тех, кто смотрит исходники/собирает сам) -- эта
# константа существует ОТДЕЛЬНО, потому что собранный .exe не имеет доступа к файлам
# репозитория и не может просто скопировать photoarchive_config.yaml.example на диск, см.
# load_yaml_config() ниже.
DEFAULT_CONFIG_YAML_TEMPLATE = """\
# photo-sort-win: необязательный файл расширенных настроек.
# Скопируйте в photoarchive_config.yaml (в ту же папку, где лежит PhotoArchive.exe / photosort_win.py) и
# раскомментируйте/поправьте нужное -- если файла нет, используются значения по умолчанию
# (те же, что показаны здесь).
#
# source/target/dry-run/sample-limit сюда НЕ входят -- они всегда задаются через CLI-флаги
# (--source/--target/--dry-run/--sample-limit) или интерактивный ввод при запуске.
# Незнакомые ключи в этом файле игнорируются с предупреждением в лог.
#
# Этот файл был автоматически создан программой при первом запуске (photoarchive_config.yaml не
# существовал) -- полностью закомментирован, ни на что не влияет, пока вы не раскомментируете
# нужные строки.

# place_lookup: offline         # город из geotag через reverse_geocoder; off = без места
# home_country: RU              # в своей стране пишем только город, за рубежом "Город, Страна"
# archive_hash_cache: true      # true (по умолчанию) = кэш хешей архива по (path,size,mtime) для
                                # ускорения повторных прогонов на растущем архиве; false = всегда
                                # пересчитывать всё заново (медленнее, но нечувствительно к
                                # теоретической коллизии path+size+mtime при разном содержимом)
# max_archive_depth: 8          # потолок вложенности архив-в-архиве
# max_dest_path: 240            # символов на сегмент пути (плюс жёсткий лимит 255 байт UTF-8)
# small_image_px: 640           # граница "маленького, но не иконки" фото
# free_space_margin_gb: 10.0
# read_retry_count: 3           # попыток прочитать файл источника перед отложенным повтором
# read_retry_delay: 5.0         # секунд между попытками

# bydate_granularity: month     # day | month | year | flat -- гранулярность папок ByDate
#   day:                  ByDate/2019/2019-07-15 Москва/
#   month (по умолчанию): ByDate/2019/2019-07 Москва/
#   year:                 ByDate/2019/
#   flat:                 ByDate/   (все дампы одной кучей, без подпапок по дате)
#   Смена этой настройки НЕ переименовывает уже собранные папки (архив append-only) --
#   касается только новых файлов, дописываемых после смены.

# scan_system_dirs: false       # заходить ли при рекурсии в системные папки (WINDIR,
                                # ProgramFiles, ProgramFiles(x86), ProgramData, LOCALAPPDATA,
                                # APPDATA, TEMP -- определяются через переменные окружения).
                                # false (по умолчанию) экономит время на заведомо не-фото
                                # системных деревьях при SOURCE=C:\\ целиком. Явно указанный
                                # SOURCE вглубь такой папки обрабатывается всегда, флаг не
                                # мешает (см. README.md, раздел "Зоны доверия и системные папки").
# default_exclude_dirs: [node_modules, .git, "$recycle.bin"]
                                # редактируемый список папок, пропускаемых по умолчанию при
                                # рекурсии -- это ЭВРИСТИКА ("скорее всего не фото"), а не
                                # защита ОС, поэтому можно менять свободно. Например, уберите
                                # "$recycle.bin", если хотите, чтобы программа заглянула в
                                # Корзину и попыталась спасти удалённые фото (каждый диск
                                # хранит там подпапку, принадлежащую именно вашей учётной
                                # записи -- прав администратора для чтения не нужно).
                                # НЕ входят и не настраиваются через этот список: папки,
                                # которые программа пропускает БЕЗУСЛОВНО, потому что реально
                                # недостижимы ни для кого (System Volume Information,
                                # Default/Default User, Мои видеозаписи/Моя музыка/мои
                                # рисунки и их англ. варианты, __служебные_файлы) -- см.
                                # README.md, раздел "Зоны доверия и системные папки".
# extra_exclude_dirs: []        # доп. имена папок, которые пропускать при рекурсии, ПОВЕРХ
                                # default_exclude_dirs выше -- например [MyBackupTool, OldSync]

# dump_segment_names: [dcim, camera, "camera uploads", фотокамера, photostream, моменты,
#   screenshots, скриншоты, downloads, загрузки, "saved pictures", pictures, изображения,
#   фотопленка, users, home, desktop, "рабочий стол", "camera roll"]
                                # редактируемый список ИМЁН папок (без учёта регистра), которые
                                # считаются "не альбом, скорее всего служебная/авто-папка" при
                                # выборе, как назвать альбом -- см. README.md, раздел
                                # "Маршрутизация: альбом или по дате". НЕ входят и не настраиваются через этот
                                # список: bydate/albums/raw/_unsorted -- эти четыре защищают
                                # уже собранный архив от самопоедания при повторном прогоне
                                # (SOURCE = уже готовый архив) и всегда учитываются программой
                                # независимо от этого списка.
# extra_dump_segment_names: []  # доп. имена ПОВЕРХ dump_segment_names -- например [YandexDisk]
# dump_segment_prefixes: [whatsapp, telegram]
                                # редактируемый список ПРЕФИКСОВ имени папки (без учёта
                                # регистра) -- совпадает по смыслу с dump_segment_names, но
                                # проверяет НАЧАЛО имени, а не имя целиком (например
                                # "WhatsApp Images" тоже совпадёт с "whatsapp").
# extra_dump_segment_prefixes: [] # доп. префиксы ПОВЕРХ dump_segment_prefixes

# mirror_raw: true              # false = избыточный RAW (есть парный JPEG уже в основном
                                # архиве) не копировать в RAW-зеркало (лог
                                # raw_skipped_has_jpeg); одинокий RAW (без JPEG) мирроится
                                # ВСЕГДА независимо от этого флага -- единственный носитель
                                # кадра никогда не пропускается молча.

# raw_layout: mirror            # mirror (по умолчанию) | sibling -- где физически лежит
                                # RAW-зеркало (flat сознательно не сделан -- см. README.md).
                                #   mirror  -- "Семейный архив (RAW отдельно)": отдельный
                                #     корень RAW\\, зеркалящий структуру основного архива
                                #     (RAW\\Albums\\..., RAW\\ByDate\\YYYY\\...).
                                #   sibling -- "Режим фотографа (RAW рядом)": подпапка RAW\\
                                #     рядом с самим кадром -- Albums\\Море 2015\\RAW\\IMG.CR2.
                                # Одинокий RAW спасается всегда независимо от этого значения.

# tmp_extract_dir: null          # null (по умолчанию) = {TARGET}\\__служебные_файлы\\tmp_extract\\
                                # -- подпапка самого TARGET, а не системный %TEMP%, поэтому
                                # уже гарантированно на том же физическом ТОМЕ, что и весь
                                # архив. На этом же томе финализация файлов из архива --
                                # мгновенный rename, а не копирование. Можно переопределить
                                # своим путём (например, если TARGET на медленном/сетевом
                                # диске, а быстрый SSD -- на другой букве); при другом томе
                                # финализация автоматически деградирует до обычного
                                # копирования с hash-verify.

# debug: false                   # true = подробные [DEBUG]-строки в __служебные_файлы\\logs\\
                                # actions.log -- причина каждого решения (альбом/dump,
                                # near-dup, tier/evidence даты) и полный traceback на
                                # ошибках вместо короткого сообщения. Для тестеров/разбора
                                # багов между релизами -- summary.txt при этом обогащён
                                # ВСЕГДА, независимо от этого флага (версии, тайминги,
                                # предупреждения). ВАЖНО: actions.log не ротируется по типу
                                # строки -- разово включённый debug оставит подробные строки
                                # вперемешку с обычными во всех будущих прогонах того же
                                # архива (см. README.md).

# ============================================================
# СПРАВОЧНО, НЕ РЕДАКТИРУЕТСЯ -- ниже НЕ настройки этого файла, а список того, что реально
# влияет на раскладку "альбом или по датам", но зашито в коде и не выведено в config.yaml.
# Точные значения -- в исходном коде photosort_win.py (открыт, см. README.md, раздел
# "Лицензия"), полное описание -- в README.md.
# ============================================================
#
# DUMP_SEGMENT_NAMES_PROTECTED = bydate, albums, raw, _unsorted
#   Эти четыре имени никогда не могут стать названием альбома -- защита уже собранного
#   архива от самопоедания, если SOURCE указывает на уже готовый TARGET ("каскадный" повторный
#   прогон). Добавляются к dump_segment_names безусловно, убрать их через этот файл нельзя.
#
# DUMP_SEGMENT_REGEXES (по regex, без учёта регистра):
#   - ^\\d{3}[A-Za-z]+$          имя вида "100CANON"/"101MSDCF" (стандартный DCIM-формат камер)
#   - ^новая папка(\\s\\(\\d+\\))?$ / ^new folder(\\s\\(\\d+\\))?$
#                               неизменённое имя новой папки Windows, включая нумерованные
#                               дубли ("Новая папка (2)")
#
# DUMP_SEGMENT_DATE_REGEX = ^\\d{6,8}$
#   Голая 6-8-значная папка-дата (YYYYMMDD/YYMMDD, например "20240802") -- dump ТОЛЬКО когда
#   решается, какой сегмент даёт имя альбому. Если альбом уже найден выше по пути, такая же
#   папка внутри него не выбрасывается -- считается осмысленной группировкой по дню съёмки.
#
# DUMP_TAG = " [PhotoArchive]"
#   Суффикс, добавляемый к каждой ByDate-папке (день/месяц/"дата неизвестна") -- отличает наши
#   автосозданные папки от одноимённых, которые пользователь мог создать вручную.
#
# FORCE_DUMP_PREFIX = "~"
#   Переименуйте папку-источник вручную в "~ИмяПапки" -- программа всегда посчитает её dump
#   (сортировка по дате), даже если имя иначе выглядело бы как настоящий альбом. Полезно для
#   папок облачной синхронизации ("Яндекс.Диск") или решения постфактум "эта папка избыточна".
#   Программа никогда не переименовывает исходники сама -- только читает.
#
# Архив как альбом (zip/rar/7z/tar внутри SOURCE):
#   find_album() ищет имя альбома в две фазы: сначала как обычно по сегментам пути на диске
#   до архива; если там ничего не нашлось (весь путь -- dump-сегменты), собственное имя ФАЙЛА
#   архива (без расширения) становится именем альбома -- имена папок внутри распакованного
#   архива никогда не используются как источник имени сами по себе.
#
# Якорь альбома (изменено 2026-07-12, RULES_VERSION):
#   find_album() выбирает САМЫЙ ВЕРХНИЙ (ближайший к корню SOURCE конкретного прогона) не-dump
#   сегмент пути с буквами, а не самый глубокий (как было до этой версии). Всё, что лежит
#   глубже якоря, сохраняется как вложенный subpath без изменений -- "Отпуск 2015\\Море"
#   становится "Albums\\Отпуск 2015\\Море\\", а не "Albums\\Море\\" (потеря внешнего уровня
#   вложенности была причиной фрагментации на реальном архиве пользователя). Соседние альбомы
#   под общим родителем ("Фото\\Свадьба" и "Фото\\Отпуск_2015") НЕ склеиваются в один -- вся
#   цепочка сегментов ниже якоря сохраняется как есть, а не сворачивается в одно имя.
"""

# поля Config, которые можно переопределить через photoarchive_config.yaml -- сознательно НЕ включает
# source/target/dry_run/sample_limit: они всегда приходят из CLI/интерактивного ввода
CONFIG_YAML_FIELDS = {
    "layout", "date_mode", "place_lookup", "home_country", "archive_hash_cache",
    "max_archive_depth", "max_dest_path", "small_image_px", "free_space_margin_gb",
    "read_retry_count", "read_retry_delay", "bydate_granularity",
    "scan_system_dirs", "default_exclude_dirs", "extra_exclude_dirs", "mirror_raw",
    "tmp_extract_dir", "raw_layout", "debug",
    "dump_segment_names", "extra_dump_segment_names",
    "dump_segment_prefixes", "extra_dump_segment_prefixes",
}


def _ensure_config_yaml_exists(path: str, log=print) -> None:
    """2026-07-11: если файла нет, best-effort создаём его из DEFAULT_CONFIG_YAML_TEMPLATE --
    полностью закомментирован, ни на что не влияет сам по себе, но избавляет пользователя от
    необходимости искать/копировать photoarchive_config.yaml.example (у собранного .exe нет доступа к
    файлам репозитория). Если запись не удалась (папка только для чтения, права доступа и
    т.п.) -- не фатально, просто предупреждение в лог, работаем на дефолтах как раньше.

    Вынесено ОТДЕЛЬНО от load_yaml_config() (2026-07-11, по живой находке пользователя):
    голый запуск .exe, прерванный Ctrl-C на самом первом вопросе меню (`run_bare_launch()`)
    или на первом интерактивном "Откуда"/"Куда" (частичный CLI), никогда не доходит до
    Config()/run_for_source() -- туда, где load_yaml_config() раньше вызывался впервые --
    поэтому photoarchive_config.yaml не успевал появиться, хотя пользователю он нужен сразу (админы читают
    конфиг, не документацию). Теперь вызывается САМОЙ ПЕРВОЙ строкой и в run_bare_launch(), и
    в интерактивной ветке _main(), до единственного input()."""
    if os.path.exists(path):
        return
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(DEFAULT_CONFIG_YAML_TEMPLATE)
        log(f"photoarchive_config.yaml не найден -- создан по умолчанию ({path}), "
            f"полностью закомментирован (ни на что не влияет, пока не отредактируете)")
    except OSError as e:
        log(f"ВНИМАНИЕ: не удалось создать photoarchive_config.yaml по умолчанию ({path}): {e} -- "
            f"работаем на встроенных значениях по умолчанию")


def load_yaml_config(path: str, log=print) -> dict:
    """Необязательный файл с расширенными настройками (см. photoarchive_config.yaml.example) --
    единственный способ поменять их без правки photosort.py. CLI/интерактивный ввод
    (source/target/dry-run/sample-limit) всегда важнее и сюда не относится.

    Если файла нет -- сначала пытается его создать (см. _ensure_config_yaml_exists()), затем
    как обычно возвращает {} (только что записанный файл целиком закомментирован, override'ов
    нет)."""
    _ensure_config_yaml_exists(path, log=log)
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        log(f"ВНИМАНИЕ: {path} должен быть словарём ключ: значение, содержимое проигнорировано")
        return {}
    unknown = set(data) - CONFIG_YAML_FIELDS
    if unknown:
        log(f"ВНИМАНИЕ: ключи {', '.join(sorted(unknown))} в {path} не настраиваются через "
            f"YAML (проигнорированы) -- см. photoarchive_config.yaml.example")
    return {k: v for k, v in data.items() if k in CONFIG_YAML_FIELDS}


def check_bundled_tools(log=print):
    """Проверка, что бандленные внешние бинарники реально доступны -- защита от битой
    сборки (недостающий exiftool.exe/ffmpeg.exe и т.п. в bin/), а не проверка системных
    пакетов, как в Linux-оригинале (там PyInstaller-сборки не было, зависимости ставились
    через apt/pip на лету). Здесь всё уже должно быть внутри .exe/рядом с ним."""
    missing = []
    for label, path in (
        ("exiftool", EXIFTOOL_BIN), ("ffprobe", FFPROBE_BIN), ("ffmpeg", FFMPEG_BIN),
        ("7z", SEVENZIP_BIN), ("unrar", UNRAR_BIN),
    ):
        if os.path.isabs(path) and not os.path.exists(path):
            missing.append(f"{label} (ожидался в {path})")
    if missing:
        log("ВНИМАНИЕ: не найдены бандленные внешние инструменты:\n  " + "\n  ".join(missing) +
            "\nСборка повреждена или собрана без --add-binary для этих файлов. "
            "Функции, зависящие от них (EXIF-даты, видео, 7z/rar-архивы), будут падать в лог ошибок.")


def _detect_tool_version(binary: str, args: list) -> str:
    """p.5.3а: real installed version of a bundled external tool, for summary.txt -- у
    разных бета-тестеров разные конкретные бинарники в bin/ (см. bin/README-BIN.md), так
    что версия из кода (EXIFTOOL_BIN и т.п. -- это только ПУТЬ) не то же самое, что версия
    самого бинарника. Best-effort: first non-empty line of stdout/stderr, or "?" if the
    binary is missing/times out (mirrors check_bundled_tools() not treating this as fatal)."""
    try:
        out = subprocess.run([binary] + args, capture_output=True, timeout=10)
    except Exception:
        return "?"
    text = (out.stdout or b"").decode("utf-8", "replace") + (out.stderr or b"").decode("utf-8", "replace")
    for line in text.splitlines():
        line = line.strip()
        if line:
            return line
    return "?"


def detect_tool_versions() -> dict:
    return {
        "exiftool": _detect_tool_version(EXIFTOOL_BIN, ["-ver"]),
        "7z": _detect_tool_version(SEVENZIP_BIN, []),
        "ffmpeg": _detect_tool_version(FFMPEG_BIN, ["-version"]),
        "ffprobe": _detect_tool_version(FFPROBE_BIN, ["-version"]),
        "unrar": _detect_tool_version(UNRAR_BIN, []),
    }


def _strip_surrounding_quotes(path: str) -> str:
    """Windows при перетаскивании папки мышкой в консоль вставляет путь в двойных кавычках
    (`"C:\\Users\\Мама\\Фото"`) -- одного .strip() недостаточно, кавычки остаются частью пути
    и валят любую проверку существования папки ниже по конвейеру."""
    if len(path) >= 2 and path[0] == '"' and path[-1] == '"':
        return path[1:-1].strip()
    return path


def interactive_input(input_fn=input):
    # p.5.4: голый английский жаргон "SOURCE"/"TARGET" смущает нетехнического пользователя в
    # интерактивном вводе -- переведено на русский без этих слов (согласовано с пользователем,
    # см. SESSION-HANDOFF.txt). CLI-флаги --source/--target и photoarchive_config.yaml НЕ переименованы --
    # это контракт для технических пользователей/скриптов, менять его отдельный вопрос.
    source = input_fn(
        "Откуда брать фото (папка, диск, файл-архив.zip, или all — все диски; "
        "можно перетащить папку сюда мышкой): "
    ).strip()
    target = input_fn(
        "Куда сложить архив (папка; можно перетащить папку сюда мышкой): "
    ).strip()
    return _strip_surrounding_quotes(source), _strip_surrounding_quotes(target)


TARGET_OWN_STRUCTURE_NAMES = {"__служебные_файлы", "_unsorted", "albums", "bydate", "raw"}


def _target_needs_confirmation(target: str) -> bool:
    """p.5.4а: риск-пропорциональная проверка ИМЕННО TARGET -- три случая:
    1. TARGET не существует, или существует, но пустая папка -> без подтверждения (ошибиться
       некритично, пустую папку потом просто удалить).
    2. TARGET существует и содержит ТОЛЬКО нашу собственную структуру (__служебные_файлы/, Albums/,
       ByDate/, RAW/, ничего больше на верхнем уровне) -> тоже без подтверждения -- штатное
       повторное пополнение уже существующего архива, самый частый сценарий (пугать здесь --
       приучить нажимать "да" не глядя именно там, где оно реально нужно).
    3. TARGET существует и содержит ЧТО-ТО ЕЩЁ (похоже на чужую папку, не на наш архив) ->
       нужно подтверждение.
    Возвращает True только для случая 3."""
    real_target = winlong(target)
    if not os.path.isdir(real_target):
        return False
    try:
        entries = os.listdir(real_target)
    except OSError:
        return False
    if not entries:
        return False
    return not all(e.lower() in TARGET_OWN_STRUCTURE_NAMES for e in entries)


def confirm_target_interactively(target: str, input_fn=input, log=print) -> bool:
    """True, если можно продолжать. Строгое подтверждение (ввод слова «да», не просто Enter,
    чтобы не проскакивало на автомате) -- только для случая 3 из _target_needs_confirmation().
    Вызывать ТОЛЬКО из интерактивного пути (main()) -- явные --source/--target из CLI никогда
    не показывают это подтверждение, это осознанное действие технического пользователя/
    скрипта, не должно ломать автоматизацию (согласовано с пользователем, p.5.4)."""
    if not _target_needs_confirmation(target):
        return True
    answer = input_fn(
        f"В папке {target} уже что-то есть — новые файлы будут добавлены туда же. "
        f"Продолжить? (введите «да»): "
    ).strip().lower()
    if answer != "да":
        log("Отменено пользователем.")
        return False
    return True


def _normalize_bare_drive_letter(path: str) -> str:
    """'C:' (голая буква диска БЕЗ обратного слеша) -- в терминах Windows это "drive-relative"
    путь, а НЕ полный путь: он неоднозначен сам по себе (зависит от текущей директории
    процесса именно на этом диске, os.path.isabs('C:') == False) -- удобный для пользователя
    короткий ввод ("источник C:, архив D:"), но без нормализации был бы отклонён проверкой
    полного пути (p.5.9) с малопонятной для нетехнического пользователя ошибкой. Нормализуем
    ТОЛЬКО эту конкретную двухсимвольную форму ('C:', любая буква) в однозначный корень
    ('C:\\') -- любая более длинная форма ('C:Фото' без слеша -- тоже реальная
    Windows-неоднозначность) НЕ трогаем, там угадывать нельзя, пусть проверка полного пути
    отклонит её как есть."""
    if len(path) == 2 and path[1] == ":" and path[0].isalpha():
        return path + "\\"
    return path


def _is_bare_drive_root(target: str) -> bool:
    """True, если TARGET указывает не на конкретную папку, а на корень тома целиком (голый
    'D:\\'/'D:', без единой вложенной папки) -- у корня os.path.dirname(path) совпадает с
    самим path (родителя нет), у любой настоящей вложенной папки -- нет. Инвариант работает
    одинаково на Windows ('D:\\') и POSIX ('/', актуально только для dev/теста на Linux, где
    буквы дисков как понятие не существуют)."""
    normalized = os.path.abspath(target)
    return os.path.dirname(normalized) == normalized


def confirm_drive_root_target_interactively(target: str, input_fn=input, log=print) -> str:
    """Если TARGET -- голый корень диска (см. _is_bare_drive_root), спрашивает пользователя,
    добавить ли к пути папку PhotoArchive -- иначе весь __служебные_файлы\\/Albums\\/ByDate\\/RAW лёг
    бы прямо в корень тома, что не всегда красиво. Один и тот же бинарный вопрос ("добавить имя
    папки к диску или нет"), но формулировка зависит от того, существует ли PhotoArchive уже:
    при повторном прогоне на тот же диск (дозапись в уже существующий архив) вопрос "создать
    папку?" был бы вводящим в заблуждение -- она уже есть, предлагаем её ИСПОЛЬЗОВАТЬ, а не
    создать заново. В отличие от confirm_target_interactively(), отказ НЕ отменяет прогон --
    пользователь может осознанно писать в корень (например выделенный под архив внешний диск,
    где лишняя вложенная папка не нужна) -- просто оставляет TARGET как есть. Возвращает
    (возможно изменённый) TARGET. Вызывать ТОЛЬКО из интерактивного пути (main()), только для
    archive (analyze-* ничего не пишет в TARGET) -- явные --target из CLI/photoarchive_config.yaml никогда
    не показывают этот вопрос и пишут в корень как есть (согласовано с пользователем
    2026-07-10, по аналогии с confirm_target_interactively -- явный ввод для скриптов/
    автоматизации не должен неожиданно перенаправляться)."""
    if not _is_bare_drive_root(target):
        return target
    photoarchive_dir = os.path.join(target, "PhotoArchive")
    if os.path.isdir(winlong(photoarchive_dir)):
        prompt = (
            f"В корне диска ({target}) уже есть папка PhotoArchive — использовать её для "
            f"архива? Если нет — архив будет собран прямо в корне диска. (да/нет): "
        )
    else:
        prompt = (
            f"TARGET указан как корень диска ({target}) — создать в нём папку PhotoArchive и "
            f"архивировать туда? Если нет — архив будет собран прямо в корне диска. (да/нет): "
        )
    answer = input_fn(prompt).strip().lower()
    if answer == "да":
        return photoarchive_dir
    return target


def resolve_drive_root_conflict(sources: list, target: str, interactive: bool,
                                 input_fn=input, log=print) -> str:
    """Единая точка разрешения TARGET, когда он указан как голый корень диска (см.
    _is_bare_drive_root). Два разных случая -- НЕ путать выбор с вынужденной необходимостью:

    1. TARGET (по realpath) совпадает с ОДНИМ ИЗ sources -- частый паттерн у нетехнического
       пользователя ("источник C:, архив тоже C:" -- ожидает получить папку с архивом на этом
       же диске из фотографий этого диска). Собрать архив ПРЯМО в этот же корень нельзя в
       принципе (самопоедание -- прогон читал бы собственную запись как источник;
       Config.__post_init__ всё равно отклонил бы это с ошибкой "SOURCE и TARGET совпадают").
       Единственное разумное разрешение здесь одно, а не выбор из вариантов -- поэтому НИЧЕГО
       не спрашиваем (даже в интерактиве, где обычно спрашивают) и просто подставляем
       {TARGET}\\PhotoArchive с информационной строкой в лог. Работает ОДИНАКОВО для
       CLI/photoarchive_config.yaml и интерактивного ввода -- иначе `--source C:\\ --target C:\\` в
       скрипте/автоматизации просто упал бы с ошибкой конфигурации, хотя намерение
       однозначно читается из самого ввода. SOURCE=all считается тем же случаем, если TARGET
       -- голый корень диска: expand_sources() больше не исключает диск TARGET (см. его
       докстринг), так что "all" гарантированно развернётся в т.ч. и в сам этот корень.
    2. TARGET -- голый корень диска, но НЕ совпадает ни с одним source -- настоящий выбор
       (создать PhotoArchive\\ или писать прямо в корень), см.
       confirm_drive_root_target_interactively() -- но ТОЛЬКО в интерактиве, явные
       --target из CLI/photoarchive_config.yaml пишут в корень как есть без вопросов (как и раньше).

    Возвращает (возможно изменённый) TARGET."""
    if not _is_bare_drive_root(target):
        return target
    target_real = os.path.normcase(os.path.realpath(os.path.abspath(target)))
    conflicts = any(
        s.strip().lower() == "all"
        or os.path.normcase(os.path.realpath(os.path.abspath(s))) == target_real
        for s in sources
    )
    if conflicts:
        redirected = os.path.join(target, "PhotoArchive")
        log(f"TARGET указан так же, как и один из источников ({target}) -- архив будет "
            f"собран в {redirected}, а не в самом корне диска (иначе прогон читал бы "
            f"собственную же запись как источник).")
        return redirected
    if interactive:
        return confirm_drive_root_target_interactively(target, input_fn=input_fn, log=log)
    return target


def local_drive_roots():
    """Список локальных дисков Windows (C:\\, D:\\, ...) для SOURCE=all. На не-Windows
    (dev-запуск на Linux) возвращает пустой список -- там "all" не имеет смысла без
    /mnt/win-конвенции оригинала, которую portable-версия больше не использует."""
    if os.name != "nt":
        return []
    import string
    return [f"{letter}:\\" for letter in string.ascii_uppercase if os.path.exists(f"{letter}:\\")]


# ROADMAP.md "Коды возврата PhotoArchive.exe не отражают неудачу": до этой правки TargetLocked/
# ошибка конфигурации/InsufficientSpace все давали process exit code 0, неотличимо от
# настоящего успеха для скрипта автоматизации, проверяющего %ERRORLEVEL%. 1 уже занят под
# неожиданный краш (_log_unexpected_crash), 130 -- под Ctrl+C/EOF (main()) -- три новых кода
# ниже намеренно не пересекаются ни с тем, ни с другим.
EXIT_TARGET_LOCKED = 2
EXIT_CONFIG_ERROR = 3
EXIT_INSUFFICIENT_SPACE = 4


@dataclass
class RunResult:
    """Возврат run_for_source() для одного SOURCE. failed=True -- для этого SOURCE вообще
    ничего не обработалось (TargetLocked/ошибка конфигурации); интерактивные вызывающие
    (run_bare_launch()) трактуют failed так же, как раньше трактовали голый None. exit_code --
    то, что CLI-путь (_main()) в конце аггрегирует по всем источникам в единственный process
    exit code; для успешного прогона, который тем не менее остановился раньше времени
    (stopped_for_space), failed остаётся False (что-то реально скопировалось), но exit_code
    всё равно ненулевой -- эти два поля отвечают на разные вопросы, не дублируют друг друга."""
    failed: bool
    exit_code: int = 0
    stats: dict = None
    processed_count: int = 0
    stopped_for_space: bool = False


def run_for_source(source, target, dry_run, sample_limit, log=print, suppress_logs=False) -> RunResult:
    yaml_overrides = load_yaml_config(CONFIG_YAML_PATH, log=log)
    try:
        cfg = Config(source=source, target=target, dry_run=dry_run, sample_limit=sample_limit,
                     suppress_logs=suppress_logs, **yaml_overrides)
    except ValueError as e:
        log(f"ОШИБКА КОНФИГУРАЦИИ: {e}")
        return RunResult(failed=True, exit_code=EXIT_CONFIG_ERROR)
    try:
        stats, processed_count, stopped_for_space = run(cfg, log=log)
    except TargetLocked as e:
        log(f"ОШИБКА: {e}")
        return RunResult(failed=True, exit_code=EXIT_TARGET_LOCKED)
    exit_code = EXIT_INSUFFICIENT_SPACE if stopped_for_space else 0
    return RunResult(failed=False, exit_code=exit_code, stats=stats,
                      processed_count=processed_count, stopped_for_space=stopped_for_space)


def run_analyze_for_source(source, target, sample_limit, mode, log=print):
    """А.2: аналог run_for_source() для analyze-режимов -- собирает Config (dry_run/logs
    сюда не относятся, analyze-режимы никогда не пишут в TARGET), считает AnalyzeStats,
    печатает человекочитаемый отчёт и сохраняет машинный analyze_report.csv в WORKDIR."""
    yaml_overrides = load_yaml_config(CONFIG_YAML_PATH, log=log)
    try:
        cfg = Config(source=source, target=target, sample_limit=sample_limit, **yaml_overrides)
    except ValueError as e:
        log(f"ОШИБКА КОНФИГУРАЦИИ: {e}")
        return None
    stats = run_analyze(cfg, mode, log=log)
    print_analyze_report(stats, log=log)
    report_path = os.path.join(cfg.workdir, "analyze_report.csv")
    write_analyze_report_csv(report_path, stats)
    log(f"Машинный отчёт: {report_path}")
    return stats


def resolve_sources(args) -> list:
    """--source может повторяться, --source-list добавляет пути построчно (пустые строки
    и строки, начинающиеся с #, игнорируются). Оба механизма складываются в один список."""
    sources = list(args.source) if args.source else []
    if args.source_list:
        with open(args.source_list, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    sources.append(line)
    return sources


ANALYZE_MODES = ("analyze-quick", "analyze", "analyze-full")
CLI_MODES = ("archive",) + ANALYZE_MODES


def _add_common_source_args(p: argparse.ArgumentParser):
    p.add_argument("--source", action="append", default=None,
                    help="источник; флаг можно повторять для нескольких источников за один запуск")
    p.add_argument("--source-list", default=None,
                    help="файл со списком SOURCE, по одному пути на строку")
    p.add_argument("--target", default=None)
    p.add_argument("--sample-limit", type=int, default=0,
                    help="не более N файлов источника (быстрый тест на малой выборке)")


class _FormatsAction(argparse.Action):
    """Как встроенный action="version": печатает и выходит сразу при разборе, не дожидаясь
    проверки required=True на subparsers (--formats задаётся без подкоманды)."""

    def __call__(self, parser, namespace, values, option_string=None):
        print(format_formats_report())
        parser.exit()


def build_arg_parser() -> argparse.ArgumentParser:
    """Подкоманды: archive (по умолчанию, поведение как раньше) + три read-only
    analyze-режима (А.2, см. RULES.md) -- НЕ флаг DRY_RUN, отдельные подкоманды."""
    parser = argparse.ArgumentParser(
        description="PhotoArchive -- сборщик семейного фото- и видеоархива (см. README)",
        epilog=f"Репозиторий проекта: {GITHUB_URL}\n\n{DONATION_TEXT}",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--version", "-V", action="version",
                         version=f"PhotoArchive {__version__} -- Сборщик семейного фото- и "
                                  f"видеоархива (rules {RULES_VERSION})\n"
                                  f"{__copyright__}, {__license__} -- сторонние "
                                  f"компоненты: см. THIRD_PARTY_LICENSES")
    parser.add_argument("--formats", action=_FormatsAction, nargs=0,
                         help="показать распознаваемые расширения файлов и выйти")
    subparsers = parser.add_subparsers(dest="mode", required=True)

    p_archive = subparsers.add_parser("archive", help="собрать архив (поведение по умолчанию)")
    _add_common_source_args(p_archive)
    p_archive.add_argument("--dry-run", action="store_true",
                            help="прогнать все решения БЕЗ копирования; в отличие от analyze-* "
                                 "всё же пишет обычные __служебные_файлы\\logs\\*.csv в TARGET")

    analyze_help = {
        "analyze-quick": "быстрая read-only диагностика источника: только метаданные, без SHA/pHash",
        "analyze": "+ полный проход хеширования: точные и near-дубликаты ВНУТРИ источника",
        "analyze-full": "+ сверка с существующим TARGET: что уже спасено, что новое, поместится ли",
    }
    for mode in ANALYZE_MODES:
        p = subparsers.add_parser(mode, help=analyze_help[mode])
        _add_common_source_args(p)

    return parser


def expand_sources(sources: list, target: str) -> list:
    """SOURCE=all -> все локальные диски (см. local_drive_roots()), включая диск TARGET.
    Диск TARGET сознательно НЕ исключается: самопоедание уже предотвращено на уровне пути
    самой SourceWalker._walk_dir (обрывает рекурсию, дойдя до TARGET, до спуска внутрь --
    см. RULES.md), а не на уровне "весь диск целиком". Исключение диска TARGET раньше было
    унаследовано из старой Linux/SMB-версии (там "all" значило "все примонтированные
    /mnt/win/*") и на практике тихо теряло фото, лежащие на диске TARGET вне папки архива."""
    expanded = []
    for s in sources:
        if s.strip().lower() == "all":
            expanded.extend(local_drive_roots())
        else:
            expanded.append(s)
    return expanded


BARE_LAUNCH_MENU_CHOICES = {
    "1": "view",
    "2": "dry_run",
    "3": "build",
    "": "view",  # Enter без ввода -> безопасный дефолт [1], ничего не трогает
    # 2026-07-12: "0" сознательно НЕ отображается: на самом главном меню возвращаться в
    # главное меню некуда (см. prompt_bare_launch_menu()) -- если ввести "0" здесь, это
    # просто невалидный ввод, как и любая другая нераспознанная строка.
}

_MENU_BACK = object()  # sentinel: "0" в подменю с allow_back=True -- в главное меню, не выход

_VIEW_MODE_PLACEHOLDER_TARGET = os.path.join(tempfile.gettempdir(), "PhotoArchive_view_placeholder")


def _progress_note_budget(min_width: int = 20, reserve: int = 80) -> int:
    """2026-07-12, живой репорт пользователя: "самые длинные строки при распаковке архива...
    не помещаются даже на полном экране". Старый фиксированный maxlen=60 в
    _truncate_progress_note() не учитывал ни реальную ширину терминала, ни то, что рядом с
    note в той же самой строке tqdm ещё печатает префикс ("Фаза N — текст — ") и хвостовые
    счётчики (обработано/скорость/ETA) -- в сумме легко вылезало за пределы экрана ЛЮБОЙ
    ширины, не только узкой. tqdm сам НЕ обрезает desc (dynamic_ncols управляет только
    собственно полосой прогресса `{bar}`), так что бюджет под note приходится считать здесь.
    reserve -- грубая (не посимвольно точная -- note обрезается по вызову ДО того, как ProgressReporter
    вообще знает итоговый desc) оценка веса всего остального: самый длинный реальный префикс
    ("Фаза 2-5 — обработка источника — " ~33 симв.) плюс типичный tqdm-хвост
    ("XXX [MM:SS, N.Nфайл/с]" ~40+ симв.)."""
    if not sys.stderr.isatty():
        return 60  # нет реального терминала (файл/пайп) -- поведение как было до этой правки
    columns = shutil.get_terminal_size(fallback=(80, 24)).columns
    return max(min_width, columns - reserve)


def _truncate_progress_note(text: str, maxlen: int = None) -> str:
    """2026-07-11, user feedback: unlike _display_path() below (center-truncation, keeps
    head+tail to disambiguate between similarly-named full paths in one-off messages), a
    progress-bar "programm сейчас здесь копается" note only needs the END of the path -- the
    part that actually changes as the walk descends deeper -- the drive/source root at the
    start is already shown once elsewhere (the SOURCE: ... banner line) and would just be
    dead weight repeated on every directory. Leading "…" makes clear the start was cut, not
    that this is the whole (suspiciously short-looking) path.

    2026-07-12: maxlen defaults to _progress_note_budget() (real terminal width minus a
    reserve for the rest of the tqdm line) instead of a flat 60 -- see that function's
    docstring for why."""
    if maxlen is None:
        maxlen = _progress_note_budget()
    if len(text) <= maxlen:
        return text
    return "…" + text[-(maxlen - 1):]


def _display_path(path: str, maxlen: int = 60) -> str:
    """ТЗ-меню 2026-07-10, раздел 0: длинные пути в интерактивном выводе -- обрезка по
    центру с '...', показывает начало (диск/корень) и конец (то, что реально отличает один
    путь от другого) одновременно."""
    if len(path) <= maxlen:
        return path
    keep = maxlen - 3
    head = keep // 2
    tail = keep - head
    return path[:head] + "..." + path[-tail:]


@contextlib.contextmanager
def _prevent_sleep():
    """ТЗ-меню 2026-07-10, раздел 9 "Предотвращение сна компьютера": держит систему от сна
    на длинных операциях штатным SetThreadExecutionState(ES_CONTINUOUS|ES_SYSTEM_REQUIRED).
    ES_DISPLAY_REQUIRED сознательно НЕ ставится -- экрану гаснуть не мешаем, только
    сну/гибернации. Снятие -- гарантированно через finally при любом исходе (успех/ошибка/
    Ctrl-C). No-op вне Windows (dev/тест на Linux)."""
    if os.name != "nt":
        yield
        return
    ES_CONTINUOUS = 0x80000000
    ES_SYSTEM_REQUIRED = 0x00000001
    try:
        ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
    except Exception:
        yield
        return
    try:
        yield
    finally:
        try:
            ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS)
        except Exception:
            pass


def enumerate_menu_drives() -> list:
    """ТЗ-меню 2026-07-10, раздел 2: список дисков для подменю выбора источника/архива --
    ТОЛЬКО фильтр для пунктов МЕНЮ, --source/--target/«своя папка» по-прежнему принимают
    любой путь как есть. Показываем фиксированные локальные (DRIVE_FIXED) и вставленные
    читаемые съёмные/оптические (DRIVE_REMOVABLE/DRIVE_CDROM -- os.path.exists() уже
    отсеивает пустые приводы без носителя раньше, чем до них доходит GetDriveTypeW). НЕ
    показываем сетевые замапленные диски (DRIVE_REMOTE) -- не входят в перечисленные типы.
    Пустой список вне Windows (dev/тест на Linux, где буквы дисков не существуют)."""
    if os.name != "nt":
        return []
    import string
    DRIVE_REMOVABLE, DRIVE_FIXED, DRIVE_CDROM = 2, 3, 5
    drives = []
    for letter in string.ascii_uppercase:
        root = f"{letter}:\\"
        if not os.path.exists(root):
            continue
        try:
            dtype = ctypes.windll.kernel32.GetDriveTypeW(ctypes.c_wchar_p(root))
        except Exception:
            continue
        if dtype in (DRIVE_FIXED, DRIVE_REMOVABLE, DRIVE_CDROM):
            drives.append(root)
    return drives


def _menu_choice(n_options: int, default: int = None, input_fn=input, log=print,
                  allow_back: bool = False) -> int:
    """Общий цикл выбора номера пункта меню (1..n_options) -- невалидный ввод переспрашивает
    в цикле, без предела попыток, без падения (раздел 0 ТЗ). allow_back=True: "0" возвращает
    sentinel _MENU_BACK вместо числа -- не пересекается с диапазоном 1..n_options.

    2026-07-12, упрощение по прямой просьбе пользователя ("меню перегружено"): "0" везде
    означает ОДНО и то же -- вернуться в главное меню (run_bare_launch()), а не "шаг назад"
    на один экран (была более сложная стек-based версия того же дня, отменена в тот же
    заход). Дефолт allow_back=False не меняет поведение существующих вызовов (частичный CLI
    из _main() -- там возврата в меню нет, самого меню не существует)."""
    prompt = "  Ваш выбор" + (f" [по умолчанию {default}]" if default else "") + ": "
    while True:
        answer = input_fn(prompt).strip()
        if allow_back and answer == "0":
            return _MENU_BACK
        if not answer and default is not None:
            return default
        if answer.isdigit() and 1 <= int(answer) <= n_options:
            return int(answer)
        back_hint = ", 0 — главное меню" if allow_back else ""
        log(f"  Не понял ввод — введите число от 1 до {n_options}"
            + (", или нажмите Enter" if default else "") + back_hint + ".")


def prompt_source_submenu(input_fn=input, log=print, allow_back: bool = False):
    """ТЗ-меню 2026-07-10, раздел 2: выбор источника -- локальные диски
    (enumerate_menu_drives()) + «своя папка». Первый пункт -- дефолт по Enter. Поддерживает
    перетаскивание папки мышкой (Windows вставляет путь в кавычках, снимаем их).
    allow_back=True добавляет пункт [0] Главное меню и может вернуть sentinel _MENU_BACK
    вместо пути -- нумерация дисков/«своей папки» не сдвигается, 0 не пересекается с 1..N."""
    drives = enumerate_menu_drives()
    log("")
    log("  Откуда взять фотографии?")
    log("")
    for i, d in enumerate(drives, 1):
        log(f"    [{i}] Диск {d[:2]}  — найти фотографии на всём диске")
    custom_n = len(drives) + 1
    log(f"    [{custom_n}] Указать свою папку")
    if allow_back:
        log("")
        log("    [0] Главное меню")
    log("")
    choice = _menu_choice(custom_n, default=1 if drives else None, input_fn=input_fn, log=log,
                          allow_back=allow_back)
    if choice is _MENU_BACK:
        return _MENU_BACK
    if choice <= len(drives):
        return drives[choice - 1]
    path = input_fn("  Путь к папке (можно перетащить папку сюда мышкой): ").strip()
    return _strip_surrounding_quotes(path)


def prompt_target_submenu(sources: list, input_fn=input, log=print, allow_back: bool = False):
    """ТЗ-меню 2026-07-10, раздел 3: выбор архива -- ТОЛЬКО для [2]/[3]. Диск-пункты всегда
    предлагают `буква:\\PhotoArchive` целиком (снимает старый вопрос "создавать ли папку в
    корне диска" -- confirm_drive_root_target_interactively() в этом пути больше не
    вызывается для диск-пунктов, только для «своей папки», если введён голый корень).
    allow_back=True -- см. prompt_source_submenu()."""
    drives = enumerate_menu_drives()
    log("")
    log("  Куда сложить архив?")
    log("")
    sources_is_all = any(s.strip().lower() == "all" for s in sources)
    source_drive_letters = {
        os.path.splitdrive(os.path.abspath(s))[0].upper()
        for s in sources if s.strip().lower() != "all"
    }
    for i, d in enumerate(drives, 1):
        candidate = os.path.join(d, "PhotoArchive")
        if os.path.isdir(winlong(candidate)) and _target_has_existing_archive(candidate):
            status = "уже есть — допишу новые фото"
        elif os.path.isdir(winlong(candidate)):
            status = "папка уже есть"
        else:
            # 2026-07-11 (this session), user feedback: this submenu runs BEFORE the caller
            # decides dry_run vs. real build (see run_bare_launch()) -- "папка будет создана"
            # overpromised action that a dry-run preview never actually takes ("пробный прогон
            # ничего не пишет"). Neutral present-tense status instead, true regardless of what
            # the user picks next.
            status = "папки пока нет"
        same_disk = sources_is_all or d[:2].upper() in source_drive_letters
        suffix = "  (тот же диск, что и источник)" if same_disk else ""
        log(f"    [{i}] Диск {d[:2]}  →  {candidate}   ({status}){suffix}")
    custom_n = len(drives) + 1
    log(f"    [{custom_n}] Указать свою папку")
    if allow_back:
        log("")
        log("    [0] Главное меню")
    log("")
    choice = _menu_choice(custom_n, default=None, input_fn=input_fn, log=log,
                          allow_back=allow_back)
    if choice is _MENU_BACK:
        return _MENU_BACK
    if choice <= len(drives):
        return os.path.join(drives[choice - 1], "PhotoArchive")
    path = input_fn("  Путь к папке (можно перетащить папку сюда мышкой): ").strip()
    return _strip_surrounding_quotes(path)


def _sum_stats(dicts: list) -> dict:
    total = {}
    for d in dicts:
        for k, v in d.items():
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                total[k] = total.get(k, 0) + v
            elif isinstance(v, list):
                # 2026-07-11: stats["album_merge_events"] (см. _note_album_source()) -- список
                # (album, prefix), не число -- многоисточниковые прогоны (--source all/
                # повторяемый --source, или "пробный прогон" по нескольким источникам) должны
                # объединять списки со всех источников, а не молча терять их (числовая ветка
                # выше их просто отбросила бы).
                total.setdefault(k, []).extend(v)
    return total


def _print_human_view_summary(source_display: str, stats, log=print):
    """ТЗ-меню 2026-07-10, раздел 4: язык новичка, БЕЗ дубликатов/near-dup/сверки с архивом
    (это уровень [2], не [1])."""
    n_photos = stats.n_images + stats.n_raw
    log("")
    log(f"  Посмотрел {source_display}.")
    log("")
    log(f"    Нашёл фотографий:   {n_photos}")
    log(f"    Видео:              {stats.n_videos}")
    log(f"    Занимают места:     около {stats.total_bytes / 1024**3:.0f} ГБ")
    log("")
    if getattr(stats, "n_archives_found", 0):
        log("  Смотрел и внутри сжатых файлов (zip, rar) — фотографии оттуда тоже посчитаны.")
    log("  Ваши файлы не изменялись — я только посмотрел.")


def _print_human_dryrun_summary(target: str, stats: dict, log=print):
    """ТЗ-меню 2026-07-10, раздел 5: сослагательное будущее ("я сделаю"), детали уместны."""
    n_new = stats.get("appended_images", 0) + stats.get("appended_videos", 0)
    log("")
    log("  Пробный прогон завершён. Вот что я сделаю при настоящей сборке:")
    log("")
    log(f"    Скопирую в архив:        {n_new} новых фото и видео")
    if stats.get("skipped_present", 0):
        log(f"    Уже есть в архиве:       {stats['skipped_present']} "
            f"(копировать не буду — они уже сохранены)")
    if stats.get("archives_seen", 0):
        log(f"    Загляну внутрь:          {stats['archives_seen']} сжатых файлов (zip, rar)")
    if stats.get("undated", 0):
        log(f"    Не смог распознать дату: {stats['undated']} (сложу отдельно, не потеряю)")
    log("")
    bytes_needed = stats.get("bytes_appended", 0)
    log(f"    Архиву понадобится:      около {bytes_needed / 1024**3:.1f} ГБ")
    try:
        free = shutil.disk_usage(winlong(target)).free
        note = "места хватит" if free >= bytes_needed else "МЕСТА МОЖЕТ НЕ ХВАТИТЬ"
        log(f"    Свободно на диске {target[:2]}     {free / 1024**3:.0f} ГБ — {note}")
    except OSError:
        pass
    # 2026-07-11, по запросу пользователя ("как обыграть это при сухом прогоне"): предупредить
    # ЗАРАНЕЕ, что альбом соберётся из нескольких разных папок источника -- обнаружение (см.
    # _note_album_source()) не зависит от dry_run, только сама запись в файл-маркер внутри
    # альбома при пробном прогоне пропускается.
    merge_events = stats.get("album_merge_events") or []
    if merge_events:
        log("")
        log("  Внимание: эти альбомы соберутся из НЕСКОЛЬКИХ разных папок источника (в них уже")
        log("  есть похожие фото под другим именем папки):")
        for album, prefix in merge_events:
            log(f"    {album}  ←  {prefix}")
    log("")
    log("  Ничего не записано — это была только проверка.")


def _confirm_build_summary(sources: list, target: str, input_fn=input, log=print) -> bool:
    """ТЗ-меню 2026-07-10, разделы 6/9 (развилка 4 раздела 11): единственное подтверждение
    перед реальной записью. Если TARGET к тому же похож на чужую непустую папку
    (_target_needs_confirmation()==True) -- предупреждение добавляется В ТОТ ЖЕ вопрос,
    вместо отдельного второго «да» (не показывать два подтверждения подряд)."""
    log("")
    log("  Проверьте, всё ли верно:")
    log("")
    for s in sources:
        if s.strip().lower() == "all":
            desc = "все локальные диски"
        elif _is_bare_drive_root(s):
            desc = f"диск {s} (весь диск)"
        else:
            desc = _display_path(s)
        log(f"    Беру фотографии с:   {desc}")
    log(f"    Складываю архив в:   {_display_path(target)}")
    log("")
    if _target_needs_confirmation(target):
        log(f"  В папке {_display_path(target)} уже есть что-то, помимо архива — новые файлы")
        log("  будут добавлены туда же.")
    log("  Ваши исходные фотографии останутся на месте — я их не трогаю.")
    answer = input_fn("  Начать сборку? (да / нет): ").strip().lower()
    if answer != "да":
        log("Отменено.")
        return False
    return True


def _pause_before_exit(interactive_mode: bool, input_fn=input):
    """ТЗ-меню 2026-07-10, раздел 0: пауза в конце ЛЮБОГО интерактивного сценария --
    критично для запуска мышкой (иначе окно моргнёт и пропадёт). НЕ ставится для полного
    CLI (раздел 9а) -- консоль никуда не денется, а пауза повесит любой вызывающий скрипт."""
    if not interactive_mode:
        return
    try:
        input_fn("\nНажмите Enter для выхода: ")
    except EOFError:
        pass


def print_welcome_banner(log=print):
    """RULES.md, "ЗАПУСК" п.3/ТЗ-меню раздел 1: приветственный баннер вместо строки-ошибки
    при полностью голом запуске -- тон "веду", не "не хватает данных".

    2026-07-12, по прямой просьбе пользователя: версия программы и подсказка про
    `--help` для опытных пользователей раньше показывались отдельным блоком после КАЖДОГО
    выбора в меню режима (`prompt_bare_launch_menu()`) -- не связано с самим выбором,
    только сбивало с толку. Перенесено сюда, показывается один раз. Формулировка про
    `--help` заменена с "Опытным пользователям: ..." (звучало как деление на "своих"/
    "чужих") на нейтральное "Подробнее о параметрах запуска: ..."."""
    log("=" * 62)
    log("")
    log(f"   PhotoArchive версия {__version__}")
    log(f"   Репозиторий: {GITHUB_URL}")
    log("   Бережная сборка семейного фотоархива")
    log("")
    log("   - Ваши оригиналы не изменяются и не удаляются")
    log("   - Фотографии остаются на вашем компьютере — интернет не нужен")
    log("   - Остановить в любой момент: Ctrl+C")
    log("   - Подробнее о параметрах запуска: PhotoArchive --help")
    log("")
    log("=" * 62)


def prompt_bare_launch_menu(input_fn=input, log=print) -> str:
    """Меню трёх режимов для полностью голого запуска (RULES.md, "ЗАПУСК" п.3).
    Enter (пустой ввод) -> безопасный дефолт [1] -- нервный пользователь, жмущий Enter не
    глядя, должен попасть на "сканирование" (ничего не трогает), а не на реальную сборку.
    Невалидный ввод переспрашивает в цикле, не падает.

    2026-07-12, по прямой просьбе пользователя: раньше здесь тоже был пункт `[0] Выход`, но
    на всех ДРУГИХ экранах `0` означает «вернуться в ЭТО САМОЕ главное меню» -- на самом
    главном меню это не имеет смысла (возвращаться уже некуда), да и разное значение одной
    и той же кнопки на разных экранах нелогично. Явного пункта выхода здесь больше нет --
    Ctrl+C (анонсирован в приветственном баннере) и закрытие окна остаются штатным способом
    выйти. `0`, если всё же ввести, просто не совпадёт ни с одним пунктом -- обычный
    невалидный ввод, переспрос в цикле.

    2026-07-12, тем же вечером, по отдельному отзыву пользователя ("режет слух"): старые
    формулировки смешивали голоса -- [1] говорил от лица пользователя ("что у меня есть",
    двусмысленно чьё), [2] от лица программы ("покажу, что сделаю"), [3] вообще без
    пояснения. Единый стиль -- сухие технические существительные без "я"/"у меня" на всех
    трёх пунктах, пояснение в скобках у каждого."""
    log("")
    log("  Что сделать?")
    log("")
    log("    [1] Сканирование источника   (read-only)")
    log("    [2] Пробный прогон   (dry-run, без записи)")
    log("    [3] Сборка архива")
    log("")
    while True:
        answer = input_fn("  Ваш выбор [по умолчанию 1]: ").strip()
        if answer in BARE_LAUNCH_MENU_CHOICES:
            return BARE_LAUNCH_MENU_CHOICES[answer]
        log("  Не понял ввод — введите 1, 2 или 3.")


def _bare_launch_run_view(sources: list, log=print):
    """Шаг [1] меню -- read-only, ничего не пишет в TARGET, TARGET вообще не спрашивается
    (раздел 4 ТЗ). Технически всегда analyze-quick (только метаданные, без SHA/pHash) --
    дубликаты/near-dup/сверка с архивом сюда не относятся, это уровень [2]/[3]."""
    with _prevent_sleep():
        stats = run_analyze_for_source(sources[0], _VIEW_MODE_PLACEHOLDER_TARGET, 0,
                                        "analyze-quick", log=log)
    if stats is None:
        return
    _print_human_view_summary(_display_path(sources[0]), stats, log=log)


def _bare_launch_run_dryrun(sources: list, target: str, input_fn=input, log=print):
    """Шаг [2] меню -- раздел 5 ТЗ. НИКАКОГО подтверждения перед этим шагом (безопасен по
    определению): suppress_logs=True репетирует archive dry_run=True БЕЗ создания
    __служебные_файлы\\ и БЕЗ CSV/summary.txt в TARGET -- результат только на экране."""
    target = resolve_drive_root_conflict(sources, target, interactive=True, input_fn=input_fn, log=log)
    expanded = expand_sources(sources, target)
    results = []
    with _prevent_sleep():
        for s in expanded:
            if len(expanded) > 1:
                log(f"\n########## SOURCE = {s} ##########")
            result = run_for_source(s, target, dry_run=True, sample_limit=0, log=log,
                                     suppress_logs=True)
            if not result.failed:
                results.append(result.stats)
    merged = _sum_stats(results)
    _print_human_dryrun_summary(target, merged, log=log)
    return target


def _bare_launch_run_build(sources: list, target: str, input_fn=input, log=print):
    """Шаг [3] меню -- раздел 6 ТЗ. Единственное подтверждение (_confirm_build_summary,
    развилка 4 раздела 11) перед реальной записью. Возвращает (возможно изменённый) target,
    или None, если пользователь отказался (или сборка вообще не состоялась -- см.
    2026-07-12 ниже) -- вызывающий код (run_bare_launch()) в обоих случаях трактует None как
    «вернуться в главное меню», не как ошибку самого меню."""
    target = resolve_drive_root_conflict(sources, target, interactive=True, input_fn=input_fn, log=log)
    if not _confirm_build_summary(sources, target, input_fn=input_fn, log=log):
        return None
    expanded = expand_sources(sources, target)
    # 2026-07-12, живой репорт пользователя (запустил вторую копию программы в другом окне,
    # пока первая уже собирала архив в тот же TARGET): run_for_source() возвращает
    # RunResult(failed=True) и печатает "ОШИБКА: ..." при TargetLocked (LOCK-файл уже занят
    # другим процессом), но раньше возврат ИГНОРИРОВАЛСЯ -- ниже безусловно печаталось
    # "Готово. Архив собран", даже если ни один файл фактически не скопировался.
    # any_succeeded отслеживает это.
    any_succeeded = False
    with _prevent_sleep():
        for s in expanded:
            if len(expanded) > 1:
                log(f"\n########## SOURCE = {s} ##########")
            if not run_for_source(s, target, dry_run=False, sample_limit=0, log=log).failed:
                any_succeeded = True
    if not any_succeeded:
        log("")
        log("  Сборка не выполнена — см. сообщение об ошибке выше.")
        return None
    log("")
    log(f"  Готово. Архив собран в {_display_path(target)}")
    log("")
    # 2026-07-12, user feedback: старая формулировка ("не спешите удалять, пользуйтесь
    # архивом сколько нужно, чтобы убедиться, что всё на месте") звучала как намёк на
    # ненадёжность результата -- будто программа сама не уверена, что архив собрался
    # правильно. Исходные файлы остаются нетронутыми независимо от качества архива (это
    # свойство программы, а не оговорка про корректность), архив описываем уверенно, без
    # "чтобы убедиться".
    log("  Ваши исходные фотографии остались на месте — программа их не трогает.")
    log("  Архив — их полная копия, готовая к использованию.")
    return target


_AFTER_VIEW_CHOICES = {
    "0": "main_menu",
    "": "main_menu",
    "1": "dry_run",
}


def _prompt_after_view(input_fn=input, log=print) -> str:
    """2026-07-12: заменяет старый да/нет-вопрос после [1] (просмотр) -- раньше отказ
    завершал программу целиком (см. ROADMAP.md, живая находка 2026-07-11). Упрощено в тот же
    день по прямой просьбе пользователя ("меню перегружено") с промежуточной версии
    (отдельные "выбрать другой источник"/"выход") до одного универсального [0] Главное
    меню -- Enter по умолчанию тоже туда, самый безопасный вариант."""
    log("")
    log("  Что дальше?")
    log("")
    log("    [1] Показать пробный прогон — что именно скопируется")
    log("    [0] Главное меню")
    log("")
    while True:
        answer = input_fn("  Ваш выбор [по умолчанию 0]: ").strip()
        if answer in _AFTER_VIEW_CHOICES:
            return _AFTER_VIEW_CHOICES[answer]
        log("  Не понял ввод — введите 0 или 1.")


_AFTER_DRYRUN_CHOICES = {
    "0": "main_menu",
    "": "main_menu",
    "1": "build",
}


def _prompt_after_dryrun(input_fn=input, log=print) -> str:
    """2026-07-12: заменяет старый да/нет-вопрос после [2] (пробный прогон) -- см.
    _prompt_after_view() про упрощение до одного универсального [0] Главное меню."""
    log("")
    log("  Что дальше?")
    log("")
    log("    [1] Собрать архив по-настоящему")
    log("    [0] Главное меню")
    log("")
    while True:
        answer = input_fn("  Ваш выбор [по умолчанию 0]: ").strip()
        if answer in _AFTER_DRYRUN_CHOICES:
            return _AFTER_DRYRUN_CHOICES[answer]
        log("  Не понял ввод — введите 0 или 1.")


def run_bare_launch(input_fn=input, log=print):
    """Полностью голый запуск (sys.argv[1:] целиком пуст -- типично двойной клик по exe без
    единого аргумента, см. _main()) -- меню вместо мгновенного archive-прогона. ТЗ-меню
    2026-07-10 (PROMPT_interactive_menu.md) -- решения записаны в RULES.md, здесь --
    реализация.

    2026-07-12, возврат назад: сначала была отдельная стек-based версия (именованные экраны,
    возврат на один уровень назад ИЛИ на именованный уровень выше), отменена в тот же день по
    прямой просьбе пользователя -- "меню перегружено", один универсальный `[0] Главное меню`
    вместо разных вариантов "назад" достаточен. Реализовано максимально просто: весь голый
    запуск -- один `while True` вокруг лестницы `mode -> source -> [view] -> target ->
    [dry_run] -> build`, и `continue` (сброс на mode) -- единственный способ вернуться назад,
    какой бы глубины ни достигла лестница. `check_bundled_tools()` вызывается один раз за
    весь запуск (`tools_checked`), не при каждом возврате в главное меню.

    2026-07-15, живая находка: баннер печатается ПЕРВЫМ, а не после технической строки про
    photoarchive_config.yaml -- баннер специально задуман (см. print_welcome_banner()) как
    первое тёплое впечатление вместо строки-ошибки; печать служебного сообщения о
    только что созданном конфиге раньше баннера сводила этот эффект на нет. Пустая строка
    между закрывающей рамкой баннера и сообщением -- чтобы оно не липло к рамке."""
    print_welcome_banner(log=log)
    log("")
    _ensure_config_yaml_exists(CONFIG_YAML_PATH, log=log)

    tools_checked = False
    while True:
        # 2026-07-12: prompt_bare_launch_menu() больше не возвращает "exit" -- главное меню
        # не показывает [0] (возвращаться в него, будучи уже там, бессмысленно), выход --
        # только Ctrl+C/закрытие окна, см. её докстринг.
        mode = prompt_bare_launch_menu(input_fn=input_fn, log=log)

        source = prompt_source_submenu(input_fn=input_fn, log=log, allow_back=True)
        if source is _MENU_BACK:
            continue
        sources = [_normalize_bare_drive_letter(source)]

        if not tools_checked:
            check_bundled_tools(log=print)
            tools_checked = True

        if mode == "view":
            _bare_launch_run_view(sources, log=log)
            if _prompt_after_view(input_fn=input_fn, log=log) == "main_menu":
                continue
            mode = "dry_run"

        target = prompt_target_submenu(sources, input_fn=input_fn, log=log, allow_back=True)
        if target is _MENU_BACK:
            continue
        target = _normalize_bare_drive_letter(target)

        if mode == "dry_run":
            target = _bare_launch_run_dryrun(sources, target, input_fn=input_fn, log=log)
            if _prompt_after_dryrun(input_fn=input_fn, log=log) == "main_menu":
                continue
            mode = "build"

        result = _bare_launch_run_build(sources, target, input_fn=input_fn, log=log)
        if result is None:
            log("  Возвращаемся в главное меню.")
            continue
        return


def _log_unexpected_crash(log=print) -> None:
    """2026-07-11, live user report ("удалил архив во время работы с ним... программа
    срубилась"): the SOURCE-scanning fixes elsewhere this session (see _handle_archive()'s two
    new try/except OSError guards, and the equivalent one in Phase 1's archive_hash_cache path)
    close the specific race that was found -- but the user's underlying requirement is broader:
    "ни одно действие пользователя с файлами параллельно с работой программы не должно
    приводить к вылету". Targeted guards can only close races that were found; this is the
    last-resort backstop for whatever wasn't. main() previously caught only
    KeyboardInterrupt/EOFError -- anything else propagated as a raw traceback. Full traceback
    goes to crash.log next to the .exe (best-effort write -- must itself never raise and take
    down the crash handler); a short Russian message goes to the console."""
    try:
        crash_log_path = os.path.join(_app_dir(), "crash.log")
        _rotate_log_if_needed(crash_log_path)
        with open(crash_log_path, "a", encoding="utf-8") as f:
            f.write(f"\n===== {datetime.now().isoformat(timespec='seconds')} =====\n")
            f.write(traceback.format_exc())
    except OSError:
        pass
    log("\nПроизошла непредвиденная ошибка -- программа остановлена.")
    log("Ваши исходные файлы программа не изменяет и не удаляет ни при каких обстоятельствах "
        "-- эта ошибка их не затронула.")
    log(f"Подробности сохранены в {os.path.join(_app_dir(), 'crash.log')} -- приложите этот "
        f"файл, если сообщаете о проблеме.")


def main():
    # Every subprocess.run() call in this file (exiftool/7z/ffmpeg/ffprobe/UnRAR) is spawned
    # without CREATE_NEW_PROCESS_GROUP, so Ctrl-C's CTRL_C_EVENT/SIGINT already reaches those
    # children together with this process -- no separate Popen+kill needed here.
    try:
        sys.exit(_main())
    except KeyboardInterrupt:
        print("\nПрервано пользователем.")
        # 2026-07-11, live user report: on a bare double-click launch, Ctrl-C during
        # analyze/view mode printed this and closed the console window so fast it couldn't be
        # read -- this except block never called _pause_before_exit() at all, unlike every
        # normal-completion path in run_bare_launch(). Same fix, same reasoning as the
        # Exception handler below.
        if len(sys.argv) <= 1:
            _pause_before_exit(True)
        sys.exit(130)
    except EOFError:
        # Found on real Windows hardware while testing the new bare-launch menu
        # (run_bare_launch() -- multiple new input() prompts aimed exactly at
        # non-technical users): Ctrl-Z+Enter (Windows' EOF keystroke) or a closed/redirected
        # stdin at ANY interactive prompt (menu, "Откуда брать фото", risk confirmations)
        # raises EOFError -- previously an unhandled traceback + PyInstaller's
        # "Failed to execute script" banner, same bad experience Ctrl-C already avoids.
        print("\nВвод прерван (нет данных на входе).")
        # Same reasoning as the KeyboardInterrupt branch above -- this can itself raise
        # EOFError again if stdin is genuinely closed/redirected (not just Ctrl-Z), which
        # _pause_before_exit() already swallows internally.
        if len(sys.argv) <= 1:
            _pause_before_exit(True)
        sys.exit(130)
    except Exception:
        _log_unexpected_crash()
        if len(sys.argv) <= 1:
            # Bare double-click launch -- without this, the console window would flash the
            # message above and vanish before anyone could read it (same reasoning as
            # _pause_before_exit()'s own docstring). Not done for CLI/scripted invocations --
            # blocking on Enter there would hang whatever script/batch called this .exe.
            _pause_before_exit(True)
        sys.exit(1)


def _main():
    argv = sys.argv[1:]
    if not argv:
        # Полностью голый запуск -- НИ ОДНОГО аргумента командной строки (типично двойной
        # клик по exe). Единственный случай, который заменяется меню (RULES.md, "ЗАПУСК"
        # п.3) -- любой хотя бы один аргумент (флаг, подкоманда, даже неполный набор вроде
        # одного --source без --target) идёт по обычной ветке ниже без изменений.
        run_bare_launch(log=console_log)
        _pause_before_exit(True)
        return 0
    if argv and argv[0] in ("--version", "-V", "--help", "-h", "--formats"):
        # Глобальные флаги идут напрямую в верхний парсер -- НЕ подставлять "archive" перед
        # ними, иначе верхний --help показывал бы справку только по archive (скрывая
        # analyze-*), а --version/--formats падали бы с "unrecognized arguments" (эти флаги
        # не входят в p_archive). Работает только когда argv[0] -- САМ этот флаг: "archive
        # --help" (справка конкретно по archive) и "--source X --help" (частный случай ниже)
        # сюда не попадают и разбираются как раньше.
        pass
    elif not argv or argv[0] not in CLI_MODES:
        # Обратная совместимость: без подкоманды -- поведение как раньше (сборка архива).
        # "PhotoArchive.exe --source X --target Y" продолжает работать один в один.
        argv = ["archive"] + argv

    parser = build_arg_parser()
    args = parser.parse_args(argv)

    sources = resolve_sources(args)
    target = args.target

    if len(sources) > 1 and any(s.strip().lower() == "all" for s in sources):
        print("ОШИБКА: нельзя сочетать SOURCE=all с другими --source/--source-list. "
              "Укажите либо один SOURCE=all, либо список конкретных путей.")
        return EXIT_CONFIG_ERROR

    # ТЗ-меню 2026-07-10, раздел 9а + развилка 1 раздела 11: ЕДИНЫЙ признак "интерактивный
    # режим" -- определяется один раз и переиспользуется для (а) решения, что именно
    # доспросить подменю, (б) паузы "Нажмите Enter для выхода" в конце, (в) подтверждения-
    # «да» перед archive (_confirm_build_summary). Полный CLI (все нужные пути заданы явно)
    # -- ни меню, ни паузы, ни подтверждения; автоматизация/скрипты не должны спотыкаться.
    interactive_mode = not sources or not target
    if interactive_mode:
        # Частичный CLI: доспрашиваем ТОЛЬКО то, что не задано флагами (не весь набор
        # вопросов бare-launch меню -- режим уже известен из явной подкоманды/флагов).
        _ensure_config_yaml_exists(CONFIG_YAML_PATH, log=console_log)
        if not sources:
            sources = [prompt_source_submenu()]
        if not target:
            target = prompt_target_submenu(sources)

    # Голая буква диска без слеша ("C:") -- удобный короткий ввод ("источник C:, архив D:"),
    # но неоднозначный сам по себе в терминах Windows (p.5.9 отклонил бы его как "не полный
    # путь") -- нормализуем в однозначный корень ("C:\") ОДИНАКОВО для CLI-флагов и
    # интерактивного ввода, см. _normalize_bare_drive_letter().
    sources = [_normalize_bare_drive_letter(s) for s in sources]
    target = _normalize_bare_drive_letter(target)

    # Голый корень диска как TARGET -- либо вынужденное разрешение конфликта с одним из
    # SOURCE (без вопроса, одинаково для CLI/photoarchive_config.yaml и интерактива), либо, если конфликта
    # нет, настоящий выбор (создать PhotoArchive\ или писать прямо в корень) -- только в
    # интерактиве. См. resolve_drive_root_conflict().
    if args.mode == "archive":
        target = resolve_drive_root_conflict(sources, target, interactive=interactive_mode)

    # ТЗ-меню 2026-07-10, разделы 6/9 (развилка 4 раздела 11): единое подтверждение перед
    # реальной записью -- только интерактивный путь (полный CLI никогда его не показывает,
    # раздел 9а) и только archive (analyze-* ничего не пишет в TARGET, спрашивать не о чем).
    # _confirm_build_summary() уже включает в себя случай "TARGET похож на чужую папку"
    # (бывший confirm_target_interactively()) -- одним вопросом, не двумя подряд.
    if interactive_mode and args.mode == "archive" and not _confirm_build_summary(sources, target):
        _pause_before_exit(interactive_mode)
        return 0

    check_bundled_tools(log=print)

    expanded = expand_sources(sources, target)

    # ROADMAP.md "Коды возврата... не отражают неудачу": один exit code на весь прогон, даже
    # с несколькими SOURCE (--source all/повторяемый --source). InsufficientSpace всегда
    # выигрывает у любого другого кода, увиденного раньше в этом же цикле -- TARGET физически
    # некуда писать дальше, продолжать бессмысленно, это самая весомая причина остановиться.
    # TargetLocked/ошибка конфигурации на практике одинаковы для всех источников одного
    # прогона (один и тот же TARGET/photoarchive_config.yaml) -- порядок между ними самими не
    # важен, только чтобы 0 никогда не перекрывал уже увиденную ошибку.
    exit_code = 0
    with _prevent_sleep():
        for s in expanded:
            if len(expanded) > 1:
                print(f"\n########## SOURCE = {s} ##########")
            if args.mode == "archive":
                source_exit_code = run_for_source(
                    s, target, args.dry_run, args.sample_limit, log=console_log).exit_code
            else:
                stats = run_analyze_for_source(s, target, args.sample_limit, args.mode, log=console_log)
                source_exit_code = EXIT_CONFIG_ERROR if stats is None else 0
            if source_exit_code == EXIT_INSUFFICIENT_SPACE:
                exit_code = EXIT_INSUFFICIENT_SPACE
            elif source_exit_code and not exit_code:
                exit_code = source_exit_code

    _pause_before_exit(interactive_mode)
    return exit_code


if __name__ == "__main__":
    # Must run before anything else: some bundled dependency (reverse_geocoder) spawns
    # multiprocessing workers, and under a frozen PyInstaller exe each spawned worker
    # re-execs this very exe -- without freeze_support() it lands back in argparse with
    # multiprocessing's internal bootstrap args ("--multiprocessing-fork ...") and errors
    # out instead of running the worker payload. No-op on non-frozen/non-spawned runs.
    multiprocessing.freeze_support()

    # Console output is Russian throughout (log messages, RULES.md terminology). Windows'
    # default console codepage depends on system locale (e.g. cp1252 on an English-locale
    # install, cp866/1251 on a Russian one) and is NOT guaranteed to be able to encode
    # Cyrillic -- without this, the very first log() call crashes the whole run with
    # UnicodeEncodeError before any file is even touched. Force UTF-8 with a replace
    # fallback so a mismatched console codepage degrades to mojibake, not a crash. Log
    # files themselves are opened with explicit encoding="utf-8" elsewhere and are
    # unaffected either way.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    main()
