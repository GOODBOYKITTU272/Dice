from dice.qualification import is_contract, qualify_job


# 1-3. Contract detection from raw employment-type text.
def test_contract_counter_plain_contract():
    assert is_contract("Contract") is True


def test_contract_counter_fulltime_plus_contract():
    assert is_contract("Full-time, Contract") is True


def test_contract_counter_fulltime_only_does_not_count():
    assert is_contract("Full-time") is False


def test_contract_counter_third_party_plus_contract_either_order():
    assert is_contract("Third Party, Contract") is True
    assert is_contract("Contract, Third Party") is True


def test_contract_counter_empty_or_none():
    assert is_contract("") is False
    assert is_contract(None) is False


# 4-5. Third Party is a separate signal — never inferred from Contract alone.
def test_third_party_false_with_no_explicit_evidence():
    # A plain "Contract" job with is_third_party=False passed in must not
    # become qualified via the funnel check unless Contract itself counts
    # (it does), but is_third_party itself must stay exactly what was given.
    result = qualify_job("Contract", is_third_party=False, c2c_status="CONFIRMED", is_easy_apply=True)
    # Qualifies via Contract, not because Third Party was invented.
    assert result.is_qualified is True


def test_contract_does_not_automatically_imply_third_party():
    # is_third_party is an input, not derived here — qualify_job must not
    # silently flip a False to True just because employment_type says Contract.
    result = qualify_job("Contract", is_third_party=False, c2c_status="UNKNOWN", is_easy_apply=True)
    assert "Not Contract/Third Party" not in result.reason  # funnel passed via Contract
    assert result.is_qualified is False  # but still fails on C2C unknown


# 6-8. Individual disqualifying conditions.
def test_unknown_c2c_is_not_qualified():
    result = qualify_job("Contract", is_third_party=False, c2c_status="UNKNOWN", is_easy_apply=True)
    assert result.is_qualified is False
    assert "C2C unknown" in result.reason


def test_not_c2c_is_not_qualified():
    result = qualify_job("Contract", is_third_party=False, c2c_status="NOT_C2C", is_easy_apply=True)
    assert result.is_qualified is False
    assert "Not C2C" in result.reason


def test_easy_apply_false_is_not_qualified():
    result = qualify_job("Contract", is_third_party=False, c2c_status="CONFIRMED", is_easy_apply=False)
    assert result.is_qualified is False
    assert "Not Easy Apply" in result.reason


def test_not_contract_or_third_party_is_not_qualified():
    result = qualify_job("Full-time", is_third_party=False, c2c_status="CONFIRMED", is_easy_apply=True)
    assert result.is_qualified is False
    assert "Not Contract/Third Party" in result.reason


# 9-11. Positive qualifying combinations.
def test_confirmed_contract_easy_apply_qualifies():
    result = qualify_job("Contract", is_third_party=False, c2c_status="CONFIRMED", is_easy_apply=True)
    assert result.is_qualified is True
    assert result.reason == "eligible"


def test_likely_contract_easy_apply_qualifies():
    result = qualify_job("Contract", is_third_party=False, c2c_status="LIKELY", is_easy_apply=True)
    assert result.is_qualified is True


def test_confirmed_third_party_easy_apply_qualifies():
    # Third Party (not Contract) satisfying the funnel on its own.
    result = qualify_job("Third Party", is_third_party=True, c2c_status="CONFIRMED", is_easy_apply=True)
    assert result.is_qualified is True


# 13. Stored vs Qualified are independent booleans (Stored = row upserted
# into dice_jobs; Qualified = deterministic judgment computed from it).
def test_stored_and_qualified_are_independent_concepts():
    # A job can be fully stored (this function has nothing to do with
    # storage) while definitively not qualified — the two must never be
    # conflated into one flag.
    result = qualify_job("Contract", is_third_party=False, c2c_status="UNKNOWN", is_easy_apply=True)
    assert result.is_qualified is False  # storage is a separate fact (discovery.py's upsert), unaffected by this


# 14. The exact verified Dexian DISYS case from the human visual review.
def test_verified_dexian_style_case_stored_not_qualified():
    result = qualify_job(
        employment_type_text="Contract",
        is_third_party=False,
        c2c_status="UNKNOWN",
        is_easy_apply=True,
    )
    assert result.is_qualified is False
    assert result.reason == "C2C unknown"


def test_randstad_style_case_multiple_reasons():
    # UNKNOWN C2C *and* Easy Apply false at once — reason must mention both.
    result = qualify_job(
        employment_type_text="Contract",
        is_third_party=False,
        c2c_status="UNKNOWN",
        is_easy_apply=False,
    )
    assert result.is_qualified is False
    assert "C2C unknown" in result.reason
    assert "Not Easy Apply" in result.reason


def test_strategic_staffing_style_case_not_c2c_reason():
    result = qualify_job(
        employment_type_text="Full-time, Contract",
        is_third_party=False,
        c2c_status="NOT_C2C",
        is_easy_apply=True,
    )
    assert result.is_qualified is False
    assert result.reason == "Not C2C"
