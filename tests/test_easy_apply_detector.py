from dice.easy_apply_detector import detect_easy_apply


def test_search_card_only_true():
    result = detect_easy_apply(search_card_easy_apply=True)
    assert result.is_easy_apply is True
    assert result.evidence["source"] == "search_card_only"
    assert result.evidence["detail_page_checked"] is False


def test_search_card_only_false():
    result = detect_easy_apply(search_card_easy_apply=False)
    assert result.is_easy_apply is False


def test_search_and_detail_agree_true():
    result = detect_easy_apply(search_card_easy_apply=True, detail_page_easy_apply=True)
    assert result.is_easy_apply is True
    assert result.evidence["conflict"] is False


def test_search_and_detail_agree_false():
    result = detect_easy_apply(search_card_easy_apply=False, detail_page_easy_apply=False)
    assert result.is_easy_apply is False
    assert result.evidence["conflict"] is False


def test_conflict_detail_page_overrides_search_card():
    # Search card said yes, detail page says no -> trust the detail page,
    # but record the conflict rather than silently picking one.
    result = detect_easy_apply(search_card_easy_apply=True, detail_page_easy_apply=False)
    assert result.is_easy_apply is False
    assert result.evidence["conflict"] is True
    assert result.evidence["source"] == "detail_page_overrides_search_card"


def test_never_assumes_true_by_default():
    result = detect_easy_apply(search_card_easy_apply=False, detail_page_easy_apply=None)
    assert result.is_easy_apply is False
