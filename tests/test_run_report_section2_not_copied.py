"""PROMPT_report_run_redesign.md, Промпт 2/3 (2026-08-14): Раздел 2 "Что не скопировано"
сводного прогонного отчёта -- _render_run_not_copied() и его подключение через
_generate_from_model()."""
import report as r


def _unreadable_row(source, ts="2026-01-01 00:00:01"):
    return {"timestamp": ts, "source": source, "error": "bad"}


class TestRenderRunNotCopiedEmptyState:
    def test_no_facts_renders_nothing_at_all(self):
        assert r._render_run_not_copied({}, {}, "target") == ""

    def test_stopped_for_space_alone_does_not_trigger_section(self):
        """2.5: stopped_for_space -- состояние прогона (уже показано в шапке), не категория
        "не скопировано" -- само по себе не должно включать раздел."""
        html_out = r._render_run_not_copied({"stopped_for_space": True}, {}, "target")
        assert html_out == ""


class TestRenderRunNotCopiedUnreadable:
    def test_small_count_no_pattern_line(self):
        html_out = r._render_run_not_copied({"unreadable_count": 3}, {}, "target")
        assert "<h2>Что не скопировано</h2>" in html_out
        assert "3</b> файла не прочитано" in html_out
        assert "Так много обычно значит" not in html_out

    def test_large_count_shows_dominant_pattern_line(self):
        rows = [_unreadable_row(rf"D:\Крым\photo{i}.CR2") for i in range(25)]
        checklist_new = {"unreadable": rows}
        html_out = r._render_run_not_copied({"unreadable_count": 25}, checklist_new, "target")
        assert "25 из 25 — файлы .CR2 из папки" in html_out
        assert "Так много обычно значит" in html_out

    def test_wording_same_regardless_of_level_read_phase_is_real_either_way(self):
        target = r._render_run_not_copied({"unreadable_count": 3}, {}, "target")
        preview = r._render_run_not_copied({"unreadable_count": 3}, {}, "workdir")
        assert "3</b> файла не прочитано" in target
        assert "3</b> файла не прочитано" in preview


class TestRenderRunNotCopiedEncryptedArchives:
    def test_few_paths_shown_inline(self):
        run_stats = {"encrypted_archives": [r"D:\Фото\secret.zip"]}
        html_out = r._render_run_not_copied(run_stats, {}, "target")
        assert "1</b> запароленный архив" in html_out
        assert "распакуйте их вручную" in html_out
        assert "secret.zip" in html_out

    def test_many_paths_not_shown_inline(self):
        run_stats = {"encrypted_archives": [rf"D:\a{i}.zip" for i in range(10)]}
        html_out = r._render_run_not_copied(run_stats, {}, "target")
        assert "10</b> запароленных архивов" in html_out
        assert "a0.zip" not in html_out

    def test_plural_forms(self):
        two = r._render_run_not_copied({"encrypted_archives": ["a.zip", "b.zip"]}, {}, "target")
        assert "2</b> запароленных архива " in two
        five_plus = r._render_run_not_copied(
            {"encrypted_archives": [f"{i}.zip" for i in range(6)]}, {}, "target")
        assert "6</b> запароленных архивов" in five_plus


class TestRenderRunNotCopiedDuplicates:
    def test_shown_calmly_at_the_bottom(self):
        html_out = r._render_run_not_copied({"skipped_present": 7}, {}, "target")
        assert "7 файлов" in html_out
        assert "уже в архиве, повторно не копировались" in html_out
        assert "class=\"muted\"" in html_out

    def test_preview_wording_differs(self):
        html_out = r._render_run_not_copied({"skipped_present": 7}, {}, "workdir")
        assert "повторно копироваться не будут" in html_out

    def test_bytes_saved_shown_when_nonzero(self):
        html_out = r._render_run_not_copied(
            {"skipped_present": 7, "bytes_saved_by_dedup": 3 * 1024**3}, {}, "target")
        assert "сэкономлено 3.0 ГБ" in html_out

    def test_bytes_saved_omitted_when_zero(self):
        html_out = r._render_run_not_copied({"skipped_present": 7}, {}, "target")
        assert "сэкономлено" not in html_out


class TestRenderRunNotCopiedOrder:
    def test_order_is_unreadable_then_encrypted_then_duplicates_regardless_of_size(self):
        """Инвариант промпта: порядок ПО ДЕЙСТВИЮ, не по величине -- дублей здесь заведомо
        больше всех, но они должны остаться внизу."""
        run_stats = {
            "unreadable_count": 2,
            "encrypted_archives": ["a.zip"],
            "skipped_present": 9000,
        }
        html_out = r._render_run_not_copied(run_stats, {}, "target")
        idx_unreadable = html_out.index("не прочитано")
        idx_encrypted = html_out.index("запароленный архив")
        idx_dup = html_out.index("уже в архиве")
        assert idx_unreadable < idx_encrypted < idx_dup


class TestGenerateReportWiresSectionTwo:
    def test_target_report_contains_section_two(self, tmp_path):
        data = {"appended": [], "skipped": [
            {"timestamp": "2026-02-01 00:00:01", "source": "s",
             "matched_with": r"C:\T\dst\ByDate\2020\a.jpg", "reason": "already_present"},
        ]}
        out_path = tmp_path / "report.html"
        r.generate_report(data, str(out_path), level="target",
                           run_start="2026-02-01 00:00:00",
                           run_stats={"skipped_present": 1})
        html_out = out_path.read_text(encoding="utf-8")
        assert "<h2>Что не скопировано</h2>" in html_out

    def test_cli_dry_run_without_full_workdir_still_has_section_two(self, tmp_path):
        """2026-08-14, прямая просьба пользователя -- унификация dry-run/реального прогона
        (см. одноимённый тест в tests/test_run_report_section1_copied.py). Параметр
        `full_workdir` убран из report.py целиком (REVIEW-HANDOFF.md, Раунд 92)."""
        data = {"appended": [{"timestamp": "2026-02-01 00:00:01", "source": "s",
                               "dest": r"C:\T\dst\ByDate\2020\a.jpg",
                               "reason": "appended_new", "flags": ""}]}
        out_path = tmp_path / "report.html"
        r.generate_report(data, str(out_path), level="workdir",
                           run_start="2026-02-01 00:00:00",
                           run_stats={"skipped_present": 1})
        html_out = out_path.read_text(encoding="utf-8")
        assert "<h2>Что не скопировано</h2>" in html_out
