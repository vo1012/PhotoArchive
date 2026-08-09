"""2026-08-07, боевой прогон пользователя -- домашнее видео на DVD (папка VIDEO_TS) не
попадало в архив вообще (.vob/.ifo/.bup не распознавались как медиа). По итогам разговора с
пользователем: VIDEO_TS -- одна неделимая единица, копируется в Albums/<имя>/VIDEO_TS/ целиком
или не копируется вовсе ("объединение DVD-папок недопустимо" -- ничего не дописывается в уже
скопированный юнит на повторных прогонах).

tests/test_progress_phase2.py уже покрывает SourceWalker.walk()-уровень (детекция/fingerprint/
yield SourceItem с dvd_dest_path) на синтетических деревьях без реального копирования. Этот
файл -- сквозной тест через m.run() (реальную сборку), проверяет, что байты физически попадают
в TARGET и что повторный прогон того же источника не дублирует уже архивированный диск."""
import photosort_win as m


def _run(cfg):
    return m.run(cfg, log=lambda *a, **k: None)


def test_dvd_video_ts_copied_whole_into_albums(tmp_path):
    source = tmp_path / "source"
    disc = source / "Отпуск_2005"
    (disc / "VIDEO_TS").mkdir(parents=True)
    (disc / "VIDEO_TS" / "VTS_01_0.VOB").write_bytes(b"v" * 500)
    (disc / "VIDEO_TS" / "VIDEO_TS.IFO").write_bytes(b"i" * 50)
    target = tmp_path / "target"
    target.mkdir()
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    cfg = m.Config(source=str(source), target=str(target), dry_run=False, sample_limit=0,
                    workdir=str(workdir))

    stats, *_rest = _run(cfg)

    dest = target / "Albums" / "Отпуск_2005" / "VIDEO_TS"
    assert (dest / "VTS_01_0.VOB").read_bytes() == b"v" * 500
    assert (dest / "VIDEO_TS.IFO").read_bytes() == b"i" * 50  # "как есть" -- .ifo тоже копируется
    assert stats["dvd_units_copied"][0]["name"] == "Отпуск_2005"
    assert stats["dvd_units_copied"][0]["n_files"] == 2
    assert stats["appended_videos"] == 2  # считается в общем "новых файлов", см. _process_dvd_item()


def test_dvd_video_ts_rerun_recognizes_duplicate_not_reappended(tmp_path):
    """Прямое требование пользователя: "объединение DVD-папок недопустимо" -- второй прогон
    ТОГО ЖЕ источника на тот же TARGET не должен ни дописать что-то в уже скопированную папку,
    ни завести рядом вторую копию под другим именем."""
    source = tmp_path / "source"
    disc = source / "Disc1"
    (disc / "VIDEO_TS").mkdir(parents=True)
    (disc / "VIDEO_TS" / "VTS_01_0.VOB").write_bytes(b"v" * 200)
    target = tmp_path / "target"
    target.mkdir()
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    cfg = m.Config(source=str(source), target=str(target), dry_run=False, sample_limit=0,
                    workdir=str(workdir))

    stats1, *_ = _run(cfg)
    assert stats1["dvd_units_copied"][0]["name"] == "Disc1"

    stats2, *_ = _run(cfg)
    assert stats2["dvd_units_copied"] == []
    assert stats2["dvd_units_skipped_duplicate"][0]["name"] == "Disc1"
    # No second "Disc1 (2)" folder, no extra files appended into the existing one.
    albums = list((target / "Albums").iterdir())
    assert [p.name for p in albums] == ["Disc1"]
    vob_files = list((target / "Albums" / "Disc1" / "VIDEO_TS").iterdir())
    assert len(vob_files) == 1


def test_two_different_discs_same_album_name_get_suffixed_not_merged(tmp_path):
    """Реалистичный коллизионный сценарий, требующий ДВУХ отдельных прогонов (одна и та же
    SOURCE-папка "Video" на верхнем уровне не может содержать две РАЗНЫЕ VIDEO_TS
    одновременно) -- второй визит с другим физическим диском, той же папкой-алиасом "Video".
    find_album() отдаёт один и тот же альбом "Video" оба раза -- коллизия ловится на уровне
    подпапки VIDEO_TS ВНУТРИ общего альбома ("(2)" у второй), содержимое НЕ объединяется --
    прямое требование пользователя ("объединение DVD-папок недопустимо")."""
    target = tmp_path / "target"
    target.mkdir()
    workdir = tmp_path / "workdir"
    workdir.mkdir()

    source1 = tmp_path / "source1"
    (source1 / "Video" / "VIDEO_TS").mkdir(parents=True)
    (source1 / "Video" / "VIDEO_TS" / "VTS_01_0.VOB").write_bytes(b"a" * 100)
    cfg1 = m.Config(source=str(source1), target=str(target), dry_run=False, sample_limit=0,
                     workdir=str(workdir))
    _run(cfg1)

    source2 = tmp_path / "source2"
    (source2 / "Video" / "VIDEO_TS").mkdir(parents=True)
    (source2 / "Video" / "VIDEO_TS" / "VTS_01_0.VOB").write_bytes(b"b" * 100)  # different disc
    cfg2 = m.Config(source=str(source2), target=str(target), dry_run=False, sample_limit=0,
                     workdir=str(workdir))
    stats2, *_ = _run(cfg2)

    assert stats2["dvd_units_copied"][0]["dest_path"] == str(target / "Albums" / "Video" / "VIDEO_TS (2)")
    albums = sorted(p.name for p in (target / "Albums").iterdir())
    assert albums == ["Video"]  # one shared album folder, not two
    contents = {
        (target / "Albums" / "Video" / "VIDEO_TS" / "VTS_01_0.VOB").read_bytes(),
        (target / "Albums" / "Video" / "VIDEO_TS (2)" / "VTS_01_0.VOB").read_bytes(),
    }
    assert contents == {b"a" * 100, b"b" * 100}  # both discs' real content preserved, not overwritten


def test_dvd_video_ts_inside_tilde_dump_folder_goes_to_bydate_whole(tmp_path):
    """2026-08-07, прямой вопрос пользователя: "что будет, если VIDEO_TS папка расположена
    внутри ~Имя папки?" -- "~" (FORCE_DUMP_PREFIX) -- явный сигнал пользователя "это не
    альбом, сортируй по дате" (см. RULES.md), тот же сигнал find_album() применяет к обычному
    файлу на этом месте. VIDEO_TS должна восприниматься "по аналогии с одним файлом" -- целиком
    уходит в ByDate (не в Albums), но ОДНИМ куском (все файлы юнита в одной и той же корзине),
    не рассыпаясь по отдельным файлам внутри."""
    source = tmp_path / "source"
    disc = source / "~Яндекс_Диск" / "VIDEO_TS"
    disc.mkdir(parents=True)
    (disc / "VTS_01_0.VOB").write_bytes(b"v" * 200)
    (disc / "VIDEO_TS.IFO").write_bytes(b"i" * 20)
    target = tmp_path / "target"
    target.mkdir()
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    cfg = m.Config(source=str(source), target=str(target), dry_run=False, sample_limit=0,
                    workdir=str(workdir))

    stats, *_ = _run(cfg)

    assert not (target / "Albums").exists() or not any((target / "Albums").iterdir())
    bydate_vobs = list((target / "ByDate").rglob("VTS_01_0.VOB"))
    assert len(bydate_vobs) == 1
    video_ts_dir = bydate_vobs[0].parent
    assert video_ts_dir.name == "VIDEO_TS"
    assert (video_ts_dir / "VIDEO_TS.IFO").exists()  # same folder -- unit stayed whole, not scattered
    assert stats["dvd_units_copied"][0]["n_files"] == 2


def test_partial_write_failure_inside_unit_does_not_register_as_archived(tmp_path, monkeypatch):
    """REVIEW-HANDOFF.md, Раунд 71, БЛОКЕР: если из N файлов юнита физически скопировалась
    только часть (антивирус/плохой сектор/место кончилось на середине), юнит НЕ должен
    попасть ни в dvd_units_copied (отчёт "скопировано целиком"), ни в реестр dvd_units в БД
    -- иначе следующий прогон видит fingerprint как уже архивированный и пропускает
    недостающие файлы навсегда ("тихая необратимая потеря данных", живой репро ревизора).

    Раунд 72 ревью, ЗАМЕЧАНИЕ (побочный эффект фикса Раунда 71): недокопированная папка
    должна быть удалена целиком, не оставлена сиротой на диске -- иначе она физически
    занимает имя "VIDEO_TS", и второй прогон, увидев её через os.path.isdir(), решает, что
    это КОЛЛИЗИЯ С ДРУГИМ диском (не мой же остаток) и заводит рядом "VIDEO_TS (2)",
    копируя всё заново, а обрезанный огрызок первого прогона так и остаётся лежать вечно,
    неотличимый на вид от второго настоящего диска."""
    source = tmp_path / "source"
    disc = source / "Disc1"
    (disc / "VIDEO_TS").mkdir(parents=True)
    (disc / "VIDEO_TS" / "VTS_01_0.VOB").write_bytes(b"v" * 200)
    (disc / "VIDEO_TS" / "VTS_02_0.VOB").write_bytes(b"w" * 200)
    target = tmp_path / "target"
    target.mkdir()
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    cfg = m.Config(source=str(source), target=str(target), dry_run=False, sample_limit=0,
                    workdir=str(workdir))

    real_place_file = m.place_file
    calls = []

    def flaky_place_file(item, dest_path, *a, **k):
        calls.append(dest_path)
        if len(calls) == 2:
            raise OSError("simulated antivirus lock / bad sector on 2nd file")
        return real_place_file(item, dest_path, *a, **k)

    monkeypatch.setattr(m, "place_file", flaky_place_file)

    stats1, *_ = _run(cfg)

    # Not counted as a completed unit -- report/DB must not claim "copied whole".
    assert stats1["dvd_units_copied"] == []
    # The incomplete unit folder is removed entirely -- best-effort cleanup, symmetric to how
    # a regular file that never made it into the pool "self-heals" on the next run.
    assert not (target / "Albums" / "Disc1" / "VIDEO_TS").exists()
    conn = m._open_archive_cache_conn(str(target))
    rows = conn.execute("SELECT fingerprint FROM dvd_units").fetchall()
    conn.close()
    assert rows == []  # unit must NOT be in the persistent "already archived" registry

    monkeypatch.setattr(m, "place_file", real_place_file)
    stats2, *_ = _run(cfg)

    # Second run (no more flakiness): walker doesn't know about the unit yet (not
    # registered) -- it must retry it as new and this time complete it whole, not treat
    # the partially-copied disc as an already-archived duplicate to skip.
    assert stats2["dvd_units_skipped_duplicate"] == []
    assert stats2["dvd_units_copied"][0]["n_files"] == 2
    all_bytes = {p.read_bytes() for p in target.rglob("VTS_*.VOB")}
    assert all_bytes == {b"v" * 200, b"w" * 200}  # both files' real content present somewhere
    # Cleanup after run1 freed up the "VIDEO_TS" name -- run2 reuses it directly, no orphaned
    # "VIDEO_TS" sitting next to a "VIDEO_TS (2)" that would look like two different discs.
    albums = list((target / "Albums" / "Disc1").iterdir())
    assert [p.name for p in albums] == ["VIDEO_TS"]


def test_sample_limit_truncated_unit_is_not_cleaned_up(tmp_path):
    """REVIEW-HANDOFF.md, Раунд 73, ЗАМЕЧАНИЕ: --sample-limit -- документированный флаг,
    доступный и для настоящей (не dry-run) сборки -- может оборвать основной цикл ровно
    серединой DVD-юнита, БЕЗ единой ошибки (обычный break по достижении лимита, не сбой). Файл,
    успевший реально долететь до place_file() ДО обрыва, не должен удаляться "уборкой"
    недокопированных юнитов (Раунд 72 фикс) -- тот же принцип "оставить как есть", что и у
    обычных (не-DVD) файлов под тем же --sample-limit."""
    source = tmp_path / "source"
    disc = source / "Disc1"
    (disc / "VIDEO_TS").mkdir(parents=True)
    (disc / "VIDEO_TS" / "VTS_01_0.VOB").write_bytes(b"v" * 200)
    (disc / "VIDEO_TS" / "VTS_02_0.VOB").write_bytes(b"w" * 200)
    target = tmp_path / "target"
    target.mkdir()
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    cfg = m.Config(source=str(source), target=str(target), dry_run=False, sample_limit=1,
                    workdir=str(workdir))

    stats, *_ = _run(cfg)

    # Not counted as a completed unit -- genuinely incomplete, must not be registered as
    # "fully archived" (same reasoning as a real failure -- see Round 71 blocker fix).
    assert stats["dvd_units_copied"] == []
    # But the one file that DID make it to place_file() without any error must survive --
    # this is not a failure, cleanup must not touch it.
    vob_names = {p.name for p in (target / "Albums" / "Disc1" / "VIDEO_TS").iterdir()}
    assert vob_names == {"VTS_01_0.VOB"}
    assert (target / "Albums" / "Disc1" / "VIDEO_TS" / "VTS_01_0.VOB").read_bytes() == b"v" * 200


def test_dvd_video_ts_duplicate_within_same_run_not_recopied(tmp_path):
    """2026-08-08, живой боевой прогон F:->D: -- два физически идентичных VIDEO_TS в РАЗНЫХ
    папках SOURCE (тот же диск, скопированный пользователем в двух местах) обнаружены в ОДНОМ
    прогоне -- registry с прошлых прогонов пуст (fingerprint нигде не записан), но второй юнит
    всё равно обязан быть распознан как дубль ПЕРВОГО, встреченного в этом же прогоне, а не
    скопирован повторно (~ вдвое впустую занятое место, реальная находка боевого прогона)."""
    source = tmp_path / "source"
    (source / "a" / "VIDEO_TS").mkdir(parents=True)
    (source / "a" / "VIDEO_TS" / "VTS_01_0.VOB").write_bytes(b"v" * 200)
    (source / "b" / "VIDEO_TS").mkdir(parents=True)
    (source / "b" / "VIDEO_TS" / "VTS_01_0.VOB").write_bytes(b"v" * 200)  # byte-identical disc
    target = tmp_path / "target"
    target.mkdir()
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    cfg = m.Config(source=str(source), target=str(target), dry_run=False, sample_limit=0,
                    workdir=str(workdir))

    stats, *_ = _run(cfg)

    assert len(stats["dvd_units_copied"]) == 1
    assert stats["dvd_units_skipped_duplicate"][0]["name"] in ("a", "b")
    albums = list((target / "Albums").iterdir())
    assert len(albums) == 1  # only one VIDEO_TS folder physically written, not two


def test_dry_run_does_not_copy_dvd_files(tmp_path):
    source = tmp_path / "source"
    disc = source / "Disc1"
    (disc / "VIDEO_TS").mkdir(parents=True)
    (disc / "VIDEO_TS" / "VTS_01_0.VOB").write_bytes(b"v" * 100)
    target = tmp_path / "target"
    target.mkdir()
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    cfg = m.Config(source=str(source), target=str(target), dry_run=True, sample_limit=0,
                    workdir=str(workdir))

    stats, *_ = _run(cfg)

    # ensure_target_layout() создаёт Albums/ безусловно как обычный служебный скелет TARGET
    # (даже в dry_run, для любой сборки, не только DVD) -- проверяем не отсутствие самой
    # папки, а что реального содержимого DVD-диска в ней нет.
    assert not (target / "Albums" / "Disc1").exists()
    assert stats["dvd_units_copied"][0]["name"] == "Disc1"  # решение "новый" всё равно посчитано (для превью)
