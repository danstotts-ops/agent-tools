"""Tokens match brand canon, and the themes built from them stay readable.

Standard library only, so this runs in CI with nothing installed. The
python-pptx builder is covered separately in test_deck_style.py, which skips
itself when the package is absent.
"""

import copy
import importlib
import json
import os
import tempfile
from unittest import mock
import unittest
import urllib.error
import urllib.request
import sys


# These tests pin the committed snapshot, so nothing on the machine running them
# may change which brand.json gets loaded: no override, no network, no cache.
os.environ.pop("BRAND_JSON_PATH", None)
os.environ["BRAND_JSON_OFFLINE"] = "1"

from agent_tools import brand as bt  # noqa: E402

# Runpod Brand Guidelines V.2 (July 2026, modified 2026-09-09), Core p.19 and
# Extended Core p.20, as confirmed by the brand design lead on 2026-09-22. These are held here
# a second time on purpose. bt.BRAND is read from brand.json; this table is the
# independent statement of what it must say. If brand.json changes one of these
# values, the snapshot refresh fails here until a person agrees the change is
# real.
CANON = {
    "purple": "#5D29F0",        # Runpod Purple
    "purple_light": "#9289FE",  # Purple 200
    "purple_950": "#100433",    # Purple 950
    "midnight": "#000010",      # Runpod Black
    "white": "#FFFFFF",         # Runpod White
    "black": "#000010",         # alias of Runpod Black
}

# Utility colors, guidelines p.22. Canon.
CANON_UTILITY = {
    "information": "#43FFE3",
    "success": "#41F420",
    "data": "#A7FF43",
    "error": "#FF661A",
    "warning": "#FFC01F",
}

# WCAG 2.1 AA: 4.5:1 for body text, 3:1 for large text (18pt+, or 14pt+ bold).
# The guidelines state no target of their own; this is the skill's choice.
AA_BODY = 4.5
AA_LARGE = 3.0


class TestCanon(unittest.TestCase):
    def test_brand_palette_matches_the_guidelines(self):
        self.assertEqual(bt.BRAND, CANON)

    def test_palette_has_no_extra_colors(self):
        # A palette that grows quietly is how brand drift starts.
        self.assertEqual(len(bt.BRAND), 6)

    def test_utility_colors_match_the_guidelines(self):
        self.assertEqual(bt.UTILITY, CANON_UTILITY)

    def test_the_system_has_no_red(self):
        """The error state is an orange, guidelines p.22. A red on a Runpod
        slide is off system, and reaching for one is the obvious mistake."""
        self.assertEqual(bt.UTILITY["error"], "#FF661A")
        r, g, b = bt.parse_hex(bt.UTILITY["error"])
        self.assertGreater(g, 80, "Error drifted toward a pure red")

    def test_typefaces_match_the_guidelines(self):
        self.assertEqual(bt.TYPEFACE, "ABC Diatype")
        self.assertEqual(bt.MONO_DISPLAY_TYPEFACE, "Departure Mono")
        self.assertEqual(bt.MONO_CODE_TYPEFACE, "JetBrains Mono")

    def test_the_fallback_stack_matches_brand_json(self):
        """Inter, Helvetica, Arial, sans-serif. brand.json now carries it at
        status canonical under the Notion canon rule; the PDF itself still
        names no fallback, so see the note on the entry before calling it
        "approved" in copy."""
        self.assertEqual(bt.FALLBACK_TYPEFACE, "Inter")
        self.assertEqual(bt.FALLBACK_STACK,
                         ("Inter", "Helvetica", "Arial", "sans-serif"))
        with open(bt.SNAPSHOT_PATH, encoding="utf-8") as fh:
            snapshot = json.load(fh)
        fallback = snapshot["typography"]["families"]["fallback"]
        self.assertEqual(fallback["name"], "Inter")
        self.assertEqual(fallback["stack"], list(bt.FALLBACK_STACK))

    def test_the_two_guideline_weights_are_present(self):
        """The guidelines specify two weights, p.27. brand.json also carries
        light, bold and black at status provisional (the Notion canon rule), so
        they load; only a deprecated weight is refused, covered in
        test_brand_json.py."""
        self.assertEqual(bt.WEIGHTS["regular"], 400)
        self.assertEqual(bt.WEIGHTS["medium"], 500)

    def test_accent_colors_match_brand_json(self):
        """Radiant Pink and Ambient Pink were left out of this skill on
        2026-09-22 because their printed hexes were shifted on page 21. the brand design lead
        confirmed both in writing the same day. Electric Purple and Highlight
        Glow are canonical in brand.json under the Notion canon rule."""
        self.assertEqual(bt.ACCENT, {
            "electric_purple": "#874BFF",
            "radiant_pink": "#AE6DFF",
            "ambient_pink": "#D58EFF",
            "pink_glow": "#FCB1FF",
        })

    def test_electric_purple_is_an_accent_not_a_brand_color(self):
        """Electric Purple (#874BFF) is gradient and expressive-moment color.
        It must not stand in for Runpod Purple or Purple 200 in BRAND."""
        self.assertEqual(bt.ACCENT["electric_purple"], "#874BFF")
        self.assertNotIn("#874BFF", bt.BRAND.values())
        for colors in bt.THEMES.values():
            self.assertNotIn("#874BFF", [v.upper() for v in colors.values()])

    def test_radiant_pink_is_not_the_dark_surface_accent(self):
        """The dark-surface accent is Purple 200. #AE6DFF is Radiant Pink, and
        it is also what the deprecated color.brand.purple-light carried, which
        is how it got mistaken for the dark accent. It must not appear in
        BRAND."""
        self.assertNotIn("#AE6DFF", bt.BRAND.values())
        self.assertEqual(bt.BRAND["purple_light"], "#9289FE")

    def test_every_theme_color_is_a_parseable_hex(self):
        for theme, colors in bt.THEMES.items():
            for role, value in colors.items():
                with self.subTest(theme=theme, role=role):
                    r, g, b = bt.parse_hex(value)
                    for channel in (r, g, b):
                        self.assertTrue(0 <= channel <= 255)

    def test_parse_hex_rejects_malformed_input(self):
        for bad in ("#FFF", "5D29F", "", "#5D29F0F0"):
            with self.subTest(value=bad):
                with self.assertRaises(ValueError):
                    bt.parse_hex(bad)

    def test_parse_hex_accepts_with_or_without_hash(self):
        self.assertEqual(bt.parse_hex("#5D29F0"), bt.parse_hex("5D29F0"))
        self.assertEqual(bt.parse_hex("#5D29F0"), (93, 41, 240))


class TestContrast(unittest.TestCase):
    """The reason to check these numbers rather than trust them.

    Deep Midnight and Runpod Purple are both dark, so the pairing that feels
    most on brand (purple on midnight) is the one that fails. The dark theme
    accents with the light purple for exactly this reason, and this suite is
    what keeps someone from "fixing" it back.
    """

    def test_contrast_ratio_endpoints(self):
        self.assertAlmostEqual(bt.contrast_ratio("#000000", "#FFFFFF"), 21.0,
                               places=2)
        self.assertAlmostEqual(bt.contrast_ratio("#5D29F0", "#5D29F0"), 1.0,
                               places=6)

    def test_contrast_is_symmetric(self):
        self.assertAlmostEqual(bt.contrast_ratio("#5D29F0", "#FFFFFF"),
                               bt.contrast_ratio("#FFFFFF", "#5D29F0"),
                               places=9)

    def test_body_text_passes_aa_on_both_themes(self):
        for theme, colors in bt.THEMES.items():
            for role in ("ink", "ink_soft"):
                with self.subTest(theme=theme, role=role):
                    ratio = bt.contrast_ratio(colors[role], colors["bg"])
                    self.assertGreaterEqual(
                        ratio, AA_BODY,
                        "%s/%s is %.2f:1 on the %s background"
                        % (theme, role, ratio, theme))

    def test_accent_passes_aa_body_on_its_own_background(self):
        # The accent carries stat-row numbers and section heads, which are
        # large, but it also carries 12pt card headings. Hold it to body level.
        for theme, colors in bt.THEMES.items():
            with self.subTest(theme=theme):
                ratio = bt.contrast_ratio(colors["accent"], colors["bg"])
                self.assertGreaterEqual(ratio, AA_BODY,
                                        "%s accent is %.2f:1" % (theme, ratio))

    def test_primary_purple_would_fail_on_the_dark_base(self):
        # Documents why the dark theme does not accent with it, and why p.23
        # says to use Purple 200 there. 3.02:1 on Runpod Black: fails body
        # text outright and sits on the floor for large text and UI.
        ratio = bt.contrast_ratio(bt.BRAND["purple"], bt.BRAND["midnight"])
        self.assertLess(ratio, AA_BODY)
        self.assertLess(ratio, 3.1)

    def test_the_dark_accent_is_purple_200_and_beats_the_alternatives(self):
        """p.23: 'Purple 200 provides better contrast than the primary purple
        on dark backgrounds.' This is the rule the brand design lead restated on 2026-09-22
        and the one most likely to get 'fixed' back to the primary purple."""
        bg = bt.BRAND["midnight"]
        purple_200 = bt.contrast_ratio(bt.BRAND["purple_light"], bg)
        self.assertEqual(bt.THEMES["dark"]["accent"], "#9289FE")
        self.assertGreaterEqual(purple_200, AA_BODY)
        self.assertGreater(purple_200,
                           bt.contrast_ratio(bt.BRAND["purple"], bg))

    def test_utility_colors_are_readable_on_the_dark_base(self):
        for name, value in bt.UTILITY.items():
            with self.subTest(color=name):
                ratio = bt.contrast_ratio(value, bt.BRAND["midnight"])
                self.assertGreaterEqual(ratio, AA_BODY,
                                        "%s is %.2f:1" % (name, ratio))

    def test_no_utility_color_is_used_raw_on_a_light_slide(self):
        """All five are built for dark surfaces; the best on white is Error at
        2.93:1. The light theme must darken them, not use them straight."""
        white = bt.THEMES["light"]["bg"]
        for name, value in bt.UTILITY.items():
            with self.subTest(color=name):
                self.assertLess(bt.contrast_ratio(value, white), AA_LARGE)
        raw = set(bt.UTILITY.values())
        for state, (ink, fill) in bt.STATUS["light"].items():
            with self.subTest(state=state):
                self.assertNotIn(ink, raw)
                self.assertNotIn(fill, raw)

    def test_dark_status_ink_is_the_canonical_utility_color(self):
        self.assertEqual(bt.STATUS["dark"]["beat"][0], bt.UTILITY["success"])
        self.assertEqual(bt.STATUS["dark"]["watch"][0], bt.UTILITY["warning"])
        self.assertEqual(bt.STATUS["dark"]["miss"][0], bt.UTILITY["error"])

    def test_text_on_cards_passes_aa(self):
        for theme, colors in bt.THEMES.items():
            for role in ("ink", "ink_soft", "accent"):
                with self.subTest(theme=theme, role=role):
                    ratio = bt.contrast_ratio(colors[role], colors["surface"])
                    self.assertGreaterEqual(
                        ratio, AA_BODY,
                        "%s/%s on card is %.2f:1" % (theme, role, ratio))

    def test_table_header_text_passes_aa(self):
        for theme, colors in bt.THEMES.items():
            with self.subTest(theme=theme):
                ratio = bt.contrast_ratio(colors["head_ink"],
                                          colors["head_bg"])
                self.assertGreaterEqual(ratio, AA_BODY)

    def test_status_text_passes_aa_on_its_own_fill(self):
        for theme, states in bt.STATUS.items():
            for state, (ink, fill) in states.items():
                with self.subTest(theme=theme, state=state):
                    ratio = bt.contrast_ratio(ink, fill)
                    self.assertGreaterEqual(
                        ratio, AA_BODY,
                        "%s/%s is %.2f:1" % (theme, state, ratio))

    def test_status_fills_read_against_the_slide(self):
        # A status fill that vanishes into the background is not signalling.
        for theme, states in bt.STATUS.items():
            bg = bt.THEMES[theme]["bg"]
            for state, (_, fill) in states.items():
                with self.subTest(theme=theme, state=state):
                    self.assertNotEqual(fill.upper(), bg.upper())

    def test_hairline_is_visible_against_its_card(self):
        for theme, colors in bt.THEMES.items():
            with self.subTest(theme=theme):
                ratio = bt.contrast_ratio(colors["hairline"],
                                          colors["surface"])
                self.assertGreater(ratio, 1.05)


class TestThemeShape(unittest.TestCase):
    def test_both_templates_have_a_theme(self):
        self.assertEqual(sorted(bt.THEMES), ["dark", "light"])
        self.assertEqual(sorted(bt.STATUS), ["dark", "light"])

    def test_themes_define_the_same_roles(self):
        roles = [sorted(c) for c in bt.THEMES.values()]
        self.assertEqual(roles[0], roles[1])

    def test_every_theme_has_all_three_status_states(self):
        for theme, states in bt.STATUS.items():
            with self.subTest(theme=theme):
                self.assertEqual(sorted(states), ["beat", "miss", "watch"])

    def test_theme_backgrounds_and_ink_come_from_canon(self):
        self.assertEqual(bt.THEMES["light"]["bg"], bt.BRAND["white"])
        self.assertEqual(bt.THEMES["light"]["ink"], bt.BRAND["midnight"])
        self.assertEqual(bt.THEMES["dark"]["bg"], bt.BRAND["midnight"])
        self.assertEqual(bt.THEMES["dark"]["ink"], bt.BRAND["white"])

    def test_the_dark_card_surface_is_purple_950_not_a_derived_tint(self):
        """p.23 names Purple 950 for exactly this job, so the card fill is
        canon rather than something this file made up."""
        self.assertEqual(bt.THEMES["dark"]["surface"], bt.BRAND["purple_950"])

    def test_nothing_still_carries_the_stale_values(self):
        """#06040D was the old dark base, #AE6DFF the old dark accent, and
        #000000 a pure black the system does not have. None belongs in a theme
        or in BRAND. (#AE6DFF is legitimately Radiant Pink in ACCENT.) Guard
        against a well-meaning revert."""
        stale = {"#06040D", "#AE6DFF", "#000000"}
        for theme, colors in bt.THEMES.items():
            for role, value in colors.items():
                with self.subTest(theme=theme, role=role):
                    self.assertNotIn(value.upper(), stale)
        for value in bt.BRAND.values():
            self.assertNotIn(value.upper(), stale)

    def test_both_accents_come_from_canon(self):
        canon_values = set(bt.BRAND.values())
        for theme, colors in bt.THEMES.items():
            with self.subTest(theme=theme):
                self.assertIn(colors["accent"], canon_values)


S3_URL = "https://runpod-brand-json.s3.us-east-1.amazonaws.com/brand.json"
STEPS = ("50", "100", "200", "300", "400", "500", "600", "700", "800", "900",
         "950")


def pinned_snapshot():
    with open(bt.SNAPSHOT_PATH, encoding="utf-8") as fh:
        return json.load(fh)


class TestLoaderV2(unittest.TestCase):
    """Public surface added in 0.10.0, and the fetch order. The pinned snapshot
    stands in for the document; nothing here touches the network or the real
    user cache directory."""

    def setUp(self):
        # cache_path reads the process environment, not the env passed to
        # resolve(), so every test here gets a private cache directory.
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.cache_dir = tmp.name
        patcher = mock.patch.dict(os.environ,
                                  {"BRAND_JSON_CACHE_DIR": self.cache_dir})
        patcher.start()
        self.addCleanup(patcher.stop)

    def recorder(self, doc=None):
        calls = []

        def fetch(url, token=None):
            calls.append(url)
            if doc is None:
                raise OSError("404")
            return doc
        return calls, fetch

    def test_s3_is_the_first_url_source_after_the_env_path(self):
        self.assertEqual(bt.PUBLIC_URL, S3_URL)
        # An unusable override: the S3 URL is the first thing fetched.
        calls, fetch = self.recorder()
        with self.assertLogs("agent_tools.brand", level="WARNING"):
            _, source = bt.resolve(
                env={"BRAND_JSON_PATH": "/nonexistent.json"}, fetch=fetch)
        self.assertEqual(calls[0], S3_URL)
        self.assertEqual(source, "snapshot")

    def test_a_usable_env_path_beats_every_url(self):
        calls, fetch = self.recorder()
        path = os.path.join(self.cache_dir, "override.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(pinned_snapshot(), fh)
        _, source = bt.resolve(env={"BRAND_JSON_PATH": path}, fetch=fetch)
        self.assertEqual(source, "env:" + path)
        self.assertEqual(calls, [])

    def test_raw_github_comes_after_s3_then_the_snapshot(self):
        calls, fetch = self.recorder()
        with self.assertLogs("agent_tools.brand", level="WARNING"):
            _, source = bt.resolve(env={}, fetch=fetch)
        self.assertEqual(calls, [S3_URL, bt.BRAND_JSON_URL])
        self.assertEqual(source, "snapshot")

    def test_well_known_url_is_not_a_source(self):
        self.assertFalse(hasattr(bt, "WELL_KNOWN_URL"))
        calls, fetch = self.recorder()
        with self.assertLogs("agent_tools.brand", level="WARNING"):
            bt.resolve(env={"GITHUB_TOKEN": "t0k"}, fetch=fetch)
        self.assertEqual(len(calls), 2)
        for url in calls:
            self.assertNotIn("well-known", url)
        with open(bt.__file__, encoding="utf-8") as fh:
            self.assertNotIn("well-known", fh.read())

    def test_reaching_the_snapshot_after_failed_urls_warns_and_names_them(self):
        calls, fetch = self.recorder()
        with self.assertLogs("agent_tools.brand", level="WARNING") as logged:
            bt.resolve(env={}, fetch=fetch)
        text = "\n".join(logged.output)
        self.assertIn(S3_URL, text)
        self.assertIn(bt.BRAND_JSON_URL, text)
        self.assertIn("snapshot", text)

    def test_offline_flag_reaching_the_snapshot_does_not_warn(self):
        calls, fetch = self.recorder()
        with self.assertNoLogs("agent_tools.brand", level="WARNING"):
            bt.resolve(env={"BRAND_JSON_OFFLINE": "1"}, fetch=fetch)
        self.assertEqual(calls, [])

    def test_resolve_falls_back_to_snapshot_when_urls_fail(self):
        """Reloads the module with the network failing and no cache, which is
        the state of a laptop on a plane."""
        def refuse(*args, **kwargs):
            raise urllib.error.URLError("no network")

        saved = {k: os.environ.get(k) for k in
                 ("BRAND_JSON_OFFLINE", "BRAND_JSON_PATH", "GITHUB_TOKEN")}
        real = urllib.request.urlopen

        def restore():
            urllib.request.urlopen = real
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
            importlib.reload(bt)
        self.addCleanup(restore)

        urllib.request.urlopen = refuse
        for key in saved:
            os.environ.pop(key, None)
        with self.assertLogs("agent_tools.brand", level="WARNING"):
            importlib.reload(bt)
        self.assertEqual(bt.source_used(), "snapshot")
        self.assertEqual(bt.BRAND["purple"], "#5D29F0")

    def test_source_used_folds_the_source_to_four_names(self):
        saved = bt.BRAND_SOURCE
        self.addCleanup(setattr, bt, "BRAND_SOURCE", saved)
        for source, want in (("env:/x/brand.json", "env"),
                             ("url:" + S3_URL, "s3"),
                             ("cache:" + S3_URL, "s3"),
                             ("url:" + bt.BRAND_JSON_URL, "github"),
                             ("cache:" + bt.BRAND_JSON_URL, "github"),
                             ("snapshot", "snapshot")):
            with self.subTest(source=source):
                bt.BRAND_SOURCE = source
                self.assertEqual(bt.source_used(), want)

    def test_derive_ignores_unknown_top_level_keys(self):
        doc = pinned_snapshot()
        # The snapshot is v1.1.0 and carries all three blocks, so empty them
        # here; the point is that "zzz" is ignored.
        doc.update({"surfaces": {}, "layout": {}, "accessibility": {},
                    "zzz": 1})
        tokens = bt.derive(doc)
        self.assertEqual(tokens["SURFACES"], {})
        self.assertEqual(tokens["LAYOUT"], {})
        self.assertEqual(tokens["ACCESSIBILITY"], {})
        self.assertNotIn("zzz", tokens)

    def test_surfaces_layout_accessibility_pass_through_when_present(self):
        with_blocks = pinned_snapshot()
        with_blocks["version"] = "1.1.0"
        with_blocks["surfaces"] = {"deck": {"bg": "#000010"}}
        with_blocks["layout"] = {"grid": {"columns": 12}}
        with_blocks["accessibility"] = {"min_contrast": 4.5}
        got = bt.derive(with_blocks)
        self.assertEqual(got["SURFACES"], {"deck": {"bg": "#000010"}})
        self.assertEqual(got["LAYOUT"], {"grid": {"columns": 12}})
        self.assertEqual(got["ACCESSIBILITY"], {"min_contrast": 4.5})
        # Derived copies, not the document's own objects.
        got["SURFACES"]["deck"]["bg"] = "#FFFFFF"
        self.assertEqual(with_blocks["surfaces"]["deck"]["bg"], "#000010")

        without = pinned_snapshot()
        for key in ("surfaces", "layout", "accessibility"):
            without.pop(key, None)
        got = bt.derive(without)
        self.assertEqual((got["SURFACES"], got["LAYOUT"], got["ACCESSIBILITY"]),
                         ({}, {}, {}))
        self.assertEqual((got["SURFACES_META"], got["LAYOUT_META"],
                          got["ACCESSIBILITY_META"]), ({}, {}, {}))

    def test_block_level_metadata_is_split_from_the_entries(self):
        """surfaces and layout carry block-level note/source/status next to
        their entries. Iterating SURFACES must only ever yield surface dicts,
        so those keys live in *_META instead."""
        doc = pinned_snapshot()
        doc["surfaces"] = {"note": "n", "source": "docs/surfaces.md",
                           "status": "provisional", "$source": "x",
                           "og": {"width": 1200, "height": 630,
                                  "safe_area": 0}}
        doc["layout"] = {"note": "n", "source": "s", "status": "provisional",
                         "space": {"1": {"px": 4}}, "stray": "scalar"}
        doc["accessibility"] = {"contrast_body": 4.5, "tap_target_px": 44,
                                "source": "docs/accessibility.md",
                                "status": "canonical"}
        got = bt.derive(doc)
        self.assertEqual(list(got["SURFACES"]), ["og"])
        self.assertEqual(got["SURFACES_META"],
                         {"note": "n", "source": "docs/surfaces.md",
                          "status": "provisional", "$source": "x"})
        self.assertEqual(list(got["LAYOUT"]), ["space"])
        self.assertEqual(got["LAYOUT_META"],
                         {"note": "n", "source": "s", "status": "provisional",
                          "stray": "scalar"})
        self.assertEqual(got["ACCESSIBILITY"],
                         {"contrast_body": 4.5, "tap_target_px": 44})
        self.assertEqual(got["ACCESSIBILITY_META"],
                         {"source": "docs/accessibility.md",
                          "status": "canonical"})
        for meta in ("note", "source", "status", "$source"):
            for block in ("SURFACES", "LAYOUT", "ACCESSIBILITY"):
                self.assertNotIn(meta, got[block])

    def test_ramp_and_style_are_loaded(self):
        self.assertEqual(set(bt.RAMP), {"purple", "neutral"})
        for name, ramp in bt.RAMP.items():
            with self.subTest(ramp=name):
                self.assertEqual(tuple(ramp), STEPS)
                for step, value in ramp.items():
                    self.assertRegex(value, r"^#[0-9A-F]{6}$")
        # Purple 500 is Runpod Purple, 200 is the dark-surface accent and 950
        # the dark canvas: the ramp and BRAND must agree.
        self.assertEqual(bt.RAMP["purple"]["500"], bt.BRAND["purple"])
        self.assertEqual(bt.RAMP["purple"]["200"], bt.BRAND["purple_light"])
        self.assertEqual(bt.RAMP["purple"]["950"], bt.BRAND["purple_950"])
        self.assertIs(bt.STYLE["em_dashes"], False)
        self.assertEqual(bt.STYLE["casing"], "sentence")
        forms = {f["form"]: f["correct"] for f in bt.STYLE["name_forms"]}
        self.assertIs(forms["Runpod"], True)
        self.assertIs(forms["RunPod"], False)

    def test_a_document_without_ramps_or_style_still_derives(self):
        doc = pinned_snapshot()
        for token in [t for t in doc["color"]
                      if t.startswith(("color.purple.", "color.neutral."))
                      and t[-1].isdigit()]:
            del doc["color"][token]
        del doc["style"]
        tokens = bt.derive(doc)
        self.assertEqual(tokens["RAMP"], {"purple": {}, "neutral": {}})
        self.assertEqual(tokens["STYLE"], {"em_dashes": False})

    def test_a_deprecated_ramp_step_is_left_out(self):
        doc = copy.deepcopy(pinned_snapshot())
        doc["color"]["color.purple.300"]["status"] = "deprecated"
        self.assertNotIn("300", bt.derive(doc)["RAMP"]["purple"])

    def test_portable_fallback_is_documented_exception(self):
        self.assertEqual(bt.PORTABLE_FALLBACK, {"pptx": "Arial", "docx": "Aptos"})
        with open(bt.__file__, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
        at = next(i for i, line in enumerate(lines)
                  if line.startswith("PORTABLE_FALLBACK"))
        around = "\n".join(lines[max(0, at - 8):at + 5]).lower()
        self.assertIn("exception", around)
        self.assertIn("not brand values", around)

    def test_user_agent_names_the_agent_tools_version(self):
        from importlib.metadata import version
        self.assertEqual(bt.USER_AGENT,
                         "runpod-agent-tools-brand/" + version("agent-tools"))

    def test_snapshot_is_the_public_variant(self):
        with open(bt.SNAPSHOT_PATH, encoding="utf-8") as fh:
            doc = json.load(fh)
        self.assertEqual(doc.get("distribution", {}).get("variant"), "public")
        text = json.dumps(doc)
        for private in ("Slack DM", "HJ Kim", "Stotts"):
            self.assertNotIn(private, text)

    def test_cache_dir_is_runpod_brand(self):
        os.environ.pop("BRAND_JSON_CACHE_DIR")  # patch.dict restores it
        self.assertIn("runpod-brand", bt.cache_path(S3_URL))
        self.assertNotIn("runpod-decks", bt.cache_path(S3_URL))


FIXTURE_110 = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "brand_fixtures", "brand-1.1.0.json")
SURFACE_IDS = {"blog_header", "email_header", "instagram_square",
               "linkedin_carousel", "linkedin_post", "og", "slide", "x_card",
               "youtube_thumbnail"}


class TestBrandJson110(unittest.TestCase):
    """The live v1.1.0 document (S3, sha256 ca65bce6b38d...), committed as a
    fixture and loaded through BRAND_JSON_PATH, the documented override."""

    def assert_v110_blocks(self, surfaces, layout, accessibility):
        self.assertEqual(set(surfaces), SURFACE_IDS)
        self.assertEqual(len(surfaces), 9)
        for sid, s in surfaces.items():
            with self.subTest(surface=sid):
                self.assertIsInstance(s, dict)
                for key in ("width", "height", "safe_area"):
                    self.assertIn(key, s)
                s["width"]  # must not raise for any value of SURFACES
        self.assertEqual(set(layout), {"space", "radius", "type", "button"})
        self.assertEqual(len(layout["space"]), 8)
        self.assertEqual(len(layout["radius"]), 4)
        self.assertEqual(len(layout["type"]["scale"]["web"]), 6)
        self.assertIn("height_px", layout["button"])
        self.assertEqual(accessibility["contrast_body"], 4.5)
        self.assertEqual(accessibility["contrast_large"], 3.0)
        self.assertEqual(accessibility["tap_target_px"], 44)
        self.assertNotIn("source", accessibility)
        self.assertNotIn("status", accessibility)

    def test_resolve_with_brand_json_path_loads_v110(self):
        tokens, source = bt.resolve(env={"BRAND_JSON_PATH": FIXTURE_110},
                                    fetch=None)
        self.assertEqual(source, "env:" + FIXTURE_110)
        self.assert_v110_blocks(tokens["SURFACES"], tokens["LAYOUT"],
                                tokens["ACCESSIBILITY"])
        self.assertEqual(tokens["SURFACES_META"]["status"], "provisional")
        self.assertEqual(tokens["SURFACES_META"]["source"], "docs/surfaces.md")
        self.assertEqual(tokens["LAYOUT_META"]["status"], "provisional")
        self.assertEqual(tokens["ACCESSIBILITY_META"]["status"], "canonical")

    def test_module_constants_with_brand_json_path(self):
        saved = {k: os.environ.get(k)
                 for k in ("BRAND_JSON_PATH", "BRAND_JSON_OFFLINE")}

        def restore():
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
            importlib.reload(bt)
        self.addCleanup(restore)
        os.environ["BRAND_JSON_PATH"] = FIXTURE_110
        os.environ["BRAND_JSON_OFFLINE"] = "1"
        importlib.reload(bt)
        self.assertEqual(bt.source_used(), "env")
        self.assert_v110_blocks(bt.SURFACES, bt.LAYOUT, bt.ACCESSIBILITY)
        widths = {sid: s["width"] for sid, s in bt.SURFACES.items()}
        self.assertEqual(len(widths), 9)
        self.assertEqual(bt.SURFACES_META["source"], "docs/surfaces.md")
        self.assertEqual(bt.ACCESSIBILITY_META["status"], "canonical")

    def test_snapshot_is_the_v110_document(self):
        with open(bt.SNAPSHOT_PATH, "rb") as fh:
            snapshot = fh.read()
        with open(FIXTURE_110, "rb") as fh:
            self.assertEqual(snapshot, fh.read())

    def test_offline_constants_are_populated_from_the_snapshot(self):
        # The suite runs with BRAND_JSON_OFFLINE=1, so these come from the
        # committed snapshot.
        self.assertEqual(bt.source_used(), "snapshot")
        self.assert_v110_blocks(bt.SURFACES, bt.LAYOUT, bt.ACCESSIBILITY)


if __name__ == "__main__":
    unittest.main()
