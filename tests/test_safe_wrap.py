import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from quality_rules.safe_wrap import (DEFAULT_FONT_FAMILY, DEFAULT_FONT_PATH, classify_explicit_boundary,
                                     classify_rendered_line_boundaries, preflight, producer_preflight,
                                     resolve_cjk_font, validate_ratio)

FONT = resolve_cjk_font()
FIXTURES = json.loads((Path(__file__).parent / "fixtures" / "safe-wrap-fixtures.json").read_text(encoding="utf-8"))


def _fixture_result(case):
    defaults = FIXTURES["defaults"]
    return preflight(
        case["text"],
        frame_width=case.get("frame_width", defaults["frame_width"]),
        margins=tuple(case.get("margins", defaults["margins"])),
        font_size=case.get("font_size", defaults["font_size"]),
        max_lines=case.get("max_lines", defaults["max_lines"]),
        safe_ratio=case.get("safe_ratio", defaults["safe_ratio"]),
        font=FONT,
    )


def test_ratio_and_installed_biz_udpgothic_contract():
    assert validate_ratio(None) == 0.90
    assert validate_ratio(0.92) == 0.92
    try: validate_ratio(0.89)
    except ValueError as error: assert str(error) == "SAFE-WRAP-RATIO-01"
    else: assert False
    assert FONT["family"] == DEFAULT_FONT_FAMILY and FONT["status"] == "ud-installed"
    assert FONT["path"] == str(DEFAULT_FONT_PATH) and len(FONT["sha256"]) == 64


def test_positive_natural_phrase_breaks_use_actual_font_metrics():
    result = preflight("確認する前に、保存する内容を見直します。", frame_width=120, margins=(10, 10), font_size=12, max_lines=4, font=FONT)
    assert result.status == "pass" and len(result.lines) >= 2
    assert all(width <= result.safe_width for width in result.measured_widths)
    assert result.font_path == str(DEFAULT_FONT_PATH)


def test_protected_tokens_and_unbreakable_overflow_block():
    for text in (
        "https://example.com/very-long-unbreakable-token",
        "123456789012345678901234",
        "UnbreakableEnglishWord",
        "確認する",
        "見られる",
        "見直します",
        "(12345678901234567890)",
    ):
        result = preflight(text, frame_width=55, margins=(5, 5), font_size=12, max_lines=2, font=FONT)
        assert result.status == "block" and result.rule_ids == ["SAFE-WRAP-NATURAL-BREAK-01"]


def test_max_lines_and_no_silent_font_fallback():
    result = preflight("第一文です。第二文です。第三文です。", frame_width=90, margins=(5, 5), font_size=12, max_lines=1, font=FONT)
    assert result.status == "block" and "SAFE-WRAP-MAX-LINES-01" in result.rule_ids
    missing = resolve_cjk_font(font_path=Path("/tmp/no-biz-ud.ttf"))
    result = preflight("確認する", frame_width=90, margins=(5, 5), font_size=12, max_lines=2, font=missing)
    assert result.rule_ids == ["CJK-FONT-UNAVAILABLE-01"]


def test_exact_negative_boundary_strings_are_not_permitted_breaks():
    # These represent the historical false-negative fragments; protected and kinsoku boundaries never become output lines.
    result = producer_preflight("確認する 保存する (12345) https://example.com/a", frame_width=105, margins=(5, 5), font_size=12, max_lines=4, font=FONT)
    assert result["status"] == "block" and result["rule_ids"] == ["SAFE-WRAP-NATURAL-BREAK-01"]
    lines = result["explicit_text"].split("\n")
    assert not any(line.endswith("確認") or line.endswith("保存") or line.endswith("(") for line in lines)
    assert not any(line.startswith("する") or line.startswith("る") or line.startswith("、") or line.startswith(")") for line in lines)
    assert all("https://example.com/a" == line or "https://example.com/a" not in line for line in lines)


def test_candidateize_then_explicitly_wrap_and_remeasure():
    result = producer_preflight(
        "確認する前に、保存する内容を見直します。",
        frame_width=120,
        margins=(10, 10),
        font_size=12,
        max_lines=4,
        max_height=60,
        font=FONT,
    )
    assert result["status"] == "pass"
    assert result["initial_implicit_wrap_candidates"]
    assert result["implicit_wrap_candidates"] == []
    assert all(width <= result["safe_width"] for width in result["measured_widths"])


def test_height_limit_requires_split_without_font_shrink():
    result = producer_preflight(
        "第一文です。第二文です。第三文です。",
        frame_width=90,
        margins=(5, 5),
        font_size=12,
        max_lines=10,
        max_height=12,
        font=FONT,
    )
    assert result["status"] == "block"
    assert "SAFE-WRAP-MAX-HEIGHT-01" in result["rule_ids"]
    assert result["recommended_action"] == "split-frame-or-slide"
    assert result["font_size"] == 12


def test_explicit_bad_lines_and_forbidden_pairs_hard_fail():
    one_character = preflight("確認します。\n続", frame_width=200, margins=(5, 5), font_size=12, max_lines=3, font=FONT)
    assert "SAFE-WRAP-ONE-CHAR-LINE-01" in one_character.rule_ids
    line_head = preflight("確認します。\n、続けます。", frame_width=200, margins=(5, 5), font_size=12, max_lines=3, font=FONT)
    assert "SAFE-WRAP-FORBIDDEN-LINE-HEAD-01" in line_head.rule_ids
    opening_end = preflight("項目（\n説明です）", frame_width=200, margins=(5, 5), font_size=12, max_lines=3, font=FONT)
    assert "SAFE-WRAP-OPENING-BRACKET-END-01" in opening_end.rule_ids
    pair = preflight("確認\nする", frame_width=200, margins=(5, 5), font_size=12, max_lines=3, font=FONT)
    assert "SAFE-WRAP-FORBIDDEN-PAIR-01" in pair.rule_ids


def test_ascii_parenthesized_run_is_unbreakable():
    result = preflight("(12345678901234567890)", frame_width=70, margins=(5, 5), font_size=12, max_lines=3, font=FONT)
    assert result.status == "block"
    assert result.rule_ids == ["SAFE-WRAP-NATURAL-BREAK-01"]


def test_fixture_positive_cases_stay_inside_declared_safe_ratio():
    for case in FIXTURES["positive"]:
        result = _fixture_result(case)
        assert result.status == case["expected_status"], case["id"]
        assert result.rule_ids == [], case["id"]
        assert all(width <= result.safe_width for width in result.measured_widths), case["id"]


def test_each_negative_and_historical_screenshot_fixture_emits_its_intended_rule_id():
    cases = FIXTURES["negative"] + FIXTURES["historical_false_negative_screenshot_strings"]
    seen = set()
    for case in cases:
        result = _fixture_result(case)
        assert result.status == "block", case["id"]
        assert case["expected_rule_id"] in result.rule_ids, (case["id"], result.rule_ids)
        seen.add(case["expected_rule_id"])
    assert {
        "SAFE-WRAP-NOUN-SURU-SPLIT-01",
        "SAFE-WRAP-VERB-AUX-SPLIT-01",
        "SAFE-WRAP-POLITE-FORM-SPLIT-01",
        "SAFE-WRAP-ONE-CHAR-LINE-01",
        "SAFE-WRAP-FORBIDDEN-LINE-HEAD-01",
        "SAFE-WRAP-OPENING-BRACKET-END-01",
        "SAFE-WRAP-DIGIT-SPLIT-01",
        "SAFE-WRAP-URL-SPLIT-01",
        "SAFE-WRAP-ENGLISH-SPLIT-01",
        "SAFE-WRAP-BRACKET-PAIR-BOUNDARY-01",
    } <= seen


def test_canonical_classifier_handles_training_phrase_boundaries_without_case_rules():
    for phrase in ("用件を伝える", "次の行動を見る", "会社へ確認する", "連絡文を保存する", "持ち物を整理する"):
        result = preflight(phrase, frame_width=160, margins=(5, 5), font_size=12, max_lines=3, font=FONT)
        assert result.status == "pass", (phrase, result.rule_ids)
    natural = classify_explicit_boundary("この資料は", "明日の会議で確認します")
    stranded = classify_explicit_boundary("この資料", "は明日の会議で確認します")
    predicate = classify_explicit_boundary("用件を", "伝える")
    assert natural.allowed and natural.kind == "particle-ending"
    assert not stranded.allowed and "SAFE-WRAP-STRANDED-PARTICLE-01" in stranded.rule_ids
    assert not predicate.allowed and "SAFE-WRAP-PREDICATE-ISOLATION-01" in predicate.rule_ids


def test_uncertain_japanese_and_known_bad_morpheme_boundaries_block_canonically():
    for left, right in (
        ("ご紹介し", "ていく"), ("支援し", "ます"), ("考えて", "います"),
        ("内容確認", "次工程"),
    ):
        decision = classify_explicit_boundary(left, right)
        assert not decision.allowed, (left, right, decision)
    assert "SAFE-WRAP-AMBIGUOUS-JA-BOUNDARY-01" in classify_explicit_boundary("内容確認", "次工程").rule_ids


def test_symbol_after_boundaries_are_preferred_without_weakening_protections():
    for symbol in "：:／/・；;":
        decision = classify_explicit_boundary(f"確認項目{symbol}", "内容です。")
        assert decision.allowed and decision.kind == "symbol-after", (symbol, decision)
    for symbol in "、。！？!?":
        decision = classify_explicit_boundary(f"確認項目{symbol}", "内容です。")
        assert decision.allowed and decision.kind == "sentence", (symbol, decision)

    leading_bullet = classify_explicit_boundary("・", "確認項目")
    after_opening = classify_explicit_boundary("（", "確認項目）")
    protected_url = classify_explicit_boundary("https://example.com/", "path")
    protected_ascii = classify_explicit_boundary("Alpha/", "Beta")
    protected_digits = classify_explicit_boundary("2026/", "08")
    assert not leading_bullet.allowed and leading_bullet.kind == "bullet-prefix"
    assert not after_opening.allowed and after_opening.kind in {"opening-bracket-end", "protected-token"}
    assert not protected_url.allowed and protected_url.kind == "protected-token"
    assert not protected_ascii.allowed and protected_ascii.kind == "protected-token"
    assert not protected_digits.allowed and protected_digits.kind == "protected-token"


def test_planner_chooses_symbol_after_before_available_particle_boundary():
    result = producer_preflight(
        "資料を確認：内容。",
        frame_width=104, margins=(5, 5), font_size=12, max_lines=3, font=FONT,
    )
    assert result["status"] == "pass", result
    assert result["explicit_text"].split("\n")[0].endswith("："), result
    assert result["boundary_kinds"][0] == "symbol-after", result


def test_planner_chooses_first_reachable_symbol_before_later_slashes():
    text = "次の用件ごとに、使う手段と理由を書く：当日の遅刻／資料の確認依頼／急ぎの事故報告／来週の面談日程"
    result = producer_preflight(
        text, frame_width=262, margins=(6, 6), font_size=9, max_lines=3, font=FONT,
    )
    assert result["status"] == "pass", result
    assert result["explicit_text"].split("\n") == [
        "次の用件ごとに、使う手段と理由を書く：",
        "当日の遅刻／資料の確認依頼／",
        "急ぎの事故報告／来週の面談日程",
    ]
    assert result["boundary_kinds"] == ["symbol-after", "symbol-after"]


def test_planner_falls_through_only_when_first_symbol_cannot_fit_line_cap():
    text = "次の用件ごとに、使う手段と理由を書く：当日の遅刻／資料の確認依頼／急ぎの事故報告／来週の面談日程"
    result = producer_preflight(
        text, frame_width=262, margins=(6, 6), font_size=9, max_lines=2, font=FONT,
    )
    assert result["status"] == "pass", result
    lines = result["explicit_text"].split("\n")
    assert len(lines) == 2
    assert lines[0].endswith("／") and not lines[0].endswith("：")
    assert result["boundary_kinds"] == ["symbol-after"]


def test_planner_preserves_order_across_slash_and_semicolon_candidates():
    result = producer_preflight(
        "確認事項：第一候補／第二候補；最終確認。",
        frame_width=120, margins=(5, 5), font_size=12, max_lines=4, font=FONT,
    )
    assert result["status"] == "pass", result
    assert result["explicit_text"].split("\n") == ["確認事項：", "第一候補／", "第二候補；", "最終確認。"]
    assert result["boundary_kinds"] == ["symbol-after", "symbol-after", "symbol-after"]


def test_global_plan_records_breaks_and_avoids_low_width_continuations():
    result = producer_preflight(
        "用件を伝えてから、次の行動を確認し、連絡文を保存します。",
        frame_width=150, margins=(8, 8), font_size=12, max_lines=5, font=FONT,
    )
    assert result["status"] == "pass"
    assert result["break_indices"] and result["boundary_kinds"]
    assert all(ratio >= 0.30 for ratio in result["width_ratios"][1:])
    assert result["implicit_wrap_candidates"] == []


def test_prefix_aware_capacities_use_the_same_classifier_and_body_cost_graph():
    result = producer_preflight(
        "名乗り、用件を伝える",
        frame_width=356, margins=(6, 6), font_size=22, max_lines=3, font=FONT,
        line_capacities=(193.15, 193.15),
    )
    assert result["status"] == "pass"
    assert result["explicit_text"].split("\n") == ["名乗り、", "用件を伝える"]
    assert result["boundary_kinds"] == ["sentence"]
    assert result["line_capacities"] == [193.15, 193.15]
    assert all(ratio >= 0.30 for ratio in result["width_ratios"])


def test_prefix_body_one_character_candidate_is_replanned_not_accepted():
    result = producer_preflight(
        "名、用件を伝える",
        frame_width=356, margins=(6, 6), font_size=22, max_lines=3, font=FONT,
        line_capacities=(35.0, 193.15),
    )
    assert result["status"] == "block"
    assert "SAFE-WRAP-NATURAL-BREAK-01" in result["rule_ids"] or "SAFE-WRAP-ONE-CHAR-LINE-01" in result["rule_ids"]


def test_rendered_boundary_classifier_excludes_only_declared_explicit_breaks():
    lines = ["内容確認", "次工程。", "確認します。"]
    blocked = classify_rendered_line_boundaries(lines, paragraph_indices=[0, 0, 0])
    assert blocked["status"] == "block" and blocked["boundary_count"] == 2
    excluded = classify_rendered_line_boundaries(
        lines, paragraph_indices=[0, 0, 0], explicit_break_indices=[1],
    )
    assert excluded["status"] == "pass"
    assert excluded["boundary_count"] == 1
    assert excluded["boundaries"][0]["boundary_index"] == 2


def test_rendered_boundary_classifier_rejects_invalid_explicit_break_map():
    result = classify_rendered_line_boundaries(
        ["第一行", "第二行"], paragraph_indices=[0, 0], explicit_break_indices=[0],
    )
    assert result["status"] == "block"
    assert result["reason_codes"] == ["SAFE-WRAP-RENDERED-EXPLICIT-BREAK-MAP-01"]
