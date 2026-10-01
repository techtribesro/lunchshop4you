from datetime import date

from app.services import menu_llm_extractor


def _with_footer_text(monkeypatch, text: str) -> None:
    monkeypatch.setattr(menu_llm_extractor, "_pdf_text", lambda pdf_bytes: text)


def test_extract_pdf_week_start_normal_monday_start(monkeypatch):
    """A normal week's footer already starts on the Monday -- snapping to
    that week's Monday must be a no-op."""
    _with_footer_text(monkeypatch, "Týden 5.10. - 9.10.2026")
    assert menu_llm_extractor.extract_pdf_week_start(b"") == date(2026, 10, 5)


def test_extract_pdf_week_start_holiday_shifted_footer_snaps_to_monday(monkeypatch):
    """When Monday is a public holiday, the vendor's footer sometimes starts
    from the first open day (here Wednesday) instead of the calendar
    Monday. The stored week key must still be that week's actual Monday --
    the same key week_start()/menu_by_day() look items up under -- or a
    correctly-parsed week silently never shows up anywhere."""
    _with_footer_text(monkeypatch, "Týden 7.10. - 9.10.2026")
    assert menu_llm_extractor.extract_pdf_week_start(b"") == date(2026, 10, 5)


def test_extract_pdf_week_start_no_footer_match_returns_none(monkeypatch):
    _with_footer_text(monkeypatch, "no week range here")
    assert menu_llm_extractor.extract_pdf_week_start(b"") is None
