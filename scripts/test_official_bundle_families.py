import json
import tempfile
import unittest
from pathlib import Path

import official_bundle_families as families


def registry(family_rows, **extra):
    document = {
        "schema_version": 1,
        "families": family_rows,
        "exclude": extra.get("exclude", []),
        "reviewed_unclassified": extra.get("reviewed_unclassified", []),
    }
    return document


class LogicalNameTests(unittest.TestCase):
    def test_unity3d_suffix_is_added_once(self):
        self.assertEqual(families.logical_bundle_name("md_jp.gtx"), "md_jp.gtx.unity3d")
        self.assertEqual(families.logical_bundle_name("md_jp.gtx.unity3d"), "md_jp.gtx.unity3d")
        self.assertEqual(families.logical_bundle_name("  "), "")

    def test_family_signature_collapses_digits_and_case(self):
        self.assertEqual(
            families.family_signature("event_0448_story_01_jp.gtx"),
            families.family_signature("event_0450_story_12_jp.gtx.unity3d"),
        )
        self.assertEqual(
            families.family_signature("SCROBJ_ITTANA.unity3d"), "scrobj_ittana"
        )


class ClassifyTests(unittest.TestCase):
    def setUp(self):
        self.document = registry([
            {"id": "gtx_text", "pipeline": "gtx_text", "match": {"suffix": "_jp.gtx.unity3d"}},
            {"id": "song_lyrics", "pipeline": "song_lyrics", "match": {"prefix": "scrobj_"}},
        ])

    def test_new_bundle_inside_a_known_family_is_selected(self):
        """The 2026-10 regression: an unseen name must still match."""
        self.assertEqual(families.classify(self.document, "event_0448_story_01_jp.gtx.unity3d"), "gtx_text")
        self.assertEqual(families.classify(self.document, "scrobj_ittana.unity3d"), "song_lyrics")

    def test_official_typo_and_case_variants_still_match(self):
        # The official index really ships "pecial_108_fc_01_jp.gtx".
        self.assertEqual(families.classify(self.document, "pecial_108_fc_01_jp.gtx.unity3d"), "gtx_text")
        self.assertEqual(families.classify(self.document, "SCROBJ_ITTANA.UNITY3D"), "song_lyrics")

    def test_unrelated_resources_are_not_selected(self):
        for name in ("costume_icon_001.unity3d", "bg2d_g1.unity3d", "jacket_aftspt.unity3d"):
            self.assertIsNone(families.classify(self.document, name))

    def test_exclusion_wins_over_a_family(self):
        document = registry(
            [{"id": "gtx_text", "pipeline": "gtx_text", "match": {"suffix": "_jp.gtx.unity3d"}}],
            exclude=["broken_jp.gtx.unity3d"],
        )
        self.assertEqual(families.classify(document, "other_jp.gtx"), "gtx_text")
        # The excluded name normalizes to the same logical form and is refused.
        self.assertIsNone(families.classify(document, "broken_jp.gtx"))
        self.assertIsNone(families.classify(document, "broken_jp.gtx.unity3d"))

    def test_multiple_match_keys_must_all_hold(self):
        document = registry([
            {"id": "narrow", "pipeline": "gtx_text",
             "match": {"prefix": "card_episode_", "suffix": "_jp.gtx.unity3d"}},
        ])
        self.assertEqual(families.classify(document, "card_episode_003mik0614_jp.gtx.unity3d"), "narrow")
        self.assertIsNone(families.classify(document, "event_0448_story_01_jp.gtx.unity3d"))


class RegistryValidationTests(unittest.TestCase):
    def _load(self, document, name="registry.json"):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / name
        path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
        return families.load_registry(path)

    def test_missing_file_is_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(families.RegistryError):
                families.load_registry(Path(tmp) / "absent.json")

    def test_unknown_schema_is_an_error(self):
        with self.assertRaises(families.RegistryError):
            self._load({"schema_version": 99, "families": [{"id": "a", "pipeline": "p", "match": {"prefix": "x"}}]})

    def test_duplicate_family_id_is_an_error(self):
        with self.assertRaises(families.RegistryError):
            self._load({"schema_version": 1, "families": [
                {"id": "a", "pipeline": "p", "match": {"prefix": "x"}},
                {"id": "a", "pipeline": "p", "match": {"prefix": "y"}},
            ]})

    def test_unsupported_match_operator_is_an_error(self):
        with self.assertRaises(families.RegistryError):
            self._load({"schema_version": 1, "families": [
                {"id": "a", "pipeline": "p", "match": {"glob": "*.gtx"}},
            ]})

    def test_empty_family_list_is_an_error(self):
        with self.assertRaises(families.RegistryError):
            self._load({"schema_version": 1, "families": []})

    def test_reviewed_unclassified_is_case_insensitive(self):
        document = self._load({
            "schema_version": 1,
            "families": [{"id": "a", "pipeline": "p", "match": {"prefix": "x"}}],
            "reviewed_unclassified": ["Costume_Icon"],
        })
        self.assertEqual(families.reviewed_unclassified(document), {"costume_icon"})
        # Prefix semantics: one reviewed entry covers the whole branch.
        self.assertTrue(families.is_reviewed("costume_icon_#", families.reviewed_unclassified(document)))
        self.assertFalse(families.is_reviewed("titlebg_#", families.reviewed_unclassified(document)))


class CommittedRegistryTests(unittest.TestCase):
    """The registry shipped in the repository must actually select the two surfaces."""

    def test_committed_registry_selects_text_and_lyrics(self):
        document = families.load_registry()
        self.assertEqual(families.classify(document, "event_0448_story_01_jp.gtx.unity3d"), "gtx_text")
        self.assertEqual(families.classify(document, "scrobj_ittana.unity3d"), "song_lyrics")
        self.assertIsNone(families.classify(document, "costume_icon_0001.unity3d"))
        self.assertEqual(
            sorted(families.registry_families(document)),
            ["gtx_text", "song_lyrics"],
        )

    def test_reviewed_prefixes_suppress_known_non_text_surfaces(self):
        """The audit result is committed, not just described in a document.

        These families were sampled and confirmed to hold audio (Criware ACB),
        animation curves or sprites only, so counting them as "unclassified
        resource types worth a human look" is noise.
        """
        reviewed = families.reviewed_unclassified(families.load_registry())
        for signature in ("event_#_story_#.acb", "card_episode_#miz#.acb", "main_chat_#.acb",
                          "fhout_event_#_story_#.json", "blog#", "event_#_info",
                          "costumesalesinfo#", "titlebg_#"):
            self.assertTrue(families.is_reviewed(signature, reviewed), signature)

    def test_reviewed_prefixes_stay_narrow(self):
        """A too-broad prefix would hide genuinely new resource types.

        ``event_`` or a bare ``#`` would silently suppress every new family that
        happens to share the first characters, so the committed list is checked
        against the two families that must keep their own decision.
        """
        reviewed = families.reviewed_unclassified(families.load_registry())
        for prefix in reviewed:
            self.assertTrue(prefix.strip())
            self.assertNotIn(prefix, ("#", "event_", "card_", "song", "adv_"))
        for signature in ("event_#_story_#_jp.gtx", "scrobj_newref", "live_info_#",
                          "songname_#", "adv_imo_#", "the_act_#"):
            self.assertFalse(families.is_reviewed(signature, reviewed), signature)


if __name__ == "__main__":
    unittest.main()
