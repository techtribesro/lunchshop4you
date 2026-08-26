from datetime import date
from unittest.mock import patch

import pytest

from app.services.email_poller import EmailPollError, find_pdf_for_week, refresh_menu

NEXT_WEEK = date(2026, 8, 31)
THIS_WEEK = date(2026, 8, 24)


def _msg(has_pdf: bool):
    """A stand-in for email.message.Message -- find_pdf_for_week only ever
    passes these to _find_pdf_attachment, which is mocked directly below,
    so the object's own identity is all that matters for these tests."""
    return object() if has_pdf else "no-pdf-marker"


def test_find_pdf_for_week_skips_non_matching_and_non_pdf_messages():
    """The bug this guards against: the old code took the single newest
    message of any kind (PDF or plain text) and either trusted it blindly
    or, after a first fix, still accepted a mismatched/text message because
    it never looked past the first one. This walks past a text-only
    message, a PDF for the wrong week, and finds the real match further
    back -- confirmed necessary in production, not just theoretical."""
    text_msg, wrong_week_msg, right_week_msg = _msg(False), _msg(True), _msg(True)

    def fake_find_pdf_attachment(msg):
        return b"pdf-bytes" if msg is not text_msg else None

    with patch("app.services.email_poller._iter_recent_messages", return_value=[text_msg, wrong_week_msg, right_week_msg]), \
         patch("app.services.email_poller._find_pdf_attachment", side_effect=fake_find_pdf_attachment), \
         patch("app.services.email_poller.extract_pdf_week_start", side_effect=[THIS_WEEK, NEXT_WEEK]) as mock_extract:
        result = find_pdf_for_week(NEXT_WEEK)
        assert result == b"pdf-bytes"
        assert mock_extract.call_count == 2  # skipped straight past the text message


def test_find_pdf_for_week_raises_when_nothing_matches():
    with patch("app.services.email_poller._iter_recent_messages", return_value=[_msg(True)]), \
         patch("app.services.email_poller._find_pdf_attachment", return_value=b"pdf-bytes"), \
         patch("app.services.email_poller.extract_pdf_week_start", return_value=THIS_WEEK):
        with pytest.raises(EmailPollError, match="hasn't arrived yet"):
            find_pdf_for_week(NEXT_WEEK)


def test_refresh_menu_force_parse_uses_find_pdf_for_week():
    fake_db = object()
    with patch("app.services.email_poller.find_pdf_for_week", return_value=b"pdf-bytes") as mock_find, \
         patch("app.services.email_poller._parse_menu_source", return_value=["item1", "item2"]), \
         patch("app.services.email_poller._store_week") as mock_store:
        count = refresh_menu(db=fake_db, target_week_start=NEXT_WEEK)
        assert count == 2
        mock_find.assert_called_once_with(NEXT_WEEK)
        mock_store.assert_called_once_with(fake_db, NEXT_WEEK, ["item1", "item2"])


def test_refresh_menu_force_parse_propagates_not_found():
    with patch("app.services.email_poller.find_pdf_for_week", side_effect=EmailPollError("nope")), \
         patch("app.services.email_poller._store_week") as mock_store:
        with pytest.raises(EmailPollError):
            refresh_menu(db=object(), target_week_start=NEXT_WEEK)
        mock_store.assert_not_called()
