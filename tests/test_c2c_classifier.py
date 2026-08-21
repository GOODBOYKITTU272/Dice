from dice.c2c_classifier import classify_c2c


def test_c2c_positive_evidence_confirms():
    result = classify_c2c("We are open to Corp to Corp candidates for this role.")
    assert result.status == "CONFIRMED"
    assert "corp to corp" in result.evidence


def test_c2c_positive_evidence_c2c_abbreviation():
    result = classify_c2c("C2C is fine for the right candidate.")
    assert result.status == "CONFIRMED"
    assert "c2c" in result.evidence


def test_c2c_positive_does_not_match_substring():
    # "c2c" must be word-boundaried — it shouldn't fire inside an unrelated token.
    result = classify_c2c("Contact abc2client for details, no other info given.")
    assert result.status == "UNKNOWN"


def test_c2c_negative_evidence_not_c2c():
    result = classify_c2c("This is a W2 only position, no C2C accepted.")
    assert result.status == "NOT_C2C"
    assert "w2 only" in result.evidence
    assert "no c2c" in result.evidence


def test_c2c_negative_no_third_parties():
    result = classify_c2c("Direct hire only, no third parties, no vendors.")
    assert result.status == "NOT_C2C"
    assert "no third parties" in result.evidence
    assert "no vendors" in result.evidence


def test_c2c_conflicting_evidence_negative_overrides():
    # Both a strong positive phrase and an explicit negation appear —
    # negative must win per the V1 decision.
    text = "We generally do Corp to Corp, but for this role: no C2C, W2 only."
    result = classify_c2c(text)
    assert result.status == "NOT_C2C"
    assert "corp to corp" in result.evidence
    assert "no c2c" in result.evidence


def test_c2c_unknown_when_no_evidence():
    result = classify_c2c("Join our team and grow your career with us.")
    assert result.status == "UNKNOWN"
    assert result.evidence == []


def test_c2c_likely_from_employment_type_when_description_silent():
    result = classify_c2c(
        "Looking for an experienced engineer to join the team.",
        employment_type_text="Contract, Third Party",
    )
    assert result.status == "LIKELY"
    assert "employment_type=Contract, Third Party" in result.evidence


def test_c2c_explicit_phrase_beats_employment_type_signal():
    # Explicit description evidence should still win over/alongside the
    # weaker structural employment_type signal.
    result = classify_c2c(
        "Subcontractor arrangements are welcome.",
        employment_type_text="Contract, Third Party",
    )
    assert result.status == "CONFIRMED"
    assert "subcontractor" in result.evidence


def test_c2c_subcontractor_and_contract_corp_phrases():
    r1 = classify_c2c("We work with subcontractors regularly.")
    assert r1.status == "CONFIRMED"
    assert "subcontractor" in r1.evidence

    r2 = classify_c2c("This is a contract corp arrangement.")
    assert r2.status == "CONFIRMED"
    assert "contract corp" in r2.evidence


def test_c2c_third_party_candidates_and_vendors_phrases():
    r1 = classify_c2c("We accept third party candidates for this position.")
    assert r1.status == "CONFIRMED"
    assert "third party candidates" in r1.evidence

    r2 = classify_c2c("Open to third party vendors as well.")
    assert r2.status == "CONFIRMED"
    assert "third party vendors" in r2.evidence


# ── Phase 3C: broadened negative-evidence refusal frames ────────────────
# Real Dice job 5c2d489c-327d-4a69-8fd3-95b46c004d68 and
# 173695bb-b7db-427e-b1a9-7b7e8ba0cd20 (see STATE.md Phase 3B) were
# misclassified CONFIRMED because the old negative list only recognized a
# handful of literal "no X" phrases. These tests cover the broadened,
# still-bounded refusal-verb frames added in Phase 3C.


def test_c2c_negative_not_accepting_phrasing():
    result = classify_c2c("We are not accepting C2C or 1099 arrangements.")
    assert result.status == "NOT_C2C"


def test_c2c_negative_no_modifier_target_permitted_phrasing():
    result = classify_c2c("Contract Direct W2 (No 3rd Party Subcontractors Permitted)")
    assert result.status == "NOT_C2C"


def test_c2c_negative_3rd_party_with_ordinal_sup_split():
    # Real job 173695bb-b7db-427e-b1a9-7b7e8ba0cd20's live HTML marks the
    # ordinal suffix as "3<sup>rd</sup> Party" — the whitespace-boundary
    # fix in upstream_adapter turns that into "3 rd Party" (a real tag
    # boundary), not "3rdParty". The target pattern must tolerate that.
    result = classify_c2c("Contract Direct W2 (No 3 rd Party Subcontractors Permitted)")
    assert result.status == "NOT_C2C"


def test_c2c_negative_post_negation_not_accepted_allowed_permitted():
    assert classify_c2c("C2C not accepted").status == "NOT_C2C"
    assert classify_c2c("C2C not allowed").status == "NOT_C2C"
    assert classify_c2c("C2C not permitted").status == "NOT_C2C"
    assert classify_c2c("Vendors not accepted").status == "NOT_C2C"
    assert classify_c2c("Subcontractors not permitted").status == "NOT_C2C"


def test_c2c_negative_cannot_unable_to_accept_phrasing():
    assert classify_c2c("Cannot accept Corp to Corp").status == "NOT_C2C"
    assert classify_c2c("Unable to accept C2C candidates").status == "NOT_C2C"


def test_c2c_negative_no_vendors_no_subcontractors_direct():
    assert classify_c2c("No vendors").status == "NOT_C2C"
    assert classify_c2c("Vendors not accepted").status == "NOT_C2C"
    assert classify_c2c("No subcontractors").status == "NOT_C2C"
    assert classify_c2c("Subcontractors not permitted").status == "NOT_C2C"


def test_c2c_negative_direct_w2_only_phrasing_variants():
    # These already matched the pre-existing "w2 only" pattern as a
    # substring — asserted explicitly here as part of the Phase 3C
    # required-case list, not because the pattern changed.
    assert classify_c2c("Direct W2 only").status == "NOT_C2C"
    assert classify_c2c("Must work on W2 only").status == "NOT_C2C"


def test_c2c_negative_overrides_positive_in_compound_sentence():
    result = classify_c2c(
        "C2C experience preferred, but we are not accepting C2C candidates for this role."
    )
    assert result.status == "NOT_C2C"

    result2 = classify_c2c(
        "Previously worked C2C candidates welcome to apply, but this position is W2 only."
    )
    assert result2.status == "NOT_C2C"


def test_c2c_legitimate_positives_still_confirm():
    assert classify_c2c("C2C/1099 Contract").status == "CONFIRMED"
    assert classify_c2c("W2 or C2C").status == "CONFIRMED"
    assert classify_c2c("C2C candidates accepted").status == "CONFIRMED"


def test_c2c_overmatching_guard_experience_requirement_phrasing():
    # These all contain a negation near a target word, but refuse an
    # *experience requirement*, not the C2C/third-party arrangement
    # itself — must not be misread as a refusal.
    r1 = classify_c2c("We do not require previous C2C consulting experience.")
    assert r1.status != "NOT_C2C"

    r2 = classify_c2c("No prior C2C experience required.")
    assert r2.status != "NOT_C2C"

    r3 = classify_c2c(
        "The candidate does not need experience managing third-party vendors."
    )
    assert r3.status != "NOT_C2C"
