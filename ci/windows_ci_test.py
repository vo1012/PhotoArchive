"""Functional smoke test for photosort_win.py, meant to run on a real Windows filesystem
(GitHub Actions windows-latest runner). Builds synthetic fixtures on the fly, runs the
script as a subprocess (same code path a real user hits from the CLI), and asserts on the
resulting TARGET tree + CSV logs. Exits non-zero on the first failed assertion so the CI
job fails loudly.

Covers, in addition to the regression basics: zones/rejected_noise, MIRROR_RAW, multi-source
CLI, a >260-char TARGET path (winlong()), native hidden-attribute handling, and a zip archive
with Windows-forbidden characters in an internal member name (sanitize + 7z.exe extraction
behavior) -- these last three were previously only verifiable by hand on a real Windows PC.
"""
import csv
import os
import random
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile

# Same fix as applied to photosort_win.py itself: this harness also print()s Cyrillic
# fixture/album names, which crashes on a Windows console whose codepage can't encode them
# (e.g. cp1252 on the CI runner).
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# 2026-07-11 finding: fixture folders under WORK are real folders on disk while a test run is
# in progress -- when this used to fall back to ROOT (repo root, i.e. C:\photo-sort-win, live
# on the same C: drive), a user's own SOURCE=C:\ full-disk scan running at the same time as a
# local test run picked up dozens of test-fixture folders (src_*, bare_menu_*, pstest itself)
# as real "albums" in his actual family archive. %LOCALAPPDATA% is already excluded from any
# whole-disk scan (see SYSTEM_DIR_ENV_VARS/is_under_system_dir in photosort_win.py), so it can
# never leak into a real archive this way again, on this machine or the CI runner alike.
# Deliberately NOT raw %TEMP% (tempfile.gettempdir()) -- its path contains a literal "Temp"
# segment, which classify_zone()'s NOISE_SEGMENT_NAMES treats as a noisy zone, silently
# changing several zone-sensitive tests' expected outcomes (disputed vs rejected_noise).
WORK = os.path.join(os.environ.get("RUNNER_TEMP") or os.environ.get("LOCALAPPDATA") or tempfile.gettempdir(),
                     "photosort_ci_pstest")
SCRIPT = os.path.join(ROOT, "photosort_win.py")

FAILURES = []


def check(cond, label):
    if cond:
        print(f"  PASS: {label}")
    else:
        print(f"  FAIL: {label}")
        FAILURES.append(label)


def image(path, w, h, exif=False, dt="2019:07:15 12:00:00"):
    from PIL import Image
    os.makedirs(os.path.dirname(path), exist_ok=True)
    im = Image.new("RGB", (w, h))
    px = im.load()
    random.seed(path)
    for x in range(w):
        for y in range(h):
            px[x, y] = (random.randint(0, 255), random.randint(0, 255), random.randint(0, 255))
    im.save(path, "JPEG", quality=90)
    if exif:
        # exiftool.exe on Windows does its own wildcard-expansion of argv and mangles
        # non-ASCII (e.g. Cyrillic) paths passed directly -- same issue fixed in
        # exiftool_batch() in photosort_win.py; route the path through -@ argfile here too.
        import tempfile as _tempfile
        with _tempfile.NamedTemporaryFile(mode="w", suffix=".args", delete=False, encoding="utf-8") as af:
            af.write(path + "\n")
            argfile_path = af.name
        try:
            r = subprocess.run(
                ["exiftool", "-charset", "filename=utf8", "-overwrite_original",
                 f"-DateTimeOriginal={dt}", "-Make=Canon", "-Model=Canon EOS 80D",
                 "-@", argfile_path],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
            )
        finally:
            os.unlink(argfile_path)
        if r.returncode != 0:
            raise RuntimeError(f"exiftool failed on {path!r}:\nstdout={r.stdout}\nstderr={r.stderr}")


def run_photosort(source, target, extra_args=None, env=None):
    cmd = [sys.executable, SCRIPT, "--source", source, "--target", target]
    if extra_args:
        cmd.extend(extra_args)
    full_env = dict(os.environ)
    if env:
        full_env.update(env)
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                             errors="replace", env=full_env)
    print(result.stdout[-3000:])
    if result.returncode != 0:
        print(result.stderr[-3000:])
    return result


def read_csv(path):
    if not os.path.exists(path):
        return []
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def test_regression_and_zones():
    print("\n=== regression: dedup/album/bydate + zones ===")
    src = os.path.join(WORK, "src1")
    tgt = os.path.join(WORK, "target1")
    image(os.path.join(src, "Отпуск 2015", "Море", "photo1.jpg"), 1600, 1200, exif=True)
    # "DCIM" is a recognized dump-segment name (see DUMP_SEGMENT_NAMES in photosort_win.py)
    # -- unlike an arbitrary folder name, it must NOT be treated as an album.
    image(os.path.join(src, "DCIM", "IMG_0001.jpg"), 1400, 1000, exif=True,
          dt="2020:01:02 08:00:00")
    image(os.path.join(src, "Pictures", "Cache", "tiny_icon.jpg"), 64, 64)
    image(os.path.join(src, "Pictures", "Cache", "real_photo.jpg"), 1600, 1200, exif=True)
    # dump photo (no album) under a Cyrillic dir -- must land by its EXIF (Tier A) date,
    # not fall back to Tier C/D, which is exactly what exiftool_batch()'s argv-wildcard bug
    # on Windows would silently cause for any non-Latin1 path (see exiftool_batch() comment).
    image(os.path.join(src, "Загрузки", "IMG_9999.jpg"), 1500, 1100, exif=True,
          dt="2021:11:03 09:00:00")

    r = run_photosort(src, tgt)
    check(r.returncode == 0, "regression run exits 0")
    print("  -- appended.csv --")
    for row in read_csv(os.path.join(tgt, "__служебные_файлы", "logs", "appended.csv")):
        print(f"    {row}")
    print("  -- dates_review.csv --")
    for row in read_csv(os.path.join(tgt, "__служебные_файлы", "logs", "dates_review.csv")):
        print(f"    {row}")
    check(os.path.isfile(os.path.join(tgt, "Albums", "Отпуск 2015", "Море", "photo1.jpg")),
          "album placement (Отпуск 2015/Море -> Albums/Отпуск 2015/Море)")
    check(os.path.isfile(os.path.join(tgt, "ByDate", "2020", "2020-01 [PhotoArchive]", "IMG_0001.jpg")),
          "bydate placement for dump photo")
    check(os.path.isfile(os.path.join(tgt, "ByDate", "2021", "2021-11 [PhotoArchive]", "IMG_9999.jpg")),
          "EXIF Tier-A date correctly read via exiftool for a Cyrillic source path")
    rn = read_csv(os.path.join(tgt, "__служебные_файлы", "logs", "rejected_noise.csv"))
    check(any("tiny_icon" in row["source"] for row in rn), "tiny icon in Cache -> rejected_noise.csv")
    check(not os.path.exists(os.path.join(tgt, "_Unsorted", "Pictures")),
          "noisy-zone icon did NOT land in _Unsorted")
    check(os.path.isfile(os.path.join(tgt, "Albums", "Cache", "real_photo.jpg")),
          "confident photo in noisy zone still archived")

    # re-run against the same target: must dedup, not duplicate
    r2 = run_photosort(src, tgt)
    check(r2.returncode == 0, "second run (dedup) exits 0")
    n_files = sum(len(files) for _, _, files in os.walk(os.path.join(tgt, "Albums"))) + \
              sum(len(files) for _, _, files in os.walk(os.path.join(tgt, "ByDate")))
    check(n_files == 4, f"no duplicate files after re-run (found {n_files}, expected 4)")


def test_sibling_albums_not_merged():
    print("\n=== sibling albums under a common parent stay separate (topmost-anchor rule) ===")
    src = os.path.join(WORK, "src_sibling_albums")
    tgt = os.path.join(WORK, "target_sibling_albums")
    image(os.path.join(src, "Фото", "Свадьба", "a.jpg"), 1600, 1200, exif=True)
    image(os.path.join(src, "Фото", "Отпуск_2015", "b.jpg"), 1600, 1200, exif=True)

    r = run_photosort(src, tgt)
    check(r.returncode == 0, "sibling-albums run exits 0")
    check(os.path.isfile(os.path.join(tgt, "Albums", "Фото", "Свадьба", "a.jpg")),
          "sibling album kept separate (Фото/Свадьба -> Albums/Фото/Свадьба, not merged into Фото)")
    check(os.path.isfile(os.path.join(tgt, "Albums", "Фото", "Отпуск_2015", "b.jpg")),
          "sibling album kept separate (Фото/Отпуск_2015 -> Albums/Фото/Отпуск_2015, not merged into Фото)")


def test_mirror_raw():
    print("\n=== MIRROR_RAW true/false ===")
    src = os.path.join(WORK, "src_raw")
    jpg = os.path.join(src, "IMG_0001.JPG")
    image(jpg, 1500, 1100, exif=True)
    with open(os.path.join(src, "IMG_0001.CR2"), "wb") as f:
        f.write(b"FAKE-CR2-PAIRED" + os.urandom(256))
    with open(os.path.join(src, "IMG_0002.CR2"), "wb") as f:
        f.write(b"FAKE-CR2-LONE" + os.urandom(256))

    tgt_true = os.path.join(WORK, "target_raw_true")
    run_photosort(src, tgt_true)
    check(os.path.isfile(os.path.join(tgt_true, "RAW", "ByDate", "2019", "2019-07 [PhotoArchive]", "IMG_0001.CR2")),
          "MIRROR_RAW=true (default): paired RAW mirrored")
    check(os.path.isfile(os.path.join(tgt_true, "RAW", "ByDate", "2019", "2019-07 [PhotoArchive]", "IMG_0002.CR2")),
          "MIRROR_RAW=true (default): lone RAW mirrored")

    cfg_path = os.path.join(WORK, "config_raw_false.yaml")
    with open(cfg_path, "w", encoding="utf-8") as f:
        f.write("mirror_raw: false\n")
    tgt_false = os.path.join(WORK, "target_raw_false")
    # photoarchive_config.yaml must sit next to the script for the running process to pick it up
    script_cfg = os.path.join(ROOT, "photoarchive_config.yaml")
    shutil.copy(cfg_path, script_cfg)
    try:
        run_photosort(src, tgt_false)
    finally:
        os.remove(script_cfg)
    check(not os.path.exists(os.path.join(tgt_false, "RAW", "ByDate", "2019", "2019-07 [PhotoArchive]", "IMG_0001.CR2")),
          "MIRROR_RAW=false: paired RAW NOT copied")
    check(os.path.isfile(os.path.join(tgt_false, "RAW", "ByDate", "2019", "2019-07 [PhotoArchive]", "IMG_0002.CR2")),
          "MIRROR_RAW=false: lone RAW still mirrored")
    skipped = read_csv(os.path.join(tgt_false, "__служебные_файлы", "logs", "skipped.csv"))
    check(any(row["reason"] == "raw_skipped_has_jpeg" for row in skipped),
          "MIRROR_RAW=false: raw_skipped_has_jpeg logged")


def test_multi_source():
    print("\n=== multi-source CLI (--source repeated) ===")
    srcA = os.path.join(WORK, "srcA")
    srcB = os.path.join(WORK, "srcB")
    image(os.path.join(srcA, "a.jpg"), 1300, 1000, exif=True, dt="2018:01:01 10:00:00")
    image(os.path.join(srcB, "b.jpg"), 1300, 1000, exif=True, dt="2018:02:02 10:00:00")
    tgt = os.path.join(WORK, "target_multi")
    cmd = [sys.executable, SCRIPT, "--source", srcA, "--source", srcB, "--target", tgt]
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    print(result.stdout[-2000:])
    check(os.path.isfile(os.path.join(tgt, "ByDate", "2018", "2018-01 [PhotoArchive]", "a.jpg")),
          "multi-source: first --source processed")
    check(os.path.isfile(os.path.join(tgt, "ByDate", "2018", "2018-02 [PhotoArchive]", "b.jpg")),
          "multi-source: second --source processed")


def test_long_path():
    print("\n=== long path (>260 chars) TARGET ===")
    src = os.path.join(WORK, "src_long")
    image(os.path.join(src, "deep_photo.jpg"), 1300, 1000, exif=True)
    deep_target = os.path.join(WORK, "t_long", "x" * 60, "y" * 60, "z" * 60, "w" * 60)
    print(f"  target path length: {len(deep_target)}")
    try:
        r = run_photosort(src, deep_target)
        check(r.returncode == 0, "long-path run exits 0")
        found = False
        # winlong() (photosort_win.py) is a no-op on non-Windows -- the file lands at the plain
        # path there, not behind a "\\?\" prefix, which only means something to the Win32 API.
        walk_root = ("\\\\?\\" + os.path.abspath(deep_target)) if os.name == "nt" else deep_target
        for _dirpath, _, files in os.walk(walk_root):
            if any(f.endswith(".jpg") for f in files):
                found = True
                break
        check(found, f"file physically present under >260-char TARGET (len={len(deep_target)})")
    finally:
        # 2026-07-11 (this session -- this exact artifact blocked THREE separate full CI runs
        # in a row before this fix): a >260-char tree survives past this test's end, and
        # plain shutil.rmtree(WORK) at the START of the NEXT run can't delete it -- Windows'
        # own os.rmdir() refuses a bare path over MAX_PATH without the "\\?\" long-path-safe
        # prefix (and LongPathsEnabled isn't reliably on for every dev machine/CI runner).
        # Clean up after ourselves here instead of leaving it for the next run to trip over.
        long_root = os.path.join(WORK, "t_long")
        if os.path.isdir(long_root):
            walk_root = ("\\\\?\\" + os.path.abspath(long_root)) if os.name == "nt" else long_root
            shutil.rmtree(walk_root, ignore_errors=True)


def test_hidden_attribute():
    print("\n=== native Windows hidden attribute ===")
    if os.name != "nt":
        print("  SKIP: 'attrib' is Windows-only, this check only runs on the windows-latest CI runner")
        return
    src = os.path.join(WORK, "src_hidden")
    p = os.path.join(src, "hidden_photo.jpg")
    image(p, 64, 64)  # small, no exif -> will be flagged not-media -> disputed with was_hidden
    subprocess.run(["attrib", "+H", p], check=True, shell=False)
    tgt = os.path.join(WORK, "target_hidden")
    r = run_photosort(src, tgt)
    check(r.returncode == 0, "hidden-file run exits 0")
    disputes = read_csv(os.path.join(tgt, "__служебные_файлы", "logs", "disputes.csv"))
    check(any(row.get("was_hidden") == "1" for row in disputes),
          "native hidden attribute detected (was_hidden=1 in disputes.csv)")


def test_progress_note_does_not_stick():
    print("\n=== 2026-07-11 finding (live production run): ProgressReporter's tqdm note "
          "resets to the plain phase description instead of sticking around forever ===")
    # Before this fix: a "хеширование большого видео" note from one large video stayed on
    # screen for every subsequent photo (note=None), falsely suggesting the run was stuck
    # processing video when it had long since moved on -- real user report while watching a
    # live console run. Fake an isatty() stderr so ProgressReporter takes the tqdm live-bar
    # path (the bug was specific to that path; the non-tty periodic-line path was already
    # per-call correct).
    code = (
        "import sys; sys.path.insert(0, %r)\n"
        "import io\n"
        "class FakeTTY(io.StringIO):\n"
        "    def isatty(self): return True\n"
        "sys.stderr = FakeTTY()\n"
        "import photosort_win as m\n"
        "bar = m.ProgressReporter(total=None, desc='Фаза X', unit='файл')\n"
        "bar.update(1, note='хеширование большого видео')\n"
        "with_note = bar._bar.desc\n"
        "bar.update(1, note=None)\n"
        "after_none = bar._bar.desc\n"
        "print('with_note_has_video:', 'видео' in with_note)\n"
        "print('after_none_has_video:', 'видео' in after_none)\n"
        # tqdm's set_description() appends its own ': ' separator to whatever string you
        # give it -- strip that before comparing, this test cares about OUR text, not tqdm's
        # internal formatting.
        "print('after_none_is_plain:', after_none.rstrip(': ') == 'Фаза X')\n"
    ) % ROOT
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                        encoding="utf-8", errors="replace")
    check(r.returncode == 0, f"progress-note: unit script exits 0 (stderr={r.stderr[-500:]})")
    check("with_note_has_video: True" in r.stdout,
          "progress-note: a note DOES show up in the description when given")
    check("after_none_has_video: False" in r.stdout,
          "progress-note: a stale note does NOT survive into the next no-note update")
    check("after_none_is_plain: True" in r.stdout,
          "progress-note: description resets to exactly the plain phase desc, not just "
          "'anything without видео'")


def test_progress_bar_desc_separated_even_with_zero_updates():
    print("\n=== live user report 2026-07-11: a bar that never has a single update() call "
          "(e.g. 'Фаза 1 — индексация архива' against a brand-new empty archive, 0 files) "
          "used to render its desc glued directly onto the literal bar_format text with no "
          "separator at all ('архивавсего') for its ENTIRE lifetime, not just briefly ===")
    # Root cause was two-layered: (1) tqdm's set_description() (called from update()) appends
    # its own trailing ": ' to its internal desc -- but the _tqdm(...) CONSTRUCTOR'S desc=
    # kwarg bypasses that entirely, so the very first frame (rendered by tqdm's own on-init
    # refresh, before ANY of our code runs again) had no separator; (2) a bar that never
    # processes a single file (this test's exact scenario) never calls update() naturally, so
    # that first raw frame was also the ONLY frame anyone ever saw. Fixed by constructing with
    # an empty desc and immediately normalizing via update(0). Checks the FULL captured
    # stream (every frame, not just the last) -- unlike a real terminal, nothing here
    # overwrites an earlier bad frame, so this is a stricter check than a human would ever see.
    code = (
        "import sys; sys.path.insert(0, %r)\n"
        "import io\n"
        "class FakeTTY(io.StringIO):\n"
        "    def isatty(self): return True\n"
        "sys.stderr = FakeTTY()\n"
        "for s in (sys.stdout,):\n"
        "    s.reconfigure(encoding='utf-8', errors='replace')\n"
        "import photosort_win as m\n"
        "bar = m.ProgressReporter(total=None, desc='Фаза 1 — индексация архива', unit='файл')\n"
        "bar.close()  # no update() call in between -- 0-file archive, exact live scenario\n"
        "out = sys.stderr.getvalue()\n"
        "print('no_glued_text:', 'архивавсего' not in out)\n"
        "print('desc_shown_with_separator:', 'архива: всего' in out)\n"
    ) % ROOT
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                        encoding="utf-8", errors="replace")
    check(r.returncode == 0, f"progress-bar-zero-update: unit script exits 0 (stderr={r.stderr[-500:]})")
    check("no_glued_text: True" in r.stdout,
          "progress-bar-zero-update: desc never glues onto the bar_format text with no separator")
    check("desc_shown_with_separator: True" in r.stdout,
          "progress-bar-zero-update: desc is properly ': '-separated even without a single update() call")


def test_log_line_does_not_glue_onto_active_bar():
    print("\n=== live user report 2026-07-11: an archive-event log line ('[archive] X.zip: "
          "archive_no_media') glued directly onto the active bar's current line with no "
          "separator at all -- tqdm.write()'s own bar-matching (which decides which active "
          "bars to clear before printing by comparing their output stream against the file= "
          "it's given) never recognizes OUR bar, because it writes through a custom "
          "_RussianRateStream proxy, not the raw stream tqdm.write() is told about -- so "
          "nothing gets cleared, and the message is appended straight onto the bar's "
          "already-drawn text ===")
    code = (
        "import sys; sys.path.insert(0, %r)\n"
        "import io\n"
        "class FakeTTY(io.StringIO):\n"
        "    def isatty(self): return True\n"
        "sys.stderr = FakeTTY()\n"
        "import photosort_win as m\n"
        "with m.ProgressReporter(total=None, desc='Фаза 2-5 — обработка источника — F:\\\\', "
        "unit='файл') as bar:\n"
        "    m.log_line('  [archive] test.zip: archive_no_media', log=print)\n"
        "out = sys.stderr.getvalue()\n"
        "sys.stdout.reconfigure(encoding='utf-8', errors='replace')\n"
        "print('no_glue:', 'файл/с]  [archive]' not in out)\n"
        # clear() resets the cursor to column 0 with a carriage return (not a newline) before
        # padding/printing -- the message itself still ends with a real '\\n', which is what
        # actually commits it to permanent scrollback and starts the bar fresh on the next line.
        "print('message_own_line:', '\\r  [archive] test.zip: archive_no_media\\n' in out)\n"
        "print('bar_redrawn_after:', out.rstrip().endswith(\n"
        "    'Фаза 2-5 — обработка источника — F:\\\\: всего обработано файлов: 0 [00:00, ?файл/с]'))\n"
    ) % ROOT
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                        encoding="utf-8", errors="replace")
    check(r.returncode == 0, f"log-line-glue: unit script exits 0 (stderr={r.stderr[-500:]})")
    check("no_glue: True" in r.stdout,
          "log-line-glue: archive-event message no longer glues onto the bar's current text")
    check("message_own_line: True" in r.stdout,
          "log-line-glue: the message is printed on its own clean line, newline-terminated")
    check("bar_redrawn_after: True" in r.stdout,
          "log-line-glue: the bar is properly redrawn fresh below the message afterward")


def test_progress_bar_context_note_persists_and_truncates():
    print("\n=== 2026-07-11, user feedback: SourceWalker should show which folder it's "
          "currently digging through (like the existing archive-extraction note already "
          "does), and long paths should show only the tail behind a leading ellipsis ===")
    code = (
        "import sys; sys.path.insert(0, %r)\n"
        "import io\n"
        "class FakeTTY(io.StringIO):\n"
        "    def isatty(self): return True\n"
        "sys.stderr = FakeTTY()\n"
        "import photosort_win as m\n"
        "bar = m.ProgressReporter(total=None, desc='Фаза X', unit='файл')\n"
        "bar.set_context('C:\\\\Photos\\\\2015')\n"
        "immediately = bar._bar.desc\n"
        "bar.update(1, note=None)\n"
        "after_plain_update = bar._bar.desc\n"
        "bar.update(1, note='хеширование большого видео')\n"
        "with_transient_note = bar._bar.desc\n"
        "bar.update(1, note=None)\n"
        "after_transient_cleared = bar._bar.desc\n"
        "print('shows_immediately:', 'Photos' in immediately)\n"
        "print('survives_plain_update:', 'Photos' in after_plain_update)\n"
        "print('transient_note_overrides:', 'видео' in with_transient_note)\n"
        "print('falls_back_to_context_after_transient:', 'Photos' in after_transient_cleared "
        "and 'видео' not in after_transient_cleared)\n"
        "long_path = 'C:\\\\' + ('a' * 40) + '\\\\' + ('b' * 40) + '\\\\tail.jpg'\n"
        "truncated = m._truncate_progress_note(long_path, maxlen=20)\n"
        "print('truncated_len_ok:', len(truncated) == 20)\n"
        "print('truncated_starts_with_ellipsis:', truncated.startswith('…'))\n"
        "print('truncated_keeps_tail:', truncated.endswith('tail.jpg'))\n"
        "print('short_path_unchanged:', m._truncate_progress_note('C:\\\\short.jpg', maxlen=60) "
        "== 'C:\\\\short.jpg')\n"
    ) % ROOT
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                        encoding="utf-8", errors="replace")
    check(r.returncode == 0, f"progress-context: unit script exits 0 (stderr={r.stderr[-500:]})")
    check("shows_immediately: True" in r.stdout,
          "progress-context: set_context() shows the folder right away, no update() needed")
    check("survives_plain_update: True" in r.stdout,
          "progress-context: an ordinary per-file update(note=None) does not clear the folder note")
    check("transient_note_overrides: True" in r.stdout,
          "progress-context: a one-off note (e.g. big-video) still overrides the folder note")
    check("falls_back_to_context_after_transient: True" in r.stdout,
          "progress-context: once the one-off note passes, the folder note comes back (not blank)")
    check("truncated_len_ok: True" in r.stdout, "progress-context: truncation respects maxlen")
    check("truncated_starts_with_ellipsis: True" in r.stdout,
          "progress-context: a truncated path leads with an ellipsis, not the (misleading) start")
    check("truncated_keeps_tail: True" in r.stdout,
          "progress-context: truncation keeps the END of the path, per user's explicit request")
    check("short_path_unchanged: True" in r.stdout,
          "progress-context: a path already under maxlen is left alone")


def test_source_walker_reports_current_directory():
    print("\n=== 2026-07-11, user feedback: SourceWalker actually calls progress_cb while "
          "walking, for both plain folders and archive-nested paths ===")
    src = os.path.join(WORK, "src_dir_progress")
    image(os.path.join(src, "Vacation", "Beach", "photo1.jpg"), 800, 600, exif=True,
          dt="2022:07:01 10:00:00")
    zpath = os.path.join(src, "Vacation", "album.zip")
    with zipfile.ZipFile(zpath, "w") as zf:
        zf.writestr("Inner/photo2.jpg", "not a real jpeg but irrelevant, listing-only checks here")
    code = (
        "import sys; sys.path.insert(0, %r)\n"
        "for s in (sys.stdout, sys.stderr):\n"
        "    s.reconfigure(encoding='utf-8', errors='replace')\n"
        "import photosort_win as m\n"
        "cfg = m.Config(source=%r, target=%r)\n"
        "seen = []\n"
        "w = m.SourceWalker(cfg, log=print, progress_cb=seen.append)\n"
        "for _item in w.walk():\n"
        "    pass\n"
        "print('SEEN:' + '|'.join(seen))\n"
    ) % (ROOT, src, os.path.join(WORK, "target_dir_progress"))
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                        encoding="utf-8", errors="replace")
    check(r.returncode == 0, f"dir-progress: unit script exits 0 (stderr={r.stderr[-500:]})")
    seen_line = next((line for line in r.stdout.splitlines() if line.startswith("SEEN:")), "SEEN:")
    seen = seen_line[len("SEEN:"):].split("|")
    check(any(s.endswith("Vacation") for s in seen),
          "dir-progress: the plain 'Vacation' subfolder is reported with its real disk path")
    check(any(s.endswith("Beach") for s in seen),
          "dir-progress: the nested 'Beach' subfolder is reported too")
    check(any("album.zip" in s and "Inner" in s for s in seen),
          "dir-progress: an archive-nested folder is reported via the 'archive.zip → path' "
          "display convention, not the meaningless temp extraction path")


def test_progress_bar_no_doubled_colon_or_mixed_units():
    print("\n=== 2026-07-11 finding (live production run): no-total progress bar showed a "
          "doubled colon and mixed-language rate unit (both tqdm quirks, not our formatting) ===")
    # Before this fix: a total=None ProgressReporter (used for "Фаза 2-5: обработка
    # источника", whose file count isn't known in advance) rendered as "Фаза X: : 7файл
    # [00:02, 3.62файл/s]" -- doubled ": " (tqdm's own no-total rendering path appends its
    # own ": " on top of the one set_description() already added, unlike the WITH-total path
    # which guards against this) and a Latin "s" glued onto a Russian unit ("файл/s", tqdm's
    # rate_fmt hardcodes "/s"). Real user finding while watching a live console run.
    code = (
        "import sys; sys.path.insert(0, %r)\n"
        "import io\n"
        "class FakeTTY(io.StringIO):\n"
        "    def isatty(self): return True\n"
        "sys.stderr = FakeTTY()\n"
        "import photosort_win as m\n"
        "bar = m.ProgressReporter(total=None, desc='Фаза X', unit='файл')\n"
        "for _ in range(3):\n"
        "    bar.update(1)\n"
        "bar.close()\n"
        "out = sys.stderr.getvalue()\n"
        "print('no_doubled_colon:', ': :' not in out)\n"
        "print('has_russian_rate_unit:', 'файл/с' in out)\n"
        "print('no_latin_s_suffix:', 'файл/s' not in out)\n"
    ) % ROOT
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                        encoding="utf-8", errors="replace")
    check(r.returncode == 0, f"progress-bar-format: unit script exits 0 (stderr={r.stderr[-500:]})")
    check("no_doubled_colon: True" in r.stdout,
          "progress-bar-format: no-total bar does not show a doubled ': :' after desc")
    check("has_russian_rate_unit: True" in r.stdout,
          "progress-bar-format: rate shows the Russian 'файл/с' suffix")
    check("no_latin_s_suffix: True" in r.stdout,
          "progress-bar-format: tqdm's hardcoded Latin '/s' suffix no longer leaks through")


def test_sanitize_zip():
    print("\n=== sanitize: zip with Windows-forbidden characters in member name ===")
    src = os.path.join(WORK, "src_zip")
    os.makedirs(src, exist_ok=True)
    zpath = os.path.join(src, "badnames.zip")
    tmp_jpg = os.path.join(WORK, "_tmp_for_zip.jpg")
    image(tmp_jpg, 1300, 1000, exif=True)
    with zipfile.ZipFile(zpath, "w") as zf:
        zf.write(tmp_jpg, arcname="Album: Best?/Photo<1>.jpg")
    tgt = os.path.join(WORK, "target_zip")
    r = run_photosort(src, tgt)
    check(r.returncode == 0, "zip-with-bad-names run exits 0 (does not crash)")
    bad_chars = set('<>:"|?*')
    offenders = []
    for dirpath, _, files in os.walk(tgt):
        if "__служебные_файлы" in dirpath:
            continue
        for f in files:
            if bad_chars & set(f):
                offenders.append(os.path.join(dirpath, f))
    check(not offenders, f"no forbidden characters survive into any destination filename ({offenders})")
    any_copied = any(
        f.lower().endswith((".jpg", ".jpeg"))
        for dirpath, _, files in os.walk(os.path.join(tgt, "Albums"))
        for f in files
    ) if os.path.isdir(os.path.join(tgt, "Albums")) else False
    check(any_copied, "photo from inside the zip was actually archived somewhere under Albums/")


def test_archive_no_media_skips_extraction():
    print("\n=== 2026-07-11 finding (live production run): an archive whose listing shows "
          "no plausible media is never actually extracted at all ===")
    # A whole-disk scan runs into plenty of installers/configs/backups zipped up with zero
    # photos inside -- before this fix, every one of them got FULLY extracted just to
    # discover that afterwards (real user report: dozens of such zips on a real C:\ scan).
    src = os.path.join(WORK, "src_archive_no_media")
    os.makedirs(src, exist_ok=True)
    zpath_nomedia = os.path.join(src, "installer.zip")
    with zipfile.ZipFile(zpath_nomedia, "w") as zf:
        zf.writestr("readme.txt", "not a photo")
        zf.writestr("config.json", "{}")
    tmp_jpg = os.path.join(WORK, "_tmp_for_no_media_test.jpg")
    image(tmp_jpg, 1300, 1000, exif=True, dt="2022:05:05 10:00:00")
    zpath_media = os.path.join(src, "photos.zip")
    with zipfile.ZipFile(zpath_media, "w") as zf:
        zf.write(tmp_jpg, arcname="photo.jpg")
    tgt = os.path.join(WORK, "target_archive_no_media")
    r = run_photosort(src, tgt)
    check(r.returncode == 0, "archive-no-media: run exits 0")
    archives_log = os.path.join(tgt, "__служебные_файлы", "logs", "archives.log")
    with open(archives_log, encoding="utf-8") as f:
        archives_text = f.read()
    check("installer.zip: archive_no_media" in archives_text,
          "archive-no-media: the media-less zip is correctly logged as archive_no_media")
    check("photos.zip: archive_extracted" in archives_text,
          "archive-no-media: a zip WITH a photo is still extracted and logged normally")
    check(any(f.lower() == "photo.jpg" for _, _, files in os.walk(os.path.join(tgt, "Albums")) for f in files),
          "archive-no-media: the actual photo from the media zip landed in the archive")
    # The real assertion: the media-less zip was never extracted at all -- tmp_extract should
    # only ever have held ONE archive's worth of content (photos.zip), not two.
    extract_dir = os.path.join(tgt, "__служебные_файлы", "tmp_extract")
    check(not os.path.isdir(extract_dir) or not os.listdir(extract_dir),
          "archive-no-media: tmp_extract\\ is cleaned up / was never populated for the "
          "media-less zip (extraction was skipped entirely, not just discarded after)")


def test_archive_unlistable_treated_as_bomb():
    print("\n=== 5.6: archive whose listing can't be read is treated as a suspected bomb, not extracted ===")
    src = os.path.join(WORK, "src_badarchive")
    os.makedirs(src, exist_ok=True)
    # A .zip extension but not actually a valid archive -- 7z can't produce a listing for
    # it ("----------" separator never appears in `7z l -slt` output), so list_archive()
    # returns ArchiveInfo(ok=False). Before the p.5.6 fix this fell through to a guessed
    # required-space estimate (compressed_size*3) and attempted extraction anyway.
    bad_zip = os.path.join(src, "not_really_a_zip.zip")
    with open(bad_zip, "wb") as f:
        f.write(b"this is not a valid zip file, just garbage bytes" * 20)
    tgt = os.path.join(WORK, "target_badarchive")
    r = run_photosort(src, tgt)
    check(r.returncode == 0, "5.6: run against an unlistable archive exits 0 (does not crash)")
    archives_log_path = os.path.join(tgt, "__служебные_файлы", "logs", "archives.log")
    log_text = open(archives_log_path, encoding="utf-8").read() if os.path.exists(archives_log_path) else ""
    check("archive_bomb_suspected" in log_text,
          "5.6: unlistable archive is logged as archive_bomb_suspected")
    tmp_extract = os.path.join(tgt, "__служебные_файлы", "tmp_extract")
    check(not os.path.isdir(tmp_extract) or not os.listdir(tmp_extract),
          "5.6: unlistable archive is never actually extracted (tmp_extract stays empty)")


def run_photosort_mode(mode, source, target, extra_args=None):
    """Like run_photosort() but for the analyze-* subcommands (mode is the first positional
    CLI argument, not implied via the "archive" default)."""
    cmd = [sys.executable, SCRIPT, mode, "--source", source, "--target", target]
    if extra_args:
        cmd.extend(extra_args)
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    print(result.stdout[-3000:])
    if result.returncode != 0:
        print(result.stderr[-3000:])
    return result


def test_archive_rename_finalization():
    print("\n=== A.1: archive extraction + fast rename finalization (same volume) ===")
    src = os.path.join(WORK, "src_archive")
    tgt = os.path.join(WORK, "target_archive")
    os.makedirs(src, exist_ok=True)
    tmp_jpg = os.path.join(WORK, "_tmp_for_zip.jpg")
    image(tmp_jpg, 1600, 1200, exif=True, dt="2020:07:01 09:00:00")
    # Numeric archive name -> recognized dump segment, so the file lands by date, not as an
    # album named after the archive (see find_album() / archive-name-becomes-path-segment).
    zpath = os.path.join(src, "20200701.zip")
    with zipfile.ZipFile(zpath, "w") as zf:
        zf.write(tmp_jpg, arcname="photo_from_zip.jpg")

    r = run_photosort(src, tgt)
    check(r.returncode == 0, "A.1: archive run exits 0")
    dest = os.path.join(tgt, "ByDate", "2020", "2020-07 [PhotoArchive]", "photo_from_zip.jpg")
    check(os.path.isfile(dest), "A.1: file from archive physically placed at its final path")
    with open(os.path.join(tgt, "__служебные_файлы", "logs", "actions.log"), encoding="utf-8") as f:
        actions_text = f.read()
    check("renamed(from_archive,same_volume)" in actions_text,
          "A.1: fast rename finalization (not copy) used for the archive-extracted file")
    with open(os.path.join(tgt, "__служебные_файлы", "logs", "summary.txt"), encoding="utf-8") as f:
        summary_text = f.read()
    check("Итоговый архив:" in summary_text, "A.4: friendly run summary appended to summary.txt")


def test_tar_source_never_uses_unverified_rename():
    print("\n=== data-integrity audit (2026-07-10 Phase 2): tar/tar.gz/tar.bz2 has no "
          "per-file content checksum -- unlike zip (see test_archive_rename_finalization), "
          "a tar-extracted file must go through the hash-verified atomic_copy() path, never "
          "the CRC-trusted same-volume rename shortcut ===")
    import tarfile as _tarfile
    src = os.path.join(WORK, "src_tar_integrity")
    tgt = os.path.join(WORK, "target_tar_integrity")
    os.makedirs(src, exist_ok=True)
    tmp_jpg = os.path.join(WORK, "_tmp_for_tar.jpg")
    image(tmp_jpg, 1600, 1200, exif=True, dt="2020:08:01 09:00:00")
    tpath = os.path.join(src, "20200801.tar")
    with _tarfile.open(tpath, "w") as tf:
        tf.add(tmp_jpg, arcname="photo_from_tar.jpg")

    # 2026-07-11: the raw stats.items() dump (tar_verified_copy included) moved behind
    # debug: true (see test_summary_enriched_always) -- turn it on just for this run so the
    # positive "verified copy" counter assertion below still has something to check against.
    cfg_path = os.path.join(ROOT, "photoarchive_config.yaml")
    with open(cfg_path, "w", encoding="utf-8") as f:
        f.write("debug: true\n")
    try:
        r = run_photosort(src, tgt)
    finally:
        os.remove(cfg_path)
    check(r.returncode == 0, "tar-integrity: archive run exits 0")
    dest = os.path.join(tgt, "ByDate", "2020", "2020-08 [PhotoArchive]", "photo_from_tar.jpg")
    check(os.path.isfile(dest), "tar-integrity: file from tar physically placed at its final path")
    with open(os.path.join(tgt, "__служебные_файлы", "logs", "actions.log"), encoding="utf-8") as f:
        actions_lines = f.read().splitlines()
    check(not any("photo_from_tar.jpg" in line and "renamed(from_archive,same_volume)" in line
                  for line in actions_lines),
          "tar-integrity: the tar-extracted file did NOT take the unverified rename shortcut")
    with open(os.path.join(tgt, "__служебные_файлы", "logs", "summary.txt"), encoding="utf-8") as f:
        summary_text = f.read()
    check("tar_verified_copy: 1" in summary_text,
          "tar-integrity: summary.txt counts the file as a verified (not rename-shortcut) copy")


def test_place_file_archive_no_crc_forces_hash_verify():
    print("\n=== data-integrity audit (2026-07-10 Phase 2): place_file() unit test -- "
          "archive_no_crc=True must always hash-verify (raise on mismatch, never place a "
          "corrupted file); archive_no_crc=False keeps the legacy no-verify rename (zip/7z/rar, "
          "whose extractors already CRC-checked the content) ===")
    work = os.path.join(WORK, "place_file_unit")
    code = (
        "import sys, os; sys.path.insert(0, %r)\n"
        "for s in (sys.stdout, sys.stderr):\n"
        "    s.reconfigure(encoding='utf-8', errors='replace')\n"
        "import photosort_win as m\n"
        "work = %r\n"
        "target = os.path.join(work, 'target')\n"
        "cfg = m.Config(source=os.path.join(work, 'src'), target=target)\n"
        "os.makedirs(cfg.tmp_extract, exist_ok=True)\n"
        "class FakeRunLogs:\n"
        "    def action(self, line): pass\n"
        "\n"
        "p1 = os.path.join(cfg.tmp_extract, 'fromtar.jpg')\n"
        "open(p1, 'wb').write(b'tar content')\n"
        "item1 = m.SourceItem(read_path=p1, origin_display='fromtar.jpg', rel_path='fromtar.jpg',\n"
        "                      size=os.path.getsize(p1), mtime=os.path.getmtime(p1), ftype='image',\n"
        "                      archive_no_crc=True)\n"
        "dest1 = os.path.join(target, 'ByDate', 'fromtar.jpg')\n"
        "raised = False\n"
        "try:\n"
        "    m.place_file(item1, dest1, '0' * 64, cfg, FakeRunLogs(), stats={})\n"
        "except Exception:\n"
        "    raised = True\n"
        "print('tar_mismatch_raises:', raised)\n"
        "print('tar_dest_exists:', os.path.exists(dest1))\n"
        "\n"
        "p2 = os.path.join(cfg.tmp_extract, 'fromzip.jpg')\n"
        "open(p2, 'wb').write(b'zip content')\n"
        "item2 = m.SourceItem(read_path=p2, origin_display='fromzip.jpg', rel_path='fromzip.jpg',\n"
        "                      size=os.path.getsize(p2), mtime=os.path.getmtime(p2), ftype='image',\n"
        "                      archive_no_crc=False)\n"
        "dest2 = os.path.join(target, 'ByDate', 'fromzip.jpg')\n"
        "m.place_file(item2, dest2, '0' * 64, cfg, FakeRunLogs(), stats={})\n"
        "print('zip_dest_exists:', os.path.exists(dest2))\n"
    ) % (ROOT, work)
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                        encoding="utf-8", errors="replace")
    check(r.returncode == 0, f"place_file unit test exits 0 (stderr={r.stderr[-800:]})")
    check("tar_mismatch_raises: True" in r.stdout,
          "archive_no_crc=True: a hash mismatch raises instead of silently placing the file")
    check("tar_dest_exists: False" in r.stdout,
          "archive_no_crc=True: the corrupted/mismatched file never reaches its destination")
    check("zip_dest_exists: True" in r.stdout,
          "archive_no_crc=False: legacy same-volume rename shortcut is unchanged (no verify)")


def test_analyze_modes():
    print("\n=== A.2: analyze-quick / analyze (read-only, no writes to TARGET) ===")
    src = os.path.join(WORK, "src_analyze")
    tgt = os.path.join(WORK, "target_analyze")
    image(os.path.join(src, "normal.jpg"), 1600, 1200, exif=True, dt="2018:05:01 10:00:00")
    with open(os.path.join(src, "broken.jpg"), "wb"):
        pass
    image(os.path.join(src, "dup_a.jpg"), 1400, 1000, exif=True, dt="2017:06:01 08:00:00")
    shutil.copy2(os.path.join(src, "dup_a.jpg"), os.path.join(src, "dup_b_renamed.jpg"))

    r = run_photosort_mode("analyze-quick", src, tgt)
    check(r.returncode == 0, "A.2: analyze-quick exits 0")
    check(not os.path.isdir(os.path.join(tgt, "ByDate")), "A.2: analyze-quick writes nothing to TARGET")

    r2 = run_photosort_mode("analyze", src, tgt)
    check(r2.returncode == 0, "A.2: analyze exits 0")
    check(not os.path.isdir(os.path.join(tgt, "ByDate")), "A.2: analyze writes nothing to TARGET")
    report_path = os.path.join(ROOT, "analyze_report.csv")
    rows = {row["metric"]: row["value"] for row in read_csv(report_path)}
    check(rows.get("mode") == "analyze", "A.2: analyze_report.csv reflects the analyze mode")
    check(int(rows.get("n_exact_dupes", 0)) >= 1, "A.2: analyze found the exact duplicate (dup_a/dup_b)")
    if os.path.exists(report_path):
        os.remove(report_path)


def test_raw_layout_sibling():
    print("\n=== A.3: RAW_LAYOUT=sibling ===")
    src = os.path.join(WORK, "src_rawlayout")
    jpg = os.path.join(src, "Album", "IMG_0001.JPG")
    image(jpg, 1500, 1100, exif=True, dt="2019:07:15 12:00:00")
    with open(os.path.join(src, "Album", "IMG_0001.CR2"), "wb") as f:
        f.write(b"FAKE-CR2-PAIRED" + os.urandom(256))
    tgt = os.path.join(WORK, "target_raw_sibling")
    cfg_path = os.path.join(ROOT, "photoarchive_config.yaml")
    with open(cfg_path, "w", encoding="utf-8") as f:
        f.write("raw_layout: sibling\n")
    try:
        r = run_photosort(src, tgt)
    finally:
        os.remove(cfg_path)
    check(r.returncode == 0, "A.3: raw_layout=sibling run exits 0")
    check(os.path.isfile(os.path.join(tgt, "Albums", "Album", "RAW", "IMG_0001.CR2")),
          "A.3: RAW placed sibling-style next to its JPEG (Albums/Album/RAW/)")
    raw_root = os.path.join(tgt, "RAW")
    check(not os.path.isdir(raw_root) or not any(files for _, _, files in os.walk(raw_root)),
          "A.3: separate RAW/ root (if created) stays empty under sibling layout")


def test_undated_promotion():
    print("\n=== B.2: undated files (ByDate/0000-undated) are excluded from the dedup index "
          "and promote on reprocessing ===")
    src = os.path.join(WORK, "src_undated")
    tgt = os.path.join(WORK, "target_undated")
    for i in range(3):
        image(os.path.join(src, "dcim", f"plain{i}.jpg"), 1000, 800)
    # Force a narrow, non-"now" mtime window on all three siblings so the copy-artifact
    # heuristic (RULES.md "ЛОВУШКА mtime") reliably routes them to Tier D regardless of how
    # fast file creation above actually ran.
    forced_mtime = time.time() - 3600
    for i in range(3):
        p = os.path.join(src, "dcim", f"plain{i}.jpg")
        os.utime(p, (forced_mtime, forced_mtime))

    r = run_photosort(src, tgt)
    check(r.returncode == 0, "B.2: first (undated) run exits 0")
    # Files are walked/processed in sorted (alphabetical) order per directory, and the
    # copy-artifact mtime heuristic (DateContext.record) only has enough same-folder
    # siblings to trigger once at least 3 files in this dir have been seen -- so it's
    # deterministically the LAST of the three (plain2.jpg) that lands as Tier D/undated;
    # plain0/plain1 get a Tier C mtime-guess date instead (see RULES.md "ЛОВУШКА mtime").
    undated_dest = os.path.join(tgt, "ByDate", "0000-undated", "dcim", "plain2.jpg")
    check(os.path.isfile(undated_dest),
          "B.2: file with no date signal lands in ByDate/0000-undated/, not a separate service folder")

    # Simulate "the source file now carries a date" (new EXIF, or -- in real use -- an
    # improved rules version that can now date it) and reprocess the SAME source, per
    # RULES.md "Смена правил": просто прогнать источники снова.
    image(os.path.join(src, "dcim", "plain2.jpg"), 1000, 800, exif=True, dt="2021:05:01 12:00:00")
    r2 = run_photosort(src, tgt)
    check(r2.returncode == 0, "B.2: second (now-dated) run exits 0")
    promoted_dest = os.path.join(tgt, "ByDate", "2021", "2021-05 [PhotoArchive]", "plain2.jpg")
    check(os.path.isfile(promoted_dest),
          "B.2: file promotes to its dated location -- NOT blocked as already_present by the "
          "undated copy still sitting in the (excluded) dedup index")
    check(os.path.isfile(undated_dest),
          "B.2: old undated copy is untouched (archive is append-only)")


def test_photosort_marker_excludes_subtree():
    print("\n=== B.1: single SKIP_PHOTOSORT.txt at __служебные_файлы root excludes the whole subtree ===")
    src1 = os.path.join(WORK, "src_marker1")
    tgt1 = os.path.join(WORK, "target_marker1")
    image(os.path.join(src1, "dcim", "a.jpg"), 1000, 800, exif=True, dt="2018:01:01 10:00:00")
    # RULES.md "МАРКЕР ПРОПУСКА": a user can manually drop SKIP_PHOTOSORT.txt in ANY folder of
    # any source, not just the auto-created __служебные_файлы umbrella -- exercise that path
    # explicitly (see 2026-07-11 skip_marker actions.log check below): the auto-created marker
    # inside __служебные_файлы never actually reaches the marker-scan code, because
    # __служебные_файлы is ALSO in HARD_EXCLUDE_DIRS and gets excluded by name first.
    manual_marker_dir = os.path.join(src1, "manual_skip")
    image(os.path.join(manual_marker_dir, "skipped.jpg"), 800, 600, exif=True, dt="2018:01:01 10:00:00")
    with open(os.path.join(manual_marker_dir, "SKIP_PHOTOSORT.txt"), "w", encoding="utf-8") as f:
        f.write("test marker\n")
    r = run_photosort(src1, tgt1)
    check(r.returncode == 0, "B.1: initial build exits 0")
    check(os.path.isfile(os.path.join(tgt1, "__служебные_файлы", "SKIP_PHOTOSORT.txt")),
          "B.1: SKIP_PHOTOSORT.txt auto-created at __служебные_файлы root (single marker, not per-subfolder)")
    check(not any(f == "skipped.jpg" for _, _, files in os.walk(tgt1) for f in files),
          "B.1: manually-marked folder's content excluded from the built archive")
    with open(os.path.join(tgt1, "__служебные_файлы", "logs", "actions.log"), encoding="utf-8") as f:
        actions1 = f.read()
    check("[skip_marker]" in actions1 and "manual_skip" in actions1,
          "B.1/2026-07-11: manually-placed marker's skip event is persisted to actions.log, "
          "not just printed transiently")

    # Plant a real photo INSIDE the service tree (under logs\, still fully marker-protected --
    # unlike disputed files, which moved OUT to a top-level _Unsorted\ in 2026-07-11 and
    # are deliberately NOT under this marker any more, see test_unsorted_is_not_marker_protected)
    # to prove the whole __служебные_файлы subtree is excluded, not just empty.
    sneaky = os.path.join(tgt1, "__служебные_файлы", "logs", "sneaky", "hidden.jpg")
    image(sneaky, 800, 600)

    tgt2 = os.path.join(WORK, "target_marker2")
    r2 = run_photosort(tgt1, tgt2)  # reuse tgt1 (an existing archive) as SOURCE for a new one
    check(r2.returncode == 0, "B.1: reindexing an existing archive as SOURCE exits 0")
    check(not os.path.isfile(os.path.join(tgt2, "Albums", "sneaky", "hidden.jpg")),
          "B.1: file inside __служебные_файлы/ is NOT picked up when the archive is walked as SOURCE")
    # p.5.2: the cascade ("готовый архив как источник") must re-place a.jpg by date, not
    # swallow the tagged day-folder (or the "ByDate" segment itself) as a bogus album.
    check(os.path.isfile(os.path.join(tgt2, "ByDate", "2018", "2018-01 [PhotoArchive]", "a.jpg")),
          "B.1/5.2: cascade re-places a dump file from an existing archive by date "
          "(tagged day-folder + 'ByDate' segment both correctly recognized as dump, not album)")
    check(not os.path.isdir(os.path.join(tgt2, "Albums", "ByDate")),
          "B.1/5.2: 'ByDate' segment itself never becomes a bogus top-level album")

    # 2026-07-11: __служебные_файлы is itself in HARD_EXCLUDE_DIRS, so when tgt1 is walked as
    # SOURCE it is excluded by NAME before the marker-scan code is ever reached (the marker is
    # a second, redundant layer of protection for this specific folder, see RULES.md) --
    # confirm the (now-persisted) [EXCLUDE] line shows up for it, not a [skip_marker] line.
    with open(os.path.join(tgt2, "__служебные_файлы", "logs", "actions.log"), encoding="utf-8") as f:
        actions2 = f.read()
    check("[EXCLUDE] __служебные_файлы:" in actions2,
          "B.1/2026-07-11: __служебные_файлы hard-exclude hit is now persisted to actions.log "
          "(previously silent, name-gate short-circuits before the marker-scan code runs)")

    # "Правило явного указания": pointing SOURCE straight at the excluded folder still
    # processes it.
    tgt3 = os.path.join(WORK, "target_marker3")
    r3 = run_photosort(os.path.join(tgt1, "__служебные_файлы", "logs"), tgt3)
    check(r3.returncode == 0, "B.1: explicit SOURCE into __служебные_файлы/logs exits 0")
    check(os.path.isfile(os.path.join(tgt3, "Albums", "sneaky", "hidden.jpg")),
          "B.1: explicit SOURCE overrides the exclusion (RULES.md \"правило явного указания\")")


def test_unsorted_is_not_marker_protected():
    print("\n=== 2026-07-11: disputed files land in a TOP-LEVEL _Unsorted\\, NOT under "
          "the __служебные_файлы\\ marker-protected umbrella ===")
    # A disputed file (small/borderline image, no camera EXIF -- classify_zone/is_media
    # routes it to disputed rather than a confident append) must end up as a real, visible
    # sibling of Albums/ByDate/RAW, not buried one level inside the service umbrella.
    src = os.path.join(WORK, "src_unsorted_toplevel")
    small_icon = os.path.join(src, "icon.jpg")
    image(small_icon, 32, 32)  # tiny, no EXIF -- disputed, not a confident append
    tgt = os.path.join(WORK, "target_unsorted_toplevel")
    r = run_photosort(src, tgt)
    check(r.returncode == 0, "unsorted-toplevel: run exits 0")
    check(os.path.isdir(os.path.join(tgt, "_Unsorted")),
          "unsorted-toplevel: _Unsorted\\ exists as a TOP-LEVEL folder in TARGET")
    check(not os.path.isdir(os.path.join(tgt, "__служебные_файлы", "disputed")),
          "unsorted-toplevel: the old __служебные_файлы\\disputed\\ location no longer exists")
    entries = os.listdir(os.path.join(tgt, "__служебные_файлы"))
    check("disputed" not in [e.lower() for e in entries],
          "unsorted-toplevel: __служебные_файлы\\ itself no longer contains a disputed\\ subfolder")


def test_cli_version_help_routing():
    print("\n=== B.3: --version/--help routed past the archive shim ===")
    r = subprocess.run([sys.executable, SCRIPT, "--version"], capture_output=True,
                        text=True, encoding="utf-8", errors="replace")
    check(r.returncode == 0, "B.3: --version exits 0")
    check("PhotoArchive" in r.stdout and "rules" in r.stdout,
          "B.3: --version prints program version + rules version")
    check("Vladimir Oleynikov" in r.stdout and "Apache License 2.0" in r.stdout,
          "2026-07-13: --version also prints copyright/license, matching LICENSE")

    r2 = subprocess.run([sys.executable, SCRIPT, "-V"], capture_output=True,
                         text=True, encoding="utf-8", errors="replace")
    check(r2.returncode == 0 and "PhotoArchive" in r2.stdout, "B.3: -V short flag works")

    r3 = subprocess.run([sys.executable, SCRIPT, "--help"], capture_output=True,
                         text=True, encoding="utf-8", errors="replace")
    check(r3.returncode == 0, "B.3: top-level --help exits 0")
    check("analyze-quick" in r3.stdout and "analyze-full" in r3.stdout,
          "B.3: top-level --help lists analyze-* subcommands (not swallowed by the archive shim)")
    check("vo1012.github.io/PhotoArchive" in r3.stdout,
          "2026-07-20: --help epilog links the project site (SITE_URL), not the raw repo")
    check("актуальные способы" in r3.stdout and "коммерческой выгоды" in r3.stdout,
          "2026-07-15: --help epilog carries the donation text (DONATION_TEXT). 2026-07-17: "
          "rewritten to point at a source of current details instead of a placeholder about "
          "the compromised card -- --help never gets the real card number, not even in the "
          "manual-distribution build (see build/md_to_pdf.py's _inject_donation_details, "
          "which only ever touches PhotoArchive_ot_avtora.md/FAQ.md). 2026-07-20: that source "
          "is now the project site (SITE_URL), not GitHub -- wording diverged on purpose from "
          "PhotoArchive_ot_avtora.md's P.S., which stays on GitHub since it's read there.")
    check("скомпрометирована" not in r3.stdout,
          "2026-07-17: stale compromised-card placeholder must not linger in --help")

    r5 = subprocess.run([sys.executable, SCRIPT, "--formats"], capture_output=True,
                         text=True, encoding="utf-8", errors="replace")
    check(r5.returncode == 0, "B.3: --formats exits 0")
    check(all(ext in r5.stdout for ext in ("jpg", "cr2", "mp4", "zip")),
          "B.3: --formats prints extensions from all recognized categories")

    # Backward-compat shim: --source/--target with NO explicit subcommand must still route to
    # "archive" (this is the whole reason the shim exists).
    src = os.path.join(WORK, "src_cli_shim")
    tgt = os.path.join(WORK, "target_cli_shim")
    image(os.path.join(src, "dcim", "a.jpg"), 1000, 800, exif=True, dt="2018:01:01 10:00:00")
    r4 = run_photosort(src, tgt)  # no subcommand -- exactly the pre-А.5 CLI shape
    check(r4.returncode == 0, "B.3: --source/--target without a subcommand still exits 0")
    check(os.path.isdir(os.path.join(tgt, "__служебные_файлы")),
          "B.3: --source/--target without a subcommand still routes to the archive build")


def test_target_nested_warning():
    print("\n=== B.4: warning when TARGET is nested inside an existing archive ===")
    old_archive = os.path.join(WORK, "nest_oldarchive")
    nested_target = os.path.join(old_archive, "Albums", "Свадьба")
    src = os.path.join(WORK, "nest_src")
    image(os.path.join(src, "x.jpg"), 800, 600, exif=True, dt="2019:01:01 10:00:00")

    # Seed a pre-existing archive shape at old_archive (Albums+ByDate) before running --
    # simulates a real archive without actually building one.
    os.makedirs(os.path.join(old_archive, "Albums"), exist_ok=True)
    os.makedirs(os.path.join(old_archive, "ByDate"), exist_ok=True)

    r = run_photosort(src, nested_target)
    check(r.returncode == 0, "B.4: run against a nested TARGET still exits 0 (warns, doesn't block)")
    check("подпапк" in r.stdout,
          "B.4: warns when TARGET looks like a subfolder of an existing archive")

    sibling_target = os.path.join(WORK, "nest_sibling_archive")
    r2 = run_photosort(src, sibling_target)
    check(r2.returncode == 0, "B.4: sibling top-level archive run exits 0")
    check("подпапк" not in r2.stdout,
          "B.4: stays silent for a fresh sibling top-level archive (not nested)")


def test_check_bundled_tools_detects_broken_frozen_build():
    print("\n=== security audit finding #2 (2026-07-10 follow-up): a broken frozen build "
          "(bin/<tool>.exe missing) is now flagged by check_bundled_tools() instead of being "
          "silently confused with the intentional dev-on-Linux PATH fallback ===")
    # bin/ in this checkout intentionally has no actual binaries (see bin/README-BIN.md,
    # .gitignore) -- both branches below deterministically hit "candidate does not exist".
    code_frozen = (
        "import sys, os; sys.path.insert(0, %r)\n"
        "for s in (sys.stdout, sys.stderr):\n"
        "    s.reconfigure(encoding='utf-8', errors='replace')\n"
        "sys.frozen = True\n"  # set BEFORE import: module-level *_BIN constants are computed
        "import photosort_win as m\n"          # once, at import time
        "print('unrar_is_abs:', os.path.isabs(m.UNRAR_BIN))\n"
        "messages = []\n"
        "m.check_bundled_tools(log=messages.append)\n"
        "print('flagged_unrar:', any('unrar' in msg and m.UNRAR_BIN in msg for msg in messages))\n"
    ) % ROOT
    r = subprocess.run([sys.executable, "-c", code_frozen], capture_output=True, text=True,
                        encoding="utf-8", errors="replace")
    check(r.returncode == 0, f"audit#2: frozen unit test exits 0 (stderr={r.stderr[-500:]})")
    check("unrar_is_abs: True" in r.stdout,
          "audit#2: frozen + missing bin/UnRAR.exe -> tool_binary() keeps the absolute path")
    check("flagged_unrar: True" in r.stdout,
          "audit#2: check_bundled_tools() now reports the missing binary instead of staying silent")

    # Not frozen (the normal way this whole test suite runs photosort_win.py) must keep
    # falling back to a bare PATH name exactly as before this fix -- dev machines with
    # exiftool/ffmpeg/7z/unrar already installed system-wide (see tool_binary() docstring).
    # This particular checkout may have a REAL bundled bin/UnRAR.exe physically present (a
    # portable Windows dev/test setup per bin/README-BIN.md keeps real binaries there, unlike
    # the Linux/VPS dev environment this branch models, where bin/ is genuinely empty and
    # system-installed tools are on PATH instead) -- move it aside for this one subprocess call
    # so the fallback branch itself is exercised, not just whatever happens to be on disk.
    bin_unrar = os.path.join(ROOT, "bin", "UnRAR.exe")
    bin_unrar_backup = bin_unrar + ".ci_test_backup"
    moved_aside = os.path.isfile(bin_unrar)
    if moved_aside:
        os.replace(bin_unrar, bin_unrar_backup)
    try:
        code_dev = (
            "import sys; sys.path.insert(0, %r)\n"
            "import photosort_win as m\n"
            "print('dev_path:', m.UNRAR_BIN)\n"
        ) % ROOT
        r2 = subprocess.run([sys.executable, "-c", code_dev], capture_output=True, text=True,
                             encoding="utf-8", errors="replace")
    finally:
        if moved_aside:
            os.replace(bin_unrar_backup, bin_unrar)
    check(r2.returncode == 0, "audit#2: dev-mode (not frozen) unit test exits 0")
    check("dev_path: unrar" in r2.stdout,
          "audit#2: not frozen + missing bin/UnRAR.exe -> tool_binary() still falls back to "
          "a bare PATH name (unchanged dev-on-Linux behavior)")


def test_ctrl_c_no_traceback():
    print("\n=== 5.1: Ctrl-C prints a short message, not a traceback ===")
    # A real SIGINT/CTRL_C_EVENT mid-run is flaky to simulate reliably from a test harness
    # (POSIX vs Windows console-signal semantics differ) -- exercise the exact code path
    # (main() catching KeyboardInterrupt raised out of _main()) directly instead.
    # Mirror photosort_win.py's own __main__ guard (see its comment on why -- Windows'
    # default console codepage isn't guaranteed to encode Cyrillic) -- this test calls
    # main() directly, bypassing that guard, so it must redo the same setup itself or the
    # Russian "Прервано..." message below throws UnicodeEncodeError before ever reaching
    # sys.exit(130), which looks like this test failing for an unrelated reason.
    code = (
        "import sys; sys.path.insert(0, %r)\n"
        "for s in (sys.stdout, sys.stderr):\n"
        "    s.reconfigure(encoding='utf-8', errors='replace')\n"
        "import photosort_win as m\n"
        "def _raise(): raise KeyboardInterrupt\n"
        "m._main = _raise\n"
        "m.main()\n"
    ) % ROOT
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                        encoding="utf-8", errors="replace")
    check(r.returncode == 130, f"5.1: Ctrl-C exits with code 130 (got {r.returncode})")
    check("Traceback" not in r.stdout and "Traceback" not in r.stderr,
          "5.1: Ctrl-C produces no traceback")
    check("Прервано" in r.stdout, "5.1: Ctrl-C prints a short Russian message")


def test_eof_no_traceback():
    print("\n=== EOF on any interactive prompt prints a short message, not a traceback ===")
    # Found on real Windows hardware while testing the bare-launch menu (run_bare_launch()
    # adds several new input() prompts aimed exactly at non-technical users): Ctrl-Z+Enter
    # (Windows' EOF keystroke) or a closed/redirected stdin raises EOFError out of input() --
    # same principle as Ctrl-C above, mirrored the same way (exercise main() catching the
    # exception directly, since reliably triggering a real EOF keystroke from a test harness
    # is as flaky as a real SIGINT).
    code = (
        "import sys; sys.path.insert(0, %r)\n"
        "for s in (sys.stdout, sys.stderr):\n"
        "    s.reconfigure(encoding='utf-8', errors='replace')\n"
        "import photosort_win as m\n"
        "def _raise(): raise EOFError\n"
        "m._main = _raise\n"
        "m.main()\n"
    ) % ROOT
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                        encoding="utf-8", errors="replace")
    check(r.returncode == 130, f"EOF: exits with code 130 (got {r.returncode})")
    check("Traceback" not in r.stdout and "Traceback" not in r.stderr,
          "EOF: produces no traceback")
    check("Ввод прерван" in r.stdout, "EOF: prints a short Russian message")


def test_near_dup_appended_not_skipped():
    print("\n=== 5.7: near-dup image is appended (not skipped), logged with the Hamming distance ===")
    # Same pixel content saved at two JPEG quality levels: perceptual hash stays within the
    # near-dup threshold (recompression barely moves phash), but SHA256 differs (not an exact
    # dupe) and file size differs (so image_is_strictly_better() picks a clear "not better"
    # loser) -- a deterministic way to construct a real near-dup pair without hand-crafting
    # pixel-level similarity.
    from PIL import Image
    src = os.path.join(WORK, "src_near_dup")
    os.makedirs(src, exist_ok=True)
    im = Image.new("RGB", (1200, 900))
    px = im.load()
    random.seed("near_dup_base")
    for x in range(0, 1200, 4):
        for y in range(0, 900, 4):
            color = (random.randint(0, 255), random.randint(0, 255), random.randint(0, 255))
            for dx in range(4):
                for dy in range(4):
                    px[x + dx, y + dy] = color
    first = os.path.join(src, "burst_1.jpg")
    second = os.path.join(src, "burst_2.jpg")
    im.save(first, "JPEG", quality=95)
    im.save(second, "JPEG", quality=40)
    for p, dt in ((first, "2022:03:10 10:00:00"), (second, "2022:03:10 10:00:05")):
        import tempfile as _tempfile
        with _tempfile.NamedTemporaryFile(mode="w", suffix=".args", delete=False, encoding="utf-8") as af:
            af.write(p + "\n")
            argfile_path = af.name
        try:
            subprocess.run(["exiftool", "-charset", "filename=utf8", "-overwrite_original",
                             f"-DateTimeOriginal={dt}", "-Make=Canon", "-Model=Canon EOS 80D",
                             "-@", argfile_path], capture_output=True)
        finally:
            os.unlink(argfile_path)

    tgt = os.path.join(WORK, "target_near_dup")
    r = run_photosort(src, tgt)
    check(r.returncode == 0, "5.7: run exits 0")
    dest_dir = os.path.join(tgt, "ByDate", "2022", "2022-03 [PhotoArchive]")
    both_present = (os.path.isfile(os.path.join(dest_dir, "burst_1.jpg"))
                     and os.path.isfile(os.path.join(dest_dir, "burst_2.jpg")))
    check(both_present, "5.7: BOTH near-dup files physically present (neither silently dropped)")
    appended = read_csv(os.path.join(tgt, "__служебные_файлы", "logs", "appended.csv"))
    near_dup_rows = [row for row in appended if row["reason"].startswith("near_dup_of=")]
    check(len(near_dup_rows) == 1,
          f"5.7: exactly one appended.csv row logged as near-dup (found {len(near_dup_rows)})")
    check(near_dup_rows and "_hamming=" in near_dup_rows[0]["reason"],
          "5.7: near-dup reason includes the Hamming distance")
    skipped = read_csv(os.path.join(tgt, "__служебные_файлы", "logs", "skipped.csv"))
    check(not any("equivalent" in row["reason"] for row in skipped),
          "5.7: near-dup no longer appears in skipped.csv as equivalent_present")


def test_bare_date_subfolder_not_collapsed_as_dump():
    print("\n=== 5.2: a bare date-shaped folder (no [PhotoArchive] tag) is a real segment, "
          "not auto-collapsed as dump ===")
    # Before p.5.2, DUMP_SEGMENT_REGEXES anchored a bare YYYY-MM-DD-shaped name as "dump"
    # purely by looking like a date -- a real user subfolder named e.g. "2015-08-20" inside
    # their own album would get silently collapsed out of the destination path. Now dump
    # status for a date-shaped folder requires the program's own [PhotoArchive] tag.
    src = os.path.join(WORK, "src_datefolder_album")
    jpg = os.path.join(src, "Отпуск", "2015-08-20", "photo.jpg")
    image(jpg, 1300, 1000, exif=True, dt="2015:08:20 10:00:00")
    tgt = os.path.join(WORK, "target_datefolder_album")
    r = run_photosort(src, tgt)
    check(r.returncode == 0, "5.2: run exits 0")
    check(os.path.isfile(os.path.join(tgt, "Albums", "Отпуск", "2015-08-20", "photo.jpg")),
          "5.2: bare date-named subfolder inside a real album is preserved, not collapsed")


def test_desktop_is_dump_segment():
    print("\n=== 2026-07-11 finding: 'Desktop'/'Рабочий стол' is a recognized dump segment "
          "(sort by date), not swallowed whole as an album ===")
    # Found on a real archive: a loose photo directly under .../Desktop/ landed as an album
    # instead of being sorted by date -- "Desktop" wasn't in DUMP_SEGMENT_NAMES. The pure
    # is_dump_segment() unit check for this now lives in tests/test_dump_segments.py
    # (test_is_dump_segment_known_names) -- this test keeps only the end-to-end pipeline part.

    # End-to-end: a photo with a reliable EXIF date sitting in a real user's Desktop folder
    # now sorts by date instead of becoming Albums\Desktop\.
    src = os.path.join(WORK, "src_desktop_dump")
    jpg = os.path.join(src, "Desktop", "photo.jpg")
    image(jpg, 1300, 1000, exif=True, dt="2023:10:02 11:25:21")
    tgt = os.path.join(WORK, "target_desktop_dump")
    r2 = run_photosort(src, tgt)
    check(r2.returncode == 0, "dump-segments: end-to-end run exits 0")
    check(os.path.isfile(os.path.join(tgt, "ByDate", "2023", "2023-10 [PhotoArchive]", "photo.jpg")),
          "dump-segments: a dated photo directly under Desktop\\ now sorts by date")
    check(not os.path.isdir(os.path.join(tgt, "Albums", "Desktop")),
          "dump-segments: Albums\\Desktop\\ is no longer created for a dated loose photo")


def test_camera_roll_and_new_folder_are_dump_segments():
    print("\n=== 2026-07-11 finding (second pass, real archives): 'Camera Roll' and "
          "'Новая папка'/'New Folder' (incl. numbered copies) are recognized dump segments ===")
    # Found on a real archive: E:\ has "Новая папка" AND "Новая папка (2)" as sibling
    # top-level folders (Windows' own default name for an unrenamed folder, numbered for each
    # further one), plus a real Pictures\Camera Roll (standard Windows/OneDrive phone-sync
    # folder) -- neither was in DUMP_SEGMENT_NAMES/DUMP_SEGMENT_REGEXES before this. The pure
    # is_dump_segment() unit check for this now lives in tests/test_dump_segments.py
    # (test_is_dump_segment_known_names) -- this test keeps only the end-to-end pipeline part.

    # End-to-end: a photo dropped straight in an unrenamed "Новая папка" sorts by date, not
    # into an Albums\Новая папка\ pile.
    src = os.path.join(WORK, "src_new_folder_dump")
    jpg = os.path.join(src, "Новая папка", "photo.jpg")
    image(jpg, 1300, 1000, exif=True, dt="2022:03:15 09:00:00")
    tgt = os.path.join(WORK, "target_new_folder_dump")
    r2 = run_photosort(src, tgt)
    check(r2.returncode == 0, "camera-roll/new-folder: end-to-end run exits 0")
    check(os.path.isfile(os.path.join(tgt, "ByDate", "2022", "2022-03 [PhotoArchive]", "photo.jpg")),
          "camera-roll/new-folder: a dated photo directly under 'Новая папка' now sorts by date")
    check(not os.path.isdir(os.path.join(tgt, "Albums", "Новая папка")),
          "camera-roll/new-folder: Albums\\Новая папка\\ is not created for a dated loose photo")


def test_force_dump_tilde_prefix():
    print("\n=== 2026-07-11 (user request): a '~'-prefixed folder name is ALWAYS treated as "
          "dump (not an album), even if the plain name would look like a real album ===")
    # User's own scenario: a cloud-sync folder named "Яндекс_диск" reads as a plausible real
    # album name (correctly NOT in DUMP_SEGMENT_NAMES) -- renaming it to "~Яндекс_диск" is a
    # manual, read-only (source is never renamed BY the program) way to force it to sort by
    # date instead. See FORCE_DUMP_PREFIX in photosort_win.py. The pure is_dump_segment() unit
    # check for this now lives in tests/test_dump_segments.py (test_force_dump_tilde_prefix)
    # -- this test keeps only the end-to-end pipeline part.

    # End-to-end: a photo directly under "~Яндекс_диск" sorts by date, not into
    # Albums\~Яндекс_диск\.
    src = os.path.join(WORK, "src_force_dump_tilde")
    jpg = os.path.join(src, "~Яндекс_диск", "photo.jpg")
    image(jpg, 1300, 1000, exif=True, dt="2021:09:09 08:00:00")
    tgt = os.path.join(WORK, "target_force_dump_tilde")
    r2 = run_photosort(src, tgt)
    check(r2.returncode == 0, "force-dump-tilde: end-to-end run exits 0")
    check(os.path.isfile(os.path.join(tgt, "ByDate", "2021", "2021-09 [PhotoArchive]", "photo.jpg")),
          "force-dump-tilde: a dated photo directly under '~Яндекс_диск' sorts by date")
    check(not os.path.isdir(os.path.join(tgt, "Albums", "~Яндекс_диск")),
          "force-dump-tilde: Albums\\~Яндекс_диск\\ is not created for a dated loose photo")


def test_merged_album_marker_file():
    print("\n=== 2026-07-11 (user request): __ВНИМАНИЕ_объединённая_папка.txt appears when "
          "an album is actually populated from more than one physical source location ===")
    # Real-world scenario discussed with the user: two differently-named album folders share
    # SOME exact-duplicate content (e.g. wedding photos for the groom's vs bride's relatives,
    # overlapping only on the couple's own shared photos). Content-based dedup is global and
    # not album-aware -- the duplicate physically survives only in whichever album was walked
    # first, and the SECOND album gets nothing for that file. Nothing is copied twice (hardlink/
    # real-copy/symlink all considered and rejected, see README.md) -- instead the WINNING
    # album gets a visible marker file listing where else its content conceptually came from.

    # --- unit-level: find_album() now returns a third value, album_prefix -- the path from
    # SOURCE root through (and including) the album segment itself, collapsing dump segments
    # the same way album-name selection already does, and working uniformly for an
    # archive-derived album (the archive's own filename occupies the boundary segment).
    code = (
        "import sys; sys.path.insert(0, %r)\n"
        "sys.stdout.reconfigure(encoding='utf-8', errors='replace')\n"
        "import photosort_win as m\n"
        "album, subpath, prefix = m.find_album('Свадьба_жених/img.jpg', None)\n"
        "print('plain_album:', album, '|', prefix)\n"
        "album2, subpath2, prefix2 = m.find_album('YandexDisk/Фотокамера/img.jpg', None)\n"
        "print('dump_subfolder_collapsed:', album2, '|', prefix2)\n"
        "album3, subpath3, prefix3 = m.find_album('Downloads/archive-2020-07-01/inner/photo.jpg', 1)\n"
        "print('archive_derived:', album3, '|', prefix3, '|', subpath3)\n"
    ) % ROOT
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                        encoding="utf-8", errors="replace")
    check(r.returncode == 0, f"merged-album-marker: find_album unit script exits 0 (stderr={r.stderr[-500:]})")
    check("plain_album: Свадьба_жених | Свадьба_жених" in r.stdout,
          "merged-album-marker: album_prefix for a plain disk album equals the album name itself")
    check("dump_subfolder_collapsed: YandexDisk | YandexDisk" in r.stdout,
          "merged-album-marker: album_prefix stops at the album segment, dump subfolders "
          "(Фотокамера) don't extend it")
    check("archive_derived: archive-2020-07-01 | Downloads/archive-2020-07-01 | ['inner']" in r.stdout,
          "merged-album-marker: album_prefix for an archive-derived album includes the disk-side "
          "path through the archive's own filename segment")

    # --- end-to-end: АльбомА (walked first) and АльбомБ share one byte-identical photo, plus
    # АльбомБ has one unique photo of its own.
    src = os.path.join(WORK, "src_merged_album")
    shared_a = os.path.join(src, "АльбомА", "shared.jpg")
    shared_b = os.path.join(src, "АльбомБ", "shared.jpg")
    unique_b = os.path.join(src, "АльбомБ", "unique.jpg")
    image(shared_a, 1200, 900, exif=True, dt="2020:05:05 10:00:00")
    image(unique_b, 1200, 900, exif=True, dt="2020:06:06 10:00:00")
    shutil.copyfile(shared_a, shared_b)  # byte-identical on purpose -- this IS the duplicate

    tgt = os.path.join(WORK, "target_merged_album")
    r2 = run_photosort(src, tgt)
    check(r2.returncode == 0, "merged-album-marker: end-to-end run exits 0")
    marker_a = os.path.join(tgt, "Albums", "АльбомА", "__ВНИМАНИЕ_объединённая_папка.txt")
    marker_b = os.path.join(tgt, "Albums", "АльбомБ", "__ВНИМАНИЕ_объединённая_папка.txt")
    check(os.path.isfile(marker_a),
          "merged-album-marker: marker file created in the WINNING album (АльбомА, walked first)")
    check(not os.path.isfile(marker_b),
          "merged-album-marker: no marker in АльбомБ -- it never actually merged anything INTO itself")
    if os.path.isfile(marker_a):
        with open(marker_a, encoding="utf-8") as f:
            marker_text = f.read()
        check(marker_text.rstrip("\n").endswith("— АльбомБ"),
              f"merged-album-marker: marker line names the OTHER album as the source (got: {marker_text!r})")
    check(os.path.isfile(os.path.join(tgt, "Albums", "АльбомБ", "unique.jpg")),
          "merged-album-marker: АльбомБ's own unique photo is still archived normally")

    # --- dry-run: detection is reported (both in build_final_summary's text and in stdout),
    # but nothing is physically written -- same "пробный прогон ничего не пишет" guarantee as
    # real file copies.
    tgt_dry = os.path.join(WORK, "target_merged_album_dry")
    r3 = run_photosort(src, tgt_dry, extra_args=["--dry-run"])
    check(r3.returncode == 0, "merged-album-marker: dry-run exits 0")
    check("АльбомА ← АльбомБ" in r3.stdout,
          "merged-album-marker: dry-run stdout reports the merge BEFORE any real build")
    # resolve_dest_path() unconditionally creates the destination folder skeleton even at
    # dry_run (pre-existing, documented quirk, out of scope here -- see ROADMAP.md) -- so
    # Albums\АльбомА\ itself may exist, but it must be empty: no photo, and definitely no
    # marker file (that write is explicitly dry_run-gated in _note_album_source()).
    check(not os.path.isfile(os.path.join(tgt_dry, "Albums", "АльбомА",
                                           "__ВНИМАНИЕ_объединённая_папка.txt")),
          "merged-album-marker: dry-run does not physically write the marker file")
    check(not os.path.isfile(os.path.join(tgt_dry, "Albums", "АльбомА", "shared.jpg")),
          "merged-album-marker: dry-run does not physically copy any file either")

    # --- repeat run: a THIRD source folder ("АльбомВ") sharing the same photo gets appended
    # later -- a new line (with a blank separator before it) should join the existing marker.
    third = os.path.join(WORK, "src_merged_album_round2")
    shared_c = os.path.join(third, "АльбомВ", "shared.jpg")
    os.makedirs(os.path.dirname(shared_c), exist_ok=True)
    shutil.copyfile(shared_a, shared_c)
    r4 = run_photosort(third, tgt)
    check(r4.returncode == 0, "merged-album-marker: second (supplemental) run exits 0")
    with open(marker_a, encoding="utf-8") as f:
        marker_text_2 = f.read()
    check("\n\n" in marker_text_2 or marker_text_2.count("\n") >= 3,
          "merged-album-marker: a blank separator line is inserted before the new run's addition")
    check(marker_text_2.rstrip("\n").endswith("— АльбомВ"),
          f"merged-album-marker: second run appends a new line for the new source (got: {marker_text_2!r})")
    check("— АльбомБ" in marker_text_2,
          "merged-album-marker: the original first-run entry is preserved, not overwritten")


def test_archive_file_always_becomes_an_album():
    print("\n=== 2026-07-11 finding: an archive file is always an album (named after the "
          "archive itself) unless it sits inside an already-meaningful disk-side album ===")
    # Real case: 8 Yandex.Disk export zips all unpack into an internal folder literally
    # called "archive\" -- before this fix, that generic internal name won as "the album"
    # and merged all 8 unrelated exports into one Albums\archive\ pile. Now: a folder name
    # found INSIDE an archive is never trusted alone to name an album -- if the disk-side
    # path has no real album, the ARCHIVE'S OWN FILENAME becomes the album instead, and the
    # archive's internal structure becomes that album's subpath (see find_album(),
    # archive_boundary_idx).
    tmp_jpg = os.path.join(WORK, "_tmp_for_archive_album.jpg")
    image(tmp_jpg, 1300, 1000, exif=True, dt="2023:10:02 11:25:21")

    # Case 1: zip sits loose on the "Рабочий стол" (dump) -- no real disk-side album exists,
    # so the archive's own filename ("yandex_export") becomes the album, and its internal
    # "archive\" folder becomes subpath underneath it, not a competing/winning album name.
    src1 = os.path.join(WORK, "src_archive_album_loose")
    zpath1 = os.path.join(src1, "Рабочий стол", "yandex_export.zip")
    os.makedirs(os.path.dirname(zpath1), exist_ok=True)
    with zipfile.ZipFile(zpath1, "w") as zf:
        zf.write(tmp_jpg, arcname="archive/photo.jpg")
    tgt1 = os.path.join(WORK, "target_archive_album_loose")
    r1 = run_photosort(src1, tgt1)
    check(r1.returncode == 0, "archive-album loose: run exits 0")
    check(os.path.isfile(os.path.join(tgt1, "Albums", "yandex_export", "archive", "photo.jpg")),
          "archive-album loose: album is the ARCHIVE'S OWN name, internal 'archive\\' folder "
          "survives as subpath underneath it, not as a competing album")
    check(not os.path.isdir(os.path.join(tgt1, "Albums", "archive")),
          "archive-album loose: the generic internal folder name never becomes the album itself")

    # Case 2: the SAME zip, but now sitting inside a real, already-meaningful disk album --
    # today's good behavior (archive content merges into the enclosing album) must be
    # unchanged: the archive's own filename must NOT override "Свадьба".
    src2 = os.path.join(WORK, "src_archive_album_nested")
    zpath2 = os.path.join(src2, "Свадьба", "yandex_export.zip")
    os.makedirs(os.path.dirname(zpath2), exist_ok=True)
    with zipfile.ZipFile(zpath2, "w") as zf:
        zf.write(tmp_jpg, arcname="archive/photo.jpg")
    tgt2 = os.path.join(WORK, "target_archive_album_nested")
    r2 = run_photosort(src2, tgt2)
    check(r2.returncode == 0, "archive-album nested: run exits 0")
    check(os.path.isfile(os.path.join(tgt2, "Albums", "Свадьба", "archive", "photo.jpg")),
          "archive-album nested: a real enclosing disk-side album still wins over the "
          "archive's own filename -- zip content merges into 'Свадьба' as before")
    check(not os.path.isdir(os.path.join(tgt2, "Albums", "yandex_export")),
          "archive-album nested: the archive's own name does not create a separate album "
          "when a real album already exists on the disk side")


def test_bare_digit_date_folder_kept_inside_album_but_not_as_album_name():
    print("\n=== 2026-07-11 finding: a bare 6-8 digit folder ('20240802') never NAMES an "
          "album, but survives as a subpath once already inside a real album ===")
    # The pure unit check on the two-role split (is_dump_segment(for_subpath=...)) now lives
    # in tests/test_dump_segments.py (test_bare_digit_date_folder_two_role_split) -- this test
    # keeps only the end-to-end pipeline part.

    # End-to-end: a real album containing an unrenamed camera-style date folder.
    src = os.path.join(WORK, "src_digit_date_subpath")
    jpg = os.path.join(src, "Отпуск", "20240802", "photo.jpg")
    image(jpg, 1300, 1000, exif=True, dt="2024:08:02 10:00:00")
    tgt = os.path.join(WORK, "target_digit_date_subpath")
    r2 = run_photosort(src, tgt)
    check(r2.returncode == 0, "digit-date subpath: end-to-end run exits 0")
    check(os.path.isfile(os.path.join(tgt, "Albums", "Отпуск", "20240802", "photo.jpg")),
          "digit-date subpath: the unrenamed camera-style date folder survives as a subpath "
          "inside the real album, instead of being collapsed away")


def test_summary_enriched_always():
    print("\n=== 5.3а: summary.txt is always enriched with version/timings/tool versions ===")
    src = os.path.join(WORK, "src_summary_enriched")
    tgt = os.path.join(WORK, "target_summary_enriched")
    image(os.path.join(src, "a.jpg"), 800, 600, exif=True, dt="2022:01:01 10:00:00")
    r = run_photosort(src, tgt)  # no debug flag -- enrichment must appear regardless
    check(r.returncode == 0, "5.3а: run exits 0")
    with open(os.path.join(tgt, "__служебные_файлы", "logs", "summary.txt"), encoding="utf-8") as f:
        summary_text = f.read()
    check("PhotoArchive" in summary_text and "rules" in summary_text,
          "5.3а: summary.txt includes the program version + RULES_VERSION")
    check("Vladimir Oleynikov" in summary_text and "Apache License 2.0" in summary_text,
          "2026-07-13 (author-attribution finding): summary.txt includes a copyright/license "
          "notice, matching LICENSE -- not just tucked away in a repo file nobody opens, and "
          "covers the copyright-notice obligation carried by the bundled GPL/LGPL/Artistic "
          "third-party binaries (see THIRD_PARTY_LICENSES.md)")
    check("Инструменты:" in summary_text and "exiftool=" in summary_text,
          "5.3а: summary.txt includes detected external tool versions")
    check("Тайминги:" in summary_text and "Фаза 0=" in summary_text and "Фаза 1=" in summary_text,
          "5.3а: summary.txt includes per-phase timings")
    # 2026-07-11, по замечанию пользователя: сырой англоязычный дамп stats (appended_new,
    # warn_nested_target и т.п.) раньше был безусловным -- смешивал английские внутренние
    # имена с русским текстом вокруг для обычного пользователя, при этом дублируя те же
    # цифры, что уже есть по-русски в "Итог прогона" ниже. Теперь появляется ТОЛЬКО под
    # debug=true (см. test_debug_flag_actions_log для механики переключения debug).
    check("warn_nested_target: 0" not in summary_text and "appended_new:" not in summary_text,
          "5.3а: raw English stats dump (appended_new/warn_* keys) is NOT shown without debug")

    cfg_path = os.path.join(ROOT, "photoarchive_config.yaml")
    with open(cfg_path, "w", encoding="utf-8") as f:
        f.write("debug: true\n")
    tgt_debug = os.path.join(WORK, "target_summary_enriched_debug")
    try:
        r2 = run_photosort(src, tgt_debug)
    finally:
        os.remove(cfg_path)
    check(r2.returncode == 0, "5.3а: run with debug: true exits 0")
    with open(os.path.join(tgt_debug, "__служебные_файлы", "logs", "summary.txt"), encoding="utf-8") as f:
        summary_text_debug = f.read()
    check("warn_nested_target: 0" in summary_text_debug and "warn_path_truncated: 0" in summary_text_debug,
          "5.3а: raw stats dump (incl. the p.5.3а warning counters) shows up when debug: true")


def test_default_exclude_dirs_configurable_and_logged():
    print("\n=== 2026-07-11: default_exclude_dirs (node_modules/.git/$recycle.bin) is "
          "user-editable, unlike HARD_EXCLUDE_DIRS -- and both are now aggregated + "
          "persisted to actions.log/summary.txt instead of being silently dropped ===")

    # --- unit-level: the heuristic-only names moved OUT of HARD_EXCLUDE_DIRS, the
    # OS-inaccessible ones stayed.
    code = (
        "import sys; sys.path.insert(0, %r)\n"
        "import photosort_win as m\n"
        "print('recycle_bin_not_hard:', '$recycle.bin' not in m.HARD_EXCLUDE_DIRS)\n"
        "print('node_modules_not_hard:', 'node_modules' not in m.HARD_EXCLUDE_DIRS)\n"
        "print('git_not_hard:', '.git' not in m.HARD_EXCLUDE_DIRS)\n"
        "print('svi_still_hard:', 'system volume information' in m.HARD_EXCLUDE_DIRS)\n"
        "print('default_still_hard:', 'default' in m.HARD_EXCLUDE_DIRS)\n"
        "print('umbrella_still_hard:', '__служебные_файлы' in m.HARD_EXCLUDE_DIRS)\n"
        "print('docs_and_settings_hard:', 'documents and settings' in m.HARD_EXCLUDE_DIRS)\n"
        "print('msocache_hard:', 'msocache' in m.HARD_EXCLUDE_DIRS)\n"
        "print('perflogs_hard:', 'perflogs' in m.HARD_EXCLUDE_DIRS)\n"
        "print('recovery_hard:', 'recovery' in m.HARD_EXCLUDE_DIRS)\n"
        "print('default_list_has_them:', set(m.DEFAULT_EXCLUDE_DIR_NAMES) == "
        "{'node_modules', '.git', '$recycle.bin'})\n"
    ) % ROOT
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                        encoding="utf-8", errors="replace")
    check(r.returncode == 0,
          f"default_exclude_dirs: HARD_EXCLUDE_DIRS unit script exits 0 (stderr={r.stderr[-500:]})")
    for line in ("recycle_bin_not_hard: True", "node_modules_not_hard: True", "git_not_hard: True",
                 "svi_still_hard: True", "default_still_hard: True", "umbrella_still_hard: True",
                 "docs_and_settings_hard: True", "msocache_hard: True", "perflogs_hard: True",
                 "recovery_hard: True", "default_list_has_them: True"):
        check(line in r.stdout, f"default_exclude_dirs: {line}")

    # --- e2e, default config: node_modules/.git are still skipped by default (same
    # behavior as before the split), and two separate node_modules folders aggregate into
    # ONE actions.log line with count=2, not two separate lines.
    src = os.path.join(WORK, "src_default_exclude")
    image(os.path.join(src, "normal", "a.jpg"), 800, 600, exif=True, dt="2023:03:03 10:00:00")
    image(os.path.join(src, "node_modules", "hidden1.jpg"), 800, 600)
    image(os.path.join(src, "sub", "node_modules", "hidden2.jpg"), 800, 600)
    image(os.path.join(src, ".git", "hidden3.jpg"), 800, 600)

    tgt_default = os.path.join(WORK, "target_default_exclude_on")
    r_default = run_photosort(src, tgt_default)
    check(r_default.returncode == 0, "default_exclude_dirs: default-config run exits 0")
    found_default = {f for _, _, files in os.walk(tgt_default) for f in files}
    check("a.jpg" in found_default, "default_exclude_dirs: normal photo still archived")
    check(not ({"hidden1.jpg", "hidden2.jpg", "hidden3.jpg"} & found_default),
          "default_exclude_dirs: node_modules/.git contents skipped by default, same as before")

    with open(os.path.join(tgt_default, "__служебные_файлы", "logs", "actions.log"),
              encoding="utf-8") as f:
        actions_default = f.read()
    check("[EXCLUDE] node_modules: пропущено 2 раз" in actions_default,
          "default_exclude_dirs: two separate node_modules hits aggregate into ONE line (count=2)")
    check("[EXCLUDE] .git: пропущено 1 раз" in actions_default,
          "default_exclude_dirs: single .git hit logged")

    with open(os.path.join(tgt_default, "__служебные_файлы", "logs", "summary.txt"),
              encoding="utf-8") as f:
        summary_default = f.read()
    check("Пропущено служебных/системных папок: 3" in summary_default,
          "default_exclude_dirs: human summary shows the total (2 node_modules + 1 .git)")

    # --- e2e, default_exclude_dirs: [] via photoarchive_config.yaml: the SAME node_modules/.git folders
    # are no longer skipped -- proves the list is genuinely user-removable, not just
    # cosmetically renamed.
    cfg_path = os.path.join(ROOT, "photoarchive_config.yaml")
    with open(cfg_path, "w", encoding="utf-8") as f:
        f.write("default_exclude_dirs: []\n")
    tgt_off = os.path.join(WORK, "target_default_exclude_off")
    try:
        r_off = run_photosort(src, tgt_off)
    finally:
        os.remove(cfg_path)
    check(r_off.returncode == 0, "default_exclude_dirs: run with default_exclude_dirs=[] exits 0")
    found_off = {f for _, _, files in os.walk(tgt_off) for f in files}
    check({"hidden1.jpg", "hidden2.jpg", "hidden3.jpg"} <= found_off,
          "default_exclude_dirs: [] in photoarchive_config.yaml makes node_modules/.git contents "
          "reachable again -- confirms the list is user-editable, not hardcoded")


def test_config_yaml_autocreate_before_first_prompt():
    print("\n=== 2026-07-11 (live finding): photoarchive_config.yaml must exist even if the user Ctrl-C's "
          "at the VERY FIRST interactive prompt -- admins read photoarchive_config.yaml instead of docs, "
          "so it needs to be there even for an instantly-aborted run, not only once a Config() "
          "is actually built deep inside run_for_source()/run_analyze_for_source() ===")
    code = (
        "import sys; sys.path.insert(0, %r)\n"
        "import tempfile, os, builtins\n"
        "import photosort_win as m\n"
        "\n"
        "def ctrl_c(*a, **k):\n"
        "    raise KeyboardInterrupt\n"
        "\n"
        "# 1) fully bare launch (run_bare_launch()) -- Ctrl-C on the very first menu question,\n"
        "# before source/target are ever asked, let alone a Config() built.\n"
        "d1 = tempfile.mkdtemp()\n"
        "m.CONFIG_YAML_PATH = os.path.join(d1, 'photoarchive_config.yaml')\n"
        "try:\n"
        "    m.run_bare_launch(input_fn=ctrl_c, log=lambda *a, **k: None)\n"
        "except KeyboardInterrupt:\n"
        "    pass\n"
        "print('bare_launch_creates_config:', os.path.exists(m.CONFIG_YAML_PATH))\n"
        "\n"
        "# 2) partial CLI (--source given, --target would be prompted) -- Ctrl-C on that prompt.\n"
        "d2 = tempfile.mkdtemp()\n"
        "m.CONFIG_YAML_PATH = os.path.join(d2, 'photoarchive_config.yaml')\n"
        "builtins.input = ctrl_c\n"
        "sys.argv = ['photosort_win.py', '--source', 'C:/nonexistent_src_for_this_test']\n"
        "try:\n"
        "    m._main()\n"
        "except BaseException:\n"
        "    pass\n"
        "print('partial_cli_creates_config:', os.path.exists(m.CONFIG_YAML_PATH))\n"
    ) % ROOT
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                        encoding="utf-8", errors="replace")
    check(r.returncode == 0,
          f"photoarchive_config.yaml autocreate: script exits 0 (stderr={r.stderr[-800:]})")
    check("bare_launch_creates_config: True" in r.stdout,
          "photoarchive_config.yaml autocreate: created even on Ctrl-C at the very first bare-launch menu question")
    check("partial_cli_creates_config: True" in r.stdout,
          "photoarchive_config.yaml autocreate: created even on Ctrl-C at the first partial-CLI prompt (--target)")


def test_dump_segment_names_configurable():
    print("\n=== 2026-07-11: dump_segment_names/dump_segment_prefixes (photoarchive_config.yaml) -- "
          "editable heuristic dump-folder-name list, split from the four self-protection "
          "names (bydate/albums/raw/_unsorted) which stay hardcoded even with an emptied "
          "config override -- and photoarchive_config.yaml auto-generation on first run ===")

    # --- unit-level: constants split correctly, bare calls (no cfg) still use defaults,
    # Config computes the right effective sets, and load_yaml_config() auto-creates a
    # missing photoarchive_config.yaml from DEFAULT_CONFIG_YAML_TEMPLATE.
    code = (
        "import sys; sys.path.insert(0, %r)\n"
        "import photosort_win as m\n"
        "print('protected_has_four:', m.DUMP_SEGMENT_NAMES_PROTECTED == "
        "frozenset({'bydate', 'albums', 'raw', '_unsorted'}))\n"
        "print('default_excludes_protected:', not (set(n.lower() for n in "
        "m.DEFAULT_DUMP_SEGMENT_NAMES) & m.DUMP_SEGMENT_NAMES_PROTECTED))\n"
        "print('bare_desktop_dump:', m.is_dump_segment('Desktop') is True)\n"
        "print('bare_real_album_not_dump:', m.is_dump_segment('Отпуск 2015') is False)\n"
        "print('protected_survives_empty_override:', 'albums' in m.Config(source='C:/x', "
        "target='C:/y', dump_segment_names=[]).dump_segment_names_lower)\n"
        "print('extra_name_added:', 'yandexdisk' in m.Config(source='C:/x', target='C:/y', "
        "extra_dump_segment_names=['YandexDisk']).dump_segment_names_lower)\n"
        "print('extra_prefix_added:', 'onedrive' in m.Config(source='C:/x', target='C:/y', "
        "extra_dump_segment_prefixes=['OneDrive']).dump_segment_prefixes_tuple)\n"
        "import tempfile, os\n"
        "d = tempfile.mkdtemp()\n"
        "p = os.path.join(d, 'photoarchive_config.yaml')\n"
        "logs = []\n"
        "result = m.load_yaml_config(p, log=logs.append)\n"
        "print('autogen_result_empty:', result == {})\n"
        "print('autogen_file_created:', os.path.exists(p))\n"
        "with open(p, encoding='utf-8') as f: content = f.read()\n"
        "print('autogen_looks_right:', content.startswith('# PhotoArchive') and "
        "'dump_segment_names:' in content)\n"
        "result2 = m.load_yaml_config(p, log=logs.append)\n"
        "print('autogen_second_load_no_crash:', result2 == {})\n"
    ) % ROOT
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                        encoding="utf-8", errors="replace")
    check(r.returncode == 0,
          f"dump_segment_names: unit script exits 0 (stderr={r.stderr[-500:]})")
    for line in ("protected_has_four: True", "default_excludes_protected: True",
                 "bare_desktop_dump: True", "bare_real_album_not_dump: True",
                 "protected_survives_empty_override: True", "extra_name_added: True",
                 "extra_prefix_added: True", "autogen_result_empty: True",
                 "autogen_file_created: True", "autogen_looks_right: True",
                 "autogen_second_load_no_crash: True"):
        check(line in r.stdout, f"dump_segment_names: {line}")

    # --- e2e, default config: "Camera Roll" is still dump by default (bydate placement).
    src = os.path.join(WORK, "src_dump_segment_names")
    image(os.path.join(src, "Camera Roll", "phone1.jpg"), 1200, 900, exif=True,
          dt="2022:05:05 10:00:00")
    tgt_default = os.path.join(WORK, "target_dump_segment_names_default")
    r_default = run_photosort(src, tgt_default)
    check(r_default.returncode == 0, "dump_segment_names: default-config run exits 0")
    check(os.path.isfile(os.path.join(
        tgt_default, "ByDate", "2022", "2022-05 [PhotoArchive]", "phone1.jpg")),
        "dump_segment_names: 'Camera Roll' still dump by default -> ByDate placement")

    cfg_path = os.path.join(ROOT, "photoarchive_config.yaml")

    # --- e2e, extra_dump_segment_names: a cloud-sync folder name that would otherwise become
    # a real album (has letters, not in the default heuristic list) is forced to dump.
    src2 = os.path.join(WORK, "src_dump_segment_names_extra")
    image(os.path.join(src2, "YandexDisk", "sync1.jpg"), 1200, 900, exif=True,
          dt="2022:06:06 10:00:00")
    with open(cfg_path, "w", encoding="utf-8") as f:
        f.write("extra_dump_segment_names: [YandexDisk]\n")
    tgt_extra = os.path.join(WORK, "target_dump_segment_names_extra")
    try:
        r_extra = run_photosort(src2, tgt_extra)
    finally:
        os.remove(cfg_path)
    check(r_extra.returncode == 0, "dump_segment_names: extra_dump_segment_names run exits 0")
    check(not os.path.isdir(os.path.join(tgt_extra, "Albums", "YandexDisk")),
          "dump_segment_names: extra_dump_segment_names prevents 'YandexDisk' from becoming an album")
    check(os.path.isfile(os.path.join(
        tgt_extra, "ByDate", "2022", "2022-06 [PhotoArchive]", "sync1.jpg")),
        "dump_segment_names: extra_dump_segment_names -> ByDate placement instead")

    # --- e2e control, no override: the SAME folder name genuinely becomes an album -- proves
    # the previous result was caused by the config override, not some other rule.
    tgt_noextra = os.path.join(WORK, "target_dump_segment_names_noextra")
    r_noextra = run_photosort(src2, tgt_noextra)
    check(r_noextra.returncode == 0, "dump_segment_names: no-override control run exits 0")
    check(os.path.isfile(os.path.join(tgt_noextra, "Albums", "YandexDisk", "sync1.jpg")),
          "dump_segment_names: without the override 'YandexDisk' IS a real album (control)")

    # --- e2e, dump_segment_names: [] removes a default heuristic name (Camera Roll becomes
    # a real album) but the four protected self-protection names survive regardless.
    with open(cfg_path, "w", encoding="utf-8") as f:
        f.write("dump_segment_names: []\n")
    tgt_removed = os.path.join(WORK, "target_dump_segment_names_removed")
    try:
        r_removed = run_photosort(src, tgt_removed)
    finally:
        os.remove(cfg_path)
    check(r_removed.returncode == 0, "dump_segment_names: dump_segment_names=[] run exits 0")
    check(os.path.isfile(os.path.join(tgt_removed, "Albums", "Camera Roll", "phone1.jpg")),
          "dump_segment_names: [] override makes 'Camera Roll' a real album (proves list is editable)")

    # --- e2e cascade protection: with dump_segment_names: [] (editable list emptied), a
    # source laid out with loose files directly under folders literally named
    # Albums/ByDate/RAW/_unsorted must NOT have those segments swallowed whole as an album
    # named "Albums"/"ByDate"/etc -- the hardcoded DUMP_SEGMENT_NAMES_PROTECTED constant must
    # still catch them even though the user-facing editable list is empty (this is exactly
    # the self-eating protection needed when SOURCE points at an already-built archive).
    src_cascade = os.path.join(WORK, "src_dump_segment_protected_cascade")
    for name, dt in (("Albums", "2018:01:01 10:00:00"), ("ByDate", "2018:02:02 10:00:00"),
                      ("RAW", "2018:03:03 10:00:00"), ("_unsorted", "2018:04:04 10:00:00")):
        image(os.path.join(src_cascade, name, "loose.jpg"), 1000, 800, exif=True, dt=dt)
    with open(cfg_path, "w", encoding="utf-8") as f:
        f.write("dump_segment_names: []\n")
    tgt_cascade = os.path.join(WORK, "target_dump_segment_protected_cascade")
    try:
        r_cascade = run_photosort(src_cascade, tgt_cascade)
    finally:
        os.remove(cfg_path)
    check(r_cascade.returncode == 0, "dump_segment_names: protected-cascade run exits 0")
    for name in ("Albums", "ByDate", "RAW", "_unsorted"):
        check(not os.path.isdir(os.path.join(tgt_cascade, "Albums", name)),
              f"dump_segment_names: protected name '{name}' NOT swallowed as an album "
              f"even with dump_segment_names: [] (DUMP_SEGMENT_NAMES_PROTECTED holds)")
    total_bydate_files = sum(
        len(files) for _, _, files in os.walk(os.path.join(tgt_cascade, "ByDate")))
    check(total_bydate_files == 4,
          "dump_segment_names: all four protected-name source files landed in ByDate instead")


def test_dump_segment_config_ignores_nested_yaml_alias_bomb():
    print("\n=== adversarial (2026-07-13, QA pass): a YAML anchor/alias 'billion laughs' value "
          "landing in extra_dump_segment_names/extra_dump_segment_prefixes must not blow up "
          "Config() -- found by hand-crafting such a photoarchive_config.yaml: yaml.safe_load() "
          "itself parses it instantly (aliases are shared references, not copies), but the old "
          "str(item) on each list element recursively repr()'d the whole exploded structure, "
          "turning a few-hundred-byte config file into ~15s of CPU and hundreds of MB of string "
          "data. Fix: _clean_str_set() drops non-string elements instead of str()-ing them ===")
    code = (
        "import sys, time; sys.path.insert(0, %r)\n"
        "import photosort_win as m\n"
        "bomb = ['x'] * 10\n"
        "for _ in range(8):\n"
        "    bomb = [bomb] * 10\n"  # 10**8 leaves, but O(1) per level via shared references
        "t0 = time.time()\n"
        "cfg = m.Config(source='C:/x', target='C:/y', "
        "extra_dump_segment_names=['YandexDisk', bomb], "
        "extra_dump_segment_prefixes=['backup_', bomb])\n"
        "elapsed = time.time() - t0\n"
        "print('elapsed:', elapsed)\n"
        "print('fast_enough:', elapsed < 5)\n"
        "print('yandexdisk_present:', 'yandexdisk' in cfg.dump_segment_names_lower)\n"
        "print('backup_prefix_present:', 'backup_' in cfg.dump_segment_prefixes_tuple)\n"
        "print('no_giant_string_leaked:', all(len(s) < 1000 for s in cfg.dump_segment_names_lower) "
        "and all(len(s) < 1000 for s in cfg.dump_segment_prefixes_tuple))\n"
    ) % ROOT
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                        encoding="utf-8", errors="replace", timeout=20)
    check(r.returncode == 0,
          f"dump-segment yaml-alias-bomb: unit script exits 0 (stderr={r.stderr[-500:]})")
    check("fast_enough: True" in r.stdout,
          "dump-segment yaml-alias-bomb: Config() stays fast (<5s) even with a deeply nested "
          "non-string list element (would have taken ~15s+ before the fix)")
    check("yandexdisk_present: True" in r.stdout,
          "dump-segment yaml-alias-bomb: a normal string alongside the bomb value still works")
    check("backup_prefix_present: True" in r.stdout,
          "dump-segment yaml-alias-bomb: same for extra_dump_segment_prefixes")
    check("no_giant_string_leaked: True" in r.stdout,
          "dump-segment yaml-alias-bomb: the nested non-string element is dropped outright, "
          "never turned into a giant str()'d entry")


def test_debug_flag_actions_log():
    print("\n=== 5.3б: debug: true adds [DEBUG] lines to actions.log, off by default ===")
    src = os.path.join(WORK, "src_debug_flag")
    tgt_off = os.path.join(WORK, "target_debug_off")
    tgt_on = os.path.join(WORK, "target_debug_on")
    image(os.path.join(src, "Album Море", "a.jpg"), 800, 600, exif=True, dt="2022:02:02 10:00:00")

    r_off = run_photosort(src, tgt_off)
    check(r_off.returncode == 0, "5.3б: run without debug exits 0")
    with open(os.path.join(tgt_off, "__служебные_файлы", "logs", "actions.log"), encoding="utf-8") as f:
        actions_off = f.read()
    check("[DEBUG]" not in actions_off, "5.3б: no [DEBUG] lines when debug is not set (default off)")

    cfg_path = os.path.join(ROOT, "photoarchive_config.yaml")
    with open(cfg_path, "w", encoding="utf-8") as f:
        f.write("debug: true\n")
    try:
        r_on = run_photosort(src, tgt_on)
    finally:
        os.remove(cfg_path)
    check(r_on.returncode == 0, "5.3б: run with debug: true exits 0")
    with open(os.path.join(tgt_on, "__служебные_файлы", "logs", "actions.log"), encoding="utf-8") as f:
        actions_on = f.read()
    check("[DEBUG] album_decision: segment='Album" in actions_on,
          "5.3б: [DEBUG] album_decision line present when debug is on")
    check("tag=нет -> album='Album" in actions_on,
          "5.3б: album_decision line reports tag=нет + the resolved album name")


def test_log_rotation():
    print("\n=== 5.3в: log files rotate at 20MB, keep only 3 rotated copies, CSV header restored ===")
    src = os.path.join(WORK, "src_log_rotation")
    tgt = os.path.join(WORK, "target_log_rotation")
    image(os.path.join(src, "a.jpg"), 800, 600, exif=True, dt="2022:03:03 10:00:00")
    r0 = run_photosort(src, tgt)
    check(r0.returncode == 0, "5.3в: initial run (creates the archive + logs) exits 0")

    logs_dir = os.path.join(tgt, "__служебные_файлы", "logs")
    appended_path = os.path.join(logs_dir, "appended.csv")
    for i in range(4):
        with open(appended_path, "wb") as f:
            f.write(b"x" * (21 * 1024 * 1024))
        time.sleep(1.1)  # rotated filenames carry a 1-second-resolution timestamp
        image(os.path.join(src, f"extra{i}.jpg"), 800, 600, exif=True, dt=f"2022:03:0{i + 4} 10:00:00")
        r = run_photosort(src, tgt)
        check(r.returncode == 0, f"5.3в: run #{i} (forces rotation) exits 0")

    rotated = [f for f in os.listdir(logs_dir)
               if f.startswith("appended-") and f.endswith(".csv")]
    check(len(rotated) == 3, f"5.3в: exactly 3 rotated appended-*.csv kept (found {len(rotated)})")
    check(os.path.getsize(appended_path) < 21 * 1024 * 1024,
          "5.3в: current appended.csv is a fresh (small) file after rotation")
    with open(appended_path, encoding="utf-8") as f:
        first_line = f.readline().strip()
    check(first_line == "timestamp,source,dest,reason,flags",
          "5.3в: rotated-then-reopened appended.csv has its CSV header restored")


def test_archive_cache_prunes_stale_paths():
    print("\n=== ROADMAP 'неограниченный рост служебных файлов': archive_cache no longer "
          "keeps rows forever for archive paths that were renamed/deleted -- Phase 1 prunes "
          "any cached path not seen in the same run's full walk of Albums/ByDate/RAW ===")
    src = os.path.join(WORK, "src_cache_prune")
    tgt = os.path.join(WORK, "target_cache_prune")
    # This VPS's disk is far smaller than the 10GB default free_space_margin_gb -- override it
    # so the archive-build runs below don't spuriously stop with InsufficientSpace.
    cfg_path = os.path.join(ROOT, "photoarchive_config.yaml")
    with open(cfg_path, "w", encoding="utf-8") as f:
        f.write("free_space_margin_gb: 0.01\n")
    try:
        image(os.path.join(src, "Альбом", "a.jpg"), 800, 600, exif=True, dt="2022:05:05 10:00:00")
        r1 = run_photosort(src, tgt)
        check(r1.returncode == 0, "cache-prune: initial run exits 0")
        archived_a = os.path.join(tgt, "Albums", "Альбом", "a.jpg")
        check(os.path.isfile(archived_a), "cache-prune: a.jpg archived")

        # Раунд 5 ревью, вариант D (REVIEW-HANDOFF.md): run #1 уже сеет archive_cache для
        # a.jpg сразу при размещении (не дожидаясь Phase 1 run #2) -- run #2 здесь просто
        # подтверждает, что запись пережила ещё один прогон (в т.ч. свою собственную Phase 1,
        # у которой должен быть cache-hit, а не перехеш).
        image(os.path.join(src, "Альбом", "b.jpg"), 800, 600, exif=True, dt="2022:05:06 10:00:00")
        r2 = run_photosort(src, tgt)
        check(r2.returncode == 0, "cache-prune: second run exits 0")

        db_path = os.path.join(ROOT, "work.db")
        import sqlite3
        conn = sqlite3.connect(db_path)
        cached_paths = {row[0] for row in conn.execute("SELECT path FROM archive_cache")}
        conn.close()
        check(archived_a in cached_paths, "cache-prune: archive_cache holds a.jpg's path after run #2")

        # Simulate a photo the user manually removed from the archive between runs. Must also
        # remove it from SOURCE (не только TARGET) -- иначе, с вариантом D (сев archive_cache
        # при размещении), decide() честно увидит его как отсутствующее в архиве и заново
        # скопирует обратно из ещё не убранного source-файла того же прогона, что и должно:
        # это не баг находки, а другой сценарий (файл ЛЕГИТИМНО вернулся в архив тем же
        # run #3, а не "ушёл навсегда", который здесь и проверяется).
        os.remove(archived_a)
        os.remove(os.path.join(src, "Альбом", "a.jpg"))

        image(os.path.join(src, "Альбом", "c.jpg"), 800, 600, exif=True, dt="2022:05:07 10:00:00")
        r3 = run_photosort(src, tgt)
        check(r3.returncode == 0, "cache-prune: third run exits 0")

        conn = sqlite3.connect(db_path)
        cached_paths_after = {row[0] for row in conn.execute("SELECT path FROM archive_cache")}
        conn.close()
        check(archived_a not in cached_paths_after,
              "cache-prune: archive_cache no longer holds a.jpg's path once it left the archive")
        archived_b = os.path.join(tgt, "Albums", "Альбом", "b.jpg")
        check(archived_b in cached_paths_after,
              "cache-prune: archive_cache still holds b.jpg's path (still present in the archive)")
    finally:
        os.remove(cfg_path)


def test_crash_log_rotates():
    print("\n=== ROADMAP 'неограниченный рост служебных файлов': crash.log now rotates at "
          "20MB via the same _rotate_log_if_needed() mechanism as the other service logs, "
          "instead of growing without bound across repeated crashes ===")
    tmp_dir = os.path.join(WORK, "crash_log_rotate")
    os.makedirs(tmp_dir, exist_ok=True)
    crash_path = os.path.join(tmp_dir, "crash.log")
    with open(crash_path, "wb") as f:
        f.write(b"x" * (21 * 1024 * 1024))
    code = (
        "import sys; sys.path.insert(0, %r)\n"
        "import photosort_win as m\n"
        "m._app_dir = lambda: %r\n"
        "try:\n"
        "    raise RuntimeError('synthetic crash for test')\n"
        "except RuntimeError:\n"
        "    m._log_unexpected_crash(log=lambda *a, **k: None)\n"
    ) % (ROOT, tmp_dir)
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                        encoding="utf-8", errors="replace")
    check(r.returncode == 0, f"crash-log-rotate: unit script exits 0 (stderr={r.stderr[-500:]})")
    rotated = [f for f in os.listdir(tmp_dir) if f.startswith("crash-") and f.endswith(".log")]
    check(len(rotated) == 1, f"crash-log-rotate: oversized crash.log got rotated aside (found {rotated})")
    check(os.path.getsize(crash_path) < 21 * 1024 * 1024,
          "crash-log-rotate: crash.log is a fresh (small) file after rotation, holding only the new entry")


def test_exit_code_aggregation_across_sources():
    print("\n=== ROADMAP 'коды возврата не отражают неудачу': _main() aggregates one exit "
          "code across multiple --source entries in the same run -- EXIT_INSUFFICIENT_SPACE "
          "always wins (TARGET is physically full, no point continuing), otherwise the first "
          "non-zero code seen among the sources sticks ===")
    src_a = os.path.join(WORK, "src_exitcode_a")
    src_b = os.path.join(WORK, "src_exitcode_b")
    tgt = os.path.join(WORK, "target_exitcode_agg")
    image(os.path.join(src_a, "a.jpg"), 800, 600, exif=True, dt="2022:09:09 10:00:00")
    image(os.path.join(src_b, "b.jpg"), 800, 600, exif=True, dt="2022:09:10 10:00:00")
    # run_for_source() is monkeypatched to a fake that never touches the real pipeline -- this
    # test is only about _main()'s aggregation arithmetic over RunResult.exit_code, not about
    # producing a real TargetLocked/InsufficientSpace condition (already covered by
    # test_target_lock_blocks_concurrent_run/test_disk_full_graceful_stop individually).
    code = (
        "import sys; sys.path.insert(0, %r)\n"
        # Real invocations only get UTF-8 stdout via the reconfigure() at the bottom of
        # photosort_win.py's own `if __name__ == "__main__":` block -- calling m._main()
        # directly here (as an imported module, not run as __main__) bypasses that guard, so
        # a runner whose default console codepage can't encode Cyrillic (observed on GitHub
        # Actions windows-latest, cp1252) crashes the moment _finalize_target_report() logs
        # "Отчёт: ...". Replicate the same reconfigure() here so this mock matches what a
        # real run actually gets.
        "sys.stdout.reconfigure(encoding='utf-8', errors='replace')\n"
        "import photosort_win as m\n"
        "_calls = []\n"
        "def _fake(source, target, dry_run, sample_limit, log=print, suppress_logs=False,\n"
        "          shared_pool=None):\n"
        "    _calls.append(source)\n"
        "    if len(_calls) == 1:\n"
        "        return m.RunResult(failed=True, exit_code=m.EXIT_TARGET_LOCKED)\n"
        "    return m.RunResult(failed=False, exit_code=m.EXIT_INSUFFICIENT_SPACE, stats={},\n"
        "                        processed_count=1, stopped_for_space=True)\n"
        "m.run_for_source = _fake\n"
        "sys.argv = ['PhotoArchive.exe', '--source', %r, '--source', %r, '--target', %r]\n"
        "print('EXIT_CODE:', m._main())\n"
    ) % (ROOT, src_a, src_b, tgt)
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                        encoding="utf-8", errors="replace")
    check(r.returncode == 0, f"exit-code-agg: unit script itself exits 0 (stderr={r.stderr[-500:]})")
    check("EXIT_CODE: 4" in r.stdout,
          f"exit-code-agg: EXIT_INSUFFICIENT_SPACE (2nd source) wins over EXIT_TARGET_LOCKED "
          f"(1st source) -- got {r.stdout[-300:]!r}")


def test_target_lock_blocks_concurrent_run():
    print("\n=== 5.4б: a fresh LOCK file blocks a second archive run on the same TARGET ===")
    src = os.path.join(WORK, "src_lock_fresh")
    tgt = os.path.join(WORK, "target_lock_fresh")
    image(os.path.join(src, "a.jpg"), 800, 600, exif=True, dt="2022:04:04 10:00:00")
    r0 = run_photosort(src, tgt)
    check(r0.returncode == 0, "5.4б: initial run (creates the archive) exits 0")

    lock_path = os.path.join(tgt, "__служебные_файлы", "LOCK")
    with open(lock_path, "w", encoding="utf-8") as f:
        f.write("99999999")  # fresh mtime (just written) -- simulates a run in progress

    before = sorted(
        os.path.join(dp, f) for dp, _, files in os.walk(tgt) for f in files
        if "__служебные_файлы" not in dp
    )
    image(os.path.join(src, "b.jpg"), 800, 600, exif=True, dt="2022:04:05 10:00:00")
    r1 = run_photosort(src, tgt)
    check(r1.returncode == 2,
          f"5.4б: run against a locked TARGET does not crash, exits EXIT_TARGET_LOCKED=2 "
          f"(got {r1.returncode})")
    check("ОШИБКА" in r1.stdout and ("LOCK" in r1.stdout or "TARGET" in r1.stdout),
          "5.4б: run against a locked TARGET reports a clear error")
    after = sorted(
        os.path.join(dp, f) for dp, _, files in os.walk(tgt) for f in files
        if "__служебные_файлы" not in dp
    )
    check(before == after, "5.4б: locked-out run touches nothing under TARGET (b.jpg not added)")
    check(os.path.exists(lock_path), "5.4б: the (still-fresh) LOCK file is left untouched, not deleted")


def test_target_lock_stale_auto_removed():
    print("\n=== 5.4б: a stale (>12h old) LOCK file is auto-removed and the run proceeds ===")
    src = os.path.join(WORK, "src_lock_stale")
    tgt = os.path.join(WORK, "target_lock_stale")
    image(os.path.join(src, "a.jpg"), 800, 600, exif=True, dt="2022:05:05 10:00:00")
    r0 = run_photosort(src, tgt)
    check(r0.returncode == 0, "5.4б: initial run exits 0")

    lock_path = os.path.join(tgt, "__служебные_файлы", "LOCK")
    with open(lock_path, "w", encoding="utf-8") as f:
        f.write("12345")
    old_time = time.time() - 13 * 3600  # 13h old -- past the 12h staleness threshold
    os.utime(lock_path, (old_time, old_time))

    image(os.path.join(src, "b.jpg"), 800, 600, exif=True, dt="2022:05:06 10:00:00")
    r1 = run_photosort(src, tgt)
    check(r1.returncode == 0, "5.4б: run with a stale LOCK exits 0 (proceeds normally)")
    check("устаревший LOCK" in r1.stdout, "5.4б: stale LOCK removal is logged")
    check(os.path.isfile(os.path.join(tgt, "ByDate", "2022", "2022-05 [PhotoArchive]", "b.jpg")),
          "5.4б: the run actually proceeded and archived the new file")


def test_target_lock_released_after_run():
    print("\n=== 5.4б: LOCK file does not persist after a normal run completes ===")
    src = os.path.join(WORK, "src_lock_release")
    tgt = os.path.join(WORK, "target_lock_release")
    image(os.path.join(src, "a.jpg"), 800, 600, exif=True, dt="2022:06:06 10:00:00")
    r = run_photosort(src, tgt)
    check(r.returncode == 0, "5.4б: run exits 0")
    check(not os.path.exists(os.path.join(tgt, "__служебные_файлы", "LOCK")),
          "5.4б: LOCK file is removed once the run finishes normally")


def test_target_confirmation_unit():
    print("\n=== 5.4а: risk-proportional TARGET confirmation (direct unit test) ===")
    empty_tgt = os.path.join(WORK, "confirm_empty")
    os.makedirs(empty_tgt, exist_ok=True)
    our_structure_tgt = os.path.join(WORK, "confirm_our_structure")
    os.makedirs(os.path.join(our_structure_tgt, "__служебные_файлы"), exist_ok=True)
    os.makedirs(os.path.join(our_structure_tgt, "ByDate"), exist_ok=True)
    foreign_tgt = os.path.join(WORK, "confirm_foreign")
    os.makedirs(foreign_tgt, exist_ok=True)
    with open(os.path.join(foreign_tgt, "vacation_photo.jpg"), "wb") as f:
        f.write(b"not really a jpeg, just needs to exist")
    missing_tgt = os.path.join(WORK, "confirm_missing_does_not_exist")

    code = (
        "import sys; sys.path.insert(0, %r)\n"
        "import photosort_win as m\n"
        "print('missing:', m._target_needs_confirmation(%r))\n"
        "print('empty:', m._target_needs_confirmation(%r))\n"
        "print('our_structure:', m._target_needs_confirmation(%r))\n"
        "print('foreign:', m._target_needs_confirmation(%r))\n"
    ) % (ROOT, missing_tgt, empty_tgt, our_structure_tgt, foreign_tgt)
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                        encoding="utf-8", errors="replace")
    check(r.returncode == 0, f"5.4а: unit test script exits 0 (stderr={r.stderr[-500:]})")
    check("missing: False" in r.stdout, "5.4а: case 1 (TARGET missing) needs no confirmation")
    check("empty: False" in r.stdout, "5.4а: case 1 (TARGET empty) needs no confirmation")
    check("our_structure: False" in r.stdout,
          "5.4а: case 2 (TARGET has only our own structure) needs no confirmation")
    check("foreign: True" in r.stdout,
          "5.4а: case 3 (TARGET has foreign content) needs confirmation")

    # confirm_target_interactively() logs a Cyrillic "Отменено пользователем." on decline --
    # same UTF-8-stdout gap as test_ctrl_c_no_traceback() above, same fix needed here.
    code2 = (
        "import sys; sys.path.insert(0, %r)\n"
        "for s in (sys.stdout, sys.stderr):\n"
        "    s.reconfigure(encoding='utf-8', errors='replace')\n"
        "import photosort_win as m\n"
        "ok_no = m.confirm_target_interactively(%r, input_fn=lambda p: 'нет')\n"
        "ok_yes = m.confirm_target_interactively(%r, input_fn=lambda p: 'да')\n"
        "ok_enter = m.confirm_target_interactively(%r, input_fn=lambda p: '')\n"
        "print('declined:', ok_no)\n"
        "print('confirmed:', ok_yes)\n"
        "print('bare_enter:', ok_enter)\n"
    ) % (ROOT, foreign_tgt, foreign_tgt, foreign_tgt)
    r2 = subprocess.run([sys.executable, "-c", code2], capture_output=True, text=True,
                         encoding="utf-8", errors="replace")
    check(r2.returncode == 0, f"5.4а: confirm unit test exits 0 (stderr={r2.stderr[-500:]})")
    check("declined: False" in r2.stdout, "5.4а: typing anything other than 'да' declines")
    check("confirmed: True" in r2.stdout, "5.4а: typing 'да' confirms")
    check("bare_enter: False" in r2.stdout,
          "5.4а: a bare Enter (empty string) does NOT confirm case 3 -- must type 'да'")


def test_drive_root_target_confirmation_unit():
    print("\n=== drive-root TARGET UX: bare volume root gets a redirect-or-write-as-is choice ===")
    normal_tgt = os.path.join(WORK, "confirm_drive_root_normal")
    os.makedirs(normal_tgt, exist_ok=True)

    # _is_bare_drive_root() is a platform-agnostic "path is its own dirname" check -- on this
    # Linux sandbox "/" is the only such path (there's no concept of drive letters), which is
    # enough to validate the predicate's core logic without needing a real Windows drive.
    code = (
        "import sys; sys.path.insert(0, %r)\n"
        "import photosort_win as m\n"
        "print('root:', m._is_bare_drive_root('/'))\n"
        "print('normal:', m._is_bare_drive_root(%r))\n"
    ) % (ROOT, normal_tgt)
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                        encoding="utf-8", errors="replace")
    check(r.returncode == 0, f"drive-root: predicate unit test script exits 0 (stderr={r.stderr[-500:]})")
    check("root: True" in r.stdout, "drive-root: '/' (POSIX filesystem root) is a bare root")
    check("normal: False" in r.stdout, "drive-root: a real subfolder is NOT a bare root")

    if os.name == "nt":
        code_nt = (
            "import sys; sys.path.insert(0, %r)\n"
            "import photosort_win as m\n"
            "print('drive_root:', m._is_bare_drive_root('C:\\\\'))\n"
            "print('drive_subfolder:', m._is_bare_drive_root('C:\\\\Windows'))\n"
        ) % ROOT
        r_nt = subprocess.run([sys.executable, "-c", code_nt], capture_output=True, text=True,
                               encoding="utf-8", errors="replace")
        check(r_nt.returncode == 0, f"drive-root: Windows-path unit test exits 0 (stderr={r_nt.stderr[-500:]})")
        check("drive_root: True" in r_nt.stdout, "drive-root: 'C:\\' is a bare drive root")
        check("drive_subfolder: False" in r_nt.stdout, "drive-root: 'C:\\Windows' is NOT a bare drive root")
    else:
        print("  SKIP: 'C:\\'-style drive-letter check only meaningful on the windows-latest CI runner")

    # confirm_drive_root_target_interactively() prints a Cyrillic prompt -- same UTF-8-stdout
    # gap as test_target_confirmation_unit() above, same fix needed here.
    code2 = (
        "import sys; sys.path.insert(0, %r)\n"
        "for s in (sys.stdout, sys.stderr):\n"
        "    s.reconfigure(encoding='utf-8', errors='replace')\n"
        "import photosort_win as m\n"
        "yes_result = m.confirm_drive_root_target_interactively('/', input_fn=lambda p: 'да')\n"
        "no_result = m.confirm_drive_root_target_interactively('/', input_fn=lambda p: 'нет')\n"
        "unaffected = m.confirm_drive_root_target_interactively(%r, input_fn=lambda p: (_ for _ in ()).throw(AssertionError('should not prompt for a normal folder')))\n"
        "print('yes_result:', yes_result)\n"
        "print('no_result:', no_result)\n"
        "print('unaffected:', unaffected)\n"
    ) % (ROOT, normal_tgt)
    r2 = subprocess.run([sys.executable, "-c", code2], capture_output=True, text=True,
                         encoding="utf-8", errors="replace")
    check(r2.returncode == 0, f"drive-root: confirm unit test exits 0 (stderr={r2.stderr[-500:]})")
    check("yes_result: /__PhotoArchive__" in r2.stdout,
          "drive-root: answering 'да' redirects TARGET to a __PhotoArchive__ subfolder")
    check("no_result: /" in r2.stdout,
          "drive-root: declining does NOT cancel -- TARGET stays the bare root as-is")
    check(f"unaffected: {normal_tgt}" in r2.stdout,
          "drive-root: a normal (non-root) TARGET is returned unchanged, never even prompts")

    # Prompt wording must differ when __PhotoArchive__ already exists (appending to an existing
    # archive on the same drive) -- "use it", not "create it". Monkeypatch _is_bare_drive_root()
    # to force the bare-root branch for an arbitrary test directory (real root detection is
    # already covered above), so this can freely control whether __PhotoArchive__ pre-exists inside
    # it without touching the real filesystem root.
    fake_root_existing = os.path.join(WORK, "confirm_drive_root_fake_existing")
    os.makedirs(os.path.join(fake_root_existing, "__PhotoArchive__"), exist_ok=True)
    fake_root_missing = os.path.join(WORK, "confirm_drive_root_fake_missing")
    os.makedirs(fake_root_missing, exist_ok=True)
    code3 = (
        "import sys; sys.path.insert(0, %r)\n"
        "for s in (sys.stdout, sys.stderr):\n"
        "    s.reconfigure(encoding='utf-8', errors='replace')\n"
        "import photosort_win as m\n"
        "m._is_bare_drive_root = lambda t: True\n"
        "captured = {}\n"
        "def capture_input(prompt):\n"
        "    captured['prompt'] = prompt\n"
        "    return 'да'\n"
        "existing_result = m.confirm_drive_root_target_interactively(%r, input_fn=capture_input)\n"
        "print('existing_prompt_says_use:', 'уже есть' in captured['prompt'])\n"
        "print('existing_result:', existing_result)\n"
        "captured.clear()\n"
        "missing_result = m.confirm_drive_root_target_interactively(%r, input_fn=capture_input)\n"
        "print('missing_prompt_says_create:', 'создать' in captured['prompt'])\n"
        "print('missing_result:', missing_result)\n"
    ) % (ROOT, fake_root_existing, fake_root_missing)
    r3 = subprocess.run([sys.executable, "-c", code3], capture_output=True, text=True,
                         encoding="utf-8", errors="replace")
    check(r3.returncode == 0, f"drive-root: existing-__PhotoArchive__ wording unit test exits 0 (stderr={r3.stderr[-500:]})")
    check("existing_prompt_says_use: True" in r3.stdout,
          "drive-root: prompt offers to USE the existing __PhotoArchive__ folder, not create it")
    check(f"existing_result: {os.path.join(fake_root_existing, '__PhotoArchive__')}" in r3.stdout,
          "drive-root: answering 'да' when __PhotoArchive__ already exists returns that folder")
    check("missing_prompt_says_create: True" in r3.stdout,
          "drive-root: prompt offers to CREATE __PhotoArchive__ when it doesn't exist yet")


def test_bare_drive_letter_normalization_and_source_target_conflict():
    print("\n=== drive-root TARGET UX: bare 'C:'-style letter normalized; SOURCE==TARGET "
          "bare-root conflict auto-resolved without asking (not a real choice) ===")

    code = (
        "import sys; sys.path.insert(0, %r)\n"
        "import photosort_win as m\n"
        "print('bare_c:', m._normalize_bare_drive_letter('C:'))\n"
        "print('bare_lower_d:', m._normalize_bare_drive_letter('d:'))\n"
        "print('already_root:', m._normalize_bare_drive_letter('C:\\\\'))\n"
        "print('ambiguous_not_touched:', m._normalize_bare_drive_letter('C:Foo'))\n"
        "print('normal_path:', m._normalize_bare_drive_letter('C:\\\\Foo'))\n"
    ) % ROOT
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                        encoding="utf-8", errors="replace")
    check(r.returncode == 0, f"bare-letter: normalization unit test exits 0 (stderr={r.stderr[-500:]})")
    check("bare_c: C:\\" in r.stdout, "bare-letter: 'C:' normalized to 'C:\\'")
    check("bare_lower_d: d:\\" in r.stdout, "bare-letter: lowercase 'd:' normalized too")
    check("already_root: C:\\" in r.stdout, "bare-letter: already-normalized 'C:\\' left unchanged")
    check("ambiguous_not_touched: C:Foo" in r.stdout,
          "bare-letter: 'C:Foo' (genuinely ambiguous drive-relative path) left untouched")
    check("normal_path: C:\\Foo" in r.stdout, "bare-letter: a real path is never touched")

    # resolve_drive_root_conflict(): SOURCE==TARGET bare root must NEVER prompt (it's a forced
    # resolution, not a choice) -- the injected input_fn raises if called at all, in both
    # interactive and non-interactive (CLI/photoarchive_config.yaml) modes.
    code2 = (
        "import sys; sys.path.insert(0, %r)\n"
        "for s in (sys.stdout, sys.stderr):\n"
        "    s.reconfigure(encoding='utf-8', errors='replace')\n"
        "import photosort_win as m\n"
        "def must_not_prompt(p):\n"
        "    raise AssertionError('should not prompt on a forced SOURCE==TARGET resolution')\n"
        "conflict_interactive = m.resolve_drive_root_conflict(['/'], '/', interactive=True, input_fn=must_not_prompt)\n"
        "conflict_cli = m.resolve_drive_root_conflict(['/'], '/', interactive=False, input_fn=must_not_prompt)\n"
        "print('conflict_interactive:', conflict_interactive)\n"
        "print('conflict_cli:', conflict_cli)\n"
    ) % ROOT
    r2 = subprocess.run([sys.executable, "-c", code2], capture_output=True, text=True,
                         encoding="utf-8", errors="replace")
    check(r2.returncode == 0, f"bare-letter: conflict-resolution unit test exits 0 (stderr={r2.stderr[-500:]})")
    check("conflict_interactive: /__PhotoArchive__" in r2.stdout,
          "bare-letter: SOURCE==TARGET bare root auto-redirects without asking (interactive)")
    check("conflict_cli: /__PhotoArchive__" in r2.stdout,
          "bare-letter: SOURCE==TARGET bare root auto-redirects without asking (CLI/photoarchive_config.yaml too)")

    # No conflict (TARGET bare root, but no source matches it): a real choice -- interactive
    # asks (existing confirm_drive_root_target_interactively), CLI/photoarchive_config.yaml does not.
    other_src = os.path.join(WORK, "drive_conflict_other_source")
    os.makedirs(other_src, exist_ok=True)
    code3 = (
        "import sys; sys.path.insert(0, %r)\n"
        "for s in (sys.stdout, sys.stderr):\n"
        "    s.reconfigure(encoding='utf-8', errors='replace')\n"
        "import photosort_win as m\n"
        "no_conflict_interactive = m.resolve_drive_root_conflict([%r], '/', interactive=True, input_fn=lambda p: 'да')\n"
        "no_conflict_cli = m.resolve_drive_root_conflict([%r], '/', interactive=False, input_fn=lambda p: (_ for _ in ()).throw(AssertionError('CLI path must never prompt')))\n"
        "print('no_conflict_interactive:', no_conflict_interactive)\n"
        "print('no_conflict_cli:', no_conflict_cli)\n"
    ) % (ROOT, other_src, other_src)
    r3 = subprocess.run([sys.executable, "-c", code3], capture_output=True, text=True,
                         encoding="utf-8", errors="replace")
    check(r3.returncode == 0, f"bare-letter: no-conflict unit test exits 0 (stderr={r3.stderr[-500:]})")
    check("no_conflict_interactive: /__PhotoArchive__" in r3.stdout,
          "bare-letter: no conflict + interactive=True -- real choice, answering 'да' redirects")
    check("no_conflict_cli: /" in r3.stdout,
          "bare-letter: no conflict + interactive=False (CLI/photoarchive_config.yaml) -- never prompts, root as-is")


def test_bare_launch_helpers_unit():
    print("\n=== Bare-launch menu: helper functions (quote-stripping, archive detection, menu loop) ===")

    missing_tgt = os.path.join(WORK, "bare_menu_helper_missing")
    empty_tgt = os.path.join(WORK, "bare_menu_helper_empty")
    os.makedirs(empty_tgt, exist_ok=True)
    photosort_tgt = os.path.join(WORK, "bare_menu_helper_photosort")
    os.makedirs(os.path.join(photosort_tgt, "__служебные_файлы"), exist_ok=True)
    old_archive_tgt = os.path.join(WORK, "bare_menu_helper_old_archive")
    os.makedirs(os.path.join(old_archive_tgt, "Albums"), exist_ok=True)
    os.makedirs(os.path.join(old_archive_tgt, "ByDate"), exist_ok=True)
    albums_only_tgt = os.path.join(WORK, "bare_menu_helper_albums_only")
    os.makedirs(os.path.join(albums_only_tgt, "Albums"), exist_ok=True)

    code = (
        "import sys; sys.path.insert(0, %r)\n"
        "import photosort_win as m\n"
        "print('quoted_ok:', m._strip_surrounding_quotes('\"C:/Users/mama/photos\"') == 'C:/Users/mama/photos')\n"
        "print('unquoted_ok:', m._strip_surrounding_quotes('C:/Users/mama/photos') == 'C:/Users/mama/photos')\n"
        "print('single_quote_ok:', m._strip_surrounding_quotes('\"') == '\"')\n"
        "print('missing:', m._target_has_existing_archive(%r))\n"
        "print('empty:', m._target_has_existing_archive(%r))\n"
        "print('photosort:', m._target_has_existing_archive(%r))\n"
        "print('old_archive:', m._target_has_existing_archive(%r))\n"
        "print('albums_only:', m._target_has_existing_archive(%r))\n"
    ) % (ROOT, missing_tgt, empty_tgt, photosort_tgt, old_archive_tgt, albums_only_tgt)
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                        encoding="utf-8", errors="replace")
    check(r.returncode == 0, f"bare-menu helpers: unit script exits 0 (stderr={r.stderr[-500:]})")
    check("quoted_ok: True" in r.stdout,
          "bare-menu helpers: drag-and-drop surrounding quotes are stripped")
    check("unquoted_ok: True" in r.stdout,
          "bare-menu helpers: an already-unquoted path is left untouched")
    check("single_quote_ok: True" in r.stdout,
          "bare-menu helpers: a lone quote character (not a matched pair) is left as-is")
    check("missing: False" in r.stdout, "bare-menu helpers: nonexistent TARGET has no existing archive")
    check("empty: False" in r.stdout, "bare-menu helpers: empty TARGET has no existing archive")
    check("photosort: True" in r.stdout, "bare-menu helpers: __служебные_файлы\\ marks an existing archive")
    check("old_archive: True" in r.stdout,
          "bare-menu helpers: Albums\\+ByDate\\ (older archive shape) also counts as existing")
    check("albums_only: False" in r.stdout,
          "bare-menu helpers: Albums\\ alone (no ByDate\\) is NOT treated as an existing archive")

    # Menu loop: invalid input reprompts, Enter defaults to [1], numbers map correctly.
    # 2026-07-12, by direct user feedback: the top-level mode menu no longer shows/accepts
    # "0" at all (see prompt_bare_launch_menu()'s docstring -- inconsistent for the SAME key
    # to mean "exit" here but "back to this very menu" everywhere else; Ctrl+C already covers
    # exiting) -- "0" now folded into the invalid-input list alongside 'bogus'/'.', instead of
    # its own "exit_choice" case (that used to feed a lambda that ALWAYS returns '0', which
    # would now loop forever re-prompting -- this subprocess.run() call has no timeout=).
    code2 = (
        "import sys; sys.path.insert(0, %r)\n"
        "for s in (sys.stdout, sys.stderr):\n"
        "    s.reconfigure(encoding='utf-8', errors='replace')\n"
        "import photosort_win as m\n"
        "answers = iter(['bogus', '.', '0', ''])\n"
        "choice_default = m.prompt_bare_launch_menu(input_fn=lambda p: next(answers))\n"
        "print('default_after_invalid:', choice_default)\n"
        "print('view_choice:', m.prompt_bare_launch_menu(input_fn=lambda p: '1'))\n"
        "print('dry_run_choice:', m.prompt_bare_launch_menu(input_fn=lambda p: '2'))\n"
        "print('build_choice:', m.prompt_bare_launch_menu(input_fn=lambda p: '3'))\n"
    ) % ROOT
    r2 = subprocess.run([sys.executable, "-c", code2], capture_output=True, text=True,
                         encoding="utf-8", errors="replace", timeout=60)
    check(r2.returncode == 0, f"bare-menu helpers: menu unit test exits 0 (stderr={r2.stderr[-500:]})")
    check("default_after_invalid: view" in r2.stdout,
          "bare-menu helpers: invalid input ('bogus'/'.'/'0') reprompts in a loop, Enter then "
          "defaults to [1] (view)")
    check("view_choice: view" in r2.stdout, "bare-menu helpers: '1' maps to view")
    check("dry_run_choice: dry_run" in r2.stdout, "bare-menu helpers: '2' maps to dry_run")
    check("build_choice: build" in r2.stdout, "bare-menu helpers: '3' maps to build")

    # 2026-07-12 back-navigation rewrite: _menu_choice(allow_back=True) accepts "0" as a
    # distinct sentinel (not the number 0, doesn't collide with the normal 1..n_options
    # range), while allow_back=False (default, used by the partial-CLI direct call sites in
    # _main()) keeps rejecting "0" exactly like any other out-of-range input, unchanged.
    code3 = (
        "import sys; sys.path.insert(0, %r)\n"
        "for s in (sys.stdout, sys.stderr):\n"
        "    s.reconfigure(encoding='utf-8', errors='replace')\n"
        "import photosort_win as m\n"
        "back = m._menu_choice(3, default=1, input_fn=lambda p: '0', allow_back=True)\n"
        "print('allow_back_returns_sentinel:', back is m._MENU_BACK)\n"
        "answers = iter(['0', '2'])\n"
        "no_back = m._menu_choice(3, default=1, input_fn=lambda p: next(answers), allow_back=False)\n"
        "print('no_allow_back_rejects_zero_then_accepts_2:', no_back == 2)\n"
    ) % ROOT
    r3 = subprocess.run([sys.executable, "-c", code3], capture_output=True, text=True,
                         encoding="utf-8", errors="replace")
    check(r3.returncode == 0, f"bare-menu helpers: allow_back unit script exits 0 (stderr={r3.stderr[-500:]})")
    check("allow_back_returns_sentinel: True" in r3.stdout,
          "bare-menu helpers: _menu_choice(allow_back=True) returns _MENU_BACK on '0'")
    check("no_allow_back_rejects_zero_then_accepts_2: True" in r3.stdout,
          "bare-menu helpers: _menu_choice(allow_back=False) still rejects '0' as invalid "
          "(reprompts), unaffected by the new parameter's default")

    # 2026-07-12, terminal-width line wrap (user feedback: long lines look ugly in a real
    # console window) -- _wrap_console_text() is a pure function, no need for a real tty to
    # unit-test it directly.
    code4 = (
        "import sys; sys.path.insert(0, %r)\n"
        "import photosort_win as m\n"
        "short = '  short line'\n"
        "print('short_untouched:', m._wrap_console_text(short, 40) == short)\n"
        "long_words = '  ' + ' '.join(['слово'] * 20)\n"
        "wrapped = m._wrap_console_text(long_words, 20)\n"
        "lines = wrapped.split(chr(10))\n"
        "print('wraps_into_multiple_lines:', len(lines) > 1)\n"
        "print('each_line_fits_width:', all(len(l) <= 20 for l in lines))\n"
        "print('continuation_indented:', all(l.startswith('    ') for l in lines[1:]))\n"
        "no_space = '  ' + 'x' * 60\n"
        "unbroken = m._wrap_console_text(no_space, 20)\n"
        "print('long_word_not_broken_mid_string:', unbroken.strip() == no_space.strip())\n"
        "blank = 'before\\n\\nafter'\n"
        "print('blank_line_preserved:', m._wrap_console_text(blank, 40) == blank)\n"
    ) % ROOT
    r4 = subprocess.run([sys.executable, "-c", code4], capture_output=True, text=True,
                         encoding="utf-8", errors="replace")
    check(r4.returncode == 0, f"bare-menu helpers: wrap unit script exits 0 (stderr={r4.stderr[-500:]})")
    check("short_untouched: True" in r4.stdout,
          "console wrap: a line already shorter than width is left untouched")
    check("wraps_into_multiple_lines: True" in r4.stdout,
          "console wrap: a long line with spaces wraps into several lines")
    check("each_line_fits_width: True" in r4.stdout,
          "console wrap: every wrapped line fits within the given width")
    check("continuation_indented: True" in r4.stdout,
          "console wrap: continuation lines get a hanging indent, not flush left")
    check("long_word_not_broken_mid_string: True" in r4.stdout,
          "console wrap: a single 'word' with no spaces longer than width is left unbroken "
          "(break_long_words=False -- never mangles a path/hash/banner rule mid-string)")
    check("blank_line_preserved: True" in r4.stdout,
          "console wrap: blank lines in a multi-line message are preserved, not dropped")

    # 2026-07-12, live user report ("самые длинные строки при распаковке архива... не
    # помещаются даже на полном экране") -- _truncate_progress_note()'s old flat maxlen=60
    # ignored real terminal width entirely; _progress_note_budget() scales with it instead
    # (falls back to the old 60 off a real tty, same as before for piped/file output).
    code5 = (
        "import sys; sys.path.insert(0, %r)\n"
        "import photosort_win as m\n"
        "long_path = 'C:/Users/User1/Desktop/archive-2026-07-09_20-14-47/archive.zip -> ' + 'a' * 100\n"
        "narrow = m._truncate_progress_note(long_path, maxlen=30)\n"
        "wide = m._truncate_progress_note(long_path, maxlen=120)\n"
        "print('narrow_shorter_than_wide:', len(narrow) < len(wide))\n"
        "print('narrow_fits_budget:', len(narrow) <= 30)\n"
        "print('wide_fits_budget:', len(wide) <= 120)\n"
        "print('ellipsis_prefix:', narrow.startswith(chr(8230)))\n"
        "print('non_tty_budget_is_60:', m._progress_note_budget() == 60)\n"  # stdout/stderr piped here, not a tty
    ) % ROOT
    r5 = subprocess.run([sys.executable, "-c", code5], capture_output=True, text=True,
                         encoding="utf-8", errors="replace")
    check(r5.returncode == 0, f"bare-menu helpers: progress-note-budget unit script exits 0 (stderr={r5.stderr[-500:]})")
    check("narrow_shorter_than_wide: True" in r5.stdout,
          "progress-note budget: a smaller maxlen truncates more aggressively than a larger one")
    check("narrow_fits_budget: True" in r5.stdout,
          "progress-note budget: truncated note respects the given maxlen")
    check("wide_fits_budget: True" in r5.stdout,
          "progress-note budget: a maxlen wide enough for the whole path leaves it as one piece")
    check("ellipsis_prefix: True" in r5.stdout,
          "progress-note budget: truncation keeps the END of the path (leading ellipsis), not the start")
    check("non_tty_budget_is_60: True" in r5.stdout,
          "progress-note budget: _progress_note_budget() falls back to the old flat 60 when "
          "stderr isn't a real tty (piped/file output, e.g. this very test harness)")

    # 2026-07-12, same live user report: _progress_note_budget()'s flat "reserve" constant
    # (guessing how much of the line the phase-prefix + tqdm's own tail counters take) was
    # only an approximation and could still overflow a real terminal by a few characters
    # (observed: budget=40 on a 120-column terminal still got visually cut off).
    # ProgressReporter._context_note_budget() replaces the guess with the EXACT known
    # self.desc length (set_context() uses this to re-truncate the note precisely) -- COLUMNS
    # env var lets the formula be checked deterministically without needing a real tty.
    code6 = (
        "import sys, os; os.environ['COLUMNS'] = '120'\n"
        "sys.path.insert(0, %r)\n"
        "import photosort_win as m\n"
        "bar = m.ProgressReporter(total=None, desc='Фаза 2-5 — обработка источника', unit='файл')\n"
        "expected = max(15, 120 - len(bar.desc) - len(' — ') - 58)\n"
        "print('budget_matches_exact_desc_len_formula:', bar._context_note_budget() == expected)\n"
        "os.environ['COLUMNS'] = '60'\n"
        "print('narrower_terminal_gives_smaller_budget:', bar._context_note_budget() < expected)\n"
        "bar.close()\n"
    ) % ROOT
    r6 = subprocess.run([sys.executable, "-c", code6], capture_output=True, text=True,
                         encoding="utf-8", errors="replace")
    check(r6.returncode == 0, f"bare-menu helpers: context-note-budget unit script exits 0 (stderr={r6.stderr[-500:]})")
    check("budget_matches_exact_desc_len_formula: True" in r6.stdout,
          "progress-note budget: _context_note_budget() uses the bar's EXACT desc length, "
          "not a flat guessed reserve -- fixes the observed few-character overflow")
    check("narrower_terminal_gives_smaller_budget: True" in r6.stdout,
          "progress-note budget: _context_note_budget() shrinks along with real terminal width")


def test_bare_launch_menu_argv_gate_and_flow():
    print("\n=== Bare-launch menu: replaces the old fallback ONLY for a fully empty argv ===")

    # ТЗ-меню 2026-07-10: submenus enumerate REAL local drives (enumerate_menu_drives()) --
    # to keep this test deterministic across machines (this dev box vs. windows-latest CI
    # runner, different drive counts), every scripted answer sequence below monkeypatches
    # enumerate_menu_drives() to return [] so "своя папка" is always menu option "1" with no
    # Enter-default, decoupling the test from real system drive state.

    # Boundary: even a single, source/target-unrelated flag (e.g. --sample-limit) must NOT
    # show the new welcome-banner menu -- goes through the partial-CLI submenu path instead
    # (only source/target are asked, not the [1]/[2]/[3] mode menu -- mode is already
    # "archive" by default). Real subprocess + piped stdin, so the "своя папка" option number
    # is computed from THIS machine's real enumerate_menu_drives() (can't monkeypatch across
    # a subprocess boundary).
    probe = subprocess.run(
        [sys.executable, "-c",
         "import sys; sys.path.insert(0, %r)\n"
         "import photosort_win as m\n"
         "print(len(m.enumerate_menu_drives()))\n" % ROOT],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    n_drives = int(probe.stdout.strip())
    custom_opt = str(n_drives + 1)
    src = os.path.join(WORK, "bare_menu_gate_src")
    tgt = os.path.join(WORK, "bare_menu_gate_tgt")
    image(os.path.join(src, "a.jpg"), 800, 600, exif=True, dt="2020:05:05 10:00:00")
    # Real OS-piped stdin round-trips Cyrillic through the console codepage, not UTF-8 (see
    # the big comment below on run_bare()) -- the merged build confirmation needs a literal
    # "да", so drive this one via a monkeypatched builtins.input() instead of a real pipe,
    # same sidestep as every other confirmation test in this file.
    code_gate = (
        "import sys, builtins; sys.path.insert(0, %r)\n"
        "for s in (sys.stdout, sys.stderr):\n"
        "    s.reconfigure(encoding='utf-8', errors='replace')\n"
        "sys.argv = ['photosort_win.py', '--sample-limit', '5']\n"
        "answers = iter(%r)\n"
        "builtins.input = lambda p='': next(answers)\n"
        "import photosort_win as m\n"
        "m.main()\n"
    ) % (ROOT, [custom_opt, src, custom_opt, tgt, "да", ""])  # trailing "" answers the
    # end-of-run "Нажмите Enter для выхода" pause (interactive_mode=True for partial CLI).
    r = subprocess.run([sys.executable, "-c", code_gate], capture_output=True, text=True,
                        encoding="utf-8", errors="replace", timeout=120)
    check(r.returncode == 0, f"bare-menu gate: single unrelated flag still exits 0 (stderr={r.stderr[-500:]})")
    check("Откуда взять фотографии" in r.stdout,
          "bare-menu gate: a single non-empty-argv flag asks only the missing source/target submenus")
    check("Ваши оригиналы не изменяются" not in r.stdout,
          "bare-menu gate: a single non-empty-argv flag does NOT show the new welcome banner/mode menu")
    check(os.path.isdir(os.path.join(tgt, "__служебные_файлы")),
          "bare-menu gate: partial-CLI path still builds the archive from the submenu-typed paths")

    # Everything below drives run_bare_launch() directly with an injectable input_fn -- same
    # convention as every other interactive-confirmation test in this suite (e.g.
    # test_target_confirmation_unit()). Piping real Cyrillic "да"/"нет" through actual OS
    # stdin on this box round-trips through the console codepage, not UTF-8 (see the
    # sys.stdout/sys.stderr reconfigure() comment in __main__ -- stdin is deliberately NOT
    # touched by this feature, same as before), so a literal piped "да" can arrive
    # mojibake'd and get silently read as "no". That is a test-harness-only concern with
    # THIS way of driving the process, not a product bug -- exercising the real function via
    # input_fn sidesteps it entirely while still hitting real run_for_source()/
    # run_analyze_for_source() and the real filesystem.
    def run_bare(tag, answers):
        # 2026-07-12: the top-level mode menu no longer has a scripted "exit" answer at all
        # (see prompt_bare_launch_menu()'s docstring -- "0" was dropped there by direct user
        # feedback, Ctrl+C/closing the window are the only ways out now). Once a scenario's
        # answer list runs out, input_fn raises EOFError -- exactly what a closed/redirected
        # stdin does for real (see main()'s own EOFError handling) -- and run_bare_launch()
        # is expected to let that propagate cleanly rather than needing one more scripted
        # answer to reach an "exit" option that doesn't exist anymore.
        code = (
            "import sys; sys.path.insert(0, %r)\n"
            "for s in (sys.stdout, sys.stderr):\n"
            "    s.reconfigure(encoding='utf-8', errors='replace')\n"
            "import photosort_win as m\n"
            "m.enumerate_menu_drives = lambda: []\n"
            "answers = iter(%r)\n"
            "def _input(p=''):\n"
            "    try:\n"
            "        return next(answers)\n"
            "    except StopIteration:\n"
            "        raise EOFError\n"
            "try:\n"
            "    m.run_bare_launch(input_fn=_input, log=print)\n"
            "except EOFError:\n"
            "    pass\n"
        ) % (ROOT, answers)
        rr = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                             encoding="utf-8", errors="replace", timeout=120)
        check(rr.returncode == 0, f"bare-menu {tag}: exits 0 (stderr={rr.stderr[-500:]})")
        return rr

    # [1] view -> "Что дальше?" menu, "0" = Главное меню (replaces the old да/нет -- see
    # 2026-07-12 back-navigation) -> answers run out right as the mode menu is shown again,
    # run_bare()'s EOFError handling ends the process cleanly -> nothing written to TARGET
    # (analyze-quick never writes, and [1] doesn't even ask for a TARGET -- section 4 of the ТЗ).
    src2 = os.path.join(WORK, "bare_menu_view_src")
    tgt2 = os.path.join(WORK, "bare_menu_view_tgt")
    image(os.path.join(src2, "b.jpg"), 800, 600, exif=True, dt="2020:06:06 10:00:00")
    r2 = run_bare("view", ["1", "1", src2, "0"])
    check("Ваши оригиналы не изменяются" in r2.stdout,
          "bare-menu view: fully bare launch shows the new welcome banner")
    check("Ctrl+C" in r2.stdout,
          "bare-menu view: 2026-07-11 finding -- welcome banner tells the user Ctrl+C stops "
          "the program at any time (Ctrl-C was already handled safely, just never advertised)")
    check("Подробнее о параметрах запуска: PhotoArchive --help" in r2.stdout,
          "bare-menu view: 2026-07-12 -- the CLI-flags hint moved into the welcome banner "
          "(was a separate 'Опытным пользователям: ...' block after every menu choice)")
    check("vo1012.github.io/PhotoArchive" in r2.stdout,
          "bare-menu view: 2026-07-20 -- welcome banner links the project site (SITE_URL) "
          "next to the version line, not the raw GitHub repo")
    check("Опытным пользователям" not in r2.stdout,
          "bare-menu view: 2026-07-12 -- old 'gatekeeping' wording is gone")
    check("Сканирование источника" in r2.stdout,
          "bare-menu view: menu lists the three mode options "
          "(2026-07-12: reworded to a consistent dry technical register, no more "
          "mixed first-person voice between items)")
    check(not os.path.isdir(tgt2),
          "bare-menu view: choosing [1] and declining the follow-up writes nothing to TARGET")

    # Enter (blank) defaults to [1] (view, safe) -- not the real build.
    src3 = os.path.join(WORK, "bare_menu_enter_src")
    tgt3 = os.path.join(WORK, "bare_menu_enter_tgt")
    image(os.path.join(src3, "c.jpg"), 800, 600, exif=True, dt="2020:07:07 10:00:00")
    run_bare("Enter-default", ["", "1", src3, "0"])
    check(not os.path.isdir(tgt3),
          "bare-menu Enter-default: a bare Enter lands on [1] (view), never writes to TARGET")

    # Full ladder [1] -> [2] -> [3]: view, "1" (показать пробный прогон) on the new
    # "Что дальше?" menu (TARGET asked here for the first time), "1" (собрать по-настоящему)
    # on its own "Что дальше?" menu, then "да" to the final build confirmation -- ends with a
    # real archived file on disk (SOURCE/TARGET threaded through the ladder, not re-asked once
    # given).
    src4 = os.path.join(WORK, "bare_menu_ladder_src")
    tgt4 = os.path.join(WORK, "bare_menu_ladder_tgt")
    image(os.path.join(src4, "d.jpg"), 800, 600, exif=True, dt="2020:08:08 10:00:00")
    run_bare("ladder", ["1", "1", src4, "1", "1", tgt4, "1", "да"])
    check(os.path.isdir(os.path.join(tgt4, "__служебные_файлы")),
          "bare-menu ladder: [1] -> [1] -> да reaches a real archive build (not just dry-run)")
    check(any(fn == "d.jpg" for _, _, files in os.walk(tgt4) for fn in files),
          "bare-menu ladder: the real photo actually got copied onto disk, not just logged")

    # Direct [3], no ladder: goes straight to the real build after one merged confirmation
    # (fresh, non-bare-root TARGET needs neither risk- nor drive-root sub-confirmation).
    src5 = os.path.join(WORK, "bare_menu_direct_build_src")
    tgt5 = os.path.join(WORK, "bare_menu_direct_build_tgt")
    image(os.path.join(src5, "e.jpg"), 800, 600, exif=True, dt="2020:09:09 10:00:00")
    run_bare("direct build", ["3", "1", src5, "1", tgt5, "да"])
    check(os.path.isdir(os.path.join(tgt5, "__служебные_файлы")),
          "bare-menu direct build: choosing [3] directly builds the real archive after one confirmation")

    # [2] dry-run, then "0" (Главное меню) on the new "Что дальше?" menu -> answers run out
    # right as the mode menu is shown again, ends cleanly via run_bare()'s EOFError handling
    # -- ТЗ раздел 5: suppress_logs=True means the dry-run rehearsal must NOT create
    # __служебные_файлы\ or any TARGET content at all, unlike the CLI --dry-run contract
    # (which still writes __служебные_файлы\logs\*.csv).
    src6 = os.path.join(WORK, "bare_menu_dryrun_only_src")
    tgt6 = os.path.join(WORK, "bare_menu_dryrun_only_tgt")
    image(os.path.join(src6, "f.jpg"), 800, 600, exif=True, dt="2020:10:10 10:00:00")
    r6 = run_bare("dry-run only", ["2", "1", src6, "1", tgt6, "0"])
    check("Ничего не записано" in r6.stdout,
          "bare-menu dry-run only: prints the human dry-run summary")
    # suppress_logs skips __служебные_файлы\ (ensure_target_layout/RunLogs/TargetLock) and any real
    # file copy -- but resolve_dest_path() itself (engine-internal collision detection, out of
    # this ТЗ's "don't touch the engine" scope) unconditionally os.makedirs()'s the computed
    # destination DIRECTORY regardless of dry_run, so an empty ByDate\YYYY\YYYY-MM-DD\ skeleton
    # can legitimately remain -- see ROADMAP.md. The guarantee that matters is checked instead:
    # no service folder, no logs, no actual photo file landed on disk.
    check(not os.path.isdir(os.path.join(tgt6, "__служебные_файлы")),
          "bare-menu dry-run only: suppress_logs creates no __служебные_файлы\\ (no logs/lock/summary)")
    check(not any(files for _, _, files in os.walk(tgt6)),
          "bare-menu dry-run only: suppress_logs copies no actual file into TARGET "
          "(empty date-folder skeletons are a known, harmless v1 gap -- see ROADMAP.md)")

    # 2026-07-12 back-navigation: "0" on the source submenu returns to the mode menu
    # (ROADMAP.md's biggest deliberately-deferred live finding from 2026-07-11 -- declining
    # any ladder prompt used to exit the whole program instead of returning to the menu).
    # 2026-07-12, same day, simplified again per direct user feedback ("меню перегружено"):
    # ONE universal "[0] Главное меню" everywhere (no per-screen "step back one level" /
    # named "выбрать другой источник/архив" options) -- "0" always resets straight to the
    # mode menu, implemented as a single `while True` loop in run_bare_launch(), not a stack.
    # Picking [1] (view) again after backing out, choosing the source, then going back to the
    # main menu from the "Что дальше?" screen should behave exactly like a normal
    # single-pass [1] run -- the mode menu and "Откуда взять фотографии?" screens are each
    # shown twice (once before, once after backing out), then answers run out right as the
    # mode menu is shown a third time, ending cleanly via run_bare()'s EOFError handling.
    src7 = os.path.join(WORK, "bare_menu_back_to_mode_src")
    tgt7 = os.path.join(WORK, "bare_menu_back_to_mode_tgt")
    image(os.path.join(src7, "g.jpg"), 800, 600, exif=True, dt="2020:11:11 10:00:00")
    r7 = run_bare("back to mode", ["1", "0", "1", "1", src7, "0"])
    check(r7.stdout.count("Откуда взять фотографии?") == 2,
          "bare-menu back-to-mode: '0' on the source submenu re-shows the mode menu, then the "
          "source submenu is shown a second time after choosing [1] again")
    check(not os.path.isdir(tgt7),
          "bare-menu back-to-mode: backing out and re-choosing [1] still never writes to TARGET")

    # 2026-07-12: declining the final build confirmation ("нет"/blank) inside the ladder now
    # returns all the way to the main menu (see the universal-"0" simplification above) --
    # redoing the whole flow with a different TARGET should still reach a real build.
    src8 = os.path.join(WORK, "bare_menu_decline_confirm_src")
    tgt8_declined = os.path.join(WORK, "bare_menu_decline_confirm_tgt_declined")
    tgt8_final = os.path.join(WORK, "bare_menu_decline_confirm_tgt_final")
    image(os.path.join(src8, "h.jpg"), 800, 600, exif=True, dt="2020:12:12 10:00:00")
    r8 = run_bare("decline confirm then retarget",
                  ["3", "1", src8, "1", tgt8_declined, "нет",
                   "3", "1", src8, "1", tgt8_final, "да"])
    check("Возвращаемся в главное меню" in r8.stdout,
          "bare-menu decline-confirm: declining the final build confirmation prints the "
          "back-to-main-menu message instead of just exiting")
    check(not os.path.isdir(tgt8_declined),
          "bare-menu decline-confirm: the declined TARGET is never touched")
    check(os.path.isdir(os.path.join(tgt8_final, "__служебные_файлы")),
          "bare-menu decline-confirm: redoing the flow with a different TARGET afterwards "
          "still reaches a real build")

    # 2026-07-12, live user report ("запустил вторую копию, пока первая уже собирала архив
    # в тот же TARGET"): run_for_source() correctly returns RunResult(failed=True) and logs
    # "ОШИБКА: ..." when TargetLock is already held (see TargetLock's own docstring, p.5.4б) --
    # but
    # _bare_launch_run_build() used to ignore that return value and unconditionally print
    # "Готово. Архив собран" anyway, falsely claiming success even though nothing was
    # written. Simulate a concurrent run by pre-creating the LOCK file the real TargetLock
    # would have created.
    src9 = os.path.join(WORK, "bare_menu_locked_target_src")
    tgt9 = os.path.join(WORK, "bare_menu_locked_target_tgt")
    image(os.path.join(src9, "i.jpg"), 800, 600, exif=True, dt="2021:01:01 10:00:00")
    os.makedirs(os.path.join(tgt9, "__служебные_файлы"), exist_ok=True)
    with open(os.path.join(tgt9, "__служебные_файлы", "LOCK"), "w", encoding="utf-8") as f:
        f.write("999999")  # fake PID of an "other" running instance
    r9 = run_bare("locked target", ["3", "1", src9, "1", tgt9, "да"])
    check("похоже, другой прогон PhotoArchive уже работает" in r9.stdout,
          "locked target: TargetLocked's own error message is shown")
    check("Готово. Архив собран" not in r9.stdout,
          "locked target: 2026-07-12 fix -- no longer falsely claims success when the build "
          "was blocked by another instance's LOCK file")
    check("Сборка не выполнена" in r9.stdout,
          "locked target: prints an explicit failure message instead")
    check("Возвращаемся в главное меню" in r9.stdout,
          "locked target: returns to the main menu, same universal recovery as a declined "
          "confirmation")
    check(not any(fn == "i.jpg" for _, _, files in os.walk(tgt9) for fn in files),
          "locked target: the photo was never actually copied (LOCK correctly prevented the write)")


def test_tmp_extract_wipe_protection():
    print("\n=== security audit #1: TMP_EXTRACT cleanup never touches non-own content ===")
    src = os.path.join(WORK, "src_tmp_extract_wipe")
    tgt = os.path.join(WORK, "target_tmp_extract_wipe")
    image(os.path.join(src, "a.jpg"), 800, 600, exif=True, dt="2022:09:09 10:00:00")
    r0 = run_photosort(src, tgt)
    check(r0.returncode == 0, "audit#1: initial run (creates the archive) exits 0")

    tmp_extract = os.path.join(tgt, "__служебные_файлы", "tmp_extract")
    os.makedirs(tmp_extract, exist_ok=True)
    # Foreign content -- simulates tmp_extract_dir being misconfigured (typo or a
    # maliciously "helpful" photoarchive_config.yaml) to point at a folder that isn't ours at all.
    foreign_dir = os.path.join(tmp_extract, "my_important_documents")
    os.makedirs(foreign_dir, exist_ok=True)
    foreign_file = os.path.join(foreign_dir, "precious.txt")
    with open(foreign_file, "w", encoding="utf-8") as f:
        f.write("do not delete me")
    # Our own stale extraction dir (named like a real sha256 hex digest) -- this one SHOULD
    # still be cleaned up, exactly like before the fix.
    own_stale_dir = os.path.join(tmp_extract, "a" * 64)
    os.makedirs(own_stale_dir, exist_ok=True)
    with open(os.path.join(own_stale_dir, "leftover.tmp"), "w", encoding="utf-8") as f:
        f.write("stale extraction remnant from a crashed run")

    r1 = run_photosort(src, tgt)
    check(r1.returncode == 0, "audit#1: second run exits 0")
    check(os.path.isfile(foreign_file),
          "audit#1: foreign content in TMP_EXTRACT survives untouched")
    check(not os.path.isdir(own_stale_dir),
          "audit#1: our own hash-named leftover dir IS still cleaned up as before")
    check("НЕ трогаю" in r1.stdout, "audit#1: a warning about foreign content is logged")


def test_negative_free_space_margin_and_numeric_config_validation():
    print("\n=== security audit #2/#7: numeric photoarchive_config.yaml/CLI fields are range-validated ===")
    src = os.path.join(WORK, "src_numeric_cfg")
    image(os.path.join(src, "a.jpg"), 800, 600, exif=True, dt="2022:08:08 10:00:00")

    # NB: sample_limit is deliberately NOT a photoarchive_config.yaml field (only --sample-limit CLI/
    # interactive, see CONFIG_YAML_FIELDS) -- tested separately below via the CLI flag.
    bad_configs = [
        ("free_space_margin_gb: -1\n", "free_space_margin_gb"),
        ("max_archive_depth: 0\n", "max_archive_depth"),
        ("max_dest_path: 5\n", "max_dest_path"),
        ("small_image_px: -1\n", "small_image_px"),
        ("read_retry_count: -1\n", "read_retry_count"),
        ("read_retry_delay: -1\n", "read_retry_delay"),
    ]
    cfg_path = os.path.join(ROOT, "photoarchive_config.yaml")
    for content, field_name in bad_configs:
        # A fresh TARGET per field -- avoids one iteration's (mis)behavior contaminating
        # the "never wrote anything to TARGET" check of the next.
        tgt = os.path.join(WORK, f"target_numeric_cfg_{field_name}")
        with open(cfg_path, "w", encoding="utf-8") as f:
            f.write(content)
        try:
            r = run_photosort(src, tgt)
        finally:
            os.remove(cfg_path)
        check(r.returncode == 3,
              f"audit#7: bad {field_name} does not crash, exits EXIT_CONFIG_ERROR=3 "
              f"(got {r.returncode})")
        check("ОШИБКА КОНФИГУРАЦИИ" in r.stdout and field_name in r.stdout,
              f"audit#7: bad {field_name} gets a clear config-error message")
        check(not os.path.isdir(os.path.join(tgt, "ByDate")),
              f"audit#7: bad {field_name} never wrote anything to TARGET")

    tgt_sample_limit = os.path.join(WORK, "target_numeric_cfg_sample_limit")
    r = run_photosort(src, tgt_sample_limit, extra_args=["--sample-limit=-1"])
    check(r.returncode == 3,
          f"audit#7: bad --sample-limit does not crash, exits EXIT_CONFIG_ERROR=3 "
          f"(got {r.returncode})")
    check("ОШИБКА КОНФИГУРАЦИИ" in r.stdout and "sample_limit" in r.stdout,
          "audit#7: bad --sample-limit gets a clear config-error message")
    check(not os.path.isdir(os.path.join(tgt_sample_limit, "ByDate")),
          "audit#7: bad --sample-limit never wrote anything to TARGET")


def test_disk_full_graceful_stop():
    print("\n=== security audit #2: InsufficientSpace stops gracefully via atomic_copy()'s own "
          "margin check (the same code path the new errno.ENOSPC handler sits next to) ===")
    # Actually filling the CI runner's disk to exercise the real errno.ENOSPC branch isn't
    # practical/safe here -- this instead forces atomic_copy()'s pre-write margin check
    # (free - size < margin_bytes) to trip deterministically via an oversized configured
    # margin, confirming the whole "stop cleanly, no traceback" path around it still works
    # now that free_space_margin_gb itself is validated (negative no longer bypasses it).
    src = os.path.join(WORK, "src_disk_full")
    tgt = os.path.join(WORK, "target_disk_full")
    image(os.path.join(src, "a.jpg"), 800, 600, exif=True, dt="2022:10:10 10:00:00")
    cfg_path = os.path.join(ROOT, "photoarchive_config.yaml")
    # An unreasonably large margin guarantees atomic_copy()'s free-space check trips deterministically.
    with open(cfg_path, "w", encoding="utf-8") as f:
        f.write("free_space_margin_gb: 999999\n")
    try:
        r = run_photosort(src, tgt)
    finally:
        os.remove(cfg_path)
    check(r.returncode == 4,
          f"audit#2: an oversized margin stops gracefully, exits EXIT_INSUFFICIENT_SPACE=4 "
          f"(got {r.returncode})")
    check("Traceback" not in r.stdout and "Traceback" not in r.stderr,
          "audit#2: no raw traceback when space is (artificially) insufficient")
    check("ОСТАНОВКА" in r.stdout or "недостаточно места" in r.stdout,
          "audit#2: the friendly insufficient-space message is shown")


def test_place_failure_does_not_crash_run():
    print("\n=== security audit finding #1 (2026-07-10 follow-up): a destination-path OSError "
          "(not InsufficientSpace) logs the one file and keeps the run/batch going instead of "
          "crashing with a raw traceback ===")
    # Before the fix, resolve_dest_path()'s own os.makedirs() was completely unguarded, and
    # place_file() in the raw_mirrored/main-appended branches only caught InsufficientSpace --
    # any other OSError (antivirus file lock, bad sector on a failing source disc, a reserved
    # device name after extreme truncation, or -- as forced deterministically here -- a
    # destination path segment blocked by an unrelated plain file) propagated all the way out
    # of run(), killing the entire process (and, for --source all, every remaining source).
    src = os.path.join(WORK, "src_place_failure")
    tgt = os.path.join(WORK, "target_place_failure")
    # blocked.jpg's destination dir is ByDate/2023/2023-05 [PhotoArchive] -- blocked below.
    # ok.jpg is a different YEAR (2024) so its destination dir is untouched by the collision,
    # proving the run keeps making normal progress on later files after the failure.
    image(os.path.join(src, "blocked.jpg"), 800, 600, exif=True, dt="2023:05:05 10:00:00")
    image(os.path.join(src, "ok.jpg"), 800, 600, exif=True, dt="2024:06:06 10:00:00")

    os.makedirs(os.path.join(tgt, "ByDate"), exist_ok=True)
    with open(os.path.join(tgt, "ByDate", "2023"), "w", encoding="utf-8") as f:
        f.write("i am a plain file occupying the '2023' path segment, not a directory")

    r = run_photosort(src, tgt)
    check(r.returncode == 0,
          "audit#1: run exits 0 despite one file's destination directory being unusable")
    check("Traceback" not in r.stdout and "Traceback" not in r.stderr,
          "audit#1: no raw traceback from the blocked destination")
    check("ОШИБКА записи" in r.stdout, "audit#1: the blocked file's failure is logged")
    check(os.path.isfile(os.path.join(tgt, "ByDate", "2024", "2024-06 [PhotoArchive]", "ok.jpg")),
          "audit#1: the later file in the same run is still archived normally")

    unreadable = read_csv(os.path.join(tgt, "__служебные_файлы", "logs", "unreadable.csv"))
    check(any("write_failed" in row["error"] for row in unreadable),
          "audit#1: the blocked file is recorded in unreadable.csv as write_failed")


def test_symlink_loop_protection():
    print("\n=== security audit #3: a self-referential symlink/junction doesn't crash the walk ===")
    src = os.path.join(WORK, "src_symlink_loop")
    os.makedirs(os.path.join(src, "A"), exist_ok=True)
    image(os.path.join(src, "real_photo.jpg"), 800, 600, exif=True, dt="2022:07:07 10:00:00")
    loop_link = os.path.join(src, "A", "loop")
    try:
        os.symlink(src, loop_link, target_is_directory=True)
    except OSError as e:
        print(f"  SKIP: cannot create a symlink in this environment ({e}) -- the ancestor-"
              f"realpath cycle check is still in the code, just not exercised by this run")
        return
    tgt = os.path.join(WORK, "target_symlink_loop")
    r = run_photosort(src, tgt)
    check(r.returncode == 0,
          "audit#3: run against a self-referential symlink exits 0 (not RecursionError)")
    check("Traceback" not in r.stdout and "Traceback" not in r.stderr,
          "audit#3: no raw traceback from the symlink loop")
    check(os.path.isfile(os.path.join(tgt, "ByDate", "2022", "2022-07 [PhotoArchive]",
                                       "real_photo.jpg")),
          "audit#3: the real photo outside the loop is still archived correctly")


def test_deep_directory_tree_does_not_raise_recursion_error():
    print("\n=== ROADMAP.md 'RecursionError на очень глубоком дереве папок SOURCE': "
          "SourceWalker._walk_dir() now descends into subdirectories via an explicit stack "
          "instead of `yield from self._walk_dir(...)` -- a tree far deeper than Python's "
          "default 1000-frame recursion limit no longer crashes the whole run, and an "
          "unrelated shallow file elsewhere in SOURCE is still archived normally ===")
    src = os.path.join(WORK, "src_deep_tree")
    tgt = os.path.join(WORK, "target_deep_tree")
    os.makedirs(src, exist_ok=True)
    depth = 1500  # comfortably past sys.getrecursionlimit()'s default of 1000
    deep_dir = src
    # os.makedirs() is itself recursive (recurses on the missing portion of the path) -- build
    # the chain one level at a time here so the fixture doesn't hit Python's OWN RecursionError
    # before ever reaching the code actually under test. Once every level already exists,
    # image()'s own os.makedirs(..., exist_ok=True) call below no longer needs to recurse at
    # all (path.exists() on the immediate parent short-circuits it).
    # 2026-07-15, real-Windows finding: plain os.mkdir() hits Windows' own 260-char MAX_PATH
    # around depth ~130 (this fixture is unrelated to the winlong()/_makedirs_iterative() fix
    # under test here -- that's in photosort_win.py and only kicks in once TARGET/SOURCE paths
    # are handed to it; building the fixture itself is on us). Same "\\?\" extended-length
    # prefix winlong() uses in photosort_win.py, already used the same way for TARGET paths in
    # test_long_path() above -- LongPathsEnabled isn't reliably on for every dev machine, so
    # this can't be skipped even though the tree lives entirely under one drive.
    try:
        for _ in range(depth):
            deep_dir = os.path.join(deep_dir, "d")
            mkdir_target = ("\\\\?\\" + os.path.abspath(deep_dir)) if os.name == "nt" else deep_dir
            os.mkdir(mkdir_target)
        deep_photo = os.path.join(deep_dir, "deep_photo.jpg")
        image(("\\\\?\\" + os.path.abspath(deep_photo)) if os.name == "nt" else deep_photo,
              800, 600, exif=True, dt="2023:03:03 10:00:00")
        # Independent of the deep branch and of album/dump-segment routing rules (not this
        # test's concern) -- only here to prove the OLD failure mode (RecursionError
        # propagating out of the walker generator and aborting Phase 2 entirely, per the
        # ROADMAP finding) is gone: a totally unrelated file must still get archived, not
        # silently lost along with everything else in the run.
        image(os.path.join(src, "shallow_photo.jpg"), 800, 600, exif=True, dt="2023:04:04 10:00:00")
        cfg_path = os.path.join(ROOT, "photoarchive_config.yaml")
        with open(cfg_path, "w", encoding="utf-8") as f:
            f.write("free_space_margin_gb: 0.01\n")
        try:
            r = run_photosort(src, tgt)
        finally:
            os.remove(cfg_path)
        check(r.returncode == 0,
              f"deep-tree: a {depth}-level-deep SOURCE tree does not crash the run "
              f"(got returncode={r.returncode})")
        check("RecursionError" not in r.stdout and "RecursionError" not in r.stderr
              and "maximum recursion depth exceeded" not in r.stdout
              and "maximum recursion depth exceeded" not in r.stderr,
              "deep-tree: no RecursionError anywhere in output (checked both the exception "
              "class name and its message text -- a caught-and-logged RecursionError, e.g. "
              "from a downstream os.makedirs() on an equally deep TARGET dest_dir, only ever "
              "surfaces the message text, not the class name)")
        # 2026-07-15, real-Windows finding: routing mirrors the "d"-repeated folder chain
        # into Albums\ as-is (not treated as noise/dump), so the archived copy is exactly as
        # deep as the source -- os.walk(tgt) needs the same "\\?\" treatment as the SOURCE
        # side above, same as test_long_path() does for its own TARGET walk.
        walk_tgt = ("\\\\?\\" + os.path.abspath(tgt)) if os.name == "nt" else tgt
        archived_names = {f for dirpath, _, files in os.walk(walk_tgt) for f in files
                           if "__служебные_файлы" not in dirpath}
        check("deep_photo.jpg" in archived_names,
              "deep-tree: the file at the bottom of the deep branch was actually archived")
        check("shallow_photo.jpg" in archived_names,
              "deep-tree: an unrelated shallow file elsewhere in SOURCE is archived too, not lost")
    finally:
        # Same incident as test_long_path() above: a >260-char tree left on disk survives past
        # this test's end, and the plain shutil.rmtree(WORK) at the START of the NEXT run
        # can't delete it without the same "\\?\" prefix. Both src (the fixture) AND tgt (its
        # exact-depth Albums\ mirror, see the routing finding above) need this -- tgt is
        # defined unconditionally above the try so it's always valid here, even if an
        # exception hits before run_photosort() ever runs.
        for p in (src, tgt):
            shutil.rmtree(("\\\\?\\" + os.path.abspath(p)) if os.name == "nt" else p,
                           ignore_errors=True)


def test_archive_entry_count_bomb():
    print("\n=== security audit #4: archive entry-count bomb (many tiny files) is rejected ===")
    src = os.path.join(WORK, "src_entry_bomb")
    os.makedirs(src, exist_ok=True)
    zpath = os.path.join(src, "bomb.zip")
    with zipfile.ZipFile(zpath, "w") as zf:
        for i in range(5):
            zf.writestr(f"f{i}.txt", "")
    tgt = os.path.join(WORK, "target_entry_bomb")

    # Lower MAX_ARCHIVE_ENTRIES to 3 via monkeypatch so 5 tiny files is enough to trip it --
    # constructing a real 200,001-entry zip just to hit the real threshold would work but is
    # needlessly slow for CI; this exercises the exact same comparison in _handle_archive().
    code = (
        "import sys; sys.path.insert(0, %r)\n"
        "for s in (sys.stdout, sys.stderr):\n"
        "    s.reconfigure(encoding='utf-8', errors='replace')\n"
        "import photosort_win as m\n"
        "m.MAX_ARCHIVE_ENTRIES = 3\n"
        "m.run_for_source(%r, %r, False, 0, log=print)\n"
    ) % (ROOT, src, tgt)
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                        encoding="utf-8", errors="replace")
    print(r.stdout[-2000:])
    check(r.returncode == 0, "audit#4: entry-count-bomb run exits 0")
    archives_log_path = os.path.join(tgt, "__служебные_файлы", "logs", "archives.log")
    log_text = open(archives_log_path, encoding="utf-8").read() if os.path.exists(archives_log_path) else ""
    check("archive_bomb_suspected" in log_text and "entries=" in log_text,
          "audit#4: many-tiny-files archive logged as archive_bomb_suspected (by entry count)")
    # ensure_target_layout() always creates an empty ByDate/ regardless of what (if
    # anything) ends up placed in it -- check it's empty, not that it doesn't exist.
    bydate = os.path.join(tgt, "ByDate")
    check(not os.path.isdir(bydate) or not any(os.scandir(bydate)),
          "audit#4: the bomb archive was never actually extracted (ByDate/ stays empty)")


def test_archive_deleted_mid_scan_does_not_crash():
    print("\n=== live user report 2026-07-11: an archive the user deletes WHILE PhotoArchive is "
          "still scanning it (mid os.path.getsize()/sha256_file(), both reached during the "
          "enumeration phase, well before extraction) used to raise a raw OSError straight out "
          "of the SourceWalker generator, past main()'s KeyboardInterrupt/EOFError-only catch, "
          "and crash the ENTIRE run with a traceback -- now guarded, logged as "
          "archive_read_error, and the run continues with everything else ===")
    src = os.path.join(WORK, "src_archive_vanish")
    os.makedirs(src, exist_ok=True)
    zpath = os.path.join(src, "vanishing.zip")
    with zipfile.ZipFile(zpath, "w") as zf:
        zf.writestr("photo.jpg", "not a real jpeg but irrelevant, never actually extracted")
    # A second, ordinary source file proves the run doesn't just abort after the vanished
    # archive -- it keeps going and archives everything else normally.
    image(os.path.join(src, "ok.jpg"), 800, 600, exif=True, dt="2024:06:06 10:00:00")
    tgt = os.path.join(WORK, "target_archive_vanish")

    # Deterministic, no real threading/timing race needed: monkeypatch os.path.getsize so
    # that -- for THIS archive's path only -- it deletes the file right after reporting its
    # size, simulating the user's delete landing in the window between os.path.getsize() and
    # the next read (sha256_file()), same as the live report ("программа его продолжала
    # распаковывать" -- sha256_file() reads the whole archive, real wall-clock time on a large
    # file, matching "some time passed" before the crash). Scoped to this one filename so it
    # doesn't also delete ok.jpg out from under its own unrelated os.path.getsize() call in
    # atomic_copy() later in the same run.
    code = (
        "import sys; sys.path.insert(0, %r)\n"
        "for s in (sys.stdout, sys.stderr):\n"
        "    s.reconfigure(encoding='utf-8', errors='replace')\n"
        "import os\n"
        "import photosort_win as m\n"
        "_orig_getsize = os.path.getsize\n"
        "def _getsize_then_delete(path, *a, **kw):\n"
        "    size = _orig_getsize(path, *a, **kw)\n"
        "    if os.path.basename(path) == 'vanishing.zip':\n"
        "        os.remove(path)\n"
        "    return size\n"
        "os.path.getsize = _getsize_then_delete\n"
        "m.run_for_source(%r, %r, False, 0, log=print)\n"
    ) % (ROOT, src, tgt)
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                        encoding="utf-8", errors="replace")
    print(r.stdout[-2000:])
    check(r.returncode == 0, "vanish: run exits 0 despite the archive disappearing mid-scan")
    check("Traceback" not in r.stdout and "Traceback" not in r.stderr,
          "vanish: no raw traceback from the vanished archive")
    archives_log_path = os.path.join(tgt, "__служебные_файлы", "logs", "archives.log")
    log_text = open(archives_log_path, encoding="utf-8").read() if os.path.exists(archives_log_path) else ""
    check("archive_read_error" in log_text,
          "vanish: the vanished archive is logged as archive_read_error, not silently dropped")
    check(os.path.isfile(os.path.join(tgt, "ByDate", "2024", "2024-06 [PhotoArchive]", "ok.jpg")),
          "vanish: the later ordinary file in the same run is still archived normally")


def test_archive_path_traversal_rejected():
    print("\n=== security audit #8: zip-slip (path-traversal) archive member is rejected ===")
    src = os.path.join(WORK, "src_zipslip")
    os.makedirs(src, exist_ok=True)
    zpath = os.path.join(src, "evil.zip")
    with zipfile.ZipFile(zpath, "w") as zf:
        zf.writestr("../../../evil_escape.txt", "should never land outside tmp_extract")
        zf.writestr("normal_photo.jpg", "not a real jpeg but irrelevant, never extracted")
    tgt = os.path.join(WORK, "target_zipslip")
    escape_target = os.path.abspath(os.path.join(WORK, "..", "evil_escape.txt"))
    if os.path.exists(escape_target):
        os.remove(escape_target)  # pre-clean in case a previous FAILED run of this test left it

    r = run_photosort(src, tgt)
    check(r.returncode == 0, "audit#8: zip-slip archive run exits 0")
    archives_log_path = os.path.join(tgt, "__служебные_файлы", "logs", "archives.log")
    log_text = open(archives_log_path, encoding="utf-8").read() if os.path.exists(archives_log_path) else ""
    check("archive_path_traversal_suspected" in log_text,
          "audit#8: archive with a '..'-segment member is logged as path-traversal-suspected")
    check(not os.path.exists(escape_target),
          "audit#8: nothing actually escaped to a path outside TMP_EXTRACT")
    bydate = os.path.join(tgt, "ByDate")
    check(not os.path.isdir(bydate) or not any(os.scandir(bydate)),
          "audit#8: the whole archive was rejected outright, not even partially extracted "
          "(ByDate/ stays empty)")


def test_archive_symlink_rejected():
    print("\n=== security audit (2026-07-10 Phase 2): a zip member stored as a symlink is "
          "rejected outright, not walked as ordinary source content ===")
    # Empirically confirmed against the 7z binary in this environment: a zip member with the
    # Unix S_IFLNK external_attr bit set, whose target stays WITHIN the extraction dir, is
    # extracted successfully (exit 0) as a real symlink on disk -- 7-Zip's own "Dangerous
    # link path" check only rejects a target that resolves OUTSIDE the destination, not an
    # in-bounds one. SourceWalker must therefore never trust "extraction succeeded" as proof
    # the tree contains no reparse points -- this in-bounds case is exactly what
    # find_reparse_point_in_tree() exists to catch (see photosort_win.py).
    import stat as _stat
    src = os.path.join(WORK, "src_archive_symlink")
    os.makedirs(src, exist_ok=True)
    zpath = os.path.join(src, "family_photos.zip")
    with zipfile.ZipFile(zpath, "w") as zf:
        zf.writestr("real_photo.jpg", "not a real jpeg but irrelevant, never extracted")
        link_info = zipfile.ZipInfo("innocuous_link")
        link_info.external_attr = (_stat.S_IFLNK | 0o777) << 16
        zf.writestr(link_info, "real_photo.jpg")
    tgt = os.path.join(WORK, "target_archive_symlink")

    r = run_photosort(src, tgt)
    check(r.returncode == 0, "archive-symlink: run exits 0 (not a crash)")
    archives_log_path = os.path.join(tgt, "__служебные_файлы", "logs", "archives.log")
    log_text = open(archives_log_path, encoding="utf-8").read() if os.path.exists(archives_log_path) else ""
    if "archive_extract_failed" in log_text:
        # A 7z build that refuses this symlink at extraction time (older/different version)
        # also produces a safe outcome, just via a different rejection reason -- don't fail
        # the test over a local 7z-version difference this test doesn't control.
        print("  SKIP: local 7z build failed this archive's extraction outright (also safe, "
              "just not the archive_symlink_suspected path this test targets)")
        return
    check("archive_symlink_suspected" in log_text,
          "archive-symlink: archive with a symlink member is logged as symlink-suspected")
    bydate = os.path.join(tgt, "ByDate")
    check(not os.path.isdir(bydate) or not any(os.scandir(bydate)),
          "archive-symlink: the whole archive was rejected outright, nothing archived from it")


def test_archive_entry_count_mismatch_rejected():
    print("\n=== Phase-2 finding 7: extracted file count lower than the archive's own listing "
          "is rejected (defense in depth behind the regex-based traversal-name check) ===")
    # Deterministic, no actual path-traversal bypass needed: a zip with two members sharing
    # the same name extracts (7z x -y) to just ONE file on disk (second overwrites first),
    # while the archive's own listing (7z l -slt) still reports both as separate entries --
    # empirically confirmed in this sandbox. That mismatch (entries=3, extracted=2) is exactly
    # the symptom count_extracted_files() is meant to catch, without needing to fabricate a
    # real zip-slip escape. Member names use a .jpg extension (not .txt) so the archive still
    # has a media candidate per its listing -- 2026-07-11's archive_no_media skip-extraction
    # optimization would otherwise never extract this archive at all, never reaching the
    # entry-count check this test targets (a plain-text-only archive is correctly treated as
    # having nothing worth extracting in the first place, see test_archive_no_media_skips_extraction).
    src = os.path.join(WORK, "src_dup_member")
    os.makedirs(src, exist_ok=True)
    zpath = os.path.join(src, "dup_member.zip")
    with zipfile.ZipFile(zpath, "w") as zf:
        zf.writestr("same.jpg", "AAA")
        zf.writestr("same.jpg", "BBB")
        zf.writestr("other.jpg", "CCC")
    tgt = os.path.join(WORK, "target_dup_member")

    r = run_photosort(src, tgt)
    check(r.returncode == 0, "finding7: duplicate-member archive run exits 0 (not a crash)")
    archives_log_path = os.path.join(tgt, "__служебные_файлы", "logs", "archives.log")
    log_text = open(archives_log_path, encoding="utf-8").read() if os.path.exists(archives_log_path) else ""
    check("archive_path_traversal_suspected" in log_text,
          "finding7: archive whose extracted file count is lower than its listing is rejected")
    bydate = os.path.join(tgt, "ByDate")
    check(not os.path.isdir(bydate) or not any(os.scandir(bydate)),
          "finding7: the whole archive was rejected outright, nothing archived from it")


def test_relative_path_rejected():
    print("\n=== 5.9: relative SOURCE/TARGET rejected with a clear error, not a traceback ===")
    src = os.path.join(WORK, "relpath_src")
    tgt = os.path.join(WORK, "relpath_target")
    image(os.path.join(src, "a.jpg"), 800, 600, exif=True, dt="2019:01:01 10:00:00")

    r = run_photosort("relpath_src", tgt, extra_args=[])
    # relative SOURCE, absolute TARGET -- Config.__post_init__ raises before any work happens,
    # run_for_source() catches ValueError and returns RunResult(failed=True, exit_code=3).
    check(r.returncode == 3,
          f"5.9: relative SOURCE does not crash, exits EXIT_CONFIG_ERROR=3 (got {r.returncode})")
    check("Traceback" not in r.stdout and "Traceback" not in r.stderr,
          "5.9: relative SOURCE produces no traceback")
    check("SOURCE" in r.stdout and "полным путём" in r.stdout,
          "5.9: relative SOURCE gets a clear Russian error message")
    check(not os.path.isdir(tgt), "5.9: relative SOURCE run does not create TARGET at all")

    r2 = run_photosort(src, "relpath_target", extra_args=[])
    check(r2.returncode == 3,
          f"5.9: relative TARGET does not crash, exits EXIT_CONFIG_ERROR=3 (got {r2.returncode})")
    check("TARGET" in r2.stdout and "полным путём" in r2.stdout,
          "5.9: relative TARGET gets a clear Russian error message")


def test_third_party_licenses_wired_up():
    print("\n=== Phase-2 finding 5: THIRD_PARTY_LICENSES.md wired into build.bat/RELEASING.md ===")
    licenses_path = os.path.join(ROOT, "THIRD_PARTY_LICENSES.md")
    check(os.path.isfile(licenses_path), "THIRD_PARTY_LICENSES.md exists at repo root")
    licenses_text = ""
    if os.path.isfile(licenses_path):
        with open(licenses_path, encoding="utf-8") as f:
            licenses_text = f.read()
    for name in ("ExifTool", "FFmpeg", "7-Zip", "UnRAR", "bin/licenses"):
        check(name in licenses_text, f"THIRD_PARTY_LICENSES.md mentions {name}")

    build_bat = os.path.join(ROOT, "build", "build.bat")
    with open(build_bat, encoding="utf-8") as f:
        build_text = f.read()
    check("licenses" in build_text, "build.bat references bin/licenses")

    # 2026-07-15: THIRD_PARTY_LICENSES.md is no longer copied as-is by build.bat -- it goes
    # through build/md_to_pdf.py's DOCS list like the other user docs, so dist\ only ever
    # contains THIRD_PARTY_LICENSES.pdf, never a stray .md (see RELEASING.md).
    md_to_pdf = os.path.join(ROOT, "build", "md_to_pdf.py")
    with open(md_to_pdf, encoding="utf-8") as f:
        md_to_pdf_text = f.read()
    check("THIRD_PARTY_LICENSES.md" in md_to_pdf_text,
          "build/md_to_pdf.py converts THIRD_PARTY_LICENSES.md into dist\\THIRD_PARTY_LICENSES.pdf")
    # build.bat still mentions "..\THIRD_PARTY_LICENSES.md" in dev-facing warnings/reminders
    # (the file at the repo root genuinely has that name) -- what must NOT exist anymore is a
    # command that copies it into dist\ as-is.
    check("copy /Y ..\\THIRD_PARTY_LICENSES.md" not in build_text,
          "build.bat no longer copies THIRD_PARTY_LICENSES.md into dist\\ as-is "
          "(superseded by the PDF pipeline)")

    # RELEASING.md is a dev-repo-only internal doc (not published to the public repo, which
    # mirrors this test file too) -- skip gracefully there instead of hard-failing on a file
    # that was never supposed to exist outside the dev repo.
    releasing = os.path.join(ROOT, "RELEASING.md")
    if os.path.isfile(releasing):
        with open(releasing, encoding="utf-8") as f:
            releasing_text = f.read()
        check("bin/licenses" in releasing_text or "bin\\licenses" in releasing_text,
              "RELEASING.md checklist mentions bin/licenses")

    readme = os.path.join(ROOT, "README.md")
    with open(readme, encoding="utf-8") as f:
        readme_text = f.read()
    check("THIRD_PARTY_LICENSES.md" in readme_text, "README.md links THIRD_PARTY_LICENSES.md")


def test_requirements_txt_pinned_and_wired_up():
    print("\n=== Phase-2 finding 6: pinned requirements.txt wired into build.bat/README-BUILD.md ===")
    req_path = os.path.join(ROOT, "requirements.txt")
    check(os.path.isfile(req_path), "requirements.txt exists at repo root")
    packages = ("pyinstaller", "pillow", "pillow-heif", "imagehash", "reverse_geocoder", "pyyaml", "tqdm")
    req_text = ""
    if os.path.isfile(req_path):
        with open(req_path, encoding="utf-8") as f:
            req_text = f.read()
    req_lines = [ln.strip() for ln in req_text.splitlines() if ln.strip() and not ln.strip().startswith("#")]
    for pkg in packages:
        matches = [ln for ln in req_lines if ln.lower().startswith(pkg.lower() + "==")]
        check(len(matches) == 1, f"requirements.txt lists {pkg} exactly once")
        if matches:
            check("==" in matches[0], f"requirements.txt pins {pkg} to an exact version (==)")

    build_bat = os.path.join(ROOT, "build", "build.bat")
    with open(build_bat, encoding="utf-8") as f:
        build_text = f.read()
    check("requirements.txt" in build_text, "build.bat references requirements.txt")
    check("pip install pyinstaller pillow" not in build_text,
          "build.bat no longer has the old unpinned inline pip install list")

    # README-BUILD.md is a dev-repo-only internal doc (not published to the public repo, which
    # mirrors this test file too) -- skip gracefully there, same as the RELEASING.md check above.
    readme_build = os.path.join(ROOT, "README-BUILD.md")
    if os.path.isfile(readme_build):
        with open(readme_build, encoding="utf-8") as f:
            readme_build_text = f.read()
        check("requirements.txt" in readme_build_text,
              "README-BUILD.md references requirements.txt")
        check("pip install pyinstaller pillow" not in readme_build_text,
              "README-BUILD.md no longer has the old unpinned inline pip install list")


# 2026-07-17 (вторая рецензия): плоский список из 68 прямых вызовов раньше означал, что
# запустить один тест можно было только закомментировав остальные 67 -- при том, что каждый
# тест поднимает несколько subprocess-ов, полный прогон идёт минуты, а отлаживаешь всегда
# один. Список объектов-функций + опциональный фильтр по подстроке имени в sys.argv[1] чинит
# оба случая разом: `python ci/windows_ci_test.py near_dup` гоняет только совпадающие тесты.
# Побочный эффект: забытая функция теста, которую не дописали в этот список, -- опечатка в
# ОДНОМ месте (не как раньше, когда `def test_...():` и вызов в main() были двумя независимыми
# местами, и можно было незаметно для CI никогда не вызвать новый тест).
ALL_TESTS = [
    test_regression_and_zones,
    test_sibling_albums_not_merged,
    test_mirror_raw,
    test_multi_source,
    test_long_path,
    test_hidden_attribute,
    test_progress_note_does_not_stick,
    test_progress_bar_desc_separated_even_with_zero_updates,
    test_log_line_does_not_glue_onto_active_bar,
    test_progress_bar_context_note_persists_and_truncates,
    test_source_walker_reports_current_directory,
    test_progress_bar_no_doubled_colon_or_mixed_units,
    test_sanitize_zip,
    test_archive_no_media_skips_extraction,
    test_archive_unlistable_treated_as_bomb,
    test_archive_rename_finalization,
    test_tar_source_never_uses_unverified_rename,
    test_place_file_archive_no_crc_forces_hash_verify,
    test_analyze_modes,
    test_raw_layout_sibling,
    test_photosort_marker_excludes_subtree,
    test_unsorted_is_not_marker_protected,
    test_undated_promotion,
    test_cli_version_help_routing,
    test_check_bundled_tools_detects_broken_frozen_build,
    test_target_nested_warning,
    test_ctrl_c_no_traceback,
    test_eof_no_traceback,
    test_near_dup_appended_not_skipped,
    test_bare_date_subfolder_not_collapsed_as_dump,
    test_desktop_is_dump_segment,
    test_camera_roll_and_new_folder_are_dump_segments,
    test_force_dump_tilde_prefix,
    test_merged_album_marker_file,
    test_archive_file_always_becomes_an_album,
    test_bare_digit_date_folder_kept_inside_album_but_not_as_album_name,
    test_summary_enriched_always,
    test_default_exclude_dirs_configurable_and_logged,
    test_dump_segment_names_configurable,
    test_dump_segment_config_ignores_nested_yaml_alias_bomb,
    test_config_yaml_autocreate_before_first_prompt,
    test_debug_flag_actions_log,
    test_log_rotation,
    test_archive_cache_prunes_stale_paths,
    test_crash_log_rotates,
    test_exit_code_aggregation_across_sources,
    test_target_lock_blocks_concurrent_run,
    test_target_lock_stale_auto_removed,
    test_target_lock_released_after_run,
    test_target_confirmation_unit,
    test_drive_root_target_confirmation_unit,
    test_bare_drive_letter_normalization_and_source_target_conflict,
    test_bare_launch_helpers_unit,
    test_bare_launch_menu_argv_gate_and_flow,
    test_tmp_extract_wipe_protection,
    test_negative_free_space_margin_and_numeric_config_validation,
    test_disk_full_graceful_stop,
    test_place_failure_does_not_crash_run,
    test_symlink_loop_protection,
    test_deep_directory_tree_does_not_raise_recursion_error,
    test_archive_entry_count_bomb,
    test_archive_deleted_mid_scan_does_not_crash,
    test_archive_path_traversal_rejected,
    test_archive_symlink_rejected,
    test_archive_entry_count_mismatch_rejected,
    test_relative_path_rejected,
    test_third_party_licenses_wired_up,
    test_requirements_txt_pinned_and_wired_up,
]


def main():
    # 2026-07-17 (ревизорская сессия, раунд 1): ALL_TESTS -- обычный литерал, который
    # правится вручную. Комментарий над ним утверждает, что забытая функция теста -- теперь
    # только опечатка в ОДНОМ месте, но без этой проверки её всё ещё можно было бы просто не
    # заметить: CI остался бы зелёным, а новый def test_...() никогда бы не исполнился, без
    # единого сигнала об этом. Сверяем целиком, а не полагаемся на визуальную аккуратность.
    _defined = {name for name, obj in globals().items()
                if name.startswith("test_") and callable(obj)}
    _listed = {t.__name__ for t in ALL_TESTS}
    _missing = _defined - _listed
    assert not _missing, (
        f"test function(s) defined but missing from ALL_TESTS: {sorted(_missing)}")

    if os.path.isdir(WORK):
        shutil.rmtree(WORK)
    os.makedirs(WORK, exist_ok=True)

    pattern = sys.argv[1] if len(sys.argv) > 1 else ""
    ran = 0
    for t in ALL_TESTS:
        if pattern and pattern not in t.__name__:
            continue
        print(f"\n--- {t.__name__} ---")
        t()
        ran += 1

    if pattern and ran == 0:
        sys.exit(f"No test function name contains {pattern!r} -- check the spelling "
                  f"(matches against t.__name__ in ALL_TESTS).")

    print("\n" + "=" * 60)
    if FAILURES:
        print(f"{len(FAILURES)} CHECK(S) FAILED:")
        for f in FAILURES:
            print(f"  - {f}")
        sys.exit(1)
    print(f"ALL CHECKS PASSED ({ran}/{len(ALL_TESTS)} test function(s) ran)")


if __name__ == "__main__":
    main()
