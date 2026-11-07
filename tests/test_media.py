import re
import unittest

from installer import media
from installer.settings import Settings


def acct(**kw):
    base = dict(data_dir="/srv/arr", username="admin", password="longenough", vpn_provider=None, lang="fr")
    base.update(kw)
    return Settings(**base)


def quality(qid, name, source, resolution):
    return {"quality": {"id": qid, "name": name, "source": source, "resolution": resolution, "modifier": "none"},
            "items": [], "allowed": False}


# Trimmed from the live /qualityprofile/schema (Radarr 6.4, Sonarr 4.0): same shape, fewer qualities.
RADARR_SCHEMA = {"name": "", "upgradeAllowed": False, "cutoff": 0, "minFormatScore": 0, "cutoffFormatScore": 0,
                 "minUpgradeFormatScore": 1, "formatItems": [], "language": {"id": -2, "name": "Original"},
                 "items": [quality(1, "SDTV", "tv", 480), quality(5, "WEBDL-720p", "webdl", 720),
                           quality(9, "HDTV-1080p", "tv", 1080),
                           {"name": "WEB 1080p", "id": 1001, "allowed": False,  # the schema's own groups
                            "items": [quality(3, "WEBDL-1080p", "webdl", 1080),
                                      quality(15, "WEBRip-1080p", "webrip", 1080)]},
                           quality(7, "Bluray-1080p", "bluray", 1080),
                           quality(30, "Remux-1080p", "bluray", 1080), quality(18, "WEBDL-2160p", "webdl", 2160),
                           quality(31, "Remux-2160p", "bluray", 2160), quality(22, "BR-DISK", "bluray", 1080),
                           quality(10, "Raw-HD", "tv", 1080)]}
SONARR_SCHEMA = {"name": "", "upgradeAllowed": False, "cutoff": 0, "minFormatScore": 0, "cutoffFormatScore": 0,
                 "minUpgradeFormatScore": 1, "formatItems": [],
                 "items": [quality(4, "HDTV-720p", "television", 720), quality(18, "WEBDL-2160p", "web", 2160),
                           quality(19, "Bluray-2160p", "bluray", 2160),
                           quality(21, "Bluray-2160p Remux", "blurayRaw", 2160)]}
IDS = {"lang": 11, "original": 12, "multi": 13, "subbed": 14}


def spec(fmt, implementation):
    return [s for s in fmt["specifications"] if s["implementation"] == implementation]


def lang_value(fmt):
    return spec(fmt, "LanguageSpecification")[0]["fields"][0]["value"]


def regex_of(fmt):
    return re.compile(spec(fmt, "ReleaseTitleSpecification")[0]["fields"][0]["value"], re.I)


class DefaultsTest(unittest.TestCase):
    def test_defaults_follow_plaid_language(self):
        e = media.effective(acct(lang="fr"))
        self.assertEqual((e.audio_lang, e.profiles, e.default_profile, e.subtitle_langs, e.metadata_lang),
                         ("fr", ["mine-1080p"], "mine-1080p", ["fr", "en"], "fr"))
        self.assertEqual(media.effective(acct(lang="en")).subtitle_langs, ["en"])
        self.assertFalse(media.configured(acct()))
        self.assertTrue(media.configured(e))

    def test_effective_keeps_choices(self):
        s = acct(audio_lang="de", profiles=["orig-4k"], default_profile="orig-4k", subtitle_langs=["it"],
                 metadata_lang="es")
        self.assertEqual(media.effective(s), s)

    def test_old_settings_json_loads(self):  # written before this feature: no new keys
        s = Settings.from_json('{"data_dir": "/srv/arr", "username": "a", "password": "p", "vpn_provider": null}')
        self.assertIsNone(s.profiles)
        self.assertEqual(s.arr_ids, {})

    def test_grid_order(self):
        self.assertEqual(media.KEYS[:3], ("mine-720p", "mine-1080p", "mine-4k"))
        self.assertEqual(len(media.KEYS), 9)


class ValidateTest(unittest.TestCase):
    def test_validate_media(self):
        self.assertEqual(media.validate(acct(profiles=[], default_profile=None)), ["err_profiles_none"])
        self.assertEqual(media.validate(acct(profiles=["orig-4k"], default_profile="mine-1080p")),
                         ["err_default_profile"])
        self.assertEqual(media.validate(acct(subtitle_langs=[])), ["err_subtitles_none"])
        self.assertEqual(media.validate(acct(audio_lang="pt")), ["err_media_lang"])
        self.assertEqual(media.validate(acct(profiles=["nope-1080p"], default_profile="nope-1080p")),
                         ["err_profiles_none"])
        self.assertEqual(media.validate(acct()), [])

    def test_settings_validate_includes_media(self):
        from installer.settings import validate
        self.assertIn("err_subtitles_none", validate(acct(subtitle_langs=[])))


class ScoresTest(unittest.TestCase):
    def score(self, mode, *matched):
        return sum(media.scores(mode)[k] for k in matched)

    def test_scores_rank_languages(self):
        self.assertGreater(self.score("mine", "lang"), self.score("mine", "original"))
        self.assertGreater(self.score("mine", "multi"), self.score("mine", "original"))
        self.assertGreater(self.score("orig", "original"), self.score("orig", "lang"))
        self.assertGreater(self.score("multi", "multi"), self.score("multi", "lang"))
        self.assertGreater(self.score("multi", "lang"), self.score("multi", "original"))

    def test_vostfr_loses_in_mine(self):  # Radarr parses VOSTFR as French: the subbed format must cancel it
        self.assertLess(self.score("mine", "lang", "subbed"), self.score("mine", "original"))
        self.assertLess(self.score("multi", "lang", "subbed"), self.score("multi", "original"))
        self.assertGreaterEqual(self.score("orig", "original", "subbed"), self.score("orig", "original"))

    def test_nothing_is_refused_for_its_language(self):  # never wait: every score stays above the minimum
        for mode in media.MODES:
            self.assertGreater(min(self.score(mode, "lang", "subbed"), self.score(mode, "subbed")),
                               media.MIN_SCORE)


class ProfileBodyTest(unittest.TestCase):
    def test_profile_body(self):
        body = media.profile_body(RADARR_SCHEMA, "mine-1080p", "Français · 1080p", IDS, radarr=True)
        allowed = [i for i in body["items"] if i["allowed"]]
        self.assertEqual(len(allowed), 1)  # one group: sources tie, the format score decides
        self.assertEqual({q["quality"]["name"] for q in allowed[0]["items"]},
                         {"HDTV-1080p", "WEBDL-1080p", "WEBRip-1080p", "Bluray-1080p"})
        self.assertEqual(body["cutoff"], allowed[0]["id"])
        self.assertEqual((body["name"], body["upgradeAllowed"], body["minFormatScore"], body["language"]["id"]),
                         ("Français · 1080p", False, media.MIN_SCORE, -1))
        self.assertIn({"format": 11, "score": media.scores("mine")["lang"]}, body["formatItems"])
        self.assertEqual(len(body["formatItems"]), 4)
        self.assertEqual(RADARR_SCHEMA["language"]["id"], -2)  # the schema itself is left untouched

    def test_profile_body_keeps_other_formats(self):
        # The apps refuse a profile that leaves out any existing custom format: the user's own stay, at score 0.
        schema = dict(RADARR_SCHEMA, formatItems=[{"format": 99, "name": "Mine", "score": 0},
                                                  {"format": 11, "name": "Plaid · fr", "score": 0}])
        body = media.profile_body(schema, "mine-1080p", "x", IDS, radarr=True)
        scores = {f["format"]: f["score"] for f in body["formatItems"]}
        self.assertEqual(scores[99], 0)
        self.assertEqual(scores[11], media.scores("mine")["lang"])
        self.assertEqual(len(body["formatItems"]), 5)

    def test_profile_body_sonarr_has_no_language(self):
        body = media.profile_body(SONARR_SCHEMA, "orig-4k", "x", IDS, radarr=False)
        self.assertNotIn("language", body)
        group = next(i for i in body["items"] if i["allowed"])
        self.assertEqual({q["quality"]["name"] for q in group["items"]}, {"WEBDL-2160p", "Bluray-2160p"})


class FormatsTest(unittest.TestCase):
    def test_formats_per_language(self):
        fr, de, es = media.formats("fr"), media.formats("de"), media.formats("es")
        self.assertEqual(lang_value(fr["lang"]), 2)
        self.assertEqual(lang_value(de["lang"]), 4)
        self.assertEqual(lang_value(fr["original"]), -2)
        self.assertRegex("Film.2023.MULTi.1080p.WEB.x264", regex_of(fr["multi"]))
        self.assertNotRegex("Film.2023.FRENCH.1080p.WEB.x264", regex_of(fr["multi"]))
        self.assertRegex("Film.2023.German.DL.1080p.WEB.x264", regex_of(de["multi"]))
        self.assertEqual(lang_value(de["multi"]), 4)  # DL alone says nothing of which language
        for single in ("Film.2023.German.1080p.WEB-DL.x264", "Pelicula.2023.SPANiSH.1080p.WEB.DL.x264"):
            self.assertNotRegex(single, regex_of(de["multi"]))  # WEB-DL is a source, not dual audio
        self.assertRegex("Film.2023.German.DL.1080p.WEB-DL.x264", regex_of(de["multi"]))
        self.assertRegex("Pelicula.2023.Spanish.DUAL.1080p", regex_of(es["multi"]))
        self.assertRegex("Film.2023.VOSTFR.1080p.WEB.x264", regex_of(fr["subbed"]))
        self.assertNotRegex("Film.2023.FRENCH.1080p.WEB.x264", regex_of(fr["subbed"]))
        self.assertEqual(len({f["name"] for f in fr.values()}), 4)


class NamesTest(unittest.TestCase):
    def test_profile_names(self):
        self.assertEqual(media.profile_name("mine-1080p", "fr", "fr"), "Français · 1080p")
        self.assertEqual(media.profile_name("orig-4k", "fr", "de"), "VO sous-titrée · 4K")
        self.assertEqual(media.profile_name("multi-720p", "en", "fr"), "MULTi · 720p")
        self.assertEqual(media.profile_name("mine-720p", "en", "de"), "German · 720p")


if __name__ == "__main__":
    unittest.main()
