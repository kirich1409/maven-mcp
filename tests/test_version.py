"""Tests for version classification and comparison functions in server.py.

Mirrors assertions from:
  - src/version/__tests__/classify.test.ts
  - src/version/__tests__/compare.test.ts
  - src/version/__tests__/range.test.ts

All test methods call pure functions only — no network, no mocking required.
"""

import functools
import unittest

from _helpers import server
from fixtures.version_catalogs import (
    ALIAS_ALREADY_BARE,
    ALIAS_FROM_OLDER,
    ALIAS_ONLY,
    ALPHA_AND_STABLE,
    DATESTAMP,
    JRE_LINES,
    NO_BARE_STABLE,
    ONE_PATCH,
    ONLY_NEXT_ALPHA,
    PLUS_ORDINAL,
    WIDE,
)


# ---------------------------------------------------------------------------
# classify_version
# ---------------------------------------------------------------------------

class ClassifyVersionTest(unittest.TestCase):

    def test_classifies_stable(self):
        # mirrors classify.test.ts: "classifies stable versions"
        self.assertEqual(server.classify_version("3.5.11"), "stable")
        self.assertEqual(server.classify_version("1.0"), "stable")
        self.assertEqual(server.classify_version("2.0.0"), "stable")

    def test_classifies_snapshot(self):
        # mirrors classify.test.ts: "classifies snapshot versions"
        self.assertEqual(server.classify_version("1.0-SNAPSHOT"), "snapshot")
        self.assertEqual(server.classify_version("2.0.0-SNAPSHOT"), "snapshot")

    def test_classifies_alpha(self):
        # mirrors classify.test.ts: "classifies alpha versions"
        self.assertEqual(server.classify_version("1.0-alpha-1"), "alpha")
        self.assertEqual(server.classify_version("1.0.0-alpha1"), "alpha")
        self.assertEqual(server.classify_version("1.0-a1"), "alpha")

    def test_classifies_beta(self):
        # mirrors classify.test.ts: "classifies beta versions"
        self.assertEqual(server.classify_version("1.0-beta-1"), "beta")
        self.assertEqual(server.classify_version("1.0.0-beta1"), "beta")
        self.assertEqual(server.classify_version("1.0-b1"), "beta")

    def test_classifies_rc(self):
        # mirrors classify.test.ts: "classifies RC versions"
        self.assertEqual(server.classify_version("1.0-RC1"), "rc")
        self.assertEqual(server.classify_version("1.0-rc-2"), "rc")
        self.assertEqual(server.classify_version("1.0-CR1"), "rc")

    def test_no_false_positive_on_short_form_patterns(self):
        # Short forms do not match alpha/beta. The qualifier is unknown, so
        # stability is omitted.
        self.assertIsNone(server.classify_version("1.0-bar"))
        self.assertIsNone(server.classify_version("1.0-ace"))

    def test_classifies_milestone(self):
        # mirrors classify.test.ts: "classifies milestone versions"
        self.assertEqual(server.classify_version("1.0-M1"), "milestone")
        self.assertEqual(server.classify_version("1.0-milestone-2"), "milestone")

    def test_edge_case_empty_string(self):
        # edge case: empty string matches no pattern — falls through to "stable"
        self.assertEqual(server.classify_version(""), "stable")

    def test_edge_case_garbage_input(self):
        # No numeric core and no known token. Stability is omitted.
        self.assertIsNone(server.classify_version("garbage"))

    def test_preview_eap_and_dev_are_prereleases(self):
        # #70: these published qualifiers are prereleases, not stable releases.
        self.assertEqual(server.classify_version("1.1.0-preview.1"), "alpha")
        self.assertEqual(server.classify_version("1.0.0-Preview.1"), "alpha")
        self.assertEqual(server.classify_version("1.0.0.preview.1"), "alpha")
        self.assertEqual(server.classify_version("1.1.1-eap13"), "alpha")
        self.assertEqual(server.classify_version("1.1.1-EAP13"), "alpha")
        self.assertEqual(server.classify_version("17-ea+3"), "alpha")
        self.assertEqual(server.classify_version("1.0.0-pre"), "alpha")
        self.assertEqual(server.classify_version("1.0.0-pre1"), "alpha")
        self.assertEqual(server.classify_version("2.1.20-dev-1234"), "snapshot")
        self.assertEqual(server.classify_version("1.0.0-nightly"), "snapshot")
        self.assertEqual(server.classify_version("1.0.0-nightly.10"), "snapshot")

    def test_longer_words_and_platform_qualifiers_omit_stability(self):
        # The token ends at the qualifier boundary. A longer word and a
        # platform qualifier are not a known class, so stability is omitted.
        # Final stays a stable alias.
        self.assertIsNone(server.classify_version("1.0.0-devtools"))
        self.assertIsNone(server.classify_version("1.0.0-devtools2"))
        self.assertIsNone(server.classify_version("1.0.0-prerelease"))
        self.assertIsNone(server.classify_version("1.0-develop"))
        self.assertIsNone(server.classify_version("1.0-each"))
        self.assertIsNone(server.classify_version("33.0.0-jre"))
        self.assertIsNone(server.classify_version("1.0.0-android"))
        self.assertEqual(server.classify_version("1.0.0.Final"), "stable")


# ---------------------------------------------------------------------------
# find_latest_version_for_current
# ---------------------------------------------------------------------------

_MIXED_VERSIONS = ["1.0.0", "2.0.0-alpha1", "2.0.0-beta1", "2.0.0-RC1", "2.0.0"]


class FindLatestVersionTest(unittest.TestCase):
    """find_latest_version is what handle_get_latest_version actually calls
    (server.py ~:2008) — #325 was reported against that tool specifically,
    so the regression must be pinned here, not only on the sibling
    find_latest_version_for_current used by compare/audit tools."""

    def test_bare_release_preferred_over_compat_suffix_build(self):
        # #325: get_latest_version picked "0.8.0-0.6.x-compat" over "0.8.0".
        # Mirrors how fetch_metadata builds the sorted versions list fed to
        # find_latest_version (server.py:557).
        raw = ["0.7.0", "0.8.0-0.6.x-compat", "0.8.0"]
        versions = sorted(set(raw), key=functools.cmp_to_key(server.compare_versions))
        self.assertEqual(server.find_latest_version(versions, "PREFER_STABLE"), "0.8.0")

    def test_stable_only_skips_preview_and_eap(self):
        # #70: a preview or EAP build must not be returned as the stable release.
        preview = sorted(
            ["1.0.0", "1.1.0-preview.1"],
            key=functools.cmp_to_key(server.compare_versions),
        )
        eap = sorted(
            ["1.1.0", "1.1.1-eap13"],
            key=functools.cmp_to_key(server.compare_versions),
        )
        self.assertEqual(server.find_latest_version(preview, "STABLE_ONLY"), "1.0.0")
        self.assertEqual(server.find_latest_version(preview, "PREFER_STABLE"), "1.0.0")
        self.assertEqual(server.find_latest_version(eap, "STABLE_ONLY"), "1.1.0")
        self.assertEqual(server.find_latest_version(["1.0.0-nightly"], "STABLE_ONLY"), None)
        self.assertEqual(
            server.find_latest_version(["1.0.0-nightly"], "PREFER_STABLE"),
            "1.0.0-nightly",
        )


class FindLatestVersionForCurrentTest(unittest.TestCase):

    def test_returns_stable_when_current_is_stable(self):
        # mirrors classify.test.ts: "returns the stable when current is stable"
        self.assertEqual(
            server.find_latest_version_for_current(_MIXED_VERSIONS, "1.0.0"),
            "2.0.0",
        )

    def test_current_rc_returns_stable_upgrade(self):
        # #312: a more-stable upgrade must win. From an RC current, the stable
        # 2.0.0 qualifies (stable >= rc) and is the highest candidate, so it is
        # returned in preference to the same-core 2.0.0-RC1.
        self.assertEqual(
            server.find_latest_version_for_current(_MIXED_VERSIONS, "1.0.0-RC1"),
            "2.0.0",
        )

    def test_skips_versions_less_stable_than_current_beta(self):
        # mirrors classify.test.ts: "skips less stable versions"
        v = ["1.0.0", "2.0.0-alpha1", "2.0.0-beta1"]
        self.assertEqual(
            server.find_latest_version_for_current(v, "1.0.0-beta1"),
            "2.0.0-beta1",
        )

    def test_returns_none_when_only_less_stable_available(self):
        # #312: a SNAPSHOT must never be offered as an upgrade from a stable
        # current. Snapshot rank (0) is below stable rank (5), so nothing
        # qualifies and None is returned.
        v = ["1.0.0-SNAPSHOT"]
        self.assertIsNone(
            server.find_latest_version_for_current(v, "1.0.0"),
        )

    def test_up_to_date_current_returns_current_not_none(self):
        # #312 correction: when current is already the highest acceptable
        # version it is present in the list, qualifies (compare == 0, same
        # stability) and is returned. The newer-OR-equal rule avoids the
        # spurious "no matching version" the strict-newer check produced for
        # already-up-to-date dependencies.
        self.assertEqual(
            server.find_latest_version_for_current(["1.0.0", "2.0.0"], "2.0.0"),
            "2.0.0",
        )

    def test_bare_release_preferred_over_compat_suffix_build(self):
        # #325: get_latest_version picked "0.8.0-0.6.x-compat" over "0.8.0".
        # The candidate list mirrors how the server actually feeds this
        # function — sorted ascending via compare_versions (server.py:557)
        # before selection — so this exercises the real selection pipeline,
        # not just the comparator in isolation.
        raw = ["0.7.0", "0.8.0-0.6.x-compat", "0.8.0"]
        versions = sorted(set(raw), key=functools.cmp_to_key(server.compare_versions))
        self.assertEqual(
            server.find_latest_version_for_current(versions, "0.7.0"),
            "0.8.0",
        )

    def test_preview_is_not_an_upgrade_from_stable(self):
        # #70: from a stable pin, a newer preview on the next core is less
        # stable and must not be offered. The stable completion of a preview
        # pin is offered.
        preview = sorted(
            ["1.0.0", "1.1.0-preview.1"],
            key=functools.cmp_to_key(server.compare_versions),
        )
        self.assertEqual(
            server.find_latest_version_for_current(preview, "1.0.0"),
            "1.0.0",
        )
        same_line = sorted(
            ["1.0.0-preview.1", "1.0.0"],
            key=functools.cmp_to_key(server.compare_versions),
        )
        self.assertEqual(
            server.find_latest_version_for_current(same_line, "1.0.0-preview.1"),
            "1.0.0",
        )
        newer_preview = sorted(
            ["1.0.0-preview.1", "1.0.0-preview.2"],
            key=functools.cmp_to_key(server.compare_versions),
        )
        self.assertEqual(
            server.find_latest_version_for_current(newer_preview, "1.0.0-preview.1"),
            "1.0.0-preview.2",
        )


# ---------------------------------------------------------------------------
# compare_versions
# ---------------------------------------------------------------------------

class CompareVersionsTest(unittest.TestCase):

    def test_compares_numeric_cores(self):
        # mirrors compare.test.ts: "compares numeric cores"
        self.assertLess(server.compare_versions("1.0.0", "2.0.0"), 0)
        self.assertGreater(server.compare_versions("2.0.0", "1.9.9"), 0)
        self.assertEqual(server.compare_versions("1.0.0", "1.0.0"), 0)

    def test_orders_stable_above_prerelease_of_same_core(self):
        # mirrors compare.test.ts: "orders stable above pre-release of same core"
        self.assertGreater(server.compare_versions("2.0.0", "2.0.0-RC1"), 0)

    def test_orders_prereleases_relative_to_each_other(self):
        # mirrors compare.test.ts: "orders pre-releases relative to each other"
        self.assertGreater(server.compare_versions("2.0.0-RC1", "2.0.0-beta1"), 0)
        self.assertLess(server.compare_versions("2.0.0-alpha1", "2.0.0-beta1"), 0)

    def test_bare_release_outranks_same_core_qualifier_suffix(self):
        # Unknown qualifier sorts below snapshot, so the bare stable of the
        # same core is newer.
        self.assertIsNone(server.classify_version("0.8.0-0.6.x-compat"))
        self.assertGreater(server.compare_versions("0.8.0", "0.8.0-0.6.x-compat"), 0)
        self.assertLess(server.compare_versions("0.8.0-0.6.x-compat", "0.8.0"), 0)

    def test_bare_release_outranks_digitless_qualifier_suffix(self):
        # An unknown digitless suffix is not stable and sorts below the bare core.
        self.assertIsNone(server.classify_version("0.8.0-compat"))
        self.assertGreater(server.compare_versions("0.8.0", "0.8.0-compat"), 0)
        self.assertLess(server.compare_versions("0.8.0-compat", "0.8.0"), 0)

    def test_both_prerelease_number_tails_still_compare_correctly(self):
        # #325 regression guard: the fix must not break ordering when BOTH
        # sides carry a numeric suffix — rc.2 still ranks above rc.1.
        self.assertGreater(server.compare_versions("1.0.0-rc.2", "1.0.0-rc.1"), 0)
        self.assertLess(server.compare_versions("1.0.0-rc.1", "1.0.0-rc.2"), 0)

    def test_both_suffixed_same_class_falls_back_to_prerelease_tail(self):
        # #325 regression guard: when BOTH sides carry a suffix, the new
        # suffix-presence branch must not fire (true == true), so ordering
        # still falls through to the existing prerelease-number tail compare
        # unaffected — "beta" (no digits) ranks below "beta2" exactly as
        # before this fix, both within the same classify_version() class.
        self.assertEqual(server.classify_version("1.0.0-beta"), server.classify_version("1.0.0-beta2"))
        self.assertLess(server.compare_versions("1.0.0-beta", "1.0.0-beta2"), 0)
        self.assertGreater(server.compare_versions("1.0.0-beta2", "1.0.0-beta"), 0)

    def test_release_aliases_of_one_core_compare_equal(self):
        # .Final / .RELEASE are qualifiers, not numeric core segments, and
        # they compare equal to the bare version. Display order is separate.
        self.assertEqual(server.compare_versions("1.0.0", "1.0.0.Final"), 0)
        self.assertEqual(server.compare_versions("1.0.0.Final", "1.0.0"), 0)
        self.assertEqual(server.compare_versions("1.0.0", "1.0.0.RELEASE"), 0)
        self.assertEqual(server.compare_versions("1.0.0-ga", "1.0.0.RELEASE"), 0)

    def test_numeric_fourth_segment_still_part_of_core(self):
        # #331: pure numeric dotted tails remain in the core.
        self.assertLess(server.compare_versions("1.0.0", "1.0.0.1"), 0)
        self.assertGreater(server.compare_versions("1.0.0.1", "1.0.0"), 0)

    def test_bare_release_outranks_no_separator_qualifier(self):
        # #331: glued / underscore free-text tails are qualifiers.
        self.assertGreater(server.compare_versions("1.0.0", "1.0.0legacy"), 0)
        self.assertLess(server.compare_versions("1.0.0legacy", "1.0.0"), 0)
        self.assertGreater(server.compare_versions("1.0.0", "1.0.0_legacy"), 0)
        self.assertLess(server.compare_versions("1.0.0_legacy", "1.0.0"), 0)

    def test_preview_eap_and_dev_order_by_class_then_ordinal(self):
        # preview/eap are alpha, dev/nightly are snapshot, and the number
        # attached to the token is the ordinal. A +N on that token is too.
        self.assertGreater(server.compare_versions("1.0.0", "1.0.0-preview.1"), 0)
        self.assertGreater(server.compare_versions("1.0.0-beta1", "1.0.0-preview.1"), 0)
        self.assertLess(server.compare_versions("1.0.0-nightly", "1.0.0-preview.1"), 0)
        self.assertLess(server.compare_versions("1.0.0-preview.9", "1.0.0-preview.10"), 0)
        self.assertLess(server.compare_versions("1.1.1-eap9", "1.1.1-eap13"), 0)
        self.assertLess(server.compare_versions("2.1.20-dev-9", "2.1.20-dev-10"), 0)
        self.assertLess(server.compare_versions("1.0.0-nightly.9", "1.0.0-nightly.10"), 0)
        self.assertLess(server.compare_versions("17-ea+3", "17-ea+4"), 0)
        self.assertLess(server.compare_versions("17-ea+9", "17-ea+10"), 0)
        self.assertGreater(server.compare_versions("17", "17-ea+3"), 0)
        self.assertGreater(server.compare_versions("1.0.0", "1.0.0-devtools"), 0)
        # A +N on a stable version is not an ordinal. String order keeps +10 below +9.
        self.assertLess(server.compare_versions("1.0.0+10", "1.0.0+9"), 0)
        self.assertEqual(server.classify_version("1.0.0-rc-preview"), "alpha")
        self.assertEqual(server.classify_version("1.0.0-dev-preview"), "snapshot")

    def test_symmetric_free_text_suffixes_ignore_incidental_digits(self):
        # #331: free-text digits must not act as ordinal prerelease numbers.
        # Lexicographic fallback: "0.8.0-0.6.x-compat" < "0.8.0-compat".
        self.assertGreater(
            server.compare_versions("0.8.0-compat", "0.8.0-0.6.x-compat"),
            0,
        )
        self.assertLess(
            server.compare_versions("0.8.0-0.6.x-compat", "0.8.0-compat"),
            0,
        )


# ---------------------------------------------------------------------------
# get_upgrade_type
# ---------------------------------------------------------------------------

class GetUpgradeTypeTest(unittest.TestCase):

    def test_detects_major_upgrade(self):
        # mirrors compare.test.ts: "detects major upgrade"
        self.assertEqual(server.get_upgrade_type("1.0.0", "2.0.0"), "major")

    def test_detects_minor_upgrade(self):
        # mirrors compare.test.ts: "detects minor upgrade"
        self.assertEqual(server.get_upgrade_type("1.0.0", "1.1.0"), "minor")

    def test_detects_patch_upgrade(self):
        # mirrors compare.test.ts: "detects patch upgrade"
        self.assertEqual(server.get_upgrade_type("1.0.0", "1.0.1"), "patch")

    def test_returns_none_for_downgrade(self):
        # mirrors compare.test.ts: "returns none for downgrade"
        self.assertEqual(server.get_upgrade_type("2.0.0", "1.5.0"), "none")
        self.assertEqual(server.get_upgrade_type("1.5.0", "1.3.9"), "none")

    def test_handles_two_segment_versions(self):
        # mirrors compare.test.ts: "handles two-segment versions"
        self.assertEqual(server.get_upgrade_type("1.0", "2.0"), "major")
        self.assertEqual(server.get_upgrade_type("1.0", "1.1"), "minor")

    def test_prerelease_to_stable_is_patch(self):
        # mirrors compare.test.ts: "classifies pre-release → stable as patch"
        self.assertEqual(server.get_upgrade_type("2.0.0-beta-1", "2.0.0"), "patch")
        self.assertEqual(server.get_upgrade_type("2.0.0-rc-1", "2.0.0"), "patch")

    def test_prerelease_to_higher_prerelease_is_patch(self):
        # mirrors compare.test.ts: "classifies pre-release → higher pre-release as patch"
        self.assertEqual(server.get_upgrade_type("2.0.0-beta-1", "2.0.0-beta-2"), "patch")
        self.assertEqual(server.get_upgrade_type("2.0.0-beta-1", "2.0.0-rc-1"), "patch")
        self.assertEqual(server.get_upgrade_type("2.0.0-alpha", "2.0.0-beta"), "patch")

    def test_returns_none_for_stable_to_prerelease_same_core(self):
        # mirrors compare.test.ts: "returns none for stable → pre-release of same core (downgrade)"
        self.assertEqual(server.get_upgrade_type("2.0.0", "2.0.0-beta-1"), "none")
        self.assertEqual(server.get_upgrade_type("2.0.0-rc-1", "2.0.0-beta-1"), "none")

    def test_core_level_upgrades_across_prerelease_suffixes(self):
        # mirrors compare.test.ts: "still classifies core-level upgrades across pre-release suffixes"
        self.assertEqual(server.get_upgrade_type("1.9.0", "2.0.0-beta-1"), "major")
        self.assertEqual(server.get_upgrade_type("1.3.2-1.4.0-rc", "1.3.2"), "patch")


# ---------------------------------------------------------------------------
# _filter_version_range  (mirrors TS filterVersionRange)
# ---------------------------------------------------------------------------

_RANGE_VERSIONS = ["1.0.0", "1.1.0", "1.2.0", "1.3.0", "2.0.0-beta1", "2.0.0"]


class FilterVersionRangeTest(unittest.TestCase):

    def test_returns_versions_exclusive_from_inclusive_to(self):
        # mirrors range.test.ts: "returns versions between from (exclusive) and to (inclusive)"
        self.assertEqual(
            server._filter_version_range(_RANGE_VERSIONS, "1.0.0", "1.3.0"),
            ["1.1.0", "1.2.0", "1.3.0"],
        )

    def test_includes_prerelease_versions_in_range(self):
        # mirrors range.test.ts: "includes pre-release versions in range"
        self.assertEqual(
            server._filter_version_range(_RANGE_VERSIONS, "1.2.0", "2.0.0"),
            ["1.3.0", "2.0.0-beta1", "2.0.0"],
        )

    def test_empty_when_from_equals_to(self):
        # mirrors range.test.ts: "returns empty array when from and to are the same"
        self.assertEqual(
            server._filter_version_range(_RANGE_VERSIONS, "1.0.0", "1.0.0"),
            [],
        )

    def test_from_not_found_falls_back_to_numeric_comparison(self):
        # mirrors range.test.ts: "returns empty array when fromVersion is not found"
        # Python difference: TS returns [] when fromVersion is absent from the list.
        # Python uses compare_versions, so "0.9.0" acts as a numeric lower bound
        # and all versions between 0.9.0 (exclusive) and 1.3.0 (inclusive) are returned.
        self.assertEqual(
            server._filter_version_range(_RANGE_VERSIONS, "0.9.0", "1.3.0"),
            ["1.0.0", "1.1.0", "1.2.0", "1.3.0"],
        )

    def test_to_not_found_falls_back_to_numeric_comparison(self):
        # mirrors range.test.ts: "returns empty array when toVersion is not found"
        # Python difference: TS returns [] when toVersion is absent from the list.
        # Python uses compare_versions, so "9.9.9" acts as a numeric upper bound
        # and all versions between 1.0.0 (exclusive) and 9.9.9 (inclusive) are returned.
        self.assertEqual(
            server._filter_version_range(_RANGE_VERSIONS, "1.0.0", "9.9.9"),
            ["1.1.0", "1.2.0", "1.3.0", "2.0.0-beta1", "2.0.0"],
        )


def _named_by_rule(pin, rows):
    """The skill's naming rule. Not a server field."""
    if server.version_line(pin) != "release":
        return None
    bare = [
        row for row in rows
        if row.get("stability") == "stable" and row.get("line") == "release"
    ]
    patches = [row for row in bare if row.get("distance") == "patch"]
    if len(bare) == 1 and len(patches) == 1:
        return patches[0]["version"]
    return None


def _by_version(rows):
    return {row["version"]: row for row in rows}


class CandidateCatalogTest(unittest.TestCase):
    """Fixed catalogs. The server returns rows. The naming rule is asserted here."""

    def test_one_patch_is_named(self):
        rows = server.select_candidates(ONE_PATCH["versions"], ONE_PATCH["pin"])
        self.assertEqual([row["version"] for row in rows], ["4.12.1"])
        self.assertEqual(rows[0]["distance"], "patch")
        self.assertEqual(rows[0]["stability"], "stable")
        self.assertEqual(_named_by_rule(ONE_PATCH["pin"], rows), ONE_PATCH["named"])

    def test_wide_jump_returns_every_row_and_names_none(self):
        rows = server.select_candidates(WIDE["versions"], WIDE["pin"])
        by_version = _by_version(rows)
        self.assertEqual(set(by_version), set(WIDE["versions"]))
        self.assertEqual(by_version["1.3.6"]["distance"], "patch")
        self.assertEqual(by_version["1.5.6"]["distance"], "minor")
        self.assertEqual(by_version["2.0.0"]["distance"], "major")
        self.assertEqual(by_version["1.3.7-rc.1"]["distance"], "prerelease")
        self.assertEqual(by_version["2.0.0-alpha.1"]["distance"], "prerelease")
        self.assertIsNone(_named_by_rule(WIDE["pin"], rows))

    def test_alpha_pin_with_stable_names_the_bare_stable(self):
        rows = server.select_candidates(
            ALPHA_AND_STABLE["versions"], ALPHA_AND_STABLE["pin"],
        )
        by_version = _by_version(rows)
        self.assertEqual(set(by_version), {"1.3.0-alpha.5", "1.3.6"})
        self.assertEqual(server.version_line(ALPHA_AND_STABLE["pin"]), "release")
        self.assertEqual(
            _named_by_rule(ALPHA_AND_STABLE["pin"], rows),
            ALPHA_AND_STABLE["named"],
        )

    def test_only_next_alpha_is_a_row_and_is_not_named(self):
        rows = server.select_candidates(
            ONLY_NEXT_ALPHA["versions"], ONLY_NEXT_ALPHA["pin"],
        )
        self.assertEqual([row["version"] for row in rows], ["1.3.0-alpha.5"])
        self.assertIsNone(_named_by_rule(ONLY_NEXT_ALPHA["pin"], rows))

    def test_no_bare_stable_omits_latest(self):
        self.assertIsNone(server.find_bare_stable(NO_BARE_STABLE["versions"]))

    def test_plus_ordinal_on_prerelease_and_bare_stable(self):
        self.assertLess(
            server.compare_versions("17-ea+9", "17-ea+10"), 0,
        )
        self.assertGreater(server.compare_versions("17", "17-ea+10"), 0)
        rows = server.select_candidates(PLUS_ORDINAL["versions"], PLUS_ORDINAL["pin"])
        by_version = _by_version(rows)
        self.assertEqual(set(by_version), {"17-ea+10", "17"})
        self.assertEqual(by_version["17"]["distance"], "patch")
        self.assertEqual(by_version["17-ea+10"]["distance"], "prerelease")

    def test_jre_lines_stay_beside_the_bare_stable(self):
        rows = server.select_candidates(JRE_LINES["versions"], JRE_LINES["pin"])
        by_version = _by_version(rows)
        self.assertIn("1.3.6", by_version)
        self.assertEqual(by_version["1.3.6-jre8"]["line"], "jre8")
        self.assertNotIn("1.3.6-jre-8", by_version)
        self.assertEqual(by_version["1.4.0-jre11"]["line"], "jre11")
        self.assertNotIn("stability", by_version["1.3.6-jre8"])
        self.assertIsNone(_named_by_rule(JRE_LINES["pin"], rows))

    def test_alias_display_prefers_bare_then_release(self):
        self.assertEqual(
            server.find_bare_stable(ALIAS_FROM_OLDER["versions"]),
            "1.0.0",
        )
        rows = server.select_candidates(
            ALIAS_FROM_OLDER["versions"], ALIAS_FROM_OLDER["pin"],
        )
        self.assertEqual([row["version"] for row in rows], [ALIAS_FROM_OLDER["shown"]])
        self.assertEqual(
            server.select_candidates(
                ALIAS_ALREADY_BARE["versions"], ALIAS_ALREADY_BARE["pin"],
            ),
            [],
        )
        self.assertEqual(server.find_bare_stable(ALIAS_ONLY), "1.0.0.RELEASE")

    def test_datestamp_is_not_a_candidate_beside_a_normal_release(self):
        rows = server.select_candidates(DATESTAMP["versions"], DATESTAMP["pin"])
        self.assertEqual(rows, [])
        self.assertEqual(server.find_bare_stable(DATESTAMP["versions"]), "1.0.0")
        self.assertIsNone(server.find_bare_stable(["20030203.000550"]))


if __name__ == "__main__":
    unittest.main()
