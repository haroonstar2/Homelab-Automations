import unittest

from devbot.commit_titles import propose_commit_title, validate_commit_title


class CommitTitleTests(unittest.TestCase):
    def test_proposes_scoped_descriptive_title(self):
        request = "Context first.\nAdd paginated match history service.\nReturn DTO types."
        title = propose_commit_title(
            request, ["apps/core-api/src/games/games.service.ts"]
        )
        self.assertEqual(title, "feat(core-api): add paginated match history service")

    def test_rejects_generic_or_invalid_title(self):
        for title in ("agent: fix CI", "fix(core-api): fix ci", "update files"):
            with self.subTest(title=title), self.assertRaises(ValueError):
                validate_commit_title(title)

    def test_accepts_owner_supplied_conventional_title(self):
        self.assertEqual(
            validate_commit_title("fix(core-api): remove duplicate DTO declarations"),
            "fix(core-api): remove duplicate DTO declarations",
        )
