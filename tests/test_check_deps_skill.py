"""check-deps skill text for issue 27. Reads the skill file; no SDK."""

import os
import unittest


_SKILL = os.path.join(
    os.path.dirname(__file__), "..", "plugin", "skills", "check-deps", "SKILL.md"
)


class CheckDepsSkillTest(unittest.TestCase):
    def setUp(self):
        with open(_SKILL, "r", encoding="utf-8") as fh:
            self.text = fh.read()

    def test_not_updated_section_names_the_diagnostic(self):
        self.assertIn("Not updated", self.text)
        for phrase in (
            "coordinate",
            "from → to",
            "failed command",
            "unresolved symbol",
            "spotless",
            "requiresCompileSdk",
            "requiresMinSdk",
        ):
            self.assertIn(phrase, self.text)

    def test_toolchain_confirm_includes_compose_and_minor_or_patch(self):
        for phrase in (
            "com.android.application",
            "com.android.library",
            "com.android.test",
            "com.android.dynamic-feature",
            "com.android.tools.build:gradle",
            "org.jetbrains.kotlin.android",
            "org.jetbrains.kotlin.jvm",
            "org.jetbrains.kotlin.multiplatform",
            "org.jetbrains.kotlin:kotlin-gradle-plugin",
            "com.google.devtools.ksp",
            "org.jetbrains.kotlin.plugin.compose",
            "androidx.compose.compiler:compiler",
            "minor",
            "patch",
            "own confirm",
            "Compose compiler",
        ):
            self.assertIn(phrase, self.text)

    def test_matrix_gap_is_said_out_loud(self):
        self.assertIn("agp_unknown", self.text)
        self.assertIn("kotlin_unknown", self.text)
        self.assertIn("does not cover that version", self.text)

    def test_sdk_floor_refuses_the_bump(self):
        self.assertIn(
            "Do not edit `compileSdk` or `minSdk` to make the bump pass.",
            self.text,
        )

    def test_manual_upgrade_required_is_not_the_outcome(self):
        self.assertNotIn(
            'Attempt trivial fixes; otherwise revert that entry and note "manual upgrade required".',
            self.text,
        )
        self.assertNotIn('note "manual upgrade required"', self.text)
        if "manual upgrade required" in self.text:
            self.assertIn("not a substitute", self.text)
            self.assertIn("not the outcome", self.text)

    def test_catalog_fallback_keys_off_gradle_errors_not_missing_resolved_by(self):
        self.assertIn("gradleErrors", self.text)
        self.assertIn("All Gradle dependency tasks failed.", self.text)
        self.assertIn("No production configurations resolved", self.text)
        self.assertNotIn("audit does not set `resolvedBy`", self.text)
        self.assertNotIn("missing `resolvedBy`", self.text)
        self.assertIn("not that diagnostic", self.text)
        self.assertIn("Stopped early after exceeding the", self.text)


if __name__ == "__main__":
    unittest.main()
