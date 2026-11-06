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


class RedrawTest(unittest.TestCase):
    # A line wider than the terminal takes several rows: the redraw must climb all of them,
    # or the top line is copied again at every refresh (seen during "Linking the apps").
    def test_wrapped_line_counts_its_rows(self):
        out = io.StringIO()
        t = tui.Term(read_key=lambda: "", out=out, color=True)
        t.columns = 20
        t._draw(["x" * 45, "short"])
        t._draw(["again"])
        self.assertIn("\x1b[4F", out.getvalue())  # 3 rows for the long line + 1

    def test_colour_codes_do_not_count(self):
        out = io.StringIO()
        t = tui.Term(read_key=lambda: "", out=out, color=True)
        t.columns = 20
        t._draw([t.style("x" * 15, bold=True, fg="green")])
        t._draw(["again"])
        self.assertIn("\x1b[1F", out.getvalue())

    def test_pad_ignores_colour(self):
        t = tui.Term(read_key=lambda: "", out=io.StringIO(), color=True)
        self.assertEqual(tui.visible_len(t.pad(t.style("Seerr", bold=True), 12)), 12)


class PageTest(unittest.TestCase):
    def test_each_page_starts_on_a_clear_screen(self):
        t, out = term(color=True)
        t.page("Votre compte", "Un seul compte.", step=(2, 3), recap=[("Dossier", "/srv/plaid")])
        text = out.getvalue()
        self.assertIn("\x1b[H\x1b[2J", text)
        self.assertIn("2/3", text)
        self.assertIn("/srv/plaid", text)

    def test_fullscreen_restores_the_terminal(self):
        t, out = term(color=True)
        with t.fullscreen():
            t.line("question")
        self.assertTrue(out.getvalue().startswith("\x1b[?1049h"))
        self.assertTrue(out.getvalue().endswith("\x1b[?1049l"))

    def test_fullscreen_restores_even_on_ctrl_c(self):
        t, out = term(color=True)
        with self.assertRaises(KeyboardInterrupt):
            with t.fullscreen():
                raise KeyboardInterrupt
        self.assertTrue(out.getvalue().endswith("\x1b[?1049l"))

    def test_no_screen_codes_without_colour(self):
        t, out = term()
        with t.fullscreen():
            t.page("Titre", step=(1, 3))
        self.assertNotIn("\x1b[", out.getvalue())

    def test_pause_waits_for_a_key(self):
        t, _ = term(ENTER, "x")
        t.pause("Entrée pour revenir")
        self.assertEqual(t.read_key(), "x")
