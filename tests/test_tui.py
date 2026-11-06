import io
import unittest

from installer import tui

UP, DOWN, LEFT, RIGHT, ENTER, BACKSPACE = "\x1b[A", "\x1b[B", "\x1b[D", "\x1b[C", "\r", "\x7f"


def keys(*seq):
    """A key reader fed from a script, as the raw terminal would deliver them."""
    stream = iter(tui.split_keys("".join(seq)))
    return lambda: next(stream)


def term(*seq, color=False):
    out = io.StringIO()
    return tui.Term(read_key=keys(*seq), out=out, color=color), out


class KeysTest(unittest.TestCase):
    def test_split_keys(self):
        self.assertEqual(tui.split_keys("ab\x1b[A\r"), ["a", "b", "up", "enter"])
        self.assertEqual(tui.split_keys("\x7f\x1b[D\x1b[C\x1b[B\x03"), ["backspace", "left", "right", "down", "ctrl-c"])


class SelectTest(unittest.TestCase):
    def test_arrows_then_enter(self):
        t, _ = term(DOWN, DOWN, UP, ENTER)
        self.assertEqual(t.select("VPN", [("a", "A"), ("b", "B"), ("c", "C")]), "b")

    def test_wraps_and_starts_on_default(self):
        t, _ = term(UP, ENTER)
        self.assertEqual(t.select("VPN", [("a", "A"), ("b", "B"), ("c", "C")], default="a"), "c")

    def test_ctrl_c_aborts(self):
        t, _ = term("\x03")
        with self.assertRaises(KeyboardInterrupt):
            t.select("VPN", [("a", "A")])


class TextTest(unittest.TestCase):
    def test_default_on_enter(self):
        t, _ = term(ENTER)
        self.assertEqual(t.text("Dossier", default="/home/zoe/Plaid"), "/home/zoe/Plaid")

    def test_typing_and_backspace(self):
        t, _ = term("zoex", BACKSPACE, ENTER)
        self.assertEqual(t.text("Identifiant", default="admin"), "zoe")

    def test_validation_asks_again_and_shows_why(self):
        t, out = term("abc", ENTER, "longenough", ENTER)
        check = lambda v: "Trop court" if len(v) < 8 else None
        self.assertEqual(t.text("Mot de passe", secret=True, validate=check), "longenough")
        self.assertIn("Trop court", out.getvalue())

    def test_secret_is_masked(self):
        t, out = term("hunter22", ENTER)
        t.text("Mot de passe", secret=True)
        self.assertNotIn("hunter22", out.getvalue())
        self.assertIn("•" * 8, out.getvalue())


class ConfirmTest(unittest.TestCase):
    def test_default_no(self):
        t, _ = term(ENTER)
        self.assertFalse(t.confirm("Supprimer ?", yes="Oui", no="Non"))

    def test_arrow_to_yes(self):
        t, _ = term(LEFT, ENTER)
        self.assertTrue(t.confirm("Supprimer ?", yes="Oui", no="Non"))


class ColorTest(unittest.TestCase):
    def test_no_escape_codes_without_color(self):
        t, out = term()
        t.banner("Plaid", "Sept applis")
        self.assertNotIn("\x1b[", out.getvalue())
        self.assertIn("Plaid", out.getvalue())


if __name__ == "__main__":
    unittest.main()
