#!/usr/bin/env python3
"""Print the GitLab copy of README.md (read from stdin or a path).

GitLab gets the same profile with three differences:
- the name banner is images/name.svg, because GitLab does not switch <picture> sources by colour scheme;
- lines marked <!-- github-only --> are left out;
- short links are tagged utm_source=gitlab instead of utm_source=github.
"""

import re
import sys

PICTURE = re.compile(r"<picture>\s*(?:<source\b[^>]*>\s*)*(<img\b[^>]*>)\s*</picture>", re.S)
SOURCE = re.compile(r'\bsrc="[^"]*"')


def gitlab_readme(text):
    if not PICTURE.search(text):
        raise ValueError("README.md has no <picture> name banner to replace")
    text = PICTURE.sub(lambda match: SOURCE.sub('src="images/name.svg"', match[1], count=1), text, count=1)
    text = "".join(line for line in text.splitlines(keepends=True) if "<!-- github-only -->" not in line)
    return text.replace("utm_source=github", "utm_source=gitlab")


def main():
    source = open(sys.argv[1], encoding="utf-8") if len(sys.argv) > 1 else sys.stdin
    with source:
        sys.stdout.write(gitlab_readme(source.read()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
