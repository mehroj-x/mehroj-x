#!/usr/bin/env python3
"""Local GraphQL endpoint for the 3D contribution chart (github-profile-3d-contrib).

Online, it forwards the renderer's query to GitHub and adds data/contributions-archive.json,
the previous account's history, to the response. Offline (--offline), it answers without
GitHub: days come from the commit cache (data/commit-habits.json), the archive and GitLab's
activity calendar, and the language ring from data/repo-languages.json. Combined (--combine),
it answers like offline without GitLab's calendar (the cache already has GitLab commits) and
adds what only GitHub knows: the live account's days, issues, pull requests, reviews and stars. Point the renderer's GITHUB_ENDPOINT here in every mode. Archived
days leave the chart as they leave the one-year window.
"""

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
from collections import Counter
from datetime import datetime, timedelta
from statistics import quantiles
import sys
from zoneinfo import ZoneInfo
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
UPSTREAM = "https://api.github.com/graphql"
GITLAB_CALENDAR = "https://gitlab.com/users/mekhrojmajidov/calendar.json"
GITLAB_PROJECTS = "https://gitlab.com/api/v4/users/mekhrojmajidov/projects?per_page=100&simple=true"
LEVELS = ("NONE", "FIRST_QUARTILE", "SECOND_QUARTILE", "THIRD_QUARTILE", "FOURTH_QUARTILE")
TOTAL_FIELDS = {
    "totalCommitContributions": "commits",
    "totalIssueContributions": "issues",
    "totalPullRequestContributions": "pullRequests",
    "totalPullRequestReviewContributions": "reviews",
    "totalRepositoryContributions": "repositories",
}


def level_step(active):
    # Equal steps up to the 99th percentile of active days: GitHub does not publish its rule,
    # but this reproduces every day of the archived chart.
    cap = quantiles(active, n=100, method="inclusive")[98] if len(active) > 1 else sum(active)
    return cap / 4


def level(count, step):
    if not count:
        return LEVELS[0]
    return LEVELS[min(4, 1 + sum(count > step * multiple for multiple in (1, 2, 3)))]


def merge(payload, archive, gitlab=None):
    """Add the archive to a first-page response; any other response is returned unchanged."""
    user = (payload.get("data") or {}).get("user") or {}
    collection = user.get("contributionsCollection")
    if not collection:
        return payload

    archived_days = archive["calendar"]
    calendar = collection["contributionCalendar"]
    days = [day for week in calendar["weeks"] for day in week["contributionDays"]]
    in_window = 0
    for day in days:
        date = day["date"][:10]
        archived = archived_days.get(date, 0)
        in_window += archived
        # Backfilled days cover the move between accounts: they count on the calendar only,
        # because the archive's yearly totals and languages were recorded without them.
        day["contributionCount"] += archived + archive["backfill"].get(date, 0)
        # The same work can appear on both sites, so a day takes the higher count, not the sum.
        if date >= archive["gitlabFrom"]:
            day["contributionCount"] = max(day["contributionCount"], (gitlab or {}).get(date, 0))
    step = level_step([day["contributionCount"] for day in days if day["contributionCount"]])
    for day in days:
        day["contributionLevel"] = level(day["contributionCount"], step)
    calendar["totalContributions"] = sum(day["contributionCount"] for day in days)

    # The archive has yearly totals only, so they shrink in step with its days leaving the window.
    share = in_window / (sum(archived_days.values()) or 1)
    for field, key in TOTAL_FIELDS.items():
        collection[field] += round(archive["totals"][key] * share)
    collection["commitContributionsByRepository"] += [
        {"contributions": {"totalCount": round(language["count"] * share)},
         "repository": {"primaryLanguage": {"name": language["name"], "color": language["color"]}}}
        for language in archive["languages"]
    ]
    user["repositories"]["nodes"].append({"forkCount": archive["forks"], "stargazerCount": archive["stars"]})
    return payload


def calendar_dates(today):
    """GitHub's window: weeks from Sunday to Saturday, from a year before today up to today."""
    start = today - timedelta(days=365)
    start -= timedelta(days=(start.weekday() + 1) % 7)
    return [start + timedelta(days=offset) for offset in range((today - start).days + 1)]


def synthesize(cache, languages, archive, gitlab, stars, zone, today):
    """
    The renderer's first-page response, built without GitHub. A day counts the most of its
    commits in the cache, its archived contributions and its GitLab activity: the same work can
    appear in more than one of them, so they are not added up.
    """
    dates = calendar_dates(today)
    first = dates[0].isoformat()
    commits_per_day, commits_per_repository = Counter(), Counter()
    for hour, repository in cache["commits"].values():
        day = datetime.fromisoformat(hour.replace("Z", "+00:00")).astimezone(zone).date().isoformat()
        if first <= day <= today.isoformat():
            commits_per_day[day] += 1
            if repository:
                commits_per_repository[repository] += 1

    archived_days = archive["calendar"]
    weeks, week = [], []
    in_window = 0
    for current in dates:
        day = current.isoformat()
        in_window += archived_days.get(day, 0)
        count = max(commits_per_day[day], archived_days.get(day, 0) + archive["backfill"].get(day, 0),
                    (gitlab or {}).get(day, 0))
        week.append({"contributionCount": count, "contributionLevel": LEVELS[0], "date": day})
        if current.weekday() == 5:  # Saturday ends a week
            weeks.append({"contributionDays": week})
            week = []
    if week:
        weeks.append({"contributionDays": week})
    days = [day for entry in weeks for day in entry["contributionDays"]]
    active = [day["contributionCount"] for day in days if day["contributionCount"]]
    step = level_step(active) if active else 1
    for day in days:
        day["contributionLevel"] = level(day["contributionCount"], step)

    # Issues, pull requests and reviews can't be counted without GitHub: only archived ones remain
    share = in_window / (sum(archived_days.values()) or 1)
    totals = {field: round(archive["totals"][key] * share) for field, key in TOTAL_FIELDS.items()}
    totals["totalCommitContributions"] = max(sum(commits_per_day.values()), totals["totalCommitContributions"])
    totals["totalRepositoryContributions"] = max(len(commits_per_repository), totals["totalRepositoryContributions"])

    by_language = Counter()
    colours = {}
    for repository, count in commits_per_repository.items():
        language = languages.get(repository)
        if language:
            by_language[language["name"]] += count
            colours[language["name"]] = language["color"]
    if by_language:
        ring = [(name, colours[name], count) for name, count in by_language.items()]
    else:
        ring = [(language["name"], language["color"], round(language["count"] * share)) for language in archive["languages"]]

    nodes = [{"forkCount": archive["forks"], "stargazerCount": archive["stars"]}]
    nodes += [{"forkCount": forks, "stargazerCount": count} for count, forks in stars]
    return {"data": {"user": {
        "contributionsCollection": {
            "contributionCalendar": {"isHalloween": False, "totalContributions": sum(active), "weeks": weeks},
            "commitContributionsByRepository": [
                {"contributions": {"totalCount": count}, "repository": {"primaryLanguage": {"name": name, "color": colour}}}
                for name, colour, count in ring if count
            ],
            **totals,
        },
        "repositories": {"edges": [{"cursor": str(index)} for index in range(len(nodes))], "nodes": nodes},
    }}}


def combine(payload, built):
    """
    `built` (a synthesize() response) with the live account's GitHub data added. Days take the
    higher count, as the same commits are in both; issues, pull requests and reviews are only on
    GitHub, so they add up. Anything but a first-page response is returned unchanged.
    """
    user = (payload.get("data") or {}).get("user") or {}
    collection = user.get("contributionsCollection")
    if not collection:
        return payload
    result = built["data"]["user"]
    mine = result["contributionsCollection"]
    live = {day["date"][:10]: day["contributionCount"]
            for week in collection["contributionCalendar"]["weeks"] for day in week["contributionDays"]}
    days = [day for week in mine["contributionCalendar"]["weeks"] for day in week["contributionDays"]]
    for day in days:
        day["contributionCount"] = max(day["contributionCount"], live.get(day["date"], 0))
    active = [day["contributionCount"] for day in days if day["contributionCount"]]
    step = level_step(active) if active else 1
    for day in days:
        day["contributionLevel"] = level(day["contributionCount"], step)
    mine["contributionCalendar"]["totalContributions"] = sum(active)
    for field in ("totalIssueContributions", "totalPullRequestContributions", "totalPullRequestReviewContributions"):
        mine[field] += collection.get(field) or 0
    for field in ("totalCommitContributions", "totalRepositoryContributions"):
        mine[field] = max(mine[field], collection.get(field) or 0)
    nodes = result["repositories"]["nodes"] + list((user.get("repositories") or {}).get("nodes") or [])
    result["repositories"] = {"edges": [{"cursor": str(index)} for index in range(len(nodes))], "nodes": nodes}
    return built


def gitlab_stars():
    """(stars, forks) of each public GitLab project; none when GitLab is unavailable."""
    try:
        with urlopen(Request(GITLAB_PROJECTS, headers={"User-Agent": "profile-readme"}), timeout=30) as response:
            projects = json.load(response)
    except (OSError, ValueError):
        return []
    if not isinstance(projects, list):
        return []
    return [(project.get("star_count") or 0, project.get("forks_count") or 0)
            for project in projects if isinstance(project, dict)]


def gitlab_days():
    # GitLab only adds to the chart: when it is unavailable, render from GitHub alone.
    try:
        with urlopen(Request(GITLAB_CALENDAR, headers={"User-Agent": "profile-readme"}), timeout=30) as response:
            days = json.load(response)
    except (OSError, ValueError):
        return {}
    if not isinstance(days, dict):
        return {}
    return {date: count for date, count in days.items() if isinstance(count, int)}


def fetch(body, token):
    headers = {"User-Agent": "profile-readme", "Authorization": f"Bearer {token}",
               "Content-Type": "application/json"}
    with urlopen(Request(UPSTREAM, body, headers), timeout=60) as response:
        return json.load(response)


def handler(archive, token, offline=None, combined=False):
    class Handler(BaseHTTPRequestHandler):
        def respond(self, status, payload):
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            self.respond(200 if self.path == "/health" else 404, {})

        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            try:
                if offline and combined:
                    self.respond(200, combine(fetch(body, token), offline()))
                    return
                if offline:
                    self.respond(200, offline())
                    return
                self.respond(200, merge(fetch(body, token), archive, gitlab_days()))
            except (HTTPError, URLError, ValueError, KeyError, TypeError):
                # A failed render keeps the published chart. Details stay out of the public logs.
                self.respond(502, {"errors": [{"message": "Contribution data unavailable"}]})

        def log_message(self, *args):
            pass

    return Handler


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--archive", type=Path, default=ROOT / "data" / "contributions-archive.json")
    parser.add_argument("--offline", action="store_true", help="Answer from local data instead of GitHub")
    parser.add_argument("--combine", action="store_true",
                        help="Answer from local data plus the live account's GitHub data (needs GITHUB_TOKEN)")
    parser.add_argument("--cache", type=Path, default=ROOT / "data" / "commit-habits.json")
    parser.add_argument("--languages", type=Path, default=ROOT / "data" / "repo-languages.json")
    parser.add_argument("--timezone", default="Asia/Tashkent")
    args = parser.parse_args()
    archive = json.loads(args.archive.read_text(encoding="utf-8"))
    offline = None
    if args.offline or args.combine:
        zone = ZoneInfo(args.timezone)
        cache = json.loads(args.cache.read_text(encoding="utf-8"))
        languages = json.loads(args.languages.read_text(encoding="utf-8")) if args.languages.exists() else {}
        # Fetched once: the renderer may ask more than once. Combined runs count GitLab commits
        # from the API (commit_habits.py --gitlab), and GitLab's calendar would also count the
        # nightly push to the GitLab profile, giving every day at least one contribution.
        gitlab, stars = ({} if args.combine else gitlab_days()), gitlab_stars()
        offline = lambda: synthesize(cache, languages, archive, gitlab, stars, zone, datetime.now(zone).date())
    if args.offline and not args.combine:
        token = None
    else:
        token = os.getenv("GITHUB_TOKEN")
        if not token:
            parser.error("Set GITHUB_TOKEN; the GraphQL API requires a token")
    ThreadingHTTPServer(("127.0.0.1", args.port), handler(archive, token, offline, args.combine)).serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
