"""sha256_file_with_retry() fail-fast on whole-volume loss (SESSION-HANDOFF.txt, Сценарий 3:
SOURCE disconnects physically while files remain to be hashed). Before this fix, every
remaining file paid the full retries*delay cost even though the drive itself was gone and
every attempt was guaranteed to fail -- with thousands of files already listed in the current
directory, that adds up to hours of pointless waiting. _volume_likely_gone() lets the retry
loop recognize "the whole drive vanished" and stop sleeping between attempts for it."""
import pytest

import photosort_win as m


class TestVolumeLikelyGone:
    def test_existing_drive_is_not_gone(self, tmp_path):
        assert m._volume_likely_gone(str(tmp_path / "some_file.jpg")) is False

    def test_missing_drive_letter_is_gone(self, monkeypatch):
        monkeypatch.setattr(m.os.path, "isdir", lambda p: False)
        assert m._volume_likely_gone(r"Z:\photos\a.jpg") is True


class TestSha256FileWithRetryFailFast:
    def test_transient_error_still_sleeps_between_retries(self, tmp_path, monkeypatch):
        # regression guard: a single locked/corrupt file (drive otherwise fine) must keep
        # retrying with the normal delay -- only whole-volume loss should skip the wait.
        sleeps = []
        monkeypatch.setattr(m.time, "sleep", lambda s: sleeps.append(s))
        monkeypatch.setattr(m, "_volume_likely_gone", lambda p: False)
        monkeypatch.setattr(m, "sha256_file", lambda p: (_ for _ in ()).throw(OSError("locked")))

        with pytest.raises(m.ReadError):
            m.sha256_file_with_retry(str(tmp_path / "a.jpg"), retries=3, delay=5.0)

        assert sleeps == [5.0, 5.0]

    def test_volume_gone_skips_remaining_sleeps(self, tmp_path, monkeypatch):
        sleeps = []
        monkeypatch.setattr(m.time, "sleep", lambda s: sleeps.append(s))
        monkeypatch.setattr(m, "_volume_likely_gone", lambda p: True)
        monkeypatch.setattr(m, "sha256_file", lambda p: (_ for _ in ()).throw(OSError("device removed")))

        with pytest.raises(m.ReadError):
            m.sha256_file_with_retry(str(tmp_path / "a.jpg"), retries=3, delay=5.0)

        assert sleeps == []

    def test_success_on_first_attempt_never_probes_volume(self, tmp_path, monkeypatch):
        probed = []
        monkeypatch.setattr(m, "_volume_likely_gone", lambda p: (probed.append(p), False)[1])
        monkeypatch.setattr(m, "sha256_file", lambda p: "deadbeef")

        result = m.sha256_file_with_retry(str(tmp_path / "a.jpg"), retries=3, delay=5.0)

        assert result == "deadbeef"
        assert probed == []
