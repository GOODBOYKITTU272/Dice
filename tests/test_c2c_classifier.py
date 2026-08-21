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
