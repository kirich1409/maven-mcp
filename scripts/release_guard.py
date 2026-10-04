#!/usr/bin/env python3
"""Classify a Release dispatch and decide what the workflow may publish.

Stdlib only. The workflow checks out, fetches, builds, attests, and calls
the GitHub API. This script does not upload and does not write refs.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

REPO = "kirich1409/maven-mcp"
PYPI_PROJECT_URL = "https://pypi.org/pypi/maven-mcp/{version}/json"

SHA_RE = re.compile(r"^[0-9a-f]{40}$")
VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
TAG_NAME_RE = re.compile(r"^v[0-9]+\.[0-9]+\.[0-9]+$")

# Already published. noop may name them; nothing else may upload or move refs.
FROZEN_SHAS = frozenset(
    {
        "02fa8f5a936b85151041de0f0a524db484c6d2a0",
        "1ae5ca62417826ac1af1e40e26fdbf11348a3ed4",
    }
)
FROZEN_TAGS = frozenset({"v1.0.0", "v1.1.0"})

REQUIRED_CHECK_NAMES = (
    "python-tests (3.9)",
    "python-tests (3.13)",
    "ruff",
    "mypy",
)

# First attempt is immediate. The other seven are slept before the next GET.
POLL_SLEEPS = [0, 5, 10, 15, 20, 20, 20, 20]

EXIT_CLASSES = frozenset({"publish", "missing-file", "repair-github", "noop"})
UPLOAD_CLASSES = frozenset({"publish", "missing-file", "repair-github"})


class GuardError(Exception):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


@dataclass
class ClassifyFacts:
    sha: str
    version: str
    version_on_main: str
    main_is_ancestor_of_sha: bool
    sha_is_ancestor_of_main: bool
    pypi_status: int
    pypi_body: Optional[dict]
    tag_object_sha: Optional[str]
    release_exists: bool


@dataclass
class GuardOutcome:
    ok: bool
    klass: Optional[str]
    reason: str
    checks: List[dict]


@dataclass
class GitState:
    object_type: str
    main_ref_exists: bool
    develop_ref_exists: bool
    sha_is_ancestor_of_develop: bool
    main_is_ancestor_of_sha: bool
    sha_is_ancestor_of_main: bool
    main_sha: str


@dataclass
class TagRef:
    name: str
    object_sha: str


@dataclass
class NotesDecision:
    draft: bool
    body: str


@dataclass
class PromotePlan:
    main: str
    tag: str
    release: str
    body: str


def parse_version(text: str) -> Optional[Tuple[int, int, int]]:
    if not VERSION_RE.match(text):
        return None
    major, minor, patch = text.split(".")
    return (int(major), int(minor), int(patch))


def version_greater(candidate: str, baseline: str) -> bool:
    left = parse_version(candidate)
    right = parse_version(baseline)
    if left is None or right is None:
        return False
    return left > right


def _table(text: str, name: str) -> Optional[str]:
    match = re.search(
        rf"(?m)^\[{re.escape(name)}\]\s*$([\s\S]*?)(?=^\[|\Z)",
        text,
    )
    if match is None:
        return None
    return match.group(1)


def project_version(text: str) -> str:
    block = _table(text, "project")
    if block is None:
        raise GuardError("pyproject-project")
    found = re.search(r'(?m)^version\s*=\s*"([^"]+)"', block)
    if found is None:
        raise GuardError("pyproject-version")
    return found.group(1)


def assert_release_build(text: str) -> None:
    """Reject a build backend other than the pinned hatchling floor.

    uv build runs that backend. The publish job is the one that can mint
    an OIDC token, so the backend that produced the bytes is checked here.
    """
    block = _table(text, "project")
    if block is None:
        raise GuardError("pyproject-project")
    deps = re.search(r"(?m)^dependencies\s*=\s*(\[[^\]]*\])", block)
    if deps is None or deps.group(1).strip() != "[]":
        raise GuardError("dependencies")
    build = _table(text, "build-system")
    if build is None:
        raise GuardError("build-system")
    requires = re.search(r"(?m)^requires\s*=\s*\[([^\]]*)\]", build)
    if requires is None:
        raise GuardError("build-system")
    parts = [part.strip() for part in requires.group(1).split(",") if part.strip()]
    if parts != ['"hatchling>=1.26.3"']:
        raise GuardError("build-system")
    if _hatch_build_has_hook(text):
        raise GuardError("build-hook")


_HATCH_BUILD = ("tool", "hatch", "build")
_HATCH_HOOK = ("tool", "hatch", "build", "hooks")


def _hatch_build_has_hook(text: str) -> bool:
    """True when header segments plus the key name tool.hatch.build.hooks.

    A dotted key may start at the root or under a shorter header. An inline
    table on a prefix of that path is included. A backslash in a quoted key
    is not decoded. There is no TOML parser.
    """
    table: Tuple[str, ...] = ()
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("["):
            parsed = _toml_table_path(line)
            if parsed is None:
                if _unparsed_hatch_header(line):
                    return True
                table = ()
                continue
            table = parsed
            if _contains_in_order(table, _HATCH_HOOK):
                return True
            continue
        assignment = _split_toml_assignment(line)
        if assignment is None:
            continue
        key, value = assignment
        segments = _toml_key_segments(key.strip())
        if segments is None:
            if _is_hatch_build_table(table) or ("\\" in key and _is_hatch_build_prefix(table)):
                return True
            continue
        combined = table + segments
        if _contains_in_order(combined, _HATCH_HOOK):
            return True
        if _is_hatch_build_prefix(combined) and _inline_declares_hooks(value):
            return True
    return False


def _contains_in_order(segments: Sequence[str], needle: Sequence[str]) -> bool:
    index = 0
    for segment in segments:
        if index < len(needle) and segment == needle[index]:
            index += 1
    return index == len(needle)


def _is_hatch_build_table(table: Optional[Tuple[str, ...]]) -> bool:
    if table is None or len(table) < 3:
        return False
    return table[:3] == _HATCH_BUILD


def _is_hatch_build_prefix(table: Tuple[str, ...]) -> bool:
    return table == _HATCH_BUILD[: len(table)]


def _toml_table_path(line: str) -> Optional[Tuple[str, ...]]:
    match = re.match(r"^\[{1,2}\s*(.*?)\s*\]{1,2}\s*(?:#.*)?$", line)
    if match is None:
        return None
    return _toml_key_segments(match.group(1).strip())


def _unparsed_hatch_header(line: str) -> bool:
    """A quoted backslash under tool.hatch.build is a hook we cannot name."""
    match = re.match(r"^\[{1,2}\s*(.*?)\s*\]{1,2}\s*(?:#.*)?$", line)
    if match is None or "\\" not in match.group(1):
        return False
    body = match.group(1)
    quote_at = -1
    for quote in "\"'":
        found = body.find(quote)
        if found >= 0 and (quote_at < 0 or found < quote_at):
            quote_at = found
    leading = body[:quote_at] if quote_at >= 0 else body
    leading = leading.strip().rstrip(".").strip()
    if not leading:
        return False
    segments = _toml_key_segments(leading)
    if segments is None:
        return False
    return _is_hatch_build_prefix(segments) or _is_hatch_build_table(segments)


def _split_toml_assignment(line: str) -> Optional[Tuple[str, str]]:
    split_at = line.find("=")
    if split_at < 0:
        return None
    return line[:split_at], line[split_at + 1 :]


def _toml_key_segments(text: str) -> Optional[Tuple[str, ...]]:
    """Bare and quoted key segments. A backslash inside quotes fails closed."""
    segments: List[str] = []
    index = 0
    length = len(text)
    expect_segment = True
    while index < length:
        while index < length and text[index] in " \t":
            index += 1
        if index >= length:
            break
        expect_segment = False
        if text[index] in "\"'":
            quote = text[index]
            end = index + 1
            while end < length and text[end] != quote:
                if text[end] == "\\":
                    return None
                end += 1
            if end >= length:
                return None
            segments.append(text[index + 1 : end])
            index = end + 1
        else:
            start = index
            while index < length and (text[index].isalnum() or text[index] in "-_"):
                index += 1
            if index == start:
                return None
            segments.append(text[start:index])
        while index < length and text[index] in " \t":
            index += 1
        if index >= length:
            break
        if text[index] != ".":
            return None
        index += 1
        expect_segment = True
    if expect_segment or not segments:
        return None
    return tuple(segments)


def _inline_declares_hooks(value: str) -> bool:
    """True when `{...}` names a hooks key. Escapes are not decoded."""
    text = value.strip()
    if not text.startswith("{"):
        return False
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        if char in "\"'":
            end = index + 1
            while end < length and text[end] != char:
                if text[end] == "\\":
                    return True
                end += 1
            if end >= length:
                return True
            quoted = text[index + 1 : end]
            after = _past_spaces(text, end + 1)
            if quoted == "hooks" and after < length and text[after] in ".=":
                return True
            index = end + 1
            continue
        if char == "#":
            newline = text.find("\n", index)
            index = length if newline < 0 else newline + 1
            continue
        if text.startswith("hooks", index) and _key_boundary(text, index):
            after = _past_spaces(text, index + len("hooks"))
            if after < length and text[after] in ".=":
                return True
        index += 1
    return False


def _past_spaces(text: str, index: int) -> int:
    while index < len(text) and text[index] in " \t":
        index += 1
    return index


def _key_boundary(text: str, index: int) -> bool:
    return index == 0 or text[index - 1] in " \t\r\n{[.,"


def metadata_matches(body: object, version: str) -> bool:
    if not isinstance(body, dict):
        return False
    info = body.get("info")
    if not isinstance(info, dict):
        return False
    if info.get("version") != version:
        return False
    if info.get("license_expression") != "MIT":
        return False
    if "license" not in info or info.get("license") is not None:
        return False
    if info.get("requires_python") != ">=3.9":
        return False
    return info.get("yanked") is False


def _url_rows(body: object) -> Optional[List[dict]]:
    """Clean url rows, or None when any row is yanked, unknown, or not an object.

    None is not a partial file set. classify treats it as not a gate match.
    filenames_to_upload raises on a 200 body that fails this walk.
    """
    if not isinstance(body, dict):
        return None
    urls = body.get("urls")
    if not isinstance(urls, list):
        return None
    rows: List[dict] = []
    for url in urls:
        if not isinstance(url, dict):
            return None
        if url.get("yanked") is True:
            return None
        kind = url.get("packagetype")
        if kind not in ("bdist_wheel", "sdist"):
            return None
        rows.append(url)
    return rows


def file_counts(body: object) -> Optional[Tuple[int, int]]:
    """Return (wheels, sdists) or None when a url is yanked or unexpected."""
    rows = _url_rows(body)
    if rows is None:
        return None
    wheels = sum(1 for row in rows if row.get("packagetype") == "bdist_wheel")
    sdists = sum(1 for row in rows if row.get("packagetype") == "sdist")
    return wheels, sdists


def json_gate_matches(status: int, body: Optional[dict], version: str) -> bool:
    if status != 200 or body is None:
        return False
    return metadata_matches(body, version) and file_counts(body) == (1, 1)


def classify(facts: ClassifyFacts) -> str:
    """First match wins. Raises GuardError('conflict') instead of returning it."""
    if _is_noop(facts):
        return "noop"
    if facts.sha in FROZEN_SHAS or f"v{facts.version}" in FROZEN_TAGS:
        raise GuardError("conflict")
    if facts.pypi_status == 404:
        if _is_publish(facts):
            return "publish"
        raise GuardError("conflict")
    if facts.pypi_status == 200 and facts.pypi_body is not None and metadata_matches(
        facts.pypi_body, facts.version
    ):
        counts = file_counts(facts.pypi_body)
        tag_sha = facts.tag_object_sha
        tag_moved = tag_sha is not None and tag_sha != facts.sha
        if counts in {(1, 0), (0, 1)}:
            if tag_moved or not facts.main_is_ancestor_of_sha:
                raise GuardError("conflict")
            return "missing-file"
        if counts == (1, 1):
            if tag_moved or not facts.main_is_ancestor_of_sha:
                raise GuardError("conflict")
            return "repair-github"
    raise GuardError("conflict")


def _is_noop(facts: ClassifyFacts) -> bool:
    return (
        json_gate_matches(facts.pypi_status, facts.pypi_body, facts.version)
        and facts.tag_object_sha == facts.sha
        and facts.release_exists
        and facts.sha_is_ancestor_of_main
    )


def _is_publish(facts: ClassifyFacts) -> bool:
    return (
        facts.tag_object_sha is None
        and not facts.release_exists
        and version_greater(facts.version, facts.version_on_main)
        and facts.main_is_ancestor_of_sha
    )


def latest_check_run(runs: Sequence[dict], name: str) -> dict:
    matching = [run for run in runs if run.get("name") == name]
    if not matching:
        raise GuardError(f"check-run-missing:{name}")

    def sort_key(run: dict) -> Tuple[str, int]:
        started = run.get("started_at") or ""
        if not isinstance(started, str):
            started = ""
        raw_id = run.get("id")
        ident = -1
        if isinstance(raw_id, int) and not isinstance(raw_id, bool):
            ident = raw_id
        elif isinstance(raw_id, str):
            try:
                ident = int(raw_id)
            except ValueError:
                ident = -1
        return (started, ident)

    matching.sort(key=sort_key, reverse=True)
    return matching[0]


def require_green_checks(runs: Sequence[dict]) -> List[dict]:
    chosen: List[dict] = []
    for name in REQUIRED_CHECK_NAMES:
        run = latest_check_run(runs, name)
        if run.get("status") != "completed" or run.get("conclusion") != "success":
            raise GuardError(f"check-run-not-success:{name}")
        chosen.append(run)
    return chosen


def load_check_runs(raw: object) -> List[dict]:
    if isinstance(raw, list):
        if raw and all(isinstance(item, dict) and "check_runs" in item for item in raw):
            runs: List[dict] = []
            for page in raw:
                runs.extend(_runs_in_page(page))
            return runs
        return [item for item in raw if isinstance(item, dict)]
    if isinstance(raw, dict):
        return _runs_in_page(raw)
    raise GuardError("check-runs")


def _runs_in_page(page: dict) -> List[dict]:
    runs = page.get("check_runs", [])
    if not isinstance(runs, list):
        raise GuardError("check-runs")
    return [run for run in runs if isinstance(run, dict)]


def load_check_runs_path(path: Path) -> List[dict]:
    if path.is_dir():
        pages = []
        for child in sorted(path.glob("*.json")):
            pages.append(json.loads(child.read_text(encoding="utf-8")))
        return load_check_runs(pages)
    return load_check_runs(json.loads(path.read_text(encoding="utf-8")))


def next_link(header: str) -> Optional[str]:
    if not header:
        return None
    for part in header.split(","):
        match = re.search(r"<([^>]+)>\s*;\s*rel=\"?next\"?", part.strip(), re.I)
        if match is None:
            continue
        url = match.group(1)
        if not url.startswith("https://api.github.com/"):
            raise GuardError("check-runs-link")
        return url
    return None


def link_header_value(header_text: str) -> str:
    values: List[str] = []
    for line in header_text.splitlines():
        if line.lower().startswith("link:"):
            values.append(line.split(":", 1)[1].strip())
    return ", ".join(values)


@dataclass
class GuardSpec:
    github_ref: str
    github_repository: str
    sha: str
    version: str
    object_type: str
    main_ref_exists: bool
    develop_ref_exists: bool
    sha_is_ancestor_of_develop: bool
    check_versions_sha_ok: bool
    check_versions_main_ok: bool
    pyproject_text: str
    check_runs: List[dict]
    facts: ClassifyFacts


def evaluate(spec: GuardSpec) -> GuardOutcome:
    """Preconditions, then classification. Rule 4 is not a precondition."""
    if spec.github_ref != "refs/heads/main":
        return _fail("ref")
    if spec.github_repository != REPO:
        return _fail("repository")
    if not SHA_RE.match(spec.sha):
        return _fail("sha")
    if spec.object_type != "commit":
        return _fail("sha-type")
    if not VERSION_RE.match(spec.version):
        return _fail("version")
    if not spec.main_ref_exists:
        return _fail("main-ref")
    if not spec.develop_ref_exists:
        return _fail("develop-ref")
    if not spec.sha_is_ancestor_of_develop:
        return _fail("develop-ancestor")
    if not spec.check_versions_sha_ok:
        return _fail("check-versions-sha")
    if not spec.check_versions_main_ok:
        return _fail("check-versions-main")
    try:
        assert_release_build(spec.pyproject_text)
        checks = require_green_checks(spec.check_runs)
        klass = classify(spec.facts)
    except GuardError as exc:
        return _fail(exc.reason)
    if klass not in EXIT_CLASSES:
        return _fail("conflict")
    return GuardOutcome(ok=True, klass=klass, reason="", checks=checks)


def _fail(reason: str) -> GuardOutcome:
    return GuardOutcome(ok=False, klass=None, reason=reason, checks=[])


def _git(repo: Path, args: Sequence[str], check: bool = False) -> subprocess.CompletedProcess[str]:
    # Empty fsmonitor stops a command the release tree can plant in .git/config.
    # alias.worktree= keeps a hiding alias from replacing the builtin.
    return subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "core.fsmonitor=",
            "-c",
            "alias.worktree=",
            *args,
        ],
        check=check,
        capture_output=True,
        text=True,
    )


def ref_exists(repo: Path, ref: str) -> bool:
    return _git(repo, ["rev-parse", "--verify", "--quiet", ref]).returncode == 0


def object_type(repo: Path, sha: str) -> str:
    proc = _git(repo, ["cat-file", "-t", sha])
    if proc.returncode != 0:
        return ""
    return proc.stdout.strip()


def is_ancestor(repo: Path, ancestor: str, descendant: str) -> bool:
    # merge-base --is-ancestor is true when the two commits are equal.
    return _git(repo, ["merge-base", "--is-ancestor", ancestor, descendant]).returncode == 0


def read_git_state(repo: Path, sha: str) -> GitState:
    main_exists = ref_exists(repo, "refs/heads/main")
    develop_exists = ref_exists(repo, "refs/heads/develop")
    main_sha = ""
    if main_exists:
        main_sha = _git(repo, ["rev-parse", "refs/heads/main"], check=True).stdout.strip()
    return GitState(
        object_type=object_type(repo, sha),
        main_ref_exists=main_exists,
        develop_ref_exists=develop_exists,
        sha_is_ancestor_of_develop=develop_exists and is_ancestor(repo, sha, "refs/heads/develop"),
        main_is_ancestor_of_sha=main_exists and is_ancestor(repo, "refs/heads/main", sha),
        sha_is_ancestor_of_main=main_exists and is_ancestor(repo, sha, "refs/heads/main"),
        main_sha=main_sha,
    )


def git_show(repo: Path, spec: str) -> str:
    proc = _git(repo, ["show", spec])
    if proc.returncode != 0:
        raise GuardError("git-show")
    return proc.stdout


def git_log_oneline(repo: Path, revision_range: str) -> str:
    proc = _git(repo, ["log", "--oneline", revision_range])
    if proc.returncode != 0:
        return ""
    return proc.stdout.rstrip("\n")


def parse_ls_remote_tags(text: str) -> List[TagRef]:
    """Prefer the peeled commit SHA from a `^{}` line when the tag is annotated."""
    raw: Dict[str, str] = {}
    peeled: Dict[str, str] = {}
    for line in text.splitlines():
        if "\t" not in line:
            continue
        sha, ref = line.split("\t", 1)
        if not ref.startswith("refs/tags/"):
            continue
        if ref.endswith("^{}"):
            peeled[ref[len("refs/tags/") : -3]] = sha
        else:
            raw[ref[len("refs/tags/") :]] = sha
    names = set(raw) | set(peeled)
    return [
        TagRef(name, peeled[name] if name in peeled else raw[name]) for name in sorted(names)
    ]


def select_vprev(
    tags: Sequence[TagRef],
    is_ancestor_of_sha: Callable[[str], bool],
) -> Optional[str]:
    best_name: Optional[str] = None
    best_version: Optional[Tuple[int, int, int]] = None
    for tag in tags:
        if not TAG_NAME_RE.match(tag.name):
            continue
        parsed = parse_version(tag.name[1:])
        if parsed is None:
            continue
        if not is_ancestor_of_sha(tag.object_sha):
            continue
        if best_version is None or parsed > best_version:
            best_version = parsed
            best_name = tag.name
    return best_name


def notes_decision(
    version: str,
    notes_text: Optional[str],
    vprev: Optional[str],
    log_oneline: str,
) -> NotesDecision:
    if notes_text is not None and notes_text.strip() != "":
        return NotesDecision(draft=False, body=notes_text)
    if not vprev:
        return NotesDecision(draft=True, body="")
    link = f"https://github.com/{REPO}/compare/{vprev}...v{version}"
    blurb = (
        "Install with `uvx maven-mcp`.\n"
        "This skeleton is not a substitute for the prose on the v1.1.0 page. "
        "A human should publish this draft after editing, or add "
        f"docs/releases/v{version}.md."
    )
    log = log_oneline.strip()
    body = f"{link}\n\n{log}\n\n{blurb}\n" if log else f"{link}\n\n{blurb}\n"
    return NotesDecision(draft=True, body=body)


def promote_plan(
    *,
    klass: str,
    sha: str,
    version: str,
    main_sha: str,
    main_is_ancestor_of_sha: bool,
    tag_object_sha: Optional[str],
    release_exists: bool,
    notes_text: Optional[str],
    vprev: Optional[str],
    log_oneline: str,
) -> PromotePlan:
    """Refuse before any ref write. Never forces and never deletes."""
    if klass == "noop":
        raise GuardError("noop")
    if klass not in UPLOAD_CLASSES:
        raise GuardError("class")
    if sha in FROZEN_SHAS:
        raise GuardError("frozen-sha")
    tag_name = f"v{version}"
    if tag_name in FROZEN_TAGS:
        raise GuardError("frozen-tag")
    if not main_is_ancestor_of_sha:
        raise GuardError("not-ancestor")
    if tag_object_sha is not None and tag_object_sha != sha:
        raise GuardError("tag-moved")
    main_action = "skip" if main_sha == sha else "update"
    tag_action = "skip" if tag_object_sha == sha else "create"
    if release_exists:
        plan = PromotePlan(main=main_action, tag=tag_action, release="skip", body="")
    else:
        notes = notes_decision(version, notes_text, vprev, log_oneline)
        plan = PromotePlan(
            main=main_action,
            tag=tag_action,
            release="draft" if notes.draft else "published",
            body=notes.body,
        )
    return plan


def readme_prose(readme: str) -> str:
    return next(
        block.strip()
        for block in readme.split("\n\n")
        if block.strip() and not block.lstrip().startswith("#")
    )


def _require_wheel_members(names: Sequence[str]) -> None:
    """Payload is server.py and the matrix. Other members must be one dist-info tree."""
    dist_info: Optional[str] = None
    for name in names:
        trimmed = name[:-1] if name.endswith("/") else name
        parts = trimmed.split("/")
        if (
            name.startswith("/")
            or "\\" in name
            or not parts
            or ".." in parts
            or "" in parts
        ):
            raise GuardError("wheel-member")
        if name in ("server.py", "compat-matrices.json"):
            continue
        if parts[0].endswith(".dist-info"):
            if dist_info is None:
                dist_info = parts[0]
            elif dist_info != parts[0]:
                raise GuardError("wheel-member")
            continue
        raise GuardError("wheel-member")
    if dist_info is None:
        raise GuardError("wheel-metadata")


def inspect_wheel(path: Path, version: str, readme: str, matrix: bytes, server: bytes) -> None:
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        _require_wheel_members(names)
        if "server.py" not in names:
            raise GuardError("wheel-server")
        if archive.read("server.py") != server:
            raise GuardError("wheel-server-bytes")
        if "compat-matrices.json" not in names:
            raise GuardError("wheel-matrix")
        if archive.read("compat-matrices.json") != matrix:
            raise GuardError("wheel-matrix-bytes")
        metadata_name = next(
            (name for name in names if name.endswith(".dist-info/METADATA")),
            None,
        )
        if metadata_name is None:
            raise GuardError("wheel-metadata")
        metadata = archive.read(metadata_name).decode("utf-8")
    paragraph = readme_prose(readme)
    if "Name: maven-mcp\n" not in metadata:
        raise GuardError("wheel-name")
    if f"Version: {version}\n" not in metadata:
        raise GuardError("wheel-version")
    if "License-Expression: MIT\n" not in metadata:
        raise GuardError("wheel-license")
    if "Requires-Python: >=3.9\n" not in metadata:
        raise GuardError("wheel-python")
    if "Requires-Dist:" in metadata:
        raise GuardError("wheel-requires-dist")
    if paragraph not in metadata:
        raise GuardError("wheel-readme")


def sdist_version(path: Path) -> str:
    with tarfile.open(path, "r:gz") as archive:
        for member in archive.getmembers():
            if not member.isfile():
                continue
            if member.name != "PKG-INFO" and not member.name.endswith("/PKG-INFO"):
                continue
            extracted = archive.extractfile(member)
            if extracted is None:
                continue
            text = extracted.read().decode("utf-8")
            found = re.search(r"(?m)^Version: (.+)$", text)
            if found is not None:
                return found.group(1).strip()
    raise GuardError("sdist-version")


# Relative names from `uv build --sdist` of this repository (tracked files
# plus generated PKG-INFO). Anything else is not published.
SDIST_MEMBERS = frozenset(
    {
        ".claude-plugin/marketplace.json",
        ".github/workflows/base-is-develop.yml",
        ".github/workflows/ci.yml",
        ".github/workflows/live-canary.yml",
        ".github/workflows/release.yml",
        ".gitignore",
        "AGENTS.md",
        "CLAUDE.md",
        "LICENSE",
        "PKG-INFO",
        "README.md",
        "docs/configuration.md",
        "plugin/.claude-plugin/plugin.json",
        "plugin/.codex-plugin/plugin.json",
        "plugin/.cursor-plugin/plugin.json",
        "plugin/.mcp.json",
        "plugin/hooks/cursor-hooks.json",
        "plugin/hooks/hooks.json",
        "plugin/hooks/post-edit-deps.sh",
        "plugin/hooks/pre-edit-deps.sh",
        "plugin/mcp.json",
        "plugin/server/compat-matrices.json",
        "plugin/server/server.py",
        "plugin/skills/.gitignore",
        "plugin/skills/audit-project-dependencies/SKILL.md",
        "plugin/skills/catalog-entry/SKILL.md",
        "plugin/skills/check-deps-vulnerabilities/SKILL.md",
        "plugin/skills/check-deps/SKILL.md",
        "plugin/skills/check-multiple-versions/SKILL.md",
        "plugin/skills/check-version-compatibility/SKILL.md",
        "plugin/skills/check-version-exists/SKILL.md",
        "plugin/skills/compare-dependency-versions/SKILL.md",
        "plugin/skills/dependency-changes/SKILL.md",
        "plugin/skills/dependency-conflicts/SKILL.md",
        "plugin/skills/dependency-health/SKILL.md",
        "plugin/skills/dependency-license/SKILL.md",
        "plugin/skills/dependency-vulnerabilities/SKILL.md",
        "plugin/skills/eol-status/SKILL.md",
        "plugin/skills/expand-bom/SKILL.md",
        "plugin/skills/latest-version/SKILL.md",
        "plugin/skills/license-compliance/SKILL.md",
        "plugin/skills/scan-project-dependencies/SKILL.md",
        "plugin/skills/search-artifacts/SKILL.md",
        "plugin/skills/transitive-graph/SKILL.md",
        "plugin/skills/upgrade-closure/SKILL.md",
        "plugin/skills/vulnerability-paths/SKILL.md",
        "pyproject.toml",
        "scripts/check-versions.py",
        "scripts/release_guard.py",
        "tests/_helpers.py",
        "tests/test_agent_plugin.py",
        "tests/test_airgap.py",
        "tests/test_base_is_develop.py",
        "tests/test_bom.py",
        "tests/test_catalog_entry.py",
        "tests/test_changelog_providers.py",
        "tests/test_compat.py",
        "tests/test_credentials.py",
        "tests/test_depsdev.py",
        "tests/test_dispatch.py",
        "tests/test_eol_status.py",
        "tests/test_file_cache.py",
        "tests/test_github.py",
        "tests/test_gradle_resolve.py",
        "tests/test_handlers.py",
        "tests/test_hooks_json.py",
        "tests/test_http.py",
        "tests/test_http_transport.py",
        "tests/test_license.py",
        "tests/test_license_compliance.py",
        "tests/test_live_canary.py",
        "tests/test_map_parallel.py",
        "tests/test_maven_search_osv.py",
        "tests/test_mirrors.py",
        "tests/test_output_schemas.py",
        "tests/test_parsers.py",
        "tests/test_post_edit_hook.py",
        "tests/test_pre_edit_hook.py",
        "tests/test_release_guard.py",
        "tests/test_repo_discovery.py",
        "tests/test_resolution.py",
        "tests/test_search_backends.py",
        "tests/test_smoke.py",
        "tests/test_string_distance.py",
        "tests/test_tls_proxy.py",
        "tests/test_tools_schema.py",
        "tests/test_upgrade_closure_depsdev.py",
        "tests/test_upgrade_closure_diff.py",
        "tests/test_upgrade_closure_gradle.py",
        "tests/test_verify_coordinates.py",
        "tests/test_version.py",
        "tests/test_vulnerability_paths.py",
        "tests/test_wheel.py",
    }
)


def _require_sdist_members(path: Path) -> None:
    seen: set[str] = set()
    with tarfile.open(path, "r:gz") as archive:
        for member in archive.getmembers():
            name = member.name
            parts = name.split("/")
            if (
                not member.isfile()
                or name.startswith("/")
                or "\\" in name
                or ".." in parts
                or "" in parts
                or len(parts) < 2
            ):
                raise GuardError("sdist-member")
            rel = "/".join(parts[1:])
            if rel not in SDIST_MEMBERS or rel in seen:
                raise GuardError("sdist-member")
            seen.add(rel)


def _sdist_file(path: Path, suffix: str) -> bytes:
    found: Optional[bytes] = None
    with tarfile.open(path, "r:gz") as archive:
        for member in archive.getmembers():
            if not member.isfile():
                continue
            parts = member.name.split("/")
            if member.name.startswith("/") or ".." in parts:
                raise GuardError("sdist-member")
            if not member.name.endswith(suffix):
                continue
            if found is not None:
                raise GuardError("sdist-member")
            extracted = archive.extractfile(member)
            if extracted is None:
                raise GuardError("sdist-member")
            found = extracted.read()
    if found is None:
        raise GuardError("sdist-member")
    return found


def inspect_sdist(path: Path, version: str, matrix: bytes, server: bytes) -> None:
    _require_sdist_members(path)
    if sdist_version(path) != version:
        raise GuardError("sdist-version")
    if _sdist_file(path, "/plugin/server/server.py") != server:
        raise GuardError("sdist-server-bytes")
    if _sdist_file(path, "/plugin/server/compat-matrices.json") != matrix:
        raise GuardError("sdist-matrix-bytes")


def inspect_dist(
    dist: Path,
    version: str,
    readme: str,
    matrix: bytes,
    server: bytes,
) -> None:
    wheels = sorted(dist.glob("*.whl"))
    sdists = sorted(dist.glob("*.tar.gz"))
    if len(wheels) != 1 or len(sdists) != 1:
        raise GuardError("dist-count")
    inspect_wheel(wheels[0], version, readme, matrix, server)
    inspect_sdist(sdists[0], version, matrix, server)


def filenames_to_upload(
    klass: str,
    dist_names: Sequence[str],
    pypi_status: int,
    pypi_body: Optional[dict],
) -> List[str]:
    if klass == "repair-github":
        return []
    if klass not in ("publish", "missing-file"):
        raise GuardError("class")
    wheels = [name for name in dist_names if name.endswith(".whl")]
    sdists = [name for name in dist_names if name.endswith(".tar.gz")]
    if klass == "publish" and not wheels and not sdists:
        raise GuardError("dist-empty")
    listed: set[str] = set()
    present = {"bdist_wheel": 0, "sdist": 0}
    if pypi_status == 200:
        rows = _url_rows(pypi_body)
        if rows is None:
            raise GuardError("pypi-urls")
        for url in rows:
            kind = url.get("packagetype")
            if kind in present:
                present[kind] += 1
            name = url.get("filename")
            if isinstance(name, str):
                listed.add(name)
    if klass == "publish":
        chosen = wheels + sdists
    else:
        chosen = []
        if present["bdist_wheel"] == 0:
            chosen.extend(wheels)
        if present["sdist"] == 0:
            chosen.extend(sdists)
        if not chosen:
            raise GuardError("missing-file-absent")
    return [name for name in chosen if Path(name).name not in listed]


def publish_output_is_already_exists(output: str) -> bool:
    return "already exists" in output.lower()


def _poll_info_field(body: Optional[dict], key: str) -> str:
    if not isinstance(body, dict):
        return "missing"
    info = body.get("info")
    if not isinstance(info, dict) or key not in info:
        return "missing"
    value = info.get(key)
    if value is None:
        return "null"
    return str(value)


def poll_until_match(
    fetch: Callable[[], Tuple[int, Optional[dict]]],
    version: str,
    sleep: Callable[[int], None],
) -> bool:
    for delay in POLL_SLEEPS:
        sleep(delay)
        status, body = fetch()
        print(
            "pypi status={status} license_expression={expression} license={license} requires_python={python}".format(
                status=status,
                expression=_poll_info_field(body, "license_expression"),
                license=_poll_info_field(body, "license"),
                python=_poll_info_field(body, "requires_python"),
            ),
            file=sys.stderr,
        )
        if json_gate_matches(status, body, version):
            return True
    return False


def render_summary(
    *,
    sha: str,
    version: str,
    version_on_main: str,
    log_main_to_sha: str,
    log_sha_to_main: str,
    klass: str,
    checks: Sequence[dict],
) -> str:
    lines = [
        f"- sha: `{sha}`",
        f"- version: `{version}`",
        f"- version on main: `{version_on_main}`",
        "",
        "### main..sha",
        "```",
        log_main_to_sha,
        "```",
        "",
        "### sha..main",
        "```",
        log_sha_to_main,
        "```",
        "",
        f"- class: `{klass}`",
        "",
        "### check runs",
    ]
    for run in checks:
        url = run.get("html_url") or run.get("id")
        lines.append(f"- {run.get('name')}: {url}")
    return "\n".join(lines) + "\n"


def release_tag_present(payload: object, tag: str) -> bool:
    """True when a Release object, a list, or paginated pages name tag.

    `gh api --paginate --slurp` yields a list of page arrays. A single
    object is the get-by-tag shape. A draft counts.
    """
    if isinstance(payload, dict):
        return payload.get("tag_name") == tag
    if isinstance(payload, list):
        return any(release_tag_present(item, tag) for item in payload)
    return False


def peeled_commit_sha(ref_obj: dict, tag_obj: Optional[dict]) -> str:
    obj = ref_obj.get("object")
    if not isinstance(obj, dict):
        raise GuardError("tag-ref")
    kind = obj.get("type")
    sha = obj.get("sha")
    if kind == "commit":
        if not isinstance(sha, str) or not SHA_RE.match(sha):
            raise GuardError("tag-ref")
        return sha
    if kind == "tag":
        if tag_obj is None:
            if not isinstance(sha, str) or not SHA_RE.match(sha):
                raise GuardError("tag-ref")
            raise GuardError(f"fetch-tag-object:{sha}")
        inner = tag_obj.get("object")
        if not isinstance(inner, dict):
            raise GuardError("tag-object")
        commit = inner.get("sha")
        if inner.get("type") != "commit" or not isinstance(commit, str) or not SHA_RE.match(commit):
            raise GuardError("tag-object")
        return commit
    raise GuardError("tag-ref")


def _check_versions(repo: Path, version: Optional[str]) -> bool:
    script = repo / "scripts" / "check-versions.py"
    if not script.is_file():
        return False
    cmd = [sys.executable, str(script)]
    if version is not None:
        cmd.append(version)
    dropped = {
        "RUNNER_TEMP",
        "GH_TOKEN",
        "ACTIONS_ID_TOKEN_REQUEST_TOKEN",
        "ACTIONS_ID_TOKEN_REQUEST_URL",
    }
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("GITHUB_") and key not in dropped
    }
    proc = subprocess.run(cmd, cwd=str(repo), env=env)
    return proc.returncode == 0


def check_versions_on_main(repo: Path) -> bool:
    parent = Path(tempfile.mkdtemp(prefix="maven-mcp-main-"))
    tree = parent / "tree"
    added = False
    try:
        proc = _git(repo, ["worktree", "add", "--detach", str(tree), "refs/heads/main"])
        if proc.returncode != 0:
            return False
        added = True
        return _check_versions(tree, None)
    finally:
        if added:
            _git(repo, ["worktree", "remove", "--force", str(tree)])
        shutil.rmtree(parent, ignore_errors=True)


def _read_pypi_body(path: Optional[Path], status: int) -> Optional[dict]:
    if path is None or status == 404 or not path.is_file():
        return None
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise GuardError("pypi-json") from exc
    if not isinstance(parsed, dict):
        raise GuardError("pypi-json")
    return parsed


def _dispatch_env() -> Tuple[str, str, str, str]:
    ref = os.environ.get("GITHUB_REF", "")
    repository = os.environ.get("GITHUB_REPOSITORY", "")
    sha = os.environ.get("SHA", "")
    version = os.environ.get("VERSION", "")
    return ref, repository, sha, version


def cmd_assert_build(args: argparse.Namespace) -> int:
    try:
        assert_release_build(Path(args.pyproject).read_text(encoding="utf-8"))
    except GuardError as exc:
        print(f"reason: {exc.reason}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"reason: pyproject-read:{type(exc).__name__}", file=sys.stderr)
        return 1
    return 0


def cmd_classify(args: argparse.Namespace) -> int:
    repo = Path(args.repo)
    ref, repository, sha, version = _dispatch_env()
    if not SHA_RE.match(sha):
        print("reason: sha", file=sys.stderr)
        return 1
    try:
        # Evidence is in memory before the release tree's check-versions.py runs.
        check_runs = load_check_runs_path(Path(args.check_runs))
        pyproject_text = git_show(repo, f"{sha}:pyproject.toml")
        pypi_body = _read_pypi_body(Path(args.pypi_body) if args.pypi_body else None, args.pypi_status)
        state = read_git_state(repo, sha)
        version_on_main = project_version(git_show(repo, "refs/heads/main:pyproject.toml"))
        # Main first: the SHA script can edit .git/config before the next git call.
        check_versions_main_ok = check_versions_on_main(repo)
        check_versions_sha_ok = _check_versions(repo, version)
        tag_sha = args.tag_object_sha or None
        if tag_sha == "":
            tag_sha = None
        facts = ClassifyFacts(
            sha=sha,
            version=version,
            version_on_main=version_on_main,
            main_is_ancestor_of_sha=state.main_is_ancestor_of_sha,
            sha_is_ancestor_of_main=state.sha_is_ancestor_of_main,
            pypi_status=args.pypi_status,
            pypi_body=pypi_body,
            tag_object_sha=tag_sha,
            release_exists=args.release_exists,
        )
        spec = GuardSpec(
            github_ref=ref,
            github_repository=repository,
            sha=sha,
            version=version,
            object_type=state.object_type,
            main_ref_exists=state.main_ref_exists,
            develop_ref_exists=state.develop_ref_exists,
            sha_is_ancestor_of_develop=state.sha_is_ancestor_of_develop,
            check_versions_sha_ok=check_versions_sha_ok,
            check_versions_main_ok=check_versions_main_ok,
            pyproject_text=pyproject_text,
            check_runs=check_runs,
            facts=facts,
        )
        outcome = evaluate(spec)
    except GuardError as exc:
        print(f"reason: {exc.reason}", file=sys.stderr)
        return 1
    if not outcome.ok or outcome.klass is None:
        print(f"reason: {outcome.reason}", file=sys.stderr)
        return 1
    summary = render_summary(
        sha=sha,
        version=version,
        version_on_main=facts.version_on_main,
        log_main_to_sha=git_log_oneline(repo, f"refs/heads/main..{sha}"),
        log_sha_to_main=git_log_oneline(repo, f"{sha}..refs/heads/main"),
        klass=outcome.klass,
        checks=outcome.checks,
    )
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as handle:
            handle.write(summary)
    output_path = os.environ.get("GITHUB_OUTPUT")
    if output_path:
        with open(output_path, "a", encoding="utf-8") as handle:
            handle.write(f"class={outcome.klass}\n")
    print(outcome.klass)
    return 0


def cmd_next_link() -> int:
    try:
        url = next_link(link_header_value(sys.stdin.read()))
    except GuardError as exc:
        print(f"reason: {exc.reason}", file=sys.stderr)
        return 1
    if url:
        print(url)
    return 0


def cmd_peel_tag(args: argparse.Namespace) -> int:
    ref_obj = json.loads(Path(args.ref_json).read_text(encoding="utf-8"))
    tag_obj = None
    if args.tag_object_json:
        tag_obj = json.loads(Path(args.tag_object_json).read_text(encoding="utf-8"))
    try:
        print(peeled_commit_sha(ref_obj, tag_obj))
    except GuardError as exc:
        if exc.reason.startswith("fetch-tag-object:"):
            print(exc.reason.split(":", 1)[1])
            return 2
        print(f"reason: {exc.reason}", file=sys.stderr)
        return 1
    return 0


def cmd_inspect_dist(args: argparse.Namespace) -> int:
    try:
        inspect_dist(
            Path(args.dist),
            args.version,
            Path(args.readme).read_text(encoding="utf-8"),
            Path(args.matrix).read_bytes(),
            Path(args.server).read_bytes(),
        )
    except GuardError as exc:
        print(f"reason: {exc.reason}", file=sys.stderr)
        return 1
    except (OSError, zipfile.BadZipFile, tarfile.TarError) as exc:
        print(f"reason: dist-read:{type(exc).__name__}", file=sys.stderr)
        return 1
    return 0


def cmd_upload_files(args: argparse.Namespace) -> int:
    dist = Path(args.dist)
    names = [path.name for path in sorted(dist.glob("*.whl"))]
    names.extend(path.name for path in sorted(dist.glob("*.tar.gz")))
    try:
        body = _read_pypi_body(Path(args.pypi_body) if args.pypi_body else None, args.pypi_status)
        chosen = filenames_to_upload(args.klass, names, args.pypi_status, body)
    except GuardError as exc:
        print(f"reason: {exc.reason}", file=sys.stderr)
        return 1
    for name in chosen:
        print(dist / name)
    return 0


def cmd_is_already_exists() -> int:
    if publish_output_is_already_exists(sys.stdin.read()):
        return 0
    return 1


def _fetch_pypi(version: str) -> Tuple[int, Optional[dict]]:
    url = PYPI_PROJECT_URL.format(version=version)
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            status = getattr(response, "status", 200)
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return 404, None
        return exc.code, None
    except (urllib.error.URLError, json.JSONDecodeError, TimeoutError, OSError):
        return 0, None
    if not isinstance(payload, dict):
        return status, None
    return status, payload


def cmd_poll(args: argparse.Namespace) -> int:
    matched = poll_until_match(lambda: _fetch_pypi(args.version), args.version, time.sleep)
    if not matched:
        print("reason: conflict", file=sys.stderr)
        return 1
    return 0


def cmd_release_listed(args: argparse.Namespace) -> int:
    try:
        payload = json.loads(Path(args.body).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"reason: release-json:{type(exc).__name__}", file=sys.stderr)
        return 2
    if release_tag_present(payload, args.tag):
        return 0
    return 1


def _notes_from_git(repo: Path, sha: str, version: str) -> Optional[str]:
    proc = _git(repo, ["show", f"{sha}:docs/releases/v{version}.md"])
    if proc.returncode != 0:
        return None
    return proc.stdout


def cmd_promote(args: argparse.Namespace) -> int:
    repo = Path(args.repo)
    sha = os.environ.get("SHA", "")
    version = os.environ.get("VERSION", "")
    klass = os.environ.get("CLASS", "")
    tag_sha = os.environ.get("TAG_OBJECT_SHA") or None
    release_exists = os.environ.get("RELEASE_EXISTS", "") == "true"
    try:
        if not SHA_RE.match(sha) or not VERSION_RE.match(version):
            raise GuardError("inputs")
        state = read_git_state(repo, sha)
        remote = subprocess.run(
            ["git", "ls-remote", f"https://github.com/{REPO}.git", "refs/tags/*"],
            check=False,
            capture_output=True,
            text=True,
        )
        if remote.returncode != 0:
            raise GuardError("ls-remote")
        tags = parse_ls_remote_tags(remote.stdout)

        def ancestor_of_sha(commit: str) -> bool:
            return is_ancestor(repo, commit, sha)

        vprev = select_vprev(tags, ancestor_of_sha)
        log = ""
        if vprev:
            previous = next(tag.object_sha for tag in tags if tag.name == vprev)
            log = git_log_oneline(repo, f"{previous}..{sha}")
        plan = promote_plan(
            klass=klass,
            sha=sha,
            version=version,
            main_sha=state.main_sha,
            main_is_ancestor_of_sha=state.main_is_ancestor_of_sha,
            tag_object_sha=tag_sha,
            release_exists=release_exists,
            notes_text=_notes_from_git(repo, sha, version),
            vprev=vprev,
            log_oneline=log,
        )
    except GuardError as exc:
        print(f"reason: {exc.reason}", file=sys.stderr)
        return 1
    body_path = Path(args.body_out)
    body_path.write_text(plan.body, encoding="utf-8")
    print(f"main={plan.main}")
    print(f"tag={plan.tag}")
    print(f"release={plan.release}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="release_guard.py")
    sub = parser.add_subparsers(dest="cmd", required=True)

    assert_build = sub.add_parser("assert-build")
    assert_build.add_argument("--pyproject", required=True)
    sub.add_parser("next-link")

    peel = sub.add_parser("peel-tag")
    peel.add_argument("--ref-json", required=True)
    peel.add_argument("--tag-object-json")

    classify_cmd = sub.add_parser("classify")
    classify_cmd.add_argument("--repo", default=".")
    classify_cmd.add_argument("--check-runs", required=True)
    classify_cmd.add_argument("--pypi-status", type=int, required=True)
    classify_cmd.add_argument("--pypi-body")
    classify_cmd.add_argument("--tag-object-sha", default="")
    classify_cmd.add_argument("--release-exists", action="store_true")

    inspect = sub.add_parser("inspect-dist")
    inspect.add_argument("--dist", required=True)
    inspect.add_argument("--version", required=True)
    inspect.add_argument("--readme", required=True)
    inspect.add_argument("--matrix", required=True)
    inspect.add_argument("--server", required=True)

    listed = sub.add_parser("release-listed")
    listed.add_argument("--tag", required=True)
    listed.add_argument("--body", required=True)

    upload = sub.add_parser("upload-files")
    upload.add_argument("--class", dest="klass", required=True)
    upload.add_argument("--dist", required=True)
    upload.add_argument("--pypi-status", type=int, required=True)
    upload.add_argument("--pypi-body")

    sub.add_parser("is-already-exists")

    poll = sub.add_parser("poll")
    poll.add_argument("--version", required=True)

    promote = sub.add_parser("promote")
    promote.add_argument("--repo", default=".")
    promote.add_argument("--body-out", required=True)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    commands = {
        "assert-build": lambda: cmd_assert_build(args),
        "next-link": lambda: cmd_next_link(),
        "peel-tag": lambda: cmd_peel_tag(args),
        "classify": lambda: cmd_classify(args),
        "inspect-dist": lambda: cmd_inspect_dist(args),
        "release-listed": lambda: cmd_release_listed(args),
        "upload-files": lambda: cmd_upload_files(args),
        "is-already-exists": lambda: cmd_is_already_exists(),
        "poll": lambda: cmd_poll(args),
        "promote": lambda: cmd_promote(args),
    }
    return commands[args.cmd]()


if __name__ == "__main__":
    raise SystemExit(main())
