"""SEQ-02 template engine: parsing, spintax, conditionals, variations and style.

Database-backed behaviour lives in `tests/test_template_store.py`.
"""

from collections import Counter

import pytest

from linkedin_mcp.templating import (
    DEFAULT_STYLE,
    MAX_NESTING_DEPTH,
    MAX_STYLE_SAMPLES,
    SKIPPED_SUBLIST,
    RenderRefusal,
    RenderRefusalReason,
    RenderResult,
    StylePolicy,
    TemplateStyleError,
    TemplateSyntaxError,
    assign_variations,
    broken_punctuation,
    compile_bodies,
    contains_forbidden_dash,
    inline_template,
    known_token_names,
    normalise_dashes,
    normalise_token,
    parse_template,
    preview_template,
    render_template,
    safe_render_template,
    sentences,
    spintax_index,
    style_samples,
    style_violations,
    tidy_whitespace,
    validate_template,
    variation_distribution,
    variation_index,
    variation_plan,
)


EM_DASH = "\u2014"
EN_DASH = "\u2013"

LEAD_VALUES = {
    "firstName": "Nived",
    "lastName": "Velayudhan",
    "fullName": "Nived Velayudhan",
    "company": "Microsoft",
    "position": "Solution Engineer",
    "headline": "Solution Engineer at Microsoft",
    "location": "Bengaluru",
    "memberId": "ACoAA123",
    "publicId": "nivedv",
    "mutualTotal": "12",
    "cs_industry": "SaaS",
}

ABSENT_VALUES = pytest.mark.parametrize(
    "absent",
    [
        pytest.param(None, id="none"),
        pytest.param("", id="empty"),
        pytest.param("   ", id="spaces"),
        pytest.param("\t\n ", id="mixed-whitespace"),
    ],
)

REQUIRED_TOKENS = pytest.mark.parametrize(
    "token",
    ["firstName", "company", "position", "mutualTotal", "cs_industry"],
)


def render(body, values=None, **kwargs):
    return render_template(body, values if values is not None else LEAD_VALUES, **kwargs)


# --------------------------------------------------------------------------
# Variable insertion
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("token", "expected"),
    [
        ("firstName", "Nived"),
        ("lastName", "Velayudhan"),
        ("fullName", "Nived Velayudhan"),
        ("company", "Microsoft"),
        ("position", "Solution Engineer"),
        ("headline", "Solution Engineer at Microsoft"),
        ("location", "Bengaluru"),
        ("memberId", "ACoAA123"),
        ("publicId", "nivedv"),
        ("mutualTotal", "12"),
        ("cs_industry", "SaaS"),
    ],
)
def test_every_public_token_inserts_its_value(token, expected):
    assert render("[{%s}]" % token).text == f"[{expected}]"


def test_values_are_stripped_before_insertion():
    assert render("Hi {firstName}.", {"firstName": "  Nived  "}).text == "Hi Nived."


def test_numeric_values_render_as_decimals():
    assert render("{mutualTotal} shared.", {"mutualTotal": 12}).text == "12 shared."


def test_zero_is_a_present_value_not_an_absent_one():
    # Presence is tested, never the value. `docs/plan.md` is explicit that
    # IF/THEN/ELSE checks presence only, so a real count of zero renders.
    assert render("{mutualTotal} shared.", {"mutualTotal": 0}).text == "0 shared."


def test_custom_field_token_is_case_insensitive():
    assert render("{cs_Industry}", {"cs_industry": "SaaS"}).text == "SaaS"


def test_fixed_tokens_are_case_sensitive():
    with pytest.raises(TemplateSyntaxError):
        parse_template("{firstname}")


def test_unknown_token_is_rejected_at_parse_time():
    with pytest.raises(TemplateSyntaxError) as error:
        parse_template("Hi {nickname}.")
    assert "nickname" in str(error.value)
    assert "firstName" in str(error.value)


def test_known_token_names_are_reported_in_errors():
    assert "mutualTotal" in known_token_names()
    assert "company" in known_token_names()


def test_normalise_token_lowercases_only_the_open_namespaces():
    assert normalise_token("firstName") == "firstName"
    assert normalise_token("cs_Industry") == "cs_industry"
    assert normalise_token("AI_Opener") == "ai_opener"


def test_tokens_used_is_reported():
    message = render("{firstName} at {company}")
    assert message.tokens_used == ("company", "firstName")


# --------------------------------------------------------------------------
# Requirement 1: a missing value never renders a broken message
# --------------------------------------------------------------------------


@REQUIRED_TOKENS
@ABSENT_VALUES
def test_absent_token_refuses_rather_than_rendering_a_hole(token, absent):
    values = dict(LEAD_VALUES)
    values[token] = absent
    result = safe_render_template("Hi {%s}, quick question." % token, values)

    assert not result.ok
    assert result.refusal.reason is RenderRefusalReason.MISSING_VARIABLE
    assert result.refusal.detail["token"] == token
    assert result.sublist == SKIPPED_SUBLIST


@REQUIRED_TOKENS
def test_missing_key_refuses_like_an_empty_value(token):
    values = {key: value for key, value in LEAD_VALUES.items() if key != token}
    result = safe_render_template("Hi {%s}." % token, values)
    assert not result.ok
    assert result.refusal.reason is RenderRefusalReason.MISSING_VARIABLE


@ABSENT_VALUES
def test_the_hi_comma_message_is_impossible(absent):
    result = safe_render_template("Hi {firstName}, saw your post.", {"firstName": absent})
    assert not result.ok
    assert result.refusal.reason is RenderRefusalReason.MISSING_VARIABLE


@ABSENT_VALUES
def test_guarded_token_falls_back_instead_of_refusing(absent):
    body = "{IF firstName}Hi {firstName},{ELSE}Hi there,{END} saw your post."
    assert render(body, {"firstName": absent}).text == "Hi there, saw your post."


def test_guarded_token_uses_the_then_branch_when_present():
    body = "{IF firstName}Hi {firstName},{ELSE}Hi there,{END} saw your post."
    assert render(body, {"firstName": "Nived"}).text == "Hi Nived, saw your post."


def test_render_template_raises_the_typed_refusal():
    with pytest.raises(RenderRefusal) as error:
        render_template("Hi {firstName}.", {})
    assert error.value.sublist == SKIPPED_SUBLIST
    assert error.value.is_awaiting_ai is False


def test_refusal_to_result_is_mcp_shaped():
    result = safe_render_template("Hi {firstName}.", {})
    payload = result.to_result()
    assert payload["status"] == "refused"
    assert payload["reason"] == "missing_variable"
    assert payload["sublist"] == "skipped"
    assert payload["awaiting_ai"] is False


def test_rendered_message_to_result_is_mcp_shaped():
    payload = safe_render_template("Hi {firstName}.", LEAD_VALUES).to_result()
    assert payload["status"] == "success"
    assert payload["text"] == "Hi Nived."


def test_render_result_must_carry_exactly_one_outcome():
    with pytest.raises(ValueError):
        RenderResult()
    with pytest.raises(ValueError):
        RenderResult(
            rendered=safe_render_template("Hi.", {}).rendered,
            refusal=RenderRefusal(RenderRefusalReason.EMPTY_MESSAGE, "x"),
        )


def test_empty_render_is_refused():
    result = safe_render_template("{IF firstName}Hi{END}", {"firstName": ""})
    assert not result.ok
    assert result.refusal.reason is RenderRefusalReason.EMPTY_MESSAGE


def test_dangling_punctuation_is_refused():
    # An empty ELSE branch at the start of a line leaves the comma stranded.
    result = safe_render_template("{IF company}Hi{ELSE}{END}, welcome.", {"company": ""})
    assert not result.ok
    assert result.refusal.reason is RenderRefusalReason.BROKEN_PUNCTUATION


def test_max_chars_refuses_an_over_long_connection_note():
    body = "Hi {firstName}, " + ("a" * 300)
    result = safe_render_template(body, LEAD_VALUES, max_chars=300)
    assert not result.ok
    assert result.refusal.reason is RenderRefusalReason.TOO_LONG
    assert result.refusal.detail["max_chars"] == 300


def test_max_chars_allows_a_note_that_fits():
    assert safe_render_template("Hi {firstName}.", LEAD_VALUES, max_chars=300).ok


def test_unparseable_body_refuses_rather_than_raising_a_syntax_error():
    result = safe_render_template("Hi {firstName", LEAD_VALUES)
    assert not result.ok
    assert result.refusal.reason is RenderRefusalReason.TEMPLATE_INVALID


# --------------------------------------------------------------------------
# IF / THEN / ELSE
# --------------------------------------------------------------------------


def test_conditional_without_else_drops_the_branch():
    body = "Hi there.{IF company} You are at {company}.{END}"
    assert render(body, {"company": ""}).text == "Hi there."
    assert render(body, {"company": "Microsoft"}).text == "Hi there. You are at Microsoft."


def test_conditionals_nest():
    body = (
        "{IF firstName}Hi {firstName}"
        "{IF company} at {company}{ELSE} out there{END}"
        "{ELSE}Hi there{END}."
    )
    assert render(body, {"firstName": "Nived", "company": "Microsoft"}).text == (
        "Hi Nived at Microsoft."
    )
    assert render(body, {"firstName": "Nived", "company": " "}).text == (
        "Hi Nived out there."
    )
    assert render(body, {"firstName": ""}).text == "Hi there."


def test_conditional_can_guard_a_custom_field():
    body = "{IF cs_industry}You work in {cs_industry}.{ELSE}Tell me what you build.{END}"
    assert render(body, {"cs_industry": "SaaS"}).text == "You work in SaaS."
    assert render(body, {}).text == "Tell me what you build."


def test_else_branch_referencing_an_absent_token_still_refuses():
    body = "{IF company}At {company}{ELSE}At {position}{END}."
    result = safe_render_template(body, {"company": "", "position": ""})
    assert not result.ok
    assert result.refusal.detail["token"] == "position"


def test_if_without_end_is_a_syntax_error():
    with pytest.raises(TemplateSyntaxError) as error:
        parse_template("{IF firstName}Hi")
    assert "END" in str(error.value)


def test_else_without_if_is_a_syntax_error():
    with pytest.raises(TemplateSyntaxError):
        parse_template("Hi{ELSE}there")


def test_end_without_if_is_a_syntax_error():
    with pytest.raises(TemplateSyntaxError):
        parse_template("Hi{END}")


def test_if_without_a_token_name_is_a_syntax_error():
    with pytest.raises(TemplateSyntaxError):
        parse_template("{IF}yes{END}")


def test_if_on_an_unknown_token_is_a_syntax_error():
    with pytest.raises(TemplateSyntaxError):
        parse_template("{IF nickname}yes{END}")


def test_reserved_keywords_cannot_be_token_names():
    with pytest.raises(TemplateSyntaxError):
        parse_template("{IF END}x{END}")


# --------------------------------------------------------------------------
# Requirement 3: spintax, including the degenerate cases
# --------------------------------------------------------------------------


def test_spintax_picks_an_alternative():
    assert render("{alpha|beta|gamma}", {}, sequence=0).text == "alpha"
    assert render("{alpha|beta|gamma}", {}, sequence=1).text == "beta"
    assert render("{alpha|beta|gamma}", {}, sequence=2).text == "gamma"
    assert render("{alpha|beta|gamma}", {}, sequence=3).text == "alpha"


def test_single_braced_identifier_is_a_variable_not_a_spintax():
    # `{a}` is deliberately a variable. A one-alternative spin does nothing, so
    # reading it as a variable is the only reading that can ever be useful, and
    # an unknown one is rejected loudly instead of rendering the letter "a".
    with pytest.raises(TemplateSyntaxError):
        parse_template("{a}")
    program = parse_template("{cs_a}")
    assert program.tokens() == ("cs_a",)
    assert program.spintax_count == 0


def test_empty_alternative_is_legal_and_renders_nothing():
    body = "Great{ work|}."
    assert render(body, {}, sequence=0).text == "Great work."
    assert render(body, {}, sequence=1).text == "Great."


def test_all_empty_spintax_renders_nothing():
    program = parse_template("{|}")
    assert program.spintax_sizes == {0: 2}
    result = safe_render_template("{|}", {})
    assert not result.ok
    assert result.refusal.reason is RenderRefusalReason.EMPTY_MESSAGE


def test_spintax_nests():
    body = "{one|{two|three}}"
    assert render(body, {}, sequence=0).text == "one"
    # The outer node picks index 1 on odd sequences and hands the inner node
    # sequence // 2, so both inner alternatives stay reachable.
    assert render(body, {}, sequence=1).text == "three"
    assert render(body, {}, sequence=3).text == "two"
    assert render(body, {}, sequence=5).text == "three"


def test_every_nested_spintax_alternative_is_reachable():
    body = "{one|{two|three}}"
    seen = {render(body, {}, sequence=sequence).text for sequence in range(12)}
    assert seen == {"one", "two", "three"}


def test_spintax_does_not_lock_to_the_variation_split():
    template = inline_template("{a|b} one", variations=["{a|b} two", "{a|b} three"])
    seen = {
        render_template(template, {}, sequence=sequence).text
        for sequence in range(12)
    }
    assert seen == {
        "a one",
        "b one",
        "a two",
        "b two",
        "a three",
        "b three",
    }


def test_spintax_alternatives_can_contain_variables():
    body = "{Hi {firstName}|Hey {firstName}}, quick one."
    assert render(body, {"firstName": "Sam"}, sequence=0).text == "Hi Sam, quick one."
    assert render(body, {"firstName": "Sam"}, sequence=1).text == "Hey Sam, quick one."


def test_spintax_alternative_can_start_with_a_variable():
    body = "{{firstName}|there}, hello."
    assert render(body, {"firstName": "Sam"}, sequence=0).text == "Sam, hello."
    assert render(body, {"firstName": "Sam"}, sequence=1).text == "there, hello."


def test_spintax_alternative_can_contain_a_conditional():
    body = "{{IF firstName}Hi {firstName}{ELSE}Hi there{END}|Hello}."
    assert render(body, {"firstName": "Sam"}, sequence=0).text == "Hi Sam."
    assert render(body, {"firstName": ""}, sequence=0).text == "Hi there."
    assert render(body, {"firstName": ""}, sequence=1).text == "Hello."


def test_missing_variable_inside_an_unselected_alternative_does_not_refuse():
    body = "{Hi {firstName}|Hello there}"
    assert render(body, {"firstName": ""}, sequence=1).text == "Hello there"


def test_spintax_alternatives_are_distributed_evenly():
    counts = Counter(
        render("{alpha|beta|gamma}", {}, sequence=sequence).text
        for sequence in range(99)
    )
    assert counts == {"alpha": 33, "beta": 33, "gamma": 33}


def test_two_spintax_nodes_do_not_move_in_lockstep():
    body = "{a|b} {c|d}"
    assert render(body, {}, sequence=0).text == "a d"
    assert render(body, {}, sequence=1).text == "b c"


def test_spintax_index_is_pure_and_deterministic():
    assert spintax_index(0, 0, 3) == 0
    assert spintax_index(4, 1, 3) == 2
    assert spintax_index(4, 1, 3) == 2
    with pytest.raises(ValueError):
        spintax_index(-1, 0, 3)
    with pytest.raises(ValueError):
        spintax_index(0, -1, 3)
    with pytest.raises(ValueError):
        spintax_index(0, 0, 0)


# --------------------------------------------------------------------------
# Escapes and malformed syntax
# --------------------------------------------------------------------------


def test_escaped_braces_render_literally():
    assert render("\\{not a token\\}", {}).text == "{not a token}"


def test_escaped_backslash_renders_literally():
    assert render("path \\\\ here", {}).text == "path \\ here"


def test_escaped_brace_inside_a_spintax_alternative():
    assert render("{\\{a\\}|b}", {}, sequence=0).text == "{a}"


def test_escaped_pipe_inside_a_spintax_alternative_is_literal():
    assert render("{a \\| b|c}", {}, sequence=0).text == "a | b"
    assert render("{a \\| b|c}", {}, sequence=1).text == "c"


def test_unclosed_brace_is_a_syntax_error():
    with pytest.raises(TemplateSyntaxError) as error:
        parse_template("Hi {firstName")
    assert "unclosed" in str(error.value)


def test_unmatched_closing_brace_is_a_syntax_error():
    with pytest.raises(TemplateSyntaxError) as error:
        parse_template("Hi firstName}")
    assert "unmatched" in str(error.value)


def test_empty_group_is_a_syntax_error():
    with pytest.raises(TemplateSyntaxError):
        parse_template("Hi {}")


def test_dangling_backslash_is_a_syntax_error():
    with pytest.raises(TemplateSyntaxError):
        parse_template("Hi \\")


def test_unknown_escape_is_a_syntax_error():
    with pytest.raises(TemplateSyntaxError):
        parse_template("Hi \\n there")


def test_pipe_outside_braces_is_literal_text():
    assert render("a | b", {}).text == "a | b"


# --------------------------------------------------------------------------
# Requirement 2: whole-message variations split evenly, not randomly
# --------------------------------------------------------------------------


def test_hundred_messages_across_three_variations_split_34_33_33():
    template = inline_template(
        "First body.",
        variations=["Second body.", "Third body."],
    )
    counts = Counter(
        render_template(template, {}, sequence=sequence).text for sequence in range(100)
    )
    assert counts == {"First body.": 34, "Second body.": 33, "Third body.": 33}


def test_assign_variations_is_balanced_for_a_hundred_over_three():
    assert variation_distribution(assign_variations(100, 3)) == {0: 34, 1: 33, 2: 33}


@pytest.mark.parametrize("count", [1, 2, 3, 4, 5, 7, 11])
def test_every_variation_count_stays_within_one_message_of_even(count):
    distribution = variation_distribution(assign_variations(100, count))
    assert set(distribution) == set(range(count))
    assert max(distribution.values()) - min(distribution.values()) <= 1
    assert sum(distribution.values()) == 100


def test_variation_assignment_is_deterministic_not_random():
    template = inline_template("A", variations=["B", "C"])
    first = [render_template(template, {}, sequence=n).text for n in range(30)]
    second = [render_template(template, {}, sequence=n).text for n in range(30)]
    assert first == second
    assert first[:6] == ["A", "B", "C", "A", "B", "C"]


def test_variation_index_is_reported_on_the_message():
    template = inline_template("A", variations=["B"])
    assert render_template(template, {}, sequence=5).variation_index == 1


def test_variation_plan_maps_queue_positions():
    assert variation_plan(range(4), 2) == {0: 0, 1: 1, 2: 0, 3: 1}


def test_variation_index_rejects_nonsense():
    with pytest.raises(ValueError):
        variation_index(-1, 3)
    with pytest.raises(ValueError):
        variation_index(0, 0)
    with pytest.raises(ValueError):
        assign_variations(-1, 3)


def test_variations_each_get_their_own_personalisation():
    template = inline_template(
        "Hi {firstName}, one.",
        variations=["Hi {firstName}, two."],
    )
    assert render_template(template, LEAD_VALUES, sequence=0).text == "Hi Nived, one."
    assert render_template(template, LEAD_VALUES, sequence=1).text == "Hi Nived, two."


def test_a_variation_can_refuse_while_another_would_send():
    template = inline_template("Hi there.", variations=["Hi {firstName}."])
    assert safe_render_template(template, {}, sequence=0).ok
    assert not safe_render_template(template, {}, sequence=1).ok


def test_preview_renders_a_run_in_queue_order():
    template = inline_template("A", variations=["B"])
    results = preview_template(template, [{}, {}, {}])
    assert [result.rendered.text for result in results] == ["A", "B", "A"]


def test_preview_surfaces_refusals_alongside_messages():
    results = preview_template("Hi {firstName}.", [LEAD_VALUES, {}])
    assert results[0].ok
    assert not results[1].ok
    assert results[1].sublist == SKIPPED_SUBLIST


# --------------------------------------------------------------------------
# Requirement 4: writing style rules are a hard constraint
# --------------------------------------------------------------------------


def test_em_dash_in_a_template_body_is_rejected():
    with pytest.raises(TemplateStyleError) as error:
        validate_template(f"Hi there {EM_DASH} good to meet you.")
    assert "dash" in str(error.value)


def test_em_dash_in_a_variation_is_rejected():
    with pytest.raises(TemplateStyleError):
        compile_bodies(["Hi there.", f"Hello {EM_DASH} nice work."])


def test_em_dash_in_a_spintax_alternative_is_rejected():
    with pytest.raises(TemplateStyleError):
        validate_template(f"Nice {{work|effort {EM_DASH} really}}.")


def test_em_dash_in_an_if_branch_is_rejected():
    with pytest.raises(TemplateStyleError):
        validate_template(f"{{IF company}}At {{company}}{{ELSE}}Hello {EM_DASH} hi{{END}}")


def test_en_dash_is_rejected_too():
    with pytest.raises(TemplateStyleError):
        validate_template(f"Range {EN_DASH} here.")


@pytest.mark.parametrize("dash", ["\u2014", "\u2013", "\u2012", "\u2015", "\u2212"])
def test_lead_data_dashes_are_normalised_not_refused(dash):
    message = render("At {company}.", {"company": f"Foo {dash} Bar"})
    assert message.text == "At Foo - Bar."
    assert contains_forbidden_dash(message.text) is None


def test_no_rendered_output_can_contain_an_em_dash():
    bodies = [
        "Hi {firstName}, at {company}.",
        "{IF cs_industry}In {cs_industry}.{ELSE}Tell me more.{END}",
        "{Saw|Noticed} your work at {company}.",
        "{{firstName}|there}, hello.",
    ]
    poisoned = {
        key: f"{value} {EM_DASH} suffix" for key, value in LEAD_VALUES.items()
    }
    for body in bodies:
        for sequence in range(6):
            for values in (LEAD_VALUES, poisoned):
                result = safe_render_template(body, values, sequence=sequence)
                if result.ok:
                    assert contains_forbidden_dash(result.rendered.text) is None


def test_an_unvalidated_body_with_an_em_dash_is_refused_at_render():
    # `inline_template` deliberately skips validation, so the final sweep in the
    # renderer is the last line of defence and it has to hold on its own.
    result = safe_render_template(f"Hi there {EM_DASH} hello.", {})
    assert not result.ok
    assert result.refusal.reason is RenderRefusalReason.STYLE_VIOLATION
    assert result.refusal.detail["violation"] == "forbidden_dash"


def test_an_unvalidated_body_with_a_filler_opener_is_refused_at_render():
    result = safe_render_template("Let's be honest, this is hard.", {})
    assert not result.ok
    assert result.refusal.detail["violation"] == "filler_opener"


def test_a_variation_that_smuggles_an_em_dash_is_refused_at_render():
    template = inline_template("Clean body.", variations=[f"Dirty {EM_DASH} body."])
    assert safe_render_template(template, {}, sequence=0).ok
    assert not safe_render_template(template, {}, sequence=1).ok


def test_filler_opener_in_a_body_is_rejected():
    with pytest.raises(TemplateStyleError) as error:
        validate_template("In today's world, everyone builds with AI.")
    assert "filler" in str(error.value)


def test_filler_opener_hiding_in_an_else_branch_is_rejected():
    body = "{IF firstName}Hi {firstName}.{ELSE}Let's be honest, we have not met.{END}"
    with pytest.raises(TemplateStyleError):
        validate_template(body)


def test_filler_opener_hiding_in_a_spintax_alternative_is_rejected():
    with pytest.raises(TemplateStyleError):
        validate_template("{Saw your post|It's no secret that this is hard}.")


def test_curly_apostrophes_do_not_hide_a_filler_opener():
    with pytest.raises(TemplateStyleError):
        validate_template("It\u2019s no secret that shipping is hard.")


def test_over_long_sentence_in_a_body_is_rejected():
    body = "Hi {firstName}, " + " ".join(["word"] * 40) + "."
    with pytest.raises(TemplateStyleError) as error:
        validate_template(body)
    assert "sentence" in str(error.value)


def test_a_sentence_lengthened_by_lead_data_is_a_warning_not_a_refusal():
    body = "Hi {firstName}, " + " ".join(["word"] * 25) + "."
    validate_template(body)
    message = render(body, {"firstName": " ".join(["Name"] * 20)})
    assert message.warnings
    assert "sentence" in message.warnings[0]


def test_short_message_has_no_warnings():
    assert render("Hi {firstName}.", LEAD_VALUES).warnings == ()


def test_style_policy_can_loosen_sentence_length():
    body = "Hi, " + " ".join(["word"] * 40) + "."
    with pytest.raises(TemplateStyleError):
        validate_template(body)
    validate_template(body, policy=StylePolicy(max_sentence_words=60))


def test_style_policy_cannot_loosen_the_dash_ban_by_accident():
    assert DEFAULT_STYLE.forbidden_dashes[0] == EM_DASH
    with pytest.raises(TemplateStyleError):
        validate_template(f"Hi {EM_DASH} there.", policy=StylePolicy(max_sentence_words=99))


def test_the_dash_ban_cannot_be_removed_by_a_policy():
    # There is no way to construct a policy that permits an em dash. An earlier
    # version had a `forbidden_dashes` field, and passing an empty tuple turned
    # the one absolute rule in the module into a no-op.
    with pytest.raises(TypeError):
        StylePolicy(forbidden_dashes=())

    permissive = StylePolicy(extra_forbidden_dashes=())
    assert EM_DASH in permissive.forbidden_dashes
    with pytest.raises(TemplateStyleError):
        validate_template(f"Hi {EM_DASH} there.", policy=permissive)
    result = safe_render_template(f"Hi {EM_DASH} there.", {}, policy=permissive)
    assert not result.ok
    assert result.refusal.reason is RenderRefusalReason.STYLE_VIOLATION


def test_a_policy_can_add_further_banned_characters():
    policy = StylePolicy(extra_forbidden_dashes=("~",))
    assert EM_DASH in policy.forbidden_dashes
    with pytest.raises(TemplateStyleError):
        validate_template("Hi ~ there.", policy=policy)


def test_an_ai_fragment_cannot_smuggle_a_dash_past_a_permissive_policy():
    result = safe_render_template(
        "{ai_opener} Rest.",
        {},
        fragments={"opener": f"Saw it {EM_DASH} nice."},
        policy=StylePolicy(extra_forbidden_dashes=()),
    )
    assert not result.ok
    assert result.refusal.reason is RenderRefusalReason.STYLE_VIOLATION


def test_style_samples_are_capped():
    body = "{IF company}x{END}" * (MAX_STYLE_SAMPLES + 20)
    assert len(style_samples(parse_template(body))) == MAX_STYLE_SAMPLES


def test_style_validation_is_sampled_not_exhaustive():
    # Documented limitation: one choice is varied at a time, so a violation that
    # only appears when two choices interact passes validation. The renderer is
    # the guarantee, and it refuses the sequence that produces it.
    body = "{Hello|In today's}{ there| world| now}."
    validate_template(body)
    refused = [
        sequence
        for sequence in range(12)
        if not safe_render_template(body, {}, sequence=sequence).ok
    ]
    assert refused
    for sequence in range(12):
        result = safe_render_template(body, {}, sequence=sequence)
        if result.ok:
            assert not result.rendered.text.lower().startswith("in today's world")


def test_a_bare_conditional_before_a_comma_is_refused_not_sent():
    # The reason the trailing-punctuation guard exists: this used to render
    # "Hi," for a lead with no first name and report itself as fine.
    body = "Hi {IF firstName}{firstName}{END},"
    result = safe_render_template(body, {})
    assert not result.ok
    assert result.refusal.reason is RenderRefusalReason.BROKEN_PUNCTUATION


def test_a_bare_conditional_inside_brackets_is_refused_not_sent():
    body = "Hi ({IF company}{company}{END})"
    result = safe_render_template(body, {})
    assert not result.ok
    assert result.refusal.reason is RenderRefusalReason.BROKEN_PUNCTUATION


def test_a_template_that_can_strand_punctuation_is_rejected_at_authoring():
    with pytest.raises(TemplateStyleError) as error:
        validate_template("Hi {IF firstName}{firstName}{END},")
    assert "punctuation" in str(error.value)


def test_broken_punctuation_spots_a_message_ending_on_a_comma():
    assert broken_punctuation("Hi,") == ","


def test_broken_punctuation_spots_an_empty_bracket_pair():
    assert broken_punctuation("Hi ()") == "()"
    assert broken_punctuation("Hi [ ]") == "[ ]"


def test_broken_punctuation_allows_a_greeting_line_ending_in_a_comma():
    assert broken_punctuation("Hi Nived,\n\nSaw your post.") is None


def test_broken_punctuation_allows_apostrophes():
    assert broken_punctuation("It's Nived's rock 'n' roll list.") is None


def test_deeply_nested_spintax_refuses_instead_of_blowing_the_stack():
    body = "{x|" * 600 + "x" + "}" * 600
    with pytest.raises(TemplateSyntaxError) as error:
        parse_template(body)
    assert str(MAX_NESTING_DEPTH) in str(error.value)

    result = safe_render_template(body, {})
    assert not result.ok
    assert result.refusal.reason is RenderRefusalReason.TEMPLATE_INVALID


def test_deeply_nested_conditionals_refuse_instead_of_blowing_the_stack():
    body = "{IF company}" * 600 + "x" + "{END}" * 600
    result = safe_render_template(body, LEAD_VALUES)
    assert not result.ok
    assert result.refusal.reason is RenderRefusalReason.TEMPLATE_INVALID


def test_nesting_within_the_limit_still_works():
    depth = 10
    conditionals = "{IF company}" * depth + "deep" + "{END}" * depth
    assert render(conditionals, {"company": "Contoso"}).text == "deep"

    spun = "{x|" * depth + "deep" + "}" * depth
    parse_template(spun)
    # The innermost alternative sits behind ten binary choices, so it needs the
    # one sequence whose mixed-radix digits select the second branch each time.
    assert any(
        render(spun, {}, sequence=sequence).text == "deep" for sequence in range(2048)
    )


def test_a_fragment_source_that_raises_becomes_a_refusal():
    def broken(name):
        raise RuntimeError("draft store is down")

    result = safe_render_template("{ai_opener} Rest.", {}, fragments=broken)
    assert not result.ok
    assert result.refusal.reason is RenderRefusalReason.FRAGMENT_SOURCE_FAILED
    assert result.refusal.is_awaiting_ai is True
    assert result.sublist == SKIPPED_SUBLIST
    assert "draft store is down" in result.refusal.detail["error"]


def test_a_fragment_source_raising_a_lookup_error_is_still_a_fragment_failure():
    def broken(name):
        raise LookupError("no such draft")

    result = safe_render_template("{ai_opener} Rest.", {}, fragments=broken)
    assert result.refusal.reason is RenderRefusalReason.FRAGMENT_SOURCE_FAILED


def test_a_fragment_source_is_not_called_when_the_branch_is_not_taken():
    def broken(name):
        raise RuntimeError("should never be called")

    body = "{IF company}At {company}.{ELSE}{ai_opener}{END}"
    assert render_template(body, {"company": "Contoso"}, fragments=broken).text == (
        "At Contoso."
    )


def test_style_samples_cover_else_branches_and_alternatives():
    program = parse_template("{IF company}At {company}{ELSE}Elsewhere{END} {a|b|c}")
    samples = style_samples(program)
    assert any("Elsewhere" in sample for sample in samples)
    assert any(sample.endswith("b") for sample in samples)
    assert any(sample.endswith("c") for sample in samples)


def test_sentences_treats_line_breaks_as_boundaries():
    assert sentences("One two.\nThree four") == ["One two.", "Three four"]


def test_style_violations_reports_each_broken_rule():
    kinds = {
        violation.kind
        for violation in style_violations(f"In today's world {EM_DASH} hello.")
    }
    assert kinds == {"forbidden_dash", "filler_opener"}


def test_normalise_dashes_leaves_plain_hyphens_alone():
    assert normalise_dashes("multi-model left-to-right") == "multi-model left-to-right"


# --------------------------------------------------------------------------
# Whitespace tidying and the broken-slot guard
# --------------------------------------------------------------------------


def test_tidy_collapses_the_gap_a_dropped_branch_leaves():
    body = "Hi{IF company} at {company}{END} today."
    assert render(body, {"company": ""}).text == "Hi today."


def test_tidy_removes_the_space_before_a_stranded_comma():
    assert tidy_whitespace("Hi ,  there") == "Hi, there"


def test_tidy_collapses_blank_line_runs():
    assert tidy_whitespace("One\n\n\n\nTwo") == "One\n\nTwo"


def test_tidy_preserves_a_single_paragraph_break():
    assert render("One.\n\nTwo.", {}).text == "One.\n\nTwo."


def test_broken_punctuation_spots_a_line_starting_with_a_comma():
    assert broken_punctuation(", welcome") == ","


def test_broken_punctuation_spots_doubled_punctuation():
    assert broken_punctuation("Hi,, there") == ",,"


def test_broken_punctuation_accepts_ordinary_prose():
    assert broken_punctuation("Hi there, saw your work at Microsoft. Nice.") is None


def test_broken_punctuation_accepts_an_ellipsis():
    assert broken_punctuation("Still thinking about it...") is None


# --------------------------------------------------------------------------
# The SEQ-05 seam: AI fragments are injected, never generated here
# --------------------------------------------------------------------------


def test_hybrid_template_renders_with_supplied_fragments():
    body = "{ai_opener} I work with teams rolling out Copilot at {company}."
    message = render_template(
        body,
        LEAD_VALUES,
        fragments={"opener": "Saw your talk on eval harnesses."},
    )
    assert message.text.startswith("Saw your talk on eval harnesses.")
    assert message.fragments_used == ("opener",)


def test_fragments_can_be_keyed_by_the_full_token():
    message = render_template(
        "{ai_opener} Rest.", {}, fragments={"ai_opener": "Hello."}
    )
    assert message.text == "Hello. Rest."


def test_fragments_can_be_supplied_as_a_callable():
    calls = []

    def source(name):
        calls.append(name)
        return "Generated line."

    assert render_template("{ai_opener}", {}, fragments=source).text == "Generated line."
    assert calls == ["opener"]


@ABSENT_VALUES
def test_missing_fragment_refuses_and_is_marked_as_awaiting_ai(absent):
    result = safe_render_template("{ai_opener} Rest.", {}, fragments={"opener": absent})
    assert not result.ok
    assert result.refusal.reason is RenderRefusalReason.MISSING_AI_FRAGMENT
    assert result.refusal.is_awaiting_ai is True
    assert result.sublist == SKIPPED_SUBLIST
    assert result.refusal.detail["fragment"] == "opener"


def test_no_fragment_source_at_all_refuses():
    result = safe_render_template("{ai_opener} Rest.", {})
    assert not result.ok
    assert result.refusal.reason is RenderRefusalReason.MISSING_AI_FRAGMENT


def test_fragment_presence_can_be_guarded_like_any_token():
    body = "{IF ai_opener}{ai_opener}{ELSE}Quick hello.{END} Rest."
    assert render_template(body, {}, fragments={}).text == "Quick hello. Rest."
    assert render_template(body, {}, fragments={"opener": "Hi."}).text == "Hi. Rest."


def test_ai_fragment_with_an_em_dash_is_refused_not_normalised():
    result = safe_render_template(
        "{ai_opener} Rest.",
        {},
        fragments={"opener": f"Saw your post {EM_DASH} good stuff."},
    )
    assert not result.ok
    assert result.refusal.reason is RenderRefusalReason.STYLE_VIOLATION
    assert result.refusal.detail["violation"] == "forbidden_dash"


def test_ai_fragment_with_a_filler_opener_is_refused():
    result = safe_render_template(
        "{ai_opener} Rest.",
        {},
        fragments={"opener": "In today's world everyone ships AI."},
    )
    assert not result.ok
    assert result.refusal.detail["violation"] == "filler_opener"


def test_ai_fragment_with_an_over_long_sentence_is_refused():
    result = safe_render_template(
        "{ai_opener} Rest.",
        {},
        fragments={"opener": " ".join(["word"] * 40) + "."},
    )
    assert not result.ok
    assert result.refusal.detail["violation"] == "long_sentence"


def test_program_reports_the_fragments_seq_05_must_supply():
    program = parse_template("{ai_opener} and {ai_closer} and {ai_opener}")
    assert program.fragments() == ("closer", "opener")
    assert program.ai_tokens() == ("ai_closer", "ai_opener")
    assert program.variables() == ()


def test_static_kind_rejects_ai_tokens():
    with pytest.raises(TemplateSyntaxError) as error:
        validate_template("{ai_opener} Rest.", kind="static")
    assert "hybrid" in str(error.value)


def test_hybrid_kind_requires_an_ai_token():
    with pytest.raises(TemplateSyntaxError):
        validate_template("Just static text.", kind="hybrid")


def test_hybrid_kind_accepts_a_skeleton_with_a_fragment():
    program = validate_template("Hi {firstName}. {ai_closer}", kind="hybrid")
    assert program.fragments() == ("closer",)


def test_hybrid_kind_is_satisfied_by_the_body_alone():
    programs = compile_bodies(["Hi. {ai_closer}", "Hello there."], kind="hybrid")
    assert len(programs) == 2


def test_unknown_kind_is_rejected():
    with pytest.raises(ValueError):
        validate_template("Hi.", kind="magic")


def test_compile_bodies_rejects_an_empty_variation():
    with pytest.raises(TemplateSyntaxError):
        compile_bodies(["Hi there.", "   "])


def test_compile_bodies_rejects_no_bodies_at_all():
    with pytest.raises(TemplateSyntaxError):
        compile_bodies([])
