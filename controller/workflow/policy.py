"""Deterministic, fail-closed merge eligibility; this module never merges.

Only the human-maintained policy grants authority.  Model output is evidence,
not policy.  The caller must collect current repository/PR observations, supply
``trusted_pr_url`` from its task or saved receipt, and preserve their provenance.
``eligible`` means the supplied observations satisfy policy, not that the user
has accepted the product or that GitHub has executed a merge.
"""

from __future__ import annotations

from datetime import datetime, timezone
from fnmatch import fnmatchcase
import math
import re
from typing import Any
from urllib.parse import urlsplit


_FULL_SHA = re.compile(r"[0-9a-fA-F]{40}\Z")
_PROTECTED_PATTERNS = (
    ".github/**", "**/.github/**",
    ".codex/**", "**/.codex/**", "controller/**", "**/controller/**",
    "AGENTS.md", "**/AGENTS.md", "CLAUDE.md", "**/CLAUDE.md",
    "workflow/**", "codex-workflow/workflow/**", "**/codex-workflow/workflow/**",
    "merge-policy*.json", "**/merge-policy*.json",
    "config/policy*.json", "**/config/policy*.json",
    "tests/test_policy.py", "**/tests/test_policy.py",
    "package.json", "**/package.json", "pyproject.toml", "**/pyproject.toml",
    "pytest.ini", "**/pytest.ini", "tox.ini", "**/tox.ini",
    "Makefile", "**/Makefile", "Justfile", "**/Justfile",
    ".pre-commit-config.yaml", "**/.pre-commit-config.yaml",
)


def _timestamp(value: Any) -> float | None:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return None
        try:
            result = value.timestamp()
        except (ValueError, OverflowError, OSError):
            return None
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            return None
        return _timestamp(parsed)
    elif type(value) in (int, float):
        try:
            result = float(value)
        except (OverflowError, ValueError):
            return None
    else:
        return None
    return result if math.isfinite(result) else None


def _text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _url_parts(value: Any) -> tuple[str, list[str]] | None:
    if not _text(value):
        return None
    try:
        parsed = urlsplit(value)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username
                or parsed.password or parsed.query or parsed.fragment):
            return None
        # Reading .port also rejects a malformed port without throwing upstream.
        _ = parsed.port
    except ValueError:
        return None
    segments = parsed.path.rstrip("/").lstrip("/").split("/")
    if any(not segment or segment in (".", "..") or "%" in segment
           or "\\" in segment for segment in segments):
        return None
    return parsed.netloc.casefold(), segments


def _repo_identity(value: Any) -> tuple[str, str, str] | None:
    parts = _url_parts(value)
    if not parts or len(parts[1]) != 2:
        return None
    host, (owner, repo) = parts
    if repo.endswith(".git"):
        repo = repo[:-4]
    if not repo:
        return None
    return host, owner.casefold(), repo.casefold()


def _pr_identity(value: Any) -> tuple[str, str, str, int] | None:
    parts = _url_parts(value)
    if not parts:
        return None
    host, segments = parts
    if (len(segments) not in (4, 5) or segments[2] != "pull"
            or not segments[3].isascii() or not segments[3].isdigit()
            or len(segments[3]) > 19
            or int(segments[3]) < 1
            or (len(segments) == 5 and segments[4] not in ("files", "checks", "commits"))):
        return None
    return host, segments[0].casefold(), segments[1].casefold(), int(segments[3])


def _valid_path(value: Any, *, pattern: bool = False) -> bool:
    if not _text(value) or value != value.strip():
        return False
    if "\\" in value or value.startswith("/") or ":" in value or "\x00" in value:
        return False
    segments = value.split("/")
    if any(segment in ("", ".", "..") for segment in segments):
        return False
    return pattern or not any(char in value for char in "*?[")


def _string_list(value: Any) -> bool:
    return (isinstance(value, list) and bool(value)
            and all(_text(item) for item in value)
            and len(set(value)) == len(value))


def evaluate_merge(policy: dict, evidence: dict, *, now: Any = None) -> dict:
    """Return ``{eligible: bool, reasons: list[str]}`` without side effects.

    ``now`` accepts a timezone-aware datetime, ISO timestamp or epoch seconds.
    Missing, malformed, stale, unexecuted and unknown observations all deny.
    ``required_checks`` names must appear exactly once in both check envelopes.
    Each envelope/review/browser receipt is bound to both current full SHAs.
    """
    reasons: list[str] = []

    def deny(reason: str) -> None:
        if reason not in reasons:
            reasons.append(reason)

    if not isinstance(policy, dict):
        return {"eligible": False, "reasons": ["policy must be a human-maintained object"]}
    if not isinstance(evidence, dict):
        return {"eligible": False, "reasons": ["evidence must be an object"]}
    current_time = _timestamp(datetime.now(timezone.utc) if now is None else now)
    if current_time is None:
        return {"eligible": False, "reasons": ["now must be a valid timezone-aware timestamp"]}

    if policy.get("enabled") is not True:
        deny("automatic merge is not explicitly enabled by policy")
    if not _text(policy.get("approved_by")):
        deny("policy requires a human approved_by")
    approved_at = _timestamp(policy.get("approved_at"))
    if approved_at is None or approved_at > current_time:
        deny("policy approved_at is missing, invalid or in the future")

    repo = _repo_identity(policy.get("repo_url"))
    if repo is None:
        deny("policy repo_url must identify an HTTPS repository")
    if repo is None or _repo_identity(evidence.get("repo_url")) != repo:
        deny("evidence repository does not match policy")
    if (not _text(policy.get("base_branch"))
            or evidence.get("base_branch") != policy.get("base_branch")):
        deny("base branch does not match policy")

    allowed_paths = policy.get("allowed_paths")
    if (not _string_list(allowed_paths)
            or not all(_valid_path(item, pattern=True) for item in allowed_paths)):
        deny("policy allowed_paths must contain valid unique path patterns")
        allowed_paths = []
    required_checks = policy.get("required_checks")
    if not _string_list(required_checks):
        deny("policy required_checks must be a nonempty unique list")
        required_checks = []
    local_required = policy.get("required_local_checks", required_checks)
    if not _string_list(local_required) or not set(required_checks).issubset(local_required):
        deny("policy required_local_checks must include all required_checks")
        local_required = []
    max_files = policy.get("max_changed_files")
    if type(max_files) is not int or max_files < 1:
        deny("policy max_changed_files must be a positive integer")
        max_files = 0
    max_age = policy.get("max_evidence_age_seconds", 900)
    if type(max_age) not in (int, float) or _timestamp(max_age) is None or max_age <= 0:
        deny("policy max_evidence_age_seconds must be positive and finite")
        max_age = 0
    extra_protected = policy.get("protected_paths", [])
    if (not isinstance(extra_protected, list)
            or not all(_valid_path(item, pattern=True) for item in extra_protected)):
        deny("policy protected_paths must be a list of valid path patterns")
        extra_protected = []

    head_sha, base_sha = evidence.get("head_sha"), evidence.get("base_sha")
    for name, value in (("head_sha", head_sha), ("base_sha", base_sha)):
        if not isinstance(value, str) or not _FULL_SHA.fullmatch(value):
            deny(f"{name} must be a full 40-character commit SHA")

    changed_files = evidence.get("changed_files")
    if (not isinstance(changed_files, list) or not changed_files
            or not all(_valid_path(path) for path in changed_files)):
        deny("changed_files must be a nonempty list of valid repository paths")
        changed_files = []
    if len(changed_files) != len(set(changed_files)):
        deny("changed_files contains duplicate paths")
    if len(changed_files) > max_files:
        deny("changed file count exceeds policy")
    protected = tuple(pattern.casefold() for pattern in (*_PROTECTED_PATTERNS, *extra_protected))
    for path in changed_files:
        if not any(fnmatchcase(path, pattern) for pattern in allowed_paths):
            deny(f"path is outside the allowlist: {path}")
        if any(fnmatchcase(path.casefold(), pattern) for pattern in protected):
            deny(f"protected path cannot be automatically merged: {path}")

    pr = evidence.get("pr")
    if not isinstance(pr, dict):
        deny("current PR observation is missing")
        pr = {}
    pr_identity = _pr_identity(pr.get("url"))
    trusted_identity = _pr_identity(evidence.get("trusted_pr_url"))
    if (pr_identity is None or trusted_identity is None
            or pr_identity != trusted_identity or pr_identity[:3] != repo
            or type(pr.get("number")) is not int
            or pr.get("number") != pr_identity[3]):
        deny("PR URL/number must match the trusted task PR and policy repository")
    if pr.get("state") != "open":
        deny("PR must be open")
    if pr.get("draft") is not False:
        deny("PR must explicitly be non-draft")
    if pr.get("mergeable") is not True:
        deny("GitHub must explicitly report the PR mergeable")
    if evidence.get("branch_rules_observed") is not True:
        deny("repository branch rules have not been explicitly observed")
    if evidence.get("required_reviews_satisfied") is not True:
        deny("GitHub required reviews are not explicitly satisfied")
    if evidence.get("bypass") is not False:
        deny("rule bypass must explicitly be false")

    def envelope(name: str) -> dict:
        item = evidence.get(name)
        if not isinstance(item, dict):
            deny(f"{name} evidence is missing")
            return {}
        if item.get("head_sha") != head_sha or item.get("base_sha") != base_sha:
            deny(f"{name} is not bound to current head_sha and base_sha")
        observed = _timestamp(item.get("observed_at"))
        if (observed is None or observed > current_time
                or current_time - observed > max_age):
            deny(f"{name} observed_at is missing, invalid, future or stale")
        return item

    review = envelope("review")
    if (review.get("verdict") != "pass"
            or type(review.get("blocking_findings")) is not int
            or review.get("blocking_findings") != 0):
        deny("independent review must pass with exactly zero blocking findings")

    for name in ("local_checks", "github_checks"):
        checks = envelope(name)
        results = checks.get("results")
        if not isinstance(results, list):
            deny(f"{name} results must be a list")
            results = []
        if any(not isinstance(result, dict) or not _text(result.get("name")) for result in results):
            deny(f"{name} contains malformed check records")
        for required in (local_required if name == "local_checks" else required_checks):
            matching = [item for item in results if isinstance(item, dict) and item.get("name") == required]
            if len(matching) != 1:
                deny(f"{name} requires exactly one result for: {required}")
            elif matching[0].get("status") != "success":
                deny(f"{name} required check is not success: {required}")
            elif name == "local_checks" and matching[0].get("executed") is not True:
                deny(f"local required check was not explicitly executed: {required}")

    browser = envelope("browser_receipt")
    if trusted_identity is None or _pr_identity(browser.get("url")) != trusted_identity:
        deny("browser receipt URL does not identify the trusted task PR")

    return {"eligible": not reasons, "reasons": reasons}
