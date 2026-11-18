from __future__ import annotations

import unittest

from mailbackup.imap import (
    decode_imap_utf7,
    imap_mailbox_arg,
    parse_list_response,
)
from tests.support import ROOT  # noqa: F401


class _Conn:
    """A stand-in for the subset imap_mailbox_arg inspects."""


class MailboxArgTests(unittest.TestCase):
    def test_nested_name_with_slash_is_a_valid_atom(self):
        # '/' is not an IMAP atom-special, so the hierarchy name can be sent
        # unquoted; quoting it would also be valid but is unnecessary.
        self.assertEqual(imap_mailbox_arg(_Conn(), "Read/Root"), "Read/Root")

    def test_spaces_are_quoted(self):
        self.assertEqual(
            imap_mailbox_arg(_Conn(), "Read Root"), '"Read Root"'
        )

    def test_quotes_and_backslashes_are_escaped(self):
        self.assertEqual(
            imap_mailbox_arg(_Conn(), 'Odd "name"'), '"Odd \\"name\\""'
        )
        self.assertEqual(
            imap_mailbox_arg(_Conn(), "Back\\slash"), '"Back\\\\slash"'
        )

    def test_wildcards_and_brackets_are_quoted(self):
        for name in ("100%", "star*", "[brackets]", "{brace}"):
            self.assertTrue(
                imap_mailbox_arg(_Conn(), name).startswith('"'), name
            )

    def test_modern_imaplib_quotes_itself(self):
        class ModernConn:
            _astring = object()

        # When the attribute exists, imaplib does the quoting.
        self.assertEqual(imap_mailbox_arg(ModernConn(), "Read Root"), "Read Root")


class ListParsingTests(unittest.TestCase):
    def test_nested_quoted_name(self):
        folders = parse_list_response(
            [b'(\\HasNoChildren) "/" "Read/Root"']
        )
        self.assertEqual(len(folders), 1)
        self.assertEqual(folders[0].name, "Read/Root")
        self.assertEqual(folders[0].select_name, "Read/Root")
        self.assertTrue(folders[0].selectable)

    def test_noselect_parent_and_selectable_child(self):
        folders = parse_list_response(
            [
                b'(\\Noselect \\HasChildren) "/" "Read"',
                b'(\\HasNoChildren) "/" "Read/Root"',
            ]
        )
        self.assertFalse(folders[0].selectable)
        self.assertTrue(folders[1].selectable)

    def test_nil_delimiter(self):
        folders = parse_list_response([b'(\\HasNoChildren) NIL "Flat"'])
        self.assertEqual(folders[0].name, "Flat")

    def test_modified_utf7_name_is_decoded_for_display(self):
        # "Café" in modified UTF-7 is Caf&AOk-
        folders = parse_list_response(
            [b'(\\HasNoChildren) "/" "Caf&AOk-"']
        )
        self.assertEqual(folders[0].name, "Café")
        self.assertEqual(folders[0].select_name, "Caf&AOk-")


class Utf7Tests(unittest.TestCase):
    def test_ampersand_and_shift_sequences(self):
        self.assertEqual(decode_imap_utf7("A&B"), "A&B")
        self.assertEqual(decode_imap_utf7("Caf&AOk-"), "Café")
        self.assertEqual(decode_imap_utf7("Cafe&AOk-"), "Cafeé")


if __name__ == "__main__":
    unittest.main()
