#!/usr/bin/env python3
"""Write commit habits and where commits went into README.md, without dependencies.

Commits are read from GitHub and, with --gitlab, from every GitLab project the token can see.

Commits are cached in data/commit-habits.json so that repositories you lose access to
keep counting. The repository is public, so the cache holds, per commit, a hash of its id,
the UTC hour it was authored and a hash of its repository, plus the owner of each such
repository: no repository names, messages or commit ids.

Repositories that were never cached before access was lost can be imported once from a
local clone with --import-git; their commits then stay in the cache like any other.
"""

import argparse
from collections import Counter
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

ROOT = Path(__file__).resolve().parents[1]
DAY_TIMES = ["🌞 Morning", "🌆 Daytime", "🌃 Evening", "🌙 Night"]
WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
RETRYABLE = (403, 429, 500, 502, 503, 504)
LOGIN = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?")
HOUR = re.compile(r"\d{4}-\d\d-\d\dT\d\d:00Z")
OTHER_BELOW = 1.0  # organizations with a smaller share of commits (in percent) join "Other"
BAR = 12
LOGOS = Path("images", "owners")  # next to README.md; hand-picked logos can be dropped in here
LOGO_TYPES = {b"\x89PNG\r\n\x1a\n": ".png", b"\xff\xd8\xff": ".jpg"}
MAX_LOGO = 512 * 1024

PAGE = "pageInfo { hasNextPage endCursor }"
LANGUAGE = "primaryLanguage { name color }"
OWNER = "owner { __typename login ... on Organization { name } ... on User { name } }"
USER_ID = "query($login: String!) { user(login: $login) { id } }"
ACCESSIBLE_REPOSITORIES = f"""
query($login: String!, $cursor: String) {{
  user(login: $login) {{
    repositories(first: 100, after: $cursor, isFork: false,
                 affiliations: [OWNER, COLLABORATOR, ORGANIZATION_MEMBER]) {{
      {PAGE} nodes {{ {OWNER} name defaultBranchRef {{ name }} {LANGUAGE} }}
    }}
  }}
}}"""
CONTRIBUTED_REPOSITORIES = f"""
query($login: String!, $cursor: String) {{
  user(login: $login) {{
    repositoriesContributedTo(first: 100, after: $cursor, includeUserRepositories: false,
                              contributionTypes: [COMMIT, PULL_REQUEST, REPOSITORY]) {{
      {PAGE} nodes {{ {OWNER} name isFork defaultBranchRef {{ name }} {LANGUAGE} }}
    }}
  }}
}}"""
HISTORY = f"""
query($owner: String!, $name: String!, $ref: String!, $author: ID!, $cursor: String) {{
  repository(owner: $owner, name: $name) {{
    ref(qualifiedName: $ref) {{
      target {{
        ... on Commit {{ history(first: 100, after: $cursor, author: {{ id: $author }}) {{ {PAGE} nodes {{ oid authoredDate }} }} }}
      }}
    }}
  }}
}}"""


class InaccessibleRepository(Exception):
    pass


def graphql(query, token, **variables):
    body = json.dumps({"query": query, "variables": variables}).encode()
    headers = {"User-Agent": "profile-readme", "Authorization": f"Bearer {token}"}
    for attempt in range(5):
        try:
            with urlopen(Request("https://api.github.com/graphql", body, headers), timeout=60) as response:
                payload = json.load(response)
            break
        except HTTPError as error:
            status = error.code
            error.close()
            if status not in RETRYABLE or attempt == 4:
                raise RuntimeError(f"GitHub GraphQL request failed ({status})") from None
        except URLError:
            if attempt == 4:
                raise RuntimeError("GitHub GraphQL request failed (network)") from None
        time.sleep(2 ** attempt * 5)
    if payload.get("errors"):
        # Messages can name private repositories and Actions logs are public; report types only.
        types = sorted({error.get("type", "UNKNOWN") for error in payload["errors"]})
        if payload.get("data") and set(types) <= {"NOT_FOUND", "FORBIDDEN"}:
            raise InaccessibleRepository()
        raise RuntimeError(f"GitHub GraphQL returned errors: {', '.join(types)}")
    return payload["data"]


def paginate(query, token, path, **variables):
    cursor = None
    while True:
        connection = graphql(query, token, cursor=cursor, **variables)
        for key in path:
            connection = connection[key] if connection else None
        if connection is None:
            return
        yield connection["nodes"]
        if not connection["pageInfo"]["hasNextPage"]:
            return
        cursor = connection["pageInfo"]["endCursor"]


def owner_profile(login, token=None):
    # Public profile of an owner whose repositories are no longer listed, e.g. imported ones.
    headers = {"User-Agent": "profile-readme", "Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        with urlopen(Request(f"https://api.github.com/users/{quote(login)}", headers=headers), timeout=30) as response:
            profile = json.load(response)
    except (HTTPError, URLError):
        return None
    return {"type": profile.get("type"), "name": profile.get("name")}


def repositories(username, token, languages=None):
    author = graphql(USER_ID, token, login=username)["user"]["id"]
    found, owners = {}, {}
    for query, field in ((ACCESSIBLE_REPOSITORIES, "repositories"),
                         (CONTRIBUTED_REPOSITORIES, "repositoriesContributedTo")):
        for nodes in paginate(query, token, ["user", field], login=username):
            for node in nodes:
                if node.get("isFork") or not node["defaultBranchRef"]:
                    continue
                owner = node["owner"]
                owners[owner["login"]] = {"type": owner["__typename"], "name": owner.get("name")}
                found.setdefault((owner["login"], node["name"]), node["defaultBranchRef"]["name"])
                language = node.get("primaryLanguage")
                if languages is not None and language and language.get("name"):
                    languages[repository_key(owner["login"], node["name"])] = {
                        "name": language["name"], "color": language.get("color") or UNKNOWN_COLOUR}
    # The profile repository's one commit is replaced on every refresh (publish_single_commit.sh),
    # so reading it would count a new commit each night. Its earlier commits stay cached.
    found = {key: branch for key, branch in found.items() if key[0].lower() != username.lower()
             or key[1].lower() != username.lower()}
    return author, found, owners


def commit_key(oid):
    # The cache is public: store a one-way hash, never the commit id of a private repository.
    return sha256(oid.encode()).hexdigest()[:16]


def repository_key(owner, name):
    # Lets repositories be counted per owner without publishing private repository names.
    return sha256(f"{owner}/{name}".lower().encode()).hexdigest()[:12]


def authored_hour(value):
    return datetime.fromisoformat(value).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:00Z")


def remember(cache, oid, authored, repository):
    commits, key = cache["commits"], commit_key(oid)
    if key not in commits:
        commits[key] = [authored_hour(authored), repository]
        return True
    if commits[key][1] is None:
        commits[key][1] = repository  # cached before repositories were tracked
    return False


def authored_dates(owner, name, default_branch, author, token, cache, full=False):
    # Default branches only: feature branches repeat their history, and unmerged work is left out.
    # Keyed by commit, so a commit shared by two repositories is still counted once.
    repository = repository_key(owner, name)
    cache["repositories"][repository] = owner
    pages = paginate(HISTORY, token, ["repository", "ref", "target", "history"],
                     owner=owner, name=name, ref=f"refs/heads/{default_branch}", author=author)
    for nodes in pages:
        fresh = [remember(cache, node["oid"], node["authoredDate"], repository) for node in nodes]
        # History is newest first: once a whole page is cached, so is everything older.
        if not any(fresh) and not full:
            break


def valid_cache(cache):
    repositories, owners, commits = cache["repositories"], cache["owners"], cache["commits"]
    return (
        all(re.fullmatch(r"[0-9a-f]{12}", key) and isinstance(owner, str) and LOGIN.fullmatch(owner)
            for key, owner in repositories.items())
        and all(LOGIN.fullmatch(login) and isinstance(info, dict) for login, info in owners.items())
        and all(re.fullmatch(r"[0-9a-f]{16}", key) and isinstance(value, list) and len(value) == 2
                and isinstance(value[0], str) and HOUR.fullmatch(value[0])
                and (value[1] is None or value[1] in repositories)
                for key, value in commits.items())
    )


def load_cache(path):
    if not path.exists():
        return {"owners": {}, "repositories": {}, "commits": {}}
    data = json.loads(path.read_text(encoding="utf-8"))
    version = data.get("version") if isinstance(data, dict) else None
    if version == 1 and isinstance(data.get("commits"), dict):
        # Version 1 did not track repositories; a --full run attributes these commits.
        data = {"owners": {}, "repositories": {},
                "commits": {key: [hour, None] for key, hour in data["commits"].items()}}
    elif version != 2 or not all(isinstance(data.get(field), dict) for field in ("owners", "repositories", "commits")):
        data = None
    # A broken cache must stop the run: starting over would drop commits from lost repositories.
    if data is None or not valid_cache(data):
        raise ValueError(f"{path} is not a valid commit cache")
    return {field: data[field] for field in ("owners", "repositories", "commits")}


def prune(cache):
    # Only owners and repositories that commits point to are published.
    used = {repository for _, repository in cache["commits"].values() if repository}
    cache["repositories"] = {key: owner for key, owner in cache["repositories"].items() if key in used}
    owners = set(cache["repositories"].values())
    cache["owners"] = {login: info for login, info in cache["owners"].items() if login in owners}


def cache_text(cache):
    meta = {"version": 2, "owners": dict(sorted(cache["owners"].items())),
            "repositories": dict(sorted(cache["repositories"].items()))}
    commits = sorted(cache["commits"].items(), key=lambda item: (item[1][0], item[0]))
    # One commit per line, oldest first, so nightly diffs are just the lines appended.
    body = ",\n".join(f"  {json.dumps(key)}: {json.dumps(value)}" for key, value in commits)
    return json.dumps(meta, indent=1, ensure_ascii=False)[:-2] + ',\n "commits": {\n' + body + "\n }\n}\n"


def fill_owners(cache, known, token=None):
    cache["owners"].update(known)
    for login in set(cache["repositories"].values()) - set(cache["owners"]):
        profile = owner_profile(login, token)
        if profile:
            cache["owners"][login] = profile


def habits(dates, zone):
    day_times, weekdays = [0] * 4, [0] * 7
    for value in dates:
        moment = datetime.fromisoformat(value).astimezone(zone)
        day_times[(moment.hour // 6 + 3) % 4] += 1  # 06-12 morning, ..., 00-06 night
        weekdays[moment.weekday()] += 1
    return day_times, weekdays


def rows(names, counts):
    total = sum(counts)
    lines = []
    for name, count in zip(names, counts):
        percent = round(count / total * 100, 2) if total else 0
        filled = round(percent / 4)
        bar = "█" * filled + "░" * (25 - filled)
        lines.append(f"{name:<25}{f'{count} commits':<20}{bar}   {percent:05.2f} %")
    return "\n".join(lines)


def section(day_times, weekdays):
    if not sum(day_times):
        raise ValueError("No commits found; refusing to publish empty habits")
    title = "I'm an Early 🐤" if sum(day_times[:2]) >= sum(day_times[2:]) else "I'm a Night 🦉"
    busiest = WEEKDAYS[weekdays.index(max(weekdays))]
    return (f"**{title}**\n\n```text\n{rows(DAY_TIMES, day_times)}\n```\n\n"
            f"📅 **I'm Most Productive on {busiest}**\n\n```text\n{rows(WEEKDAYS, weekdays)}\n```")


def owner_totals(cache, username):
    """Commits per owner, with small organizations, other people's repositories and
    unattributed commits merged into a final "Other" entry (owner None)."""
    totals = {}
    for _, repository in cache["commits"].values():
        owner = cache["repositories"].get(repository)
        entry = totals.setdefault(owner, {"commits": 0, "repositories": set()})
        entry["commits"] += 1
        entry["repositories"].add(repository)
    total = sum(entry["commits"] for entry in totals.values())
    shown, other = [], None
    for owner, entry in totals.items():
        organization = cache["owners"].get(owner, {}).get("type") == "Organization"
        if owner == username or (organization and entry["commits"] / total * 100 >= OTHER_BELOW):
            shown.append((owner, entry))
        elif other is None:
            other = dict(entry, repositories=set(entry["repositories"]))
        else:
            other["commits"] += entry["commits"]
            other["repositories"] |= entry["repositories"]
    shown.sort(key=lambda item: (-item[1]["commits"], item[0]))
    return shown + ([(None, other)] if other else []), total


def logo_path(directory, login):
    return next((path for path in (directory / f"{login}{suffix}" for suffix in LOGO_TYPES.values())
                 if path.exists()), None)


def download_avatar(login, url=None):
    request = Request(url or f"https://github.com/{quote(login)}.png?size=80", headers={"User-Agent": "profile-readme"})
    try:
        with urlopen(request, timeout=30) as response:
            return response.read(MAX_LOGO + 1)
    except (HTTPError, URLError):
        return None


def save_logos(logins, directory, avatars=None):
    # A saved logo survives an organization changing its avatar or disappearing. Files that
    # already exist, including hand-picked logos, are never replaced.
    for login in logins:
        if logo_path(directory, login):
            continue
        data = download_avatar(login, (avatars or {}).get(login)) or b""
        suffix = next((suffix for magic, suffix in LOGO_TYPES.items() if data.startswith(magic)), None)
        if suffix and len(data) <= MAX_LOGO:
            directory.mkdir(parents=True, exist_ok=True)
            temporary = directory / f".{login}{suffix}.tmp"
            temporary.write_bytes(data)
            temporary.replace(directory / f"{login}{suffix}")


def load_names(path):
    """Display names that replace an owner's GitHub name in the table, e.g. a shorter brand name."""
    if not path.exists():
        return {}
    names = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(names, dict) or not all(
        LOGIN.fullmatch(login) and isinstance(name, str) and 0 < len(name.strip()) <= 60
        for login, name in names.items()
    ):
        raise ValueError(f"{path} must map owner logins to display names")
    return names


def load_links(path):
    """Where each owner's row links to, instead of its GitHub page (e.g. a tracked short link)."""
    if not path.exists():
        return {}
    links = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(links, dict) or not all(
        LOGIN.fullmatch(login) and isinstance(url, str) and url.startswith("https://") and not re.search(r"[\s()]", url)
        for login, url in links.items()
    ):
        raise ValueError(f"{path} must map owner logins to https:// links")
    return links


def owners_section(cache, username, logos=None, names=None, links=None):
    lines = ["| | Where | Commits | Share | Repos |", "| :-: | --- | ---: | --- | ---: |"]
    entries, total = owner_totals(cache, username)
    for owner, entry in entries:
        if owner is None:
            avatar, label = "👥", "Other"
        else:
            name = ("Personal projects" if owner == username else (names or {}).get(owner)
                    or cache["owners"].get(owner, {}).get("name") or owner)
            # Display names come from GitHub: keep them from breaking the table or the link.
            name = re.sub(r"([\\|\[\]<>*_`])", r"\\\1", name)
            path = logo_path(logos, owner) if logos else None
            info = cache["owners"].get(owner, {})
            home = f"https://gitlab.com/{owner}" if info.get("host") == "gitlab" else f"https://github.com/{owner}"
            source = ((LOGOS / path.name).as_posix() if path
                      else info.get("avatar") or f"https://github.com/{owner}.png?size=40")
            avatar = f'<img src="{source}" width="20" height="20" alt="">'
            label = f"**[{name}]({(links or {}).get(owner) or home})**"
        percent = entry["commits"] / total * 100
        filled = round(percent / 100 * BAR)
        share = f"`{'█' * filled}{'░' * (BAR - filled)}` {f'{percent:.0f}%' if percent >= 1 else '<1%'}"
        repos = len(entry["repositories"] - {None}) or "–"
        lines.append(f"| {avatar} | {label} | {entry['commits']:,} | {share} | {repos} |")
    return "\n".join(lines)


def section_pattern(name):
    # The section may be empty, as when its markers were just added.
    return re.compile(rf"(<!--START_SECTION:{name}-->\n)(?:.*?\n)?(<!--END_SECTION:{name}-->)", re.S)


def replace_section(readme, name, content):
    pattern = section_pattern(name)
    if not pattern.search(readme):
        raise ValueError(f"README.md is missing the {name} section markers")
    return pattern.sub(lambda match: match[1] + content + "\n" + match[2], readme, count=1)


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def tally(cache):
    """Commit counts that must never go down: in total, per repository and per owner."""
    counts = Counter({"total": len(cache["commits"])})
    for _, repository in cache["commits"].values():
        if repository:
            counts["repository", repository] += 1
            counts["owner", cache["repositories"][repository]] += 1
    return counts


def check_never_lower(before, after):
    # Commits are only ever added, so a lower count than the loaded cache means a bug, never real data.
    dropped = Counter(key if key == "total" else key[0] for key, count in before.items() if after[key] < count)
    if dropped:
        # Counts by kind only: owner logins and repository hashes stay out of the public logs.
        details = ", ".join(f"{kind} ×{number}" for kind, number in sorted(dropped.items()))
        raise ValueError(f"Refusing to publish: commit counts would go down ({details})")


def publish(cache, zone, username, readme, path, before):
    prune(cache)
    check_never_lower(before, tally(cache))
    text = readme.read_text(encoding="utf-8")
    text = replace_section(text, "commit-habits", section(*habits((hour for hour, _ in cache["commits"].values()), zone)))
    if section_pattern("commit-owners").search(text):
        logos = readme.parent / LOGOS
        avatars = {login: info["avatar"] for login, info in cache["owners"].items() if info.get("avatar")}
        save_logos([owner for owner, _ in owner_totals(cache, username)[0] if owner], logos, avatars)
        names = load_names(path.parent / "owner-names.json")
        links = load_links(path.parent / "owner-links.json")
        text = replace_section(text, "commit-owners", owners_section(cache, username, logos, names, links))
    write(path, cache_text(cache))
    write(readme, text)


REMOTE = re.compile(r"(github|gitlab)\.com[:/]([^/]+)/([^/]+?)(?:\.git)?/?$")


def origin_repository(clone, aliases=None):
    """
    OWNER/NAME of a clone, read from its remotes. A GitHub remote wins, so the key matches
    commits cached from GitHub; otherwise a GitLab remote. Owners are mapped through
    `aliases`, e.g. a GitLab group to the GitHub organization it mirrors, or an old account.
    """
    urls = subprocess.run(["git", "-C", str(clone), "remote", "-v"],
                          capture_output=True, text=True, check=False).stdout.split()
    found = {}
    for url in urls:
        match = REMOTE.search(url)
        if match and LOGIN.fullmatch(match[2]):
            found.setdefault(match[1], (match[2], match[3]))
    for host in ("github", "gitlab"):
        if host in found:
            owner, name = found[host]
            return (aliases or {}).get(owner, owner), name
    raise ValueError(f"Cannot tell which repository {clone} is; pass --repository OWNER/NAME")


# Extensions counted for a repository's primary language, with GitHub's colours
LANGUAGES = {
    ".py": ("Python", "#3572A5"), ".dart": ("Dart", "#00B4AB"), ".ts": ("TypeScript", "#3178c6"),
    ".tsx": ("TypeScript", "#3178c6"), ".js": ("JavaScript", "#f1e05a"), ".jsx": ("JavaScript", "#f1e05a"),
    ".rs": ("Rust", "#dea584"), ".java": ("Java", "#b07219"), ".kt": ("Kotlin", "#A97BFF"),
    ".go": ("Go", "#00ADD8"), ".swift": ("Swift", "#F05138"), ".vue": ("Vue", "#41b883"),
    ".rb": ("Ruby", "#701516"), ".php": ("PHP", "#4F5D95"), ".c": ("C", "#555555"), ".cpp": ("C++", "#f34b7d"),
    ".sh": ("Shell", "#89e051"), ".html": ("HTML", "#e34c26"), ".css": ("CSS", "#663399"),
}


def primary_language(clone):
    """The language with the most bytes among the clone's tracked files, or None."""
    listing = subprocess.run(["git", "-C", str(clone), "ls-files", "-z"],
                             capture_output=True, check=False).stdout.split(b"\0")
    sizes = Counter()
    for raw in listing:
        path = Path(clone) / raw.decode(errors="replace")
        language = LANGUAGES.get(path.suffix.lower())
        if language:
            try:
                sizes[language] += path.stat().st_size
            except OSError:
                continue
    return sizes.most_common(1)[0][0] if sizes else None


COLOURS = {name: colour for name, colour in LANGUAGES.values()}
UNKNOWN_COLOUR = "#858585"

DEFAULT_BRANCH = "@default"


def default_branch(clone):
    """
    The remote's default branch, as the clone last saw it (refs/remotes/<remote>/HEAD), the
    GitHub remote's first. Counting it matches counting from GitHub: rebased and unmerged
    branches would add commits that never landed.
    """
    remotes = subprocess.run(["git", "-C", str(clone), "remote"], capture_output=True, text=True,
                             check=False).stdout.split()
    for remote in sorted(remotes, key=lambda name: (name != "github", name != "origin", name)):
        head = subprocess.run(["git", "-C", str(clone), "symbolic-ref", "-q", f"refs/remotes/{remote}/HEAD"],
                              capture_output=True, text=True, check=False).stdout.strip()
        if head:
            return head
    return "HEAD"


def import_git(clone, ref, emails, cache, repository=None, aliases=None, languages=None):
    owner, name = repository or origin_repository(clone, aliases)
    key = repository_key(owner, name)
    cache["repositories"][key] = owner
    if languages is not None:
        language = primary_language(clone)
        if language:
            languages[key] = {"name": language[0], "color": language[1]}
    if ref == DEFAULT_BRANCH:
        ref = default_branch(clone)
    revisions = ["--end-of-options", ref]
    log = subprocess.run(
        ["git", "-C", str(clone), "log", "--format=%H%x09%aI%x09%ae", *revisions, "--"],
        capture_output=True, text=True, check=False,
    )
    if log.returncode:
        raise RuntimeError(f"git log failed in {clone} for {ref}: {log.stderr.strip()}")
    wanted = {email.lower() for email in emails}
    found = new = 0
    for line in log.stdout.splitlines():
        oid, authored, email = line.split("\t")
        if email.lower() in wanted:
            found += 1
            new += remember(cache, oid, authored, key)
    return found, new

GITLAB_API = "https://gitlab.com/api/v4"
GITLAB_RETRYABLE = (429, 500, 502, 503, 504)


def gitlab(path, token, **params):
    """One page of a GitLab REST call and the number of the next page (None after the last)."""
    url = f"{GITLAB_API}/{path}?{urlencode(params)}"
    headers = {"User-Agent": "profile-readme", "Authorization": f"Bearer {token}"}
    for attempt in range(5):
        try:
            with urlopen(Request(url, headers=headers), timeout=60) as response:
                return json.load(response), response.headers.get("X-Next-Page") or None
        except HTTPError as error:
            status = error.code
            error.close()
            if status in (403, 404):
                raise InaccessibleRepository() from None
            if status not in GITLAB_RETRYABLE or attempt == 4:
                # Status only: GitLab's messages can name private projects and Actions logs are public.
                raise RuntimeError(f"GitLab request failed ({status})") from None
        except URLError:
            if attempt == 4:
                raise RuntimeError("GitLab request failed (network)") from None
        time.sleep(2 ** attempt * 5)


def gitlab_pages(path, token, **params):
    page = 1
    while page:
        items, following = gitlab(path, token, page=page, per_page=100, **params)
        yield items
        page = int(following) if following and str(following).isdigit() else None


def gitlab_projects(token):
    """
    Every GitLab project the token's user is a member of. Left out: forks, empty projects, ones
    scheduled for deletion, and the profile project (USER/USER), whose nightly refresh commit
    would otherwise count as a commit of yours every day.
    """
    for projects in gitlab_pages("projects", token, membership="true", order_by="id", sort="asc"):
        for project in projects:
            if (project.get("forked_from_project") or project.get("empty_repo") or not project.get("default_branch")
                    or project.get("marked_for_deletion_on") or project.get("marked_for_deletion_at")):
                continue
            namespace, _, name = project["path_with_namespace"].lower().partition("/")
            if (project.get("namespace") or {}).get("kind") == "user" and namespace == name:
                continue
            yield project


def gitlab_owner(project, aliases=None):
    """
    The cache's OWNER/NAME for a GitLab project: its top-level group or user, mapped through
    `aliases` like --import-git does, so commits already imported from clones keep their keys.
    """
    owner, _, name = project["path_with_namespace"].partition("/")
    owner = (aliases or {}).get(owner, owner)
    if not LOGIN.fullmatch(owner):
        owner = re.sub(r"[^A-Za-z0-9-]+", "-", owner).strip("-")[:39] or "gitlab"
    return owner, name


def gitlab_owner_info(project):
    namespace = project.get("namespace") or {}
    avatar = namespace.get("avatar_url") or None
    if avatar and avatar.startswith("/"):
        avatar = "https://gitlab.com" + avatar
    group = namespace.get("kind") == "group"
    info = {"type": "Organization" if group else "User", "host": "gitlab",
            "name": namespace.get("name") if "/" not in namespace.get("full_path", "/") else None}
    if avatar and avatar.startswith("https://"):
        info["avatar"] = avatar
    return info


def gitlab_language(project, token):
    try:
        shares, _ = gitlab(f"projects/{project['id']}/languages", token)
    except InaccessibleRepository:
        return None
    if not isinstance(shares, dict) or not shares:
        return None
    name = max(shares, key=lambda language: shares[language])
    return {"name": name, "color": COLOURS.get(name, UNKNOWN_COLOUR)}


def gitlab_commits(project, emails, token, cache, repository, full=False):
    """Your commits on the project's default branch, newest first, until one is already cached."""
    found = new = 0
    for commits in gitlab_pages(f"projects/{project['id']}/repository/commits", token,
                                ref_name=project["default_branch"]):
        mine = [commit for commit in commits if (commit.get("author_email") or "").lower() in emails]
        fresh = [remember(cache, commit["id"], commit["authored_date"], repository) for commit in mine]
        found, new = found + len(mine), new + sum(fresh)
        # History is newest first: once a page's commits of yours are cached, so is everything older.
        if mine and not any(fresh) and not full:
            break
    return found, new


def import_gitlab(token, emails, cache, aliases=None, languages=None, full=False):
    """Count your commits from every GitLab project you are a member of. Returns (projects, unreadable)."""
    wanted = {email.lower() for email in emails}
    if not wanted:
        raise ValueError("Counting GitLab commits needs your commit emails (--email or COMMIT_EMAILS)")
    checked = skipped = 0
    for project in gitlab_projects(token):
        owner, name = gitlab_owner(project, aliases)
        repository = repository_key(owner, name)
        try:
            gitlab_commits(project, wanted, token, cache, repository, full)
        except InaccessibleRepository:
            skipped += 1
            continue
        checked += 1
        cache["repositories"].setdefault(repository, owner)
        if owner not in cache["owners"]:
            cache["owners"][owner] = gitlab_owner_info(project)
        if languages is not None and repository not in languages:
            language = gitlab_language(project, token)
            if language:
                languages[repository] = language
    return checked, skipped


def save_languages(path, cache, languages):
    used = set(cache["repositories"])
    write(path, json.dumps({key: value for key, value in sorted(languages.items()) if key in used},
                           indent=1) + "\n")


def import_clones(clones, ref, emails, zone, username, readme, path, repository=None, token=None):
    cache = load_cache(path)
    before = tally(cache)
    aliases = load_names(path.parent / "owner-aliases.json")
    languages_path = path.parent / "repo-languages.json"
    languages = json.loads(languages_path.read_text(encoding="utf-8")) if languages_path.exists() else {}
    # Every clone is read before the cache or README is touched.
    results = [import_git(clone, ref, emails, cache, repository, aliases, languages) for clone in clones]
    fill_owners(cache, {}, token)
    publish(cache, zone, username, readme, path, before)
    save_languages(languages_path, cache, languages)
    for clone, (found, new) in zip(clones, results):
        print(f"{clone.name}: {found} of your commits on {ref}, {new} new.")
    print(f"Counted {len(cache['commits'])} commits.")


def generate(username, zone, readme, path, token, full=False, gitlab_token=None, emails=()):
    cache = load_cache(path)
    before, cached = tally(cache), len(cache["commits"])
    languages_path = path.parent / "repo-languages.json"
    languages = json.loads(languages_path.read_text(encoding="utf-8")) if languages_path.exists() else {}
    author, found, owners = repositories(username, token, languages)
    skipped = 0
    for (owner, name), default_branch in sorted(found.items()):
        try:
            authored_dates(owner, name, default_branch, author, token, cache, full)
        except InaccessibleRepository:
            skipped += 1
    fill_owners(cache, owners, token)
    # GitHub goes first, so a commit pushed to both sites keeps its GitHub repository.
    gitlab_checked = gitlab_skipped = 0
    if gitlab_token:
        aliases = load_names(path.parent / "owner-aliases.json")
        gitlab_checked, gitlab_skipped = import_gitlab(gitlab_token, emails, cache, aliases, languages, full)
    # Everything is fetched before the cache or README is touched.
    publish(cache, zone, username, readme, path, before)
    save_languages(languages_path, cache, languages)
    unattributed = sum(1 for _, repository in cache["commits"].values() if repository is None)
    print(f"Counted {len(cache['commits'])} commits ({len(cache['commits']) - cached} new, "
          f"{unattributed} without a repository) after checking {len(found) - skipped} "
          f"GitHub repositories ({skipped} unreadable)"
          + (f" and {gitlab_checked} GitLab projects ({gitlab_skipped} unreadable)." if gitlab_token else "."))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--username", default="mehroj-x")
    parser.add_argument("--timezone", default="Asia/Tashkent")
    parser.add_argument("--readme", type=Path, default=ROOT / "README.md")
    parser.add_argument("--cache", type=Path, default=ROOT / "data" / "commit-habits.json")
    parser.add_argument("--full", action="store_true",
                        help="Read every default branch to the end instead of stopping at cached history")
    parser.add_argument("--import-git", type=Path, action="append", metavar="CLONE",
                        help="Count your commits from a local clone instead of GitHub (repeatable)")
    parser.add_argument("--ref", default="HEAD", help="Branch or ref to read in --import-git clones")
    parser.add_argument("--default-branch", action="store_true",
                        help="Read each --import-git clone's remote default branch instead of --ref")
    parser.add_argument("--email", action="append", default=[],
                        help="An email your commits use, for --import-git and --gitlab (repeatable; "
                             "COMMIT_EMAILS adds more, separated by spaces or commas)")
    parser.add_argument("--gitlab", action="store_true",
                        help="Also count your commits in every GitLab project GITLAB_TOKEN can see")
    parser.add_argument("--repository", metavar="OWNER/NAME",
                        help="GitHub repository of a single --import-git clone, if origin does not say")
    args = parser.parse_args()
    if not LOGIN.fullmatch(args.username):
        parser.error("Invalid GitHub username")
    try:
        zone = ZoneInfo(args.timezone)
    except ZoneInfoNotFoundError:
        parser.error(f"Unknown time zone: {args.timezone}")
    token = os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN")
    args.email += re.split(r"[\s,]+", os.getenv("COMMIT_EMAILS", "").strip()) if os.getenv("COMMIT_EMAILS", "").strip() else []
    gitlab_token = os.getenv("GITLAB_TOKEN")
    if args.gitlab and not gitlab_token:
        parser.error("Set GITLAB_TOKEN to count GitLab commits")
    if args.gitlab and not args.email:
        parser.error("--gitlab needs your commit emails (--email or COMMIT_EMAILS)")
    if args.import_git:
        if not args.email:
            parser.error("--import-git needs at least one --email to recognise your commits")
        repository = None
        if args.repository:
            owner, _, name = args.repository.partition("/")
            if len(args.import_git) > 1 or not LOGIN.fullmatch(owner) or not name:
                parser.error("--repository takes OWNER/NAME and a single --import-git clone")
            repository = (owner, name)
        try:
            import_clones(args.import_git, DEFAULT_BRANCH if args.default_branch else args.ref, args.email, zone,
                          args.username, args.readme, args.cache, repository, token)
        except (RuntimeError, ValueError, OSError) as error:
            print(f"Commit habits import failed: {error}", file=sys.stderr)
            return 1
        return 0
    if not token:
        parser.error("Set GITHUB_TOKEN or GH_TOKEN; the GraphQL API requires a token")
    try:
        generate(args.username, zone, args.readme, args.cache, token, args.full,
                 gitlab_token if args.gitlab else None, args.email)
    except (RuntimeError, ValueError, KeyError, OSError) as error:
        print(f"Commit habits refresh failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
