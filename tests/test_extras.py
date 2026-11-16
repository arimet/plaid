import json
import os
import tempfile
import unittest
from unittest import mock

from installer import apps, extras
from installer.docker import RESTORED_STATE
from installer.settings import Settings

HOST = "jellyfin:8096"
FT_REPO = "https://www.iamparadox.dev/jellyfin/plugins/manifest.json"


def owner():
    return Settings(data_dir="/srv/arr", username="zoe", password="owner-pw", vpn_provider=None, lang="fr",
                    home="/home/zoe", platform="linux")


class CatalogTest(unittest.TestCase):
    def test_ids(self):
        self.assertEqual([x["id"] for x in extras.CATALOG],
                         ["theme", "enhanced", "mediabar", "introdb", "episodes", "homesections", "genremapper"])

    def test_dependencies_come_along(self):
        self.assertEqual(extras.packages_for(["enhanced"]),
                         {"Jellyfin Enhanced", "Custom Tabs", "Plugin Pages", "File Transformation"})
        self.assertEqual(extras.packages_for(["theme"]), set())

    def test_theme_is_not_a_plugin(self):
        self.assertFalse(extras.has_plugins(["theme"]))
        self.assertTrue(extras.has_plugins(["theme", "introdb"]))


class ThemeTest(unittest.TestCase):
    def test_line_goes_first_and_leaves_alone(self):
        on = extras.with_theme("body{color:red}", True)
        self.assertTrue(on.startswith(extras.THEME_LINE))
        self.assertIn("body{color:red}", on)
        self.assertEqual(extras.with_theme(on, True), on)
        self.assertEqual(extras.with_theme(on, False), "body{color:red}")
        self.assertEqual(extras.with_theme("", False), "")


class StateTest(unittest.TestCase):
    def test_empty_state_and_round_trip(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(extras.load_state(d), {"chosen": [], "installed": [], "removed_for_update": []})
            extras.save_state({"chosen": ["theme"], "installed": [], "removed_for_update": []}, d)
            self.assertEqual(extras.load_state(d)["chosen"], ["theme"])
        self.assertIn(extras.STATE_FILE, RESTORED_STATE)
