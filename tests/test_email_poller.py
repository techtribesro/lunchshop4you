from datetime import date
from unittest.mock import patch

import pytest

from app.services.email_poller import EmailPollError, refresh_menu

NEXT_WEEK = date(2026, 8, 31)
THIS_WEEK = date(2026, 8, 24)


def test_force_parse_rejects_mismatched_pdf_week():
    """The bug this guards against: force-parsing next week before next
    week's email has arrived used to silently grab the newest available
    email (this week's) and store it under next week's date anyway."""
    with patch("app.services.email_poller.fetch_recent_menu_sources", return_value=[("pdf", b"fake-pdf")]), \
         patch("app.services.email_poller.extract_pdf_week_start", return_value=THIS_WEEK), \
         patch("app.services.email_poller._store_week") as mock_store:
        with pytest.raises(EmailPollError, match="hasn't arrived yet"):
            refresh_menu(db=object(), target_week_start=NEXT_WEEK)
        mock_store.assert_not_called()


def test_force_parse_accepts_matching_pdf_week():
    fake_db = object()
    with patch("app.services.email_poller.fetch_recent_menu_sources", return_value=[("pdf", b"fake-pdf")]), \
         patch("app.services.email_poller.extract_pdf_week_start", return_value=NEXT_WEEK), \
         patch("app.services.email_poller._parse_menu_source", return_value=["item1", "item2"]), \
         patch("app.services.email_poller._store_week") as mock_store:
        count = refresh_menu(db=fake_db, target_week_start=NEXT_WEEK)
        assert count == 2
        mock_store.assert_called_once_with(fake_db, NEXT_WEEK, ["item1", "item2"])


def test_force_parse_falls_back_when_week_unreadable():
    """A PDF whose footer can't be parsed (extract_pdf_week_start returns
    None) still trusts the caller's target_week_start, same as before this
    fix -- there's nothing to cross-check against."""
    with patch("app.services.email_poller.fetch_recent_menu_sources", return_value=[("pdf", b"fake-pdf")]), \
         patch("app.services.email_poller.extract_pdf_week_start", return_value=None), \
         patch("app.services.email_poller._parse_menu_source", return_value=["item1"]), \
         patch("app.services.email_poller._store_week") as mock_store:
        count = refresh_menu(db=object(), target_week_start=NEXT_WEEK)
        assert count == 1
        mock_store.assert_called_once()
        assert mock_store.call_args[0][1] == NEXT_WEEK


def test_force_parse_skips_week_check_for_text_source():
    """Text-fallback sources have no footer to check at all -- unaffected
    by this fix, same trust-the-caller behavior as before."""
    with patch("app.services.email_poller.fetch_recent_menu_sources", return_value=[("text", "menu body")]), \
         patch("app.services.email_poller._parse_menu_source", return_value=["item1"]), \
         patch("app.services.email_poller._store_week") as mock_store:
        count = refresh_menu(db=object(), target_week_start=NEXT_WEEK)
        assert count == 1
        mock_store.assert_called_once()
