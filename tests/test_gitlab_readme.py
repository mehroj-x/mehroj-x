from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import gitlab_readme  # noqa: E402

README = """<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="images/name-dark.svg">
    <img src="images/name-light.svg" width="420" height="56" alt="Name">
  </picture>
</p>
  <a href="https://go.mehroj.dev/site?utm_source=github&utm_medium=readme">Site</a>
  <a href="https://go.mehroj.dev/gitlab?utm_source=github">GitLab</a> <!-- github-only -->
- kept
- dropped <!-- github-only -->
"""


class GitLabReadmeTests(unittest.TestCase):
    def test_gitlab_copy(self):
        self.assertEqual(gitlab_readme.gitlab_readme(README), """<p align="center">
  <img src="images/name.svg" width="420" height="56" alt="Name">
</p>
  <a href="https://go.mehroj.dev/site?utm_source=gitlab&utm_medium=readme">Site</a>
- kept
""")

    def test_a_readme_without_the_banner_is_refused(self):
        with self.assertRaises(ValueError):
            gitlab_readme.gitlab_readme("no banner")

    def test_the_real_readme_converts(self):
        text = gitlab_readme.gitlab_readme((Path(__file__).resolve().parents[1] / "README.md").read_text(encoding="utf-8"))
        self.assertNotIn("utm_source=github", text)
        self.assertNotIn("github-only", text)
        self.assertIn('src="images/name.svg"', text)


if __name__ == "__main__":
    unittest.main()
