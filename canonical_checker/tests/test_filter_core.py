import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("filter_core", ROOT / "filter_core.py")
module = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules[spec.name] = module
spec.loader.exec_module(module)
filter_text = module.filter_text


def test_offer_prefix_is_removed_only_for_safe_offer():
    result = filter_text("必要なら設定を確認できます。")
    assert result.text == "設定を確認できます。"
    assert result.rules == ("F1",)


def test_direct_choice_prefix_is_removed():
    assert filter_text("必要であれば選択肢は再試行です。").text == "選択肢は再試行です。"


def test_redundant_uncertainty_requires_adjacent_scope_and_unresolved_item():
    text = "確認済みは設定読取です。未確認は設定読取の配送結果です。設定読取だけでは判断できません。"
    assert filter_text(text).text == "確認済みは設定読取です。未確認は設定読取の配送結果です。"


def test_standalone_uncertainty_is_preserved():
    text = "設定読取だけでは判断できません。"
    assert filter_text(text).text == text


def test_safety_authorization_operational_and_external_send_conditions_are_preserved():
    for text in ("必要なら停止します。", "必要なら承認を取ります。", "必要なら gateway を再起動します。", "必要なら Slack に送信できます。"):
        assert filter_text(text).text == text


def test_protected_syntax_and_content_are_preserved():
    for text in ("必要なら PID 1428 を確認できます。", "必要なら https://example.test を確認できます。", "必要なら /private/work/a を確認できます。", "`MEDIA:/tmp/a.png` が必要なら確認できます。", "> 必要なら設定を確認できます。", "| 必要なら設定を確認できます。 |"):
        assert filter_text(text).text == text


def test_third_candidate_fails_open_for_entire_response():
    text = "必要なら設定を確認できます。必要ならログを確認できます。必要なら状態を確認できます。"
    assert filter_text(text).text == text


def test_no_response_body_logging_contract():
    # The pure core has no logging import and exposes only structured results.
    assert "logging" not in (ROOT / "filter_core.py").read_text(encoding="utf-8")


def test_contrast_c1_and_c2_preserve_the_direct_complete_b_predicate():
    c1 = filter_text("これは単なる分類ではなく法的な義務です。")
    c2 = filter_text("障害というより設定不備です。")
    c1_comma = filter_text("これは単なる分類ではなく、 法的な義務です。")
    c2_comma = filter_text("障害というより、 設定不備です。")
    assert (c1.text, c1.rules) == ("法的な義務です。", ("C1",))
    assert (c2.text, c2.rules) == ("設定不備です。", ("C2",))
    assert (c1_comma.text, c1_comma.rules) == ("法的な義務です。", ("C1",))
    assert (c2_comma.text, c2_comma.rules) == ("設定不備です。", ("C2",))


def test_contrast_protects_named_entities_numbers_urls_code_media_quotes_tables_and_ambiguity():
    for text in (
        "これは単なる東京都ではなく対象です。",
        "これは単なる分類ではなく2026年の義務です。",
        "これは単なる分類ではなくhttps://example.testです。",
        "これは単なる分類ではなく`code`です。",
        "これは単なる分類ではなくMEDIA:/tmp/a.pngです。",
        "> これは単なる分類ではなく義務です。",
        "| これは単なる分類ではなく義務です。 |",
        "これは単なる分類ではなく義務。",
    ):
        assert filter_text(text).text == text


def test_multiline_markdown_filters_only_prose_and_preserves_structure():
    text = "# 結果\n必要なら設定を確認できます。\n\n- 必要であれば選択肢は再試行です。\n"
    result = filter_text(text)
    assert result.text == "# 結果\n設定を確認できます。\n\n- 選択肢は再試行です。\n"
    assert result.rules == ("F1", "F2")


def test_mixed_markdown_protected_regions_are_byte_stable_while_prose_changes():
    text = (
        "必要なら設定を確認できます。\n"
        "```py\n必要なら code を確認できます。\n```\n"
        "> 必要なら引用を確認できます。\n"
        "| 必要なら表を確認できます。 |\n"
        "MEDIA:/tmp/image.png\n"
        "必要ならログを確認できます。\n"
    )
    result = filter_text(text)
    assert result.text == (
        "設定を確認できます。\n"
        "```py\n必要なら code を確認できます。\n```\n"
        "> 必要なら引用を確認できます。\n"
        "| 必要なら表を確認できます。 |\n"
        "MEDIA:/tmp/image.png\n"
        "ログを確認できます。\n"
    )
    assert result.rules == ("F1", "F1")


def test_document_mode_has_segment_local_rollback_and_global_cap():
    capped_segment = "必要ならAを確認できます。必要ならBを確認できます。必要ならCを確認できます。"
    other = "必要ならDを確認できます。"
    local = filter_text(capped_segment + "\n```x\npass\n```\n" + other, mode="document", document_global_cap=3)
    assert local.text == capped_segment + "\n```x\npass\n```\nDを確認できます。"
    assert local.rules == ("F1",)

    globally_capped = filter_text(
        "必要ならAを確認できます。\n```x\npass\n```\n必要ならBを確認できます。",
        mode="document",
        document_global_cap=1,
    )
    assert globally_capped.text == "Aを確認できます。\n```x\npass\n```\n必要ならBを確認できます。"
    assert globally_capped.rules == ("F1",)


def test_response_mode_still_rolls_back_the_entire_response_on_third_candidate():
    text = "必要ならAを確認できます。\n必要ならBを確認できます。\n必要ならCを確認できます。"
    assert filter_text(text).text == text


def test_combined_rules_share_maximum_and_third_candidate_fails_open():
    two = "必要なら設定を確認できます。これは単なる分類ではなく義務です。"
    three = "必要なら設定を確認できます。これは単なる分類ではなく義務です。障害というより設定不備です。"
    assert filter_text(two).rules == ("F1", "C1")
    assert filter_text(three).text == three
