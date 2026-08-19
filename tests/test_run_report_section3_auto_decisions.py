"""PROMPT_report_run_redesign.md, Промпт 3/3 (2026-08-14): Раздел 3 "Что программа решила
сама" сводного прогонного отчёта -- _render_run_auto_decisions(), renamed_count в
_build_checklist_fields()/_source_basename(), и подключение через _generate_from_model()."""
import report as r


class TestSourceBasename:
    def test_plain_windows_path(self):
        assert r._source_basename(r"C:\T\dst\a.jpg") == "a.jpg"

    def test_archive_origin_display_uses_forward_slash(self):
        assert r._source_basename("Foto.zip → sub/photo.jpg") == "photo.jpg"

    def test_empty_string(self):
        assert r._source_basename("") == ""


class TestRenamedCount:
    def test_same_basename_not_counted(self):
        fields = r._build_checklist_fields({"appended": [
            {"source": r"D:\Src\a.jpg", "dest": r"C:\T\dst\ByDate\2020\a.jpg"},
        ]})
        assert fields["renamed_count"] == 0

    def test_collision_suffix_counted(self):
        fields = r._build_checklist_fields({"appended": [
            {"source": r"D:\Src\a.jpg", "dest": r"C:\T\dst\ByDate\2020\a_1.jpg"},
        ]})
        assert fields["renamed_count"] == 1

    def test_archive_source_with_forward_slash_not_falsely_counted(self):
        """Регресс: origin_display из архива использует "/" -- без _source_basename() ЛЮБОЙ
        файл из архива считался бы "переименованным" (сравнение целой строки с именем)."""
        fields = r._build_checklist_fields({"appended": [
            {"source": "Foto.zip → sub/a.jpg", "dest": r"C:\T\dst\ByDate\2020\a.jpg"},
        ]})
        assert fields["renamed_count"] == 0

    def test_missing_source_or_dest_skipped(self):
        fields = r._build_checklist_fields({"appended": [
            {"source": "", "dest": r"C:\T\dst\ByDate\2020\a.jpg"},
            {"source": r"D:\Src\b.jpg", "dest": ""},
        ]})
        assert fields["renamed_count"] == 0


def _dates_review_row(tier, dest="x", ts="2026-02-01 00:00:01"):
    return {"tier": tier, "dest": dest, "source": "x", "timestamp": ts}


class TestRenderRunAutoDecisions:
    def test_empty_state_renders_nothing(self):
        assert r._render_run_auto_decisions({}, "target") == ""

    def test_tier_b_and_c_shown_separately_not_merged(self):
        checklist_new = {"date_issues_b_total": 3, "date_issues_c_total": 5}
        html_out = r._render_run_auto_decisions(checklist_new, "target")
        assert "<h2>Что программа решила сама</h2>" in html_out
        assert "<b>3</b> файла — дата определена с высокой уверенностью" in html_out
        assert "<b>5</b> файлов — дата определена приблизительно" in html_out

    def test_tier_d_undated(self):
        checklist_new = {"date_issues_d_total": 7}
        html_out = r._render_run_auto_decisions(checklist_new, "target")
        assert "<b>7</b> файлов — без надёжной даты" in html_out
        assert "0000-undated" in html_out

    def test_near_dup_series_count_and_file_sum(self):
        checklist_new = {"near_dup_clusters": [["a", "b"], ["c", "d", "e"]]}
        html_out = r._render_run_auto_decisions(checklist_new, "target")
        assert "<b>2</b> серии похожих кадров" in html_out
        assert "5 файлов" in html_out
        assert "сохранены рядом" in html_out

    def test_near_dup_series_preview_wording(self):
        """Одна серия -- единственное число, женский род ("серия будет сохранена", не
        "сохранены") -- регресс-проверка на _saved_verb()."""
        checklist_new = {"near_dup_clusters": [["a", "b"]]}
        html_out = r._render_run_auto_decisions(checklist_new, "workdir")
        assert "будет сохранена рядом" in html_out

    def test_near_dup_series_preview_wording_plural(self):
        checklist_new = {"near_dup_clusters": [["a", "b"], ["c", "d"]]}
        html_out = r._render_run_auto_decisions(checklist_new, "workdir")
        assert "будут сохранены рядом" in html_out

    def test_disputes_shown_here_not_section_two(self):
        checklist_new = {"disputes_total": 4}
        html_out = r._render_run_auto_decisions(checklist_new, "target")
        assert "<b>4</b> файла сохранены в папке" in html_out
        assert "_Unsorted" in html_out
        assert "их к альбому/дате" in html_out

    def test_disputes_singular_pronoun_and_verb(self):
        """1 файл -- единственное число: "сохранён" (не "сохранены"), местоимение "его" (не
        "их") -- регресс-проверка на _saved_verb()/согласование местоимения."""
        checklist_new = {"disputes_total": 1}
        html_out = r._render_run_auto_decisions(checklist_new, "target")
        assert "<b>1</b> файл сохранён в папке" in html_out
        assert "его к альбому/дате" in html_out

    def test_disputes_21_still_uses_singular_agreement(self):
        """21 -- та же "one"-группа, что и 1 (n%10==1, n%100!=11): "файл сохранён"/"его", не
        "файлов сохранены"/"их", хотя число двузначное."""
        checklist_new = {"disputes_total": 21}
        html_out = r._render_run_auto_decisions(checklist_new, "target")
        assert "<b>21</b> файл сохранён в папке" in html_out
        assert "его к альбому/дате" in html_out

    def test_renamed_count_shown(self):
        checklist_new = {"renamed_count": 2}
        html_out = r._render_run_auto_decisions(checklist_new, "target")
        assert "<b>2</b> файла сохранены под изменённым именем" in html_out

    def test_renamed_count_singular_verb(self):
        checklist_new = {"renamed_count": 1}
        html_out = r._render_run_auto_decisions(checklist_new, "target")
        assert "<b>1</b> файл сохранён под изменённым именем" in html_out

    def test_renamed_count_preview_wording(self):
        checklist_new = {"renamed_count": 2}
        html_out = r._render_run_auto_decisions(checklist_new, "workdir")
        assert "2</b> файла будут сохранены под изменённым именем" in html_out

    def test_order_dates_then_series_then_disputes_then_renames(self):
        checklist_new = {
            "date_issues_c_total": 1, "near_dup_clusters": [["a", "b"]],
            "disputes_total": 1, "renamed_count": 1,
        }
        html_out = r._render_run_auto_decisions(checklist_new, "target")
        idx_date = html_out.index("определена приблизительно")
        idx_series = html_out.index("серия похожих")
        idx_disputes = html_out.index("_Unsorted")
        idx_renamed = html_out.index("изменённым именем")
        assert idx_date < idx_series < idx_disputes < idx_renamed

    def test_quality_flags_both_shown(self):
        """Речь пользователя, 2026-08-18: "Качество кадров" -- диаграмма analyze-уровня, не
        перенесённая в этот отчёт даже текстом (в отличие от "Надёжности дат") -- закрывает
        пробел, quality_flags уже считался в checklist_new, просто не читался здесь."""
        checklist_new = {"quality_flags": {"small_image": 2, "low_confidence_photo": 3}}
        html_out = r._render_run_auto_decisions(checklist_new, "target")
        assert "<b>5</b> файлов" in html_out
        assert "с пометкой на проверку качества" in html_out
        assert "2 файла маленького размера" in html_out
        assert "3 файла с низкой уверенностью распознавания" in html_out

    def test_quality_flags_only_small_image(self):
        checklist_new = {"quality_flags": {"small_image": 1}}
        html_out = r._render_run_auto_decisions(checklist_new, "target")
        assert "<b>1</b> файл" in html_out
        assert "маленького размера" in html_out
        assert "низкой уверенностью" not in html_out

    def test_quality_flags_preview_wording(self):
        checklist_new = {"quality_flags": {"small_image": 1}}
        html_out = r._render_run_auto_decisions(checklist_new, "workdir")
        assert "будет сохранён с пометкой на проверку качества" in html_out

    def test_quality_flags_alone_renders_section(self):
        """Без quality_flags раздел не рендерился бы вовсе (test_empty_state_renders_nothing)
        -- один только этот сигнал обязан включить карточку."""
        checklist_new = {"quality_flags": {"small_image": 1}}
        html_out = r._render_run_auto_decisions(checklist_new, "target")
        assert "<h2>Что программа решила сама</h2>" in html_out

    def test_order_quality_flags_after_renames(self):
        checklist_new = {"renamed_count": 1, "quality_flags": {"small_image": 1}}
        html_out = r._render_run_auto_decisions(checklist_new, "target")
        idx_renamed = html_out.index("изменённым именем")
        idx_quality = html_out.index("проверку качества")
        assert idx_renamed < idx_quality


class TestGenerateReportWiresSectionThree:
    def test_target_report_contains_section_three(self, tmp_path):
        data = {"appended": [], "dates_review": [_dates_review_row("C")]}
        out_path = tmp_path / "report.html"
        r.generate_report(data, str(out_path), level="target",
                           run_start="2026-02-01 00:00:00")
        html_out = out_path.read_text(encoding="utf-8")
        assert "<h2>Что программа решила сама</h2>" in html_out

    def test_cli_dry_run_without_full_workdir_still_has_section_three(self, tmp_path):
        """2026-08-14, прямая просьба пользователя -- унификация dry-run/реального прогона
        (см. одноимённый тест в tests/test_run_report_section1_copied.py). Параметр
        `full_workdir` убран из report.py целиком (REVIEW-HANDOFF.md, Раунд 92)."""
        data = {"appended": [{"timestamp": "2026-02-01 00:00:01", "source": r"D:\Src\a.jpg",
                               "dest": r"C:\T\dst\ByDate\2020\a.jpg",
                               "reason": "appended_new", "flags": ""}],
                "dates_review": [_dates_review_row("C")]}
        out_path = tmp_path / "report.html"
        r.generate_report(data, str(out_path), level="workdir",
                           run_start="2026-02-01 00:00:00")
        html_out = out_path.read_text(encoding="utf-8")
        assert "<h2>Что программа решила сама</h2>" in html_out

    def test_old_checklist_cards_removed_alongside_new_sections(self, tmp_path):
        """Прямое решение пользователя (2026-08-14): старые _render_recommendations()/
        _render_exact_dup_examples() убраны из ветки редизайна -- релиз только после
        реализации детализации, реальные пользователи промежуточное состояние не увидят."""
        data = {"appended": [{"timestamp": "2026-02-01 00:00:01", "source": r"D:\Src\a.jpg",
                               "dest": r"C:\T\dst\ByDate\2020\a.jpg",
                               "reason": "appended_new", "flags": ""}],
                "dates_review": [_dates_review_row("C")]}
        out_path = tmp_path / "report.html"
        r.generate_report(data, str(out_path), level="target",
                           run_start="2026-02-01 00:00:00")
        html_out = out_path.read_text(encoding="utf-8")
        assert "Новое в этом пополнении" not in html_out
        assert "<h2>Что программа решила сама</h2>" in html_out
