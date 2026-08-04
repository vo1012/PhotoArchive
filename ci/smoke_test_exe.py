"""Smoke-test the frozen dist/PhotoArchive.exe (used only by the `build` CI job): confirms the
PyInstaller-packaged binary actually launches and processes a real file end-to-end, catching
frozen-import risks (reverse_geocoder/pillow_heif data files, missing --add-binary) that a
plain `python photosort_win.py` run in the `test` job cannot catch. Also exercises the new
argparse subparsers (archive is implicit / analyze explicit) and RAW_LAYOUT=sibling
through the actual frozen binary -- these previously only ran via `python photosort_win.py`
in ci/windows_ci_test.py, never through PyInstaller's frozen entry point.
"""
import os
import random
import subprocess
import sys

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXE = os.path.join(ROOT, "build", "dist", "PhotoArchive.exe")
DIST_DIR = os.path.dirname(EXE)  # WORKDIR for a frozen exe -- photoarchive_config.yaml/work.db live here
WORK = os.path.join(os.environ.get("RUNNER_TEMP", ROOT), "exe_smoke")

FAILURES = []


def check(cond, label):
    if cond:
        print(f"  PASS: {label}")
    else:
        print(f"  FAIL: {label}")
        FAILURES.append(label)


def make_photo(path, w=1200, h=900, dt="2019:07:15 12:00:00", seed=1):
    from PIL import Image
    os.makedirs(os.path.dirname(path), exist_ok=True)
    im = Image.new("RGB", (w, h))
    px = im.load()
    random.seed(seed)
    for x in range(w):
        for y in range(h):
            px[x, y] = (random.randint(0, 255), random.randint(0, 255), random.randint(0, 255))
    im.save(path, "JPEG", quality=90)
    subprocess.run(
        ["exiftool", "-overwrite_original", f"-DateTimeOriginal={dt}",
         "-Make=Canon", "-Model=Canon EOS 80D", path],
        check=True, capture_output=True,
    )


def set_config(content):
    cfg = os.path.join(DIST_DIR, "photoarchive_config.yaml")
    if content is None:
        if os.path.exists(cfg):
            os.remove(cfg)
        return
    with open(cfg, "w", encoding="utf-8") as f:
        f.write(content)


def run_exe(args):
    result = subprocess.run([EXE] + args, capture_output=True, text=True,
                             encoding="utf-8", errors="replace")
    print(result.stdout[-3000:])
    if result.returncode != 0:
        print(result.stderr[-3000:])
    return result


def test_basic_archive():
    print("\n=== frozen exe: basic archive build (implicit 'archive' subcommand) ===")
    src = os.path.join(WORK, "src")
    tgt = os.path.join(WORK, "target")
    make_photo(os.path.join(src, "photo.jpg"))

    r = run_exe(["--source", src, "--target", tgt])
    check(r.returncode == 0, "PhotoArchive.exe exits 0 (no explicit subcommand, backward-compat)")
    dest = os.path.join(tgt, "ByDate", "2019", "2019-07 [PhotoArchive]", "photo.jpg")
    check(os.path.isfile(dest), "photo archived end-to-end by the frozen exe")


def test_analyze():
    # 2026-08-04: CLI subcommand renamed analyze-quick -> analyze (see build_arg_parser());
    # internal AnalyzeStats.mode value is still "analyze-quick" (see _CLI_ANALYZE_MODE_MAP).
    # Same day, later: "analyze" no longer accepts --source and --target together (mutually
    # exclusive, --target now means "check this already-built archive instead", see
    # test_analyze_target() below) -- only --source here, or this would hit the new
    # "не оба сразу" config error instead of testing the quick-diagnostic path.
    print("\n=== frozen exe: analyze --source subcommand ===")
    src = os.path.join(WORK, "src_analyze")
    make_photo(os.path.join(src, "photo2.jpg"), dt="2020:01:01 10:00:00", seed=2)

    r = run_exe(["analyze", "--source", src])
    check(r.returncode == 0, "PhotoArchive.exe analyze --source exits 0")
    report = os.path.join(DIST_DIR, "analyze_report.csv")
    check(os.path.isfile(report), "analyze_report.csv created next to the frozen exe")
    if os.path.exists(report):
        os.remove(report)


def test_analyze_target():
    # 2026-08-04, Раунд 64 [ЗАМЕЧАНИЕ]: "analyze --target" (self-scan an already-built
    # archive, formerly its own "analyze-passport" subcommand, merged into "analyze" the same
    # day) had zero coverage through the actual frozen .exe -- only through the plain
    # interpreter in ci/windows_ci_test.py, which structurally cannot catch PyInstaller-
    # specific breakage (frozen sys.argv/_MEIPASS paths, missing --collect-data/--add-binary).
    print("\n=== frozen exe: analyze --target subcommand (self-scan / passport) ===")
    src = os.path.join(WORK, "src_analyze_target")
    tgt = os.path.join(WORK, "target_analyze_target")
    make_photo(os.path.join(src, "photo3.jpg"), dt="2022:04:04 10:00:00", seed=4)

    r = run_exe(["--source", src, "--target", tgt])
    check(r.returncode == 0, "PhotoArchive.exe (real archive build for analyze --target) exits 0")

    report_path = os.path.join(tgt, "__служебные_файлы", "passport.html")
    if os.path.isfile(report_path):
        os.remove(report_path)
    r2 = run_exe(["analyze", "--target", tgt])
    check(r2.returncode == 0, "PhotoArchive.exe analyze --target exits 0")
    check(os.path.isfile(report_path),
          "passport.html written to TARGET\\__служебные_файлы\\ by the frozen exe")


def test_raw_layout_sibling():
    print("\n=== frozen exe: RAW_LAYOUT=sibling via photoarchive_config.yaml ===")
    src = os.path.join(WORK, "src_raw")
    tgt = os.path.join(WORK, "target_raw")
    jpg = os.path.join(src, "Album", "IMG_0001.JPG")
    make_photo(jpg, dt="2021:03:03 08:00:00", seed=3)
    with open(os.path.join(src, "Album", "IMG_0001.CR2"), "wb") as f:
        f.write(b"FAKE-CR2" + os.urandom(256))

    set_config("raw_layout: sibling\n")
    try:
        r = run_exe(["--source", src, "--target", tgt])
    finally:
        set_config(None)
    check(r.returncode == 0, "PhotoArchive.exe (raw_layout=sibling) exits 0")
    check(os.path.isfile(os.path.join(tgt, "Albums", "Album", "RAW", "IMG_0001.CR2")),
          "RAW placed next to its JPEG under the frozen exe (Albums/Album/RAW/)")


ALL_TESTS = [
    test_basic_archive,
    test_analyze,
    test_analyze_target,
    test_raw_layout_sibling,
]


def main():
    # 2026-08-04, Раунд 64 [ЗАМЕЧАНИЕ]: тот же assert-страж, что в ci/windows_ci_test.py
    # (ревизорская сессия, Раунд 1) -- этот файл раньше вызывал тесты по одному в main() без
    # такого списка вообще, забытый вызов не дал бы никакого сигнала (именно так один раз уже
    # не хватало покрытия у analyze --target, см. Раунд 64).
    _defined = {name for name, obj in globals().items()
                if name.startswith("test_") and callable(obj)}
    _listed = {t.__name__ for t in ALL_TESTS}
    _missing = _defined - _listed
    assert not _missing, (
        f"test function(s) defined but missing from ALL_TESTS: {sorted(_missing)}")

    if not os.path.isfile(EXE):
        print(f"FAIL: {EXE} does not exist")
        sys.exit(1)

    for test in ALL_TESTS:
        test()

    for junk in ("work.db", "photoarchive_config.yaml", "analyze_report.csv"):
        p = os.path.join(DIST_DIR, junk)
        if os.path.exists(p):
            os.remove(p)

    print("\n" + "=" * 60)
    if FAILURES:
        print(f"{len(FAILURES)} CHECK(S) FAILED:")
        for f in FAILURES:
            print(f"  - {f}")
        sys.exit(1)
    print("PASS: PhotoArchive.exe built, launched, and exercised end-to-end "
          "(archive default + analyze --source + analyze --target + raw_layout=sibling)")


if __name__ == "__main__":
    main()
