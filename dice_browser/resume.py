"""Phase 4C: resume detection and upload only. No question answering, no
Next/Review/Submit -- those modules don't exist anywhere in this repo.

Uses one fixed V1 test resume, not the (not-yet-built, Phase 4E)
Candidate Adapter. The actual resume file lives outside git entirely --
see resume_path_from_env() -- and must be verified gitignored before any
real file is ever placed there.
"""
from __future__ import annotations

import os
from pathlib import Path

from playwright.sync_api import Page
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from dice_browser.models import ResumeUploadResult

RESUME_PATH_ENV_VAR = "DICEPILOT_TEST_RESUME_PATH"
DEFAULT_RESUME_PATH = Path(__file__).resolve().parent.parent / ".runtime" / "resume" / "test_resume.pdf"


def resume_path_from_env() -> Path:
    configured = os.environ.get(RESUME_PATH_ENV_VAR)
    return Path(configured) if configured else DEFAULT_RESUME_PATH


def _visible_count(locator) -> int:
    """Like locator.count(), but only counts elements actually visible on
    the page -- a hidden (display:none) DOM element whose ordinary text
    happens to contain a matched substring is not a real UI signal."""
    return sum(1 for el in locator.all() if el.is_visible())


def detect_existing_resume(page: Page) -> bool | None:
    """TRUE / FALSE / UNKNOWN (None) -- conservative; returns None
    whenever the page doesn't show a clear signal either way, rather than
    guessing.

    Verified live (Phase 4B.1 CDP-attach closure, 2026-08-21) against the
    real Dice apply wizard's resume step: the control is labeled "Change",
    never "Replace" (that original guess was stale). "Uploaded to profile"
    is the real confirmation phrase shown next to an already-on-file
    resume. A bare "Upload" text check is a false-negative trap -- the
    same wizard step also shows an unrelated "Upload your cover letter"
    prompt for the separate, optional cover-letter field; only a
    resume-specific upload prompt counts as a negative signal.

    Real live finding (2026-08-24, brand-new-account closure): a page
    with NO resume on file can still contain hidden (display:none), fully
    unrelated elements whose ordinary sentence text happens to contain
    "change" (a profile-visibility promo card, a cached job-description
    snippet) -- a plain get_by_text().count() with no visibility check
    counted those and returned a false True, blocking the real upload
    outright. Every positive signal below is now required to be visible."""
    has_change = _visible_count(page.get_by_text("Change", exact=False)) > 0
    has_uploaded_marker = _visible_count(page.get_by_text("Uploaded to profile", exact=False)) > 0
    has_marker = _visible_count(page.locator("[data-testid*='resume'], [class*='resume-card'], [class*='current-resume']")) > 0
    has_replace = _visible_count(page.get_by_text("Replace", exact=False)) > 0  # fallback for a different Dice UI variant
    if has_change or has_uploaded_marker or has_marker or has_replace:
        return True

    has_resume_upload_prompt = page.get_by_text("Upload your resume", exact=False).count() > 0
    if has_resume_upload_prompt:
        return False

    return None


def upload_resume(page: Page, resume_path: str | Path | None = None) -> ResumeUploadResult:
    # argparse (worker.py/worker_daemon.py's --resume-path) always hands
    # this through as a plain str -- normalize once here rather than at
    # every CLI call site.
    path = Path(resume_path) if resume_path is not None else resume_path_from_env()
    if not path.exists():
        return ResumeUploadResult(uploaded=False, existing_resume_detected=None, reason="RESUME_FILE_MISSING")

    existing = detect_existing_resume(page)

    file_input = _find_resume_file_input(page)

    # Real live finding (Phase 4B.1/4C.1 live upload, 2026-08-21): the
    # file input is already directly visible and usable on the real
    # wizard page -- no reveal click needed. The earlier "Change" text
    # match was actually an unrelated, hidden promotional card that
    # happens to contain the word "change" in its copy; blindly clicking
    # it hung waiting for visibility and crashed the whole upload. Fixed
    # to try the file input FIRST and only fall back to a click if it
    # isn't directly usable -- never click a text match on faith.
    if existing and (file_input is None or not file_input.is_visible()):
        change_control = page.get_by_text("Change", exact=False).first
        if change_control.count() > 0 and change_control.is_visible():
            change_control.click()
        else:
            replace_control = page.get_by_text("Replace", exact=False).first
            if replace_control.count() > 0 and replace_control.is_visible():
                replace_control.click()
        file_input = _find_resume_file_input(page)

    if file_input is None:
        # Real live finding (Phase 4C.1 corrected retry, 2026-08-21): once a
        # resume is already on file, the real wizard exposes NO reachable
        # <input type=file> at all -- the only control is a "File options"
        # menu trigger button. Fall back to the menu-based Replace flow
        # rather than failing outright, but only when that button can be
        # found deterministically (never guess).
        button = _find_resume_file_options_button(page)
        if button is not None:
            return _replace_resume_file(page, path)
        return ResumeUploadResult(
            uploaded=False, existing_resume_detected=existing, reason="UPLOAD_FAILED: no resume file input found"
        )

    file_input.set_input_files(str(path))
    try:
        page.wait_for_timeout(1500)
    except Exception:
        pass

    if _upload_succeeded(page, path.name):
        return ResumeUploadResult(uploaded=True, existing_resume_detected=existing, reason="uploaded successfully")

    return ResumeUploadResult(
        uploaded=False, existing_resume_detected=existing, reason="UPLOAD_FAILED: no success evidence"
    )


_FILE_CARD_SELECTOR = "div[class*='rounded-lg'][class*='border-gray-200'][class*='shadow-xs']"


def _find_file_options_button(page: Page, label_text: str):
    """Deterministically scope to the "File options" button belonging to
    the one card whose own direct text contains `label_text` verbatim.

    Verified live (Phase 4C.1 read-only observation, 2026-08-21) against
    the real Dice wizard: there are exactly two file-management cards,
    Resume and Cover Letter, sharing an identical class string
    (`my-2 rounded-lg border border-gray-200 bg-white p-4 shadow-xs
    mb-6`), each containing exactly one button[aria-label='File
    options']. Never a page-wide `.first` -- unrelated cards (a
    promotional widget, similar-job sidebar cards) can share similar
    rounded/border/shadow styling or carry their own unrelated "File
    options" button, so this only considers a card that has exactly one
    such button AND whose own text contains the exact label. `label_text`
    must be specific enough not to collide with an arbitrary filename
    substring -- "Resume *" (with the field's own required-marker
    asterisk), not bare "Resume", since a Cover Letter filename can
    itself contain the word "resume" (e.g. "my_resume_backup.pdf")."""
    for card in page.locator(_FILE_CARD_SELECTOR).all():
        file_options = card.locator("button[aria-label='File options']")
        if file_options.count() != 1:
            continue
        if label_text in card.inner_text():
            return file_options.first
    return None


def _find_resume_file_options_button(page: Page):
    return _find_file_options_button(page, "Resume *")


def _find_cover_letter_file_options_button(page: Page):
    return _find_file_options_button(page, "Cover letter")


def _dom_precedes(page: Page, earlier, later) -> bool:
    """True if `earlier` appears before `later` in DOM order."""
    later_handle = later.element_handle()
    if later_handle is None:
        return False
    try:
        return earlier.evaluate(
            "(el, laterEl) => !!(el.compareDocumentPosition(laterEl) & Node.DOCUMENT_POSITION_FOLLOWING)",
            later_handle,
        )
    except Exception:
        return False


def _find_resume_file_input(page: Page):
    """Scope strictly to the input belonging to the Resume field --
    never Cover Letter.

    Real live finding (Phase 4C.1 live upload incident, 2026-08-21): the
    wizard has TWO file inputs (Resume, Cover Letter) with no reliable id
    or class distinguishing them -- input[type='file'].first landed on
    whichever came first in raw DOM order, which turned out to be the
    Cover Letter one, and the V1 test resume was attached as an optional
    cover letter instead of the resume. There's no per-section container
    to scope into either (the real page is largely flat text), so this
    scopes by DOM position relative to the "Resume"/"Cover letter" text
    landmarks instead: the input must fall between them, not merely
    "before Cover Letter" (a glued-at-the-top input would satisfy that
    alone without truly belonging to the Resume field).

    Real live finding (2026-08-24, brand-new-account closure): the real
    page's "Cover letter" text substring also matches two ANCESTOR
    wrapper elements whose aggregated textContent happens to contain it
    (the card div, the form), not just the actual label -- get_by_text(
    ...).first then lands on one of those wrappers, and an input that is
    a DESCENDANT of that wrapper doesn't "precede" it, so the positional
    check failed for every candidate. Each real input also carries an
    explicit, unambiguous aria-describedby ("resume-description" /
    "cover letter-description") -- try that first; it can't be fooled by
    nested/ancestor text matches. Falls back to the older positional
    heuristic for any Dice UI variant that lacks it."""
    inp_id = page.locator("input#fsp-fileUpload")
    candidates = list(inp_id.all()) if inp_id.count() > 0 else list(page.locator("input[type='file']").all())
    if not candidates:
        return None

    described_as_resume = [
        c for c in candidates
        if (d := (c.get_attribute("aria-describedby") or "").lower()) and "resume" in d and "cover" not in d
    ]
    if len(described_as_resume) == 1:
        return described_as_resume[0]

    resume_marker = page.get_by_text("Resume *", exact=False).first
    cover_letter_marker = page.get_by_text("Cover letter", exact=False).first
    has_resume_marker = resume_marker.count() > 0
    has_cover_marker = cover_letter_marker.count() > 0

    for candidate in candidates:
        after_resume = (not has_resume_marker) or _dom_precedes(page, resume_marker, candidate)
        before_cover = (not has_cover_marker) or _dom_precedes(page, candidate, cover_letter_marker)
        if after_resume and before_cover:
            return candidate

    return None  # no input could be positively scoped to Resume -- never guess


def _open_file_options_menu(page: Page, button):
    """Click a "File options" button and return the menu it specifically
    controls, via its aria-controls attribute.

    Verified live (Phase 4C.1 read-only observation, 2026-08-21): the real
    button carries aria-controls pointing at the id of the exact menu it
    opens (React Aria's standard trigger/popup pattern). This is the
    "anchored popup relationship" required for deterministic menu
    association -- never a page-wide role=menu search, since the real page
    also has unrelated nav-dropdown role=menu elements, and the Cover
    Letter card has its own separate File-options menu right next to
    Resume's. If aria-controls is missing, or its target can't be resolved
    to exactly one element, this refuses rather than guessing.

    Real live finding (Phase 4C.1 corrected retry, 2026-08-21): a prior
    read-only observation this session had already opened the menu
    (aria-expanded="true"), and clicking an already-open trigger toggles
    it shut instead of opening it -- the click also hung waiting for a
    stable target. If the aria-controls target is already visible, this
    returns it directly without clicking anything."""
    if button is None:
        return None
    menu_id = button.get_attribute("aria-controls")
    if menu_id:
        already_open = page.locator(f"#{menu_id}")
        if already_open.count() == 1 and already_open.is_visible():
            return already_open
    button.click()
    try:
        page.wait_for_timeout(200)
    except Exception:
        pass
    menu_id = button.get_attribute("aria-controls")
    if not menu_id:
        return None
    menu = page.locator(f"#{menu_id}")
    if menu.count() != 1:
        return None
    return menu


def _replace_resume_file(page: Page, resume_path: Path, file_chooser_timeout_ms: int = 3000) -> ResumeUploadResult:
    """Corrected Phase 4C.1 flow for a resume that's already on file: open
    the Resume card's own File-options menu (never Cover Letter's), select
    exactly the "Replace" item -- scoped strictly to that menu, so it can
    never match Delete or a page-wide "Replace" match elsewhere -- then
    hand the file to whichever mechanism Replace actually triggers (a
    native file chooser, or a newly-revealed Resume-scoped file input).
    Never falls back to Cover Letter's input for any reason."""
    button = _find_resume_file_options_button(page)
    if button is None:
        return ResumeUploadResult(
            uploaded=False, existing_resume_detected=True, reason="UPLOAD_FAILED: resume File options button not found"
        )

    menu = _open_file_options_menu(page, button)
    if menu is None:
        return ResumeUploadResult(
            uploaded=False,
            existing_resume_detected=True,
            reason="UPLOAD_FAILED: resume menu could not be associated deterministically",
        )

    replace_item = menu.get_by_role("menuitem", name="Replace", exact=True)
    if replace_item.count() != 1:
        return ResumeUploadResult(
            uploaded=False, existing_resume_detected=True, reason="UPLOAD_FAILED: Replace menu item not found"
        )

    try:
        with page.expect_file_chooser(timeout=file_chooser_timeout_ms) as fc_info:
            replace_item.first.click()
        fc_info.value.set_files(str(resume_path))
    except PlaywrightTimeoutError:
        # No native file chooser fired -- fall back to a Resume-scoped
        # input Replace may have revealed instead. Never Cover Letter's.
        resume_input = _find_resume_file_input(page)
        if resume_input is None or not resume_input.is_visible():
            return ResumeUploadResult(
                uploaded=False,
                existing_resume_detected=True,
                reason="UPLOAD_FAILED: no file chooser or resume-scoped input appeared after Replace",
            )
        resume_input.set_input_files(str(resume_path))

    try:
        page.wait_for_timeout(1500)
    except Exception:
        pass

    if _upload_succeeded(page, resume_path.name):
        return ResumeUploadResult(uploaded=True, existing_resume_detected=True, reason="uploaded successfully via Replace")

    return ResumeUploadResult(
        uploaded=False,
        existing_resume_detected=True,
        reason="UPLOAD_FAILED: no success evidence in Resume card after Replace",
    )


def _find_file_card(page: Page, label_text: str):
    """The one file-management card whose own text contains `label_text`
    verbatim -- same structural selector as _find_file_options_button,
    but returns the card itself rather than its button."""
    for card in page.locator(_FILE_CARD_SELECTOR).all():
        if label_text in card.inner_text():
            return card
    return None


def _upload_succeeded(page: Page, filename: str) -> bool:
    # Card-scoped: only counts if `filename` appears inside the Resume
    # card's own subtree -- never inferred from Cover Letter's card, and
    # never from a page-wide match.
    #
    # Real live finding (Phase 4C.1 corrected retry, 2026-08-21): the
    # earlier version scoped by DOM order relative to "Resume *"/"Cover
    # letter" text landmarks. That broke on the real wizard because "Cover
    # letter" matches twice (a stepper/nav step label plus the field's own
    # label) and `.first` picked the nav one, which sits earlier in raw
    # DOM order than the actual Resume field content -- collapsing the
    # before/after window to nothing and reporting a genuine upload as
    # failed. Card containment sidesteps DOM-order fragility entirely.
    resume_card = _find_file_card(page, "Resume *")
    if resume_card is not None:
        if filename in resume_card.inner_text():
            return True
        return resume_card.locator("[class*='upload-complete'], [data-testid*='upload-success']").count() > 0

    # No card structure at all (e.g. a simpler/older UI variant, or the
    # offline fixtures that predate the card-based wizard shape) -- fall
    # back to the DOM-order marker scoping this module already relies on
    # elsewhere.
    resume_marker = page.get_by_text("Resume *", exact=False).first
    cover_letter_marker = page.get_by_text("Cover letter", exact=False).first
    has_resume_marker = resume_marker.count() > 0
    has_cover_marker = cover_letter_marker.count() > 0

    for evidence in (
        list(page.get_by_text(filename, exact=False).all())
        + list(page.locator("[class*='upload-complete'], [data-testid*='upload-success']").all())
    ):
        after_resume = (not has_resume_marker) or _dom_precedes(page, resume_marker, evidence)
        before_cover = (not has_cover_marker) or _dom_precedes(page, evidence, cover_letter_marker)
        if after_resume and before_cover:
            return True
    return False
