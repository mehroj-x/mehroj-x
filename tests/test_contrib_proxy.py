import copy
import io
from pathlib import Path
import sys
import unittest
from unittest import mock
from urllib.error import URLError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import contrib_proxy as proxy

ARCHIVE = {
    "calendar": {"2026-01-01": 6, "2025-01-01": 2},
    "backfill": {},
    "gitlabFrom": "2026-01-02",
    "totals": {"commits": 40, "issues": 8, "pullRequests": 4, "reviews": 0, "repositories": 2},
    "languages": [{"name": "Python", "color": "#3572A5", "count": 20}],
    "stars": 20,
    "forks": 1,
}


def response():
    return {"data": {"user": {
        "contributionsCollection": {
            "contributionCalendar": {"isHalloween": False, "totalContributions": 3, "weeks": [{"contributionDays": [
                {"contributionCount": 1, "contributionLevel": "FOURTH_QUARTILE", "date": "2026-01-01"},
                {"contributionCount": 2, "contributionLevel": "FOURTH_QUARTILE", "date": "2026-01-02"},
                {"contributionCount": 0, "contributionLevel": "NONE", "date": "2026-01-03"},
            ]}]},
            "commitContributionsByRepository": [],
            "totalCommitContributions": 3,
            "totalIssueContributions": 0,
            "totalPullRequestContributions": 0,
            "totalPullRequestReviewContributions": 0,
            "totalRepositoryContributions": 1,
        },
        "repositories": {"edges": [], "nodes": []},
    }}}


class MergeTests(unittest.TestCase):
    def merged(self):
        return proxy.merge(response(), ARCHIVE)["data"]["user"]

    def test_adds_archived_days_inside_the_window_only(self):
        calendar = self.merged()["contributionsCollection"]["contributionCalendar"]
        days = calendar["weeks"][0]["contributionDays"]
        self.assertEqual([day["contributionCount"] for day in days], [7, 2, 0])
        self.assertEqual(calendar["totalContributions"], 9)

    def test_levels_are_recomputed_from_merged_counts(self):
        days = self.merged()["contributionsCollection"]["contributionCalendar"]["weeks"][0]["contributionDays"]
        self.assertEqual([day["contributionLevel"] for day in days],
                         ["FOURTH_QUARTILE", "SECOND_QUARTILE", "NONE"])

    def test_level_thresholds_ignore_a_single_outlier(self):
        step = proxy.level_step([1] * 99 + [29] * 99 + [118, 118, 305])
        self.assertLess(step * 4, 305)
        self.assertEqual(proxy.level(305, step), "FOURTH_QUARTILE")

    def test_a_single_active_day_gets_a_level(self):
        self.assertEqual(proxy.level(5, proxy.level_step([5])), "FOURTH_QUARTILE")

    def test_totals_shrink_with_the_share_of_archive_still_in_the_window(self):
        collection = self.merged()["contributionsCollection"]
        self.assertEqual(collection["totalCommitContributions"], 3 + 30)
        self.assertEqual(collection["totalIssueContributions"], 6)
        self.assertEqual(collection["commitContributionsByRepository"][0]["contributions"]["totalCount"], 15)

    def test_backfill_adds_to_the_calendar_but_not_the_yearly_totals(self):
        archive = dict(ARCHIVE, backfill={"2026-01-03": 4})
        collection = proxy.merge(response(), archive)["data"]["user"]["contributionsCollection"]
        calendar = collection["contributionCalendar"]
        self.assertEqual(calendar["weeks"][0]["contributionDays"][2]["contributionCount"], 4)
        self.assertEqual(calendar["totalContributions"], 13)
        self.assertEqual(collection["totalCommitContributions"], 3 + 30)

    def test_gitlab_days_take_the_higher_count_from_the_start_date(self):
        gitlab = {"2026-01-01": 99, "2026-01-02": 1, "2026-01-03": 5}
        collection = proxy.merge(response(), ARCHIVE, gitlab)["data"]["user"]["contributionsCollection"]
        days = collection["contributionCalendar"]["weeks"][0]["contributionDays"]
        self.assertEqual([day["contributionCount"] for day in days], [7, 2, 5])
        self.assertEqual(collection["contributionCalendar"]["totalContributions"], 14)

    def test_gitlab_failure_renders_from_github_alone(self):
        with mock.patch.object(proxy, "urlopen", side_effect=URLError("down")):
            self.assertEqual(proxy.gitlab_days(), {})

    def test_gitlab_values_that_are_not_counts_are_ignored(self):
        body = io.BytesIO(b'{"2026-01-01": 3, "2026-01-02": "x"}')
        with mock.patch.object(proxy, "urlopen", return_value=body):
            self.assertEqual(proxy.gitlab_days(), {"2026-01-01": 3})

    def test_stars_and_forks_are_added_in_full(self):
        self.assertEqual(self.merged()["repositories"]["nodes"], [{"forkCount": 1, "stargazerCount": 20}])

    def test_errors_and_later_pages_pass_through_unchanged(self):
        for payload in ({"errors": [{"message": "nope"}]}, {"data": {"user": None}},
                        {"data": {"user": {"repositories": {"edges": [], "nodes": []}}}}):
            self.assertEqual(proxy.merge(copy.deepcopy(payload), ARCHIVE), payload)


class OfflineData:
    ZONE = proxy.ZoneInfo("Asia/Tashkent")
    TODAY = proxy.datetime(2026, 1, 3).date()  # a Saturday

    def cache(self):
        return {"owners": {}, "repositories": {"aaaaaaaaaaaa": "Me", "bbbbbbbbbbbb": "Me"}, "commits": {
            # 2026-01-02 21:00 UTC is already 2026-01-03 in Tashkent
            "1" * 16: ["2026-01-02T21:00Z", "aaaaaaaaaaaa"],
            "2" * 16: ["2026-01-02T05:00Z", "aaaaaaaaaaaa"],
            "3" * 16: ["2026-01-02T06:00Z", "bbbbbbbbbbbb"],
            "4" * 16: ["2024-06-01T06:00Z", "bbbbbbbbbbbb"],  # outside the window
        }}

    def build(self, languages=None, gitlab=None, stars=()):
        return proxy.synthesize(self.cache(), languages or {}, copy.deepcopy(ARCHIVE), gitlab or {}, list(stars),
                                self.ZONE, self.TODAY)

    def days(self, payload):
        calendar = payload["data"]["user"]["contributionsCollection"]["contributionCalendar"]
        return {day["date"]: day for week in calendar["weeks"] for day in week["contributionDays"]}



class OfflineTests(OfflineData, unittest.TestCase):
    def test_window_runs_from_a_sunday_to_today_in_whole_weeks(self):
        payload = self.build()
        weeks = payload["data"]["user"]["contributionsCollection"]["contributionCalendar"]["weeks"]
        first = proxy.datetime.fromisoformat(weeks[0]["contributionDays"][0]["date"]).date()
        self.assertEqual(first.weekday(), 6)
        self.assertEqual(weeks[-1]["contributionDays"][-1]["date"], "2026-01-03")
        self.assertTrue(all(len(week["contributionDays"]) == 7 for week in weeks))

    def test_days_take_the_highest_source_in_local_time(self):
        days = self.build(gitlab={"2026-01-03": 5})
        self.assertEqual(days and self.days(days)["2026-01-02"]["contributionCount"], 2)
        self.assertEqual(self.days(days)["2026-01-03"]["contributionCount"], 5)  # GitLab beats 1 commit
        self.assertEqual(self.days(days)["2026-01-01"]["contributionCount"], 6)  # archived day

    def test_totals_and_language_ring(self):
        languages = {"aaaaaaaaaaaa": {"name": "Python", "color": "#3572A5"}, "bbbbbbbbbbbb": {"name": "Dart", "color": "#00B4AB"}}
        collection = self.build(languages, stars=[(3, 1)])["data"]["user"]["contributionsCollection"]
        self.assertEqual(collection["totalCommitContributions"], 40)  # archive share beats 3 cached commits
        ring = {entry["repository"]["primaryLanguage"]["name"]: entry["contributions"]["totalCount"]
                for entry in collection["commitContributionsByRepository"]}
        self.assertEqual(ring, {"Python": 2, "Dart": 1})

    def test_ring_falls_back_to_the_archive_without_languages(self):
        collection = self.build()["data"]["user"]["contributionsCollection"]
        names = [entry["repository"]["primaryLanguage"]["name"] for entry in collection["commitContributionsByRepository"]]
        self.assertEqual(names, ["Python"])

    def test_stars_include_gitlab_projects(self):
        nodes = self.build(stars=[(3, 1), (0, 0)])["data"]["user"]["repositories"]["nodes"]
        self.assertEqual(sum(node["stargazerCount"] for node in nodes), 23)


class CombineTests(OfflineData, unittest.TestCase):
    def live(self):
        payload = response()
        collection = payload["data"]["user"]["contributionsCollection"]
        collection["contributionCalendar"]["weeks"][0]["contributionDays"][2]["contributionCount"] = 9
        collection["totalPullRequestContributions"] = 2
        payload["data"]["user"]["repositories"]["nodes"] = [{"forkCount": 0, "stargazerCount": 4}]
        return payload

    def test_days_take_the_higher_of_local_data_and_github(self):
        days = self.days(proxy.combine(self.live(), self.build()))
        self.assertEqual(days["2026-01-03"]["contributionCount"], 9)  # live GitHub beats 1 cached commit
        self.assertEqual(days["2026-01-02"]["contributionCount"], 2)  # same commits in both: not added up
        self.assertEqual(days["2026-01-03"]["contributionLevel"], "FOURTH_QUARTILE")

    def test_pull_requests_and_stars_add_to_local_data(self):
        user = proxy.combine(self.live(), self.build())["data"]["user"]
        self.assertEqual(user["contributionsCollection"]["totalPullRequestContributions"],
                         self.build()["data"]["user"]["contributionsCollection"]["totalPullRequestContributions"] + 2)
        self.assertEqual(sum(node["stargazerCount"] for node in user["repositories"]["nodes"]), 24)
        self.assertEqual(len(user["repositories"]["edges"]), len(user["repositories"]["nodes"]))

    def test_other_responses_pass_through(self):
        payload = {"data": {"user": {"repositories": {"edges": [], "nodes": []}}}}
        self.assertEqual(proxy.combine(copy.deepcopy(payload), self.build()), payload)


if __name__ == "__main__":
    unittest.main()
