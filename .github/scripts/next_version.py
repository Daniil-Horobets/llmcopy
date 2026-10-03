"""Decide the version to publish and write it into llmcopy.py. Prints the version.

The new version is the latest one on PyPI plus a bump: patch by default, minor if a commit message
in the push contains [minor], major if one contains [major]. A version in llmcopy.py that is
already ahead of PyPI is published as is.
"""
import json
import os
import re
import urllib.error
import urllib.request

FILE = "llmcopy.py"
PATTERN = re.compile(r'^__version__ = "(\d+)\.(\d+)\.(\d+)"$', re.M)


def published():
    try:
        with urllib.request.urlopen("https://pypi.org/pypi/llmcopy/json", timeout=30) as r:
            names = json.load(r)["releases"]
    except urllib.error.HTTPError as e:
        if e.code == 404:  # nothing published yet
            return []
        raise
    return [tuple(map(int, n.split("."))) for n in names if re.fullmatch(r"\d+\.\d+\.\d+", n)]


def bump(version, messages):
    major, minor, patch = version
    text = "\n".join(messages).lower()
    if "[major]" in text:
        return major + 1, 0, 0
    if "[minor]" in text:
        return major, minor + 1, 0
    return major, minor, patch + 1


def main():
    with open(FILE, encoding="utf-8", newline="") as f:
        text = f.read()
    match = PATTERN.search(text)
    if not match:
        raise SystemExit(f'no __version__ = "X.Y.Z" line in {FILE}')
    current = tuple(map(int, match.groups()))
    released = published()
    latest = max(released, default=None)
    if latest is None or current > latest:
        version = current
    else:
        version = bump(latest, json.loads(os.environ.get("COMMIT_MESSAGES") or "[]"))
    name = ".".join(map(str, version))
    with open(FILE, "w", encoding="utf-8", newline="") as f:
        f.write(PATTERN.sub(f'__version__ = "{name}"', text, count=1))
    print(name)


main()
