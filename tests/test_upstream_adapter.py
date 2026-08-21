"""Phase 3A: upstream jobspy-enhanced-scraper integration tests.

Covers exactly what STEP 8 asked for: the dependency imports, existing
correctness guarantees are unregressed, and the prohibited behaviors are
verifiably absent — not just "not called in the code I wrote today" but
actively asserted against.
"""
import ast
from pathlib import Path

from bs4 import BeautifulSoup

import dice.job_parser as job_parser_module
from dice.c2c_classifier import classify_c2c
from dice.job_parser import parse_job_detail_html
from dice.search import _build_url
from dice.upstream_adapter import (
    clean_description,
    extract_experience_text,
    extract_salary_text,
    try_next_data,
)

FIXTURES = Path(__file__).parent / "fixtures"


def _read(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _source_without_docstrings(py_file: Path) -> str:
    """Real code only — strips module/function/class docstrings via ast so
    prose *explaining* what a module avoids doesn't trip a substring check
    meant to catch actual usage. Comments are already excluded; they never
    appear in the ast in the first place."""
    tree = ast.parse(py_file.read_text(encoding="utf-8"))
    docstring_nodes = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if (
                node.body
                and isinstance(node.body[0], ast.Expr)
                and isinstance(node.body[0].value, ast.Constant)
                and isinstance(node.body[0].value.value, str)
            ):
                docstring_nodes.add(id(node.body[0]))
    lines = py_file.read_text(encoding="utf-8").splitlines(keepends=True)
    for node in ast.walk(tree):
        if isinstance(node, ast.Expr) and id(node) in docstring_nodes:
            for lineno in range(node.lineno, node.end_lineno + 1):
                lines[lineno - 1] = ""
    return "".join(lines)


# ── dependency import ────────────────────────────────────────────────────


def test_upstream_dependency_imports():
    from jobspy_enhanced.dice import Dice  # noqa: F401 — import-only check, never instantiated/called
    from jobspy_enhanced.dice import util  # noqa: F401

    assert hasattr(util, "extract_from_next_data")
    assert hasattr(util, "clean_description")
    assert hasattr(util, "extract_salary_from_description")
    assert hasattr(util, "extract_experience_from_description")


def test_we_never_import_the_dice_class_ourselves():
    """Static check on real code (docstrings stripped, so the module that
    *documents* avoiding Dice doesn't trip on its own explanation): no
    DicePilot module imports jobspy_enhanced.dice.Dice — only
    jobspy_enhanced.dice.util, via dice/upstream_adapter.py."""
    dice_dir = Path(__file__).parent.parent / "dice"
    for py_file in dice_dir.glob("*.py"):
        code = _source_without_docstrings(py_file)
        assert "from jobspy_enhanced.dice import Dice" not in code, py_file
        assert "jobspy_enhanced.dice.Dice" not in code, py_file


# ── Contract/Third Party search filter still present ────────────────────


def test_search_url_still_has_contract_third_party_filter():
    url = _build_url("Software Engineer", 1, "United States", "US")
    assert "filters.employmentType=CONTRACTS" in url or "filters.employmentType=CONTRACTS%7CTHIRD_PARTY" in url


# ── C2C statuses unchanged for existing fixtures ─────────────────────────


def test_c2c_fixtures_classify_identically_after_integration():
    positive = classify_c2c("We are open to Corp to Corp candidates for this role.")
    assert positive.status == "CONFIRMED"

    negative = classify_c2c("This is a W2 only position, no C2C accepted.")
    assert negative.status == "NOT_C2C"

    conflict = classify_c2c("We generally do Corp to Corp, but for this role: no C2C, W2 only.")
    assert conflict.status == "NOT_C2C"
    assert "corp to corp" in conflict.evidence
    assert "no c2c" in conflict.evidence


def test_negative_c2c_evidence_precedence_intact():
    result = classify_c2c("No C2C. No Corp to Corp. W2 only. No third parties. No vendors.")
    assert result.status == "NOT_C2C"


# ── Easy Apply still requires positive evidence, no inference ───────────


def test_easy_apply_detector_unmodified_no_url_absence_inference():
    from dice.easy_apply_detector import detect_easy_apply

    # No positive signal given at all (search_card_easy_apply=False) must
    # never become True just because no URL/other info was supplied.
    result = detect_easy_apply(search_card_easy_apply=False)
    assert result.is_easy_apply is False


def test_easy_apply_detector_source_has_no_url_absence_inference():
    """Static check: dice/easy_apply_detector.py never contains the
    prohibited 'no external URL -> EASY_APPLY' inference pattern."""
    text = (Path(__file__).parent.parent / "dice" / "easy_apply_detector.py").read_text()
    assert "job_url_direct" not in text
    assert "dice.com" not in text.lower()


# ── discovery must never call application-initiation URLs ───────────────


def test_no_apply_adjacent_url_pattern_anywhere_in_dice_package():
    """Static check on real code (docstrings stripped): no code path
    constructs or requests a job-applications/start-apply URL. Prose
    *explaining* that this is deliberately avoided (as in
    dice/upstream_adapter.py's docstring) is not itself a violation."""
    dice_dir = Path(__file__).parent.parent / "dice"
    for py_file in dice_dir.glob("*.py"):
        code = _source_without_docstrings(py_file)
        assert "job-applications" not in code, f"found in {py_file}"
        assert "start-apply" not in code, f"found in {py_file}"


def test_discovery_never_calls_requests_for_apply_urls(monkeypatch):
    """Runtime check: run a full discovery pass against fixture-shaped
    responses and assert every actual requests.get call target is either
    the search page or a /job-detail/ URL — never /job-applications/."""
    import dice.discovery as discovery_module
    import dice.search as search_module

    requested_urls = []

    class _FakeResponse:
        def __init__(self, text, status_code=200):
            self.text = text
            self.status_code = status_code

    search_html = _read("search_page_sample.html")
    detail_html = _read("job_detail_c2c_positive.html")

    def fake_get(url, headers=None, timeout=None):
        requested_urls.append(url)
        if "/jobs?" in url:
            return _FakeResponse(search_html)
        return _FakeResponse(detail_html)

    monkeypatch.setattr(search_module.requests, "get", fake_get)
    monkeypatch.setattr(job_parser_module.requests, "get", fake_get)
    monkeypatch.setattr(
        discovery_module, "upsert_dice_job", lambda row: {"id": "fake", **row}
    )

    discovery_module.run_discovery("Software Engineer", max_results=2, printer=lambda *_a: None)

    assert requested_urls, "expected at least one request"
    for url in requested_urls:
        assert "job-applications" not in url
        assert "start-apply" not in url
        assert "/jobs?" in url or "/job-detail/" in url


# ── upstream-backed parsing behavior ─────────────────────────────────────


def test_next_data_returns_none_when_absent():
    soup = BeautifulSoup(_read("job_detail_c2c_positive.html"), "html.parser")
    assert try_next_data(soup) is None


def test_json_ld_fallback_still_works_when_next_data_absent():
    html = _read("job_detail_c2c_positive.html")
    detail = parse_job_detail_html(html, fallback_url="https://www.dice.com/job-detail/x")
    assert detail.title == "Senior Backend Engineer"
    assert "Corp to Corp" in detail.description_text


def test_next_data_tier_used_when_present():
    html = """
    <html><body>
    <script id="__NEXT_DATA__" type="application/json">
    {"props": {"pageProps": {"jobData": {
        "title": "Platform Engineer II",
        "description": "<p>Corp to Corp candidates welcome for this role.</p>",
        "employmentType": "CONTRACTOR",
        "companyName": "NextData Co",
        "postedDate": "2026-08-20T00:00:00.000Z"
    }}}}
    </script>
    </body></html>
    """
    detail = parse_job_detail_html(html, fallback_url="https://www.dice.com/job-detail/y")
    assert detail.title == "Platform Engineer II"
    assert detail.company_name == "NextData Co"
    assert "Corp to Corp" in detail.description_text
    assert "<p>" not in detail.description_text


def test_next_data_falls_through_to_json_ld_when_incomplete():
    """__NEXT_DATA__ present but missing title/description -> must not
    ship a half-empty record; falls through to JSON-LD."""
    html = f"""
    <html><body>
    <script id="__NEXT_DATA__" type="application/json">
    {{"props": {{"pageProps": {{"jobData": {{"id": "abc"}}}}}}}}
    </script>
    {_read("job_detail_c2c_positive.html")}
    </body></html>
    """
    detail = parse_job_detail_html(html, fallback_url="https://www.dice.com/job-detail/x")
    assert detail.title == "Senior Backend Engineer"  # from JSON-LD, not __NEXT_DATA__


def test_clean_description_handles_unicode_escapes():
    raw = "Great role\\u2019s benefits include <br>health insurance."
    cleaned = clean_description(raw)
    assert "’" in cleaned or "'" in cleaned  # unicode-escape decoded, not left literal
    assert "<br>" not in cleaned


def test_salary_extraction_from_description():
    text = "This role pays $120,000 - $140,000 annually plus benefits."
    salary = extract_salary_text(text)
    assert salary is not None
    assert "120000" in salary or "120,000" in salary or "120000.0" in salary or "120000" in salary.replace(",", "")


def test_salary_extraction_returns_none_when_absent():
    assert extract_salary_text("No compensation details provided.") is None


def test_experience_extraction_from_description():
    text = "Requires 5+ years of experience in backend development."
    assert extract_experience_text(text) == "5+ years"


def test_experience_extraction_returns_none_when_absent():
    assert extract_experience_text("Great team culture, remote friendly.") is None


def test_raw_metadata_carries_salary_and_experience():
    html = _read("job_detail_c2c_positive.html")
    detail = parse_job_detail_html(html, fallback_url="https://www.dice.com/job-detail/x")
    # This fixture's description has no salary/experience phrases — both None is correct,
    # not a failure; asserts the fields exist on the model without erroring.
    assert hasattr(detail, "salary_text")
    assert hasattr(detail, "experience_text")
