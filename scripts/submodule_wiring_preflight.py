"""Read-only preflight for safe superproject gitlink wiring.

Companion repos must be committed inside each child repo before the
superproject gitlink is bumped. This script makes that rule machine-readable
for automation loops so dirty child worktrees are blocked before any gitlink
commit is attempted.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MODULES = ("eta_engine", "firm", "mnq_backtest")

_STATUS_RE = re.compile(
    r"^(?P<prefix>[ +\-U])(?P<sha>[0-9a-f]{40})\s+"
    r"(?P<path>\S+)(?:\s+\((?P<label>.+)\))?$"
)
_GITLINK_BY_PREFIX = {
    " ": "aligned",
    "+": "diverged",
    "-": "uninitialized",
    "U": "conflict",
}
_CHANGE_COUNT_KEYS = (
    "modified",
    "added",
    "deleted",
    "renamed",
    "copied",
    "untracked",
    "conflicted",
    "other",
)


@dataclass(frozen=True)
class ParsedSubmoduleStatus:
    path: str
    sha: str
    gitlink: str
    label: str = ""


@dataclass(frozen=True)
class ModulePreflight:
    path: str
    gitlink: str
    exists: bool
    dirty_entries: list[str]
    blockers: list[str]
    sha: str = ""
    label: str = ""

    @property
    def dirty(self) -> bool:
        return bool(self.dirty_entries)

    @property
    def ready(self) -> bool:
        return not self.blockers

    def as_payload(self) -> dict[str, object]:
        return {
            "path": self.path,
            "ready": self.ready,
            "gitlink": self.gitlink,
            "exists": self.exists,
            "dirty": self.dirty,
            "dirty_entries": self.dirty_entries,
            "dirty_summary": summarize_porcelain_status(self.dirty_entries),
            "blockers": self.blockers,
            "sha": self.sha,
            "label": self.label,
        }


@dataclass(frozen=True)
class SubmoduleWiringReport:
    root: Path
    modules: dict[str, ModulePreflight]

    @property
    def ready(self) -> bool:
        return all(module.ready for module in self.modules.values())

    @property
    def action(self) -> str:
        return "safe_to_wire_gitlinks" if self.ready else "do_not_wire_gitlinks"

    def as_payload(self) -> dict[str, object]:
        return {
            "root": str(self.root),
            "ready": self.ready,
            "action": self.action,
            "modules": {path: module.as_payload() for path, module in self.modules.items()},
        }


def parse_submodule_status_lines(
    lines: list[str],
) -> dict[str, ParsedSubmoduleStatus]:
    parsed: dict[str, ParsedSubmoduleStatus] = {}
    for line in lines:
        match = _STATUS_RE.match(line.rstrip())
        if match is None:
            continue
        prefix = match.group("prefix")
        path = match.group("path")
        parsed[path] = ParsedSubmoduleStatus(
            path=path,
            sha=match.group("sha"),
            gitlink=_GITLINK_BY_PREFIX.get(prefix, "unknown"),
            label=match.group("label") or "",
        )
    return parsed


def summarize_porcelain_status(lines: list[str]) -> dict[str, object]:
    """Return a compact, operator-friendly summary of porcelain entries."""

    entries = [line.rstrip() for line in lines if line.strip()]
    counts = {key: 0 for key in _CHANGE_COUNT_KEYS}
    change_type_preview: dict[str, list[str]] = {key: [] for key in _CHANGE_COUNT_KEYS}
    groups: dict[str, int] = {}

    for line in entries:
        prefix = line[:2]
        path = _porcelain_path(line)
        group = _path_group(path)
        groups[group] = groups.get(group, 0) + 1
        if prefix == "??":
            kind = "untracked"
        elif "U" in prefix:
            kind = "conflicted"
        elif "M" in prefix:
            kind = "modified"
        elif "A" in prefix:
            kind = "added"
        elif "D" in prefix:
            kind = "deleted"
        elif "R" in prefix:
            kind = "renamed"
        elif "C" in prefix:
            kind = "copied"
        else:
            kind = "other"
        counts[kind] += 1
        if len(change_type_preview[kind]) < 5:
            change_type_preview[kind].append(path)

    top_groups = [
        {"group": name, "count": count}
        for name, count in sorted(groups.items(), key=lambda item: (-item[1], item[0]))[:8]
    ]
    preview = [_porcelain_path(line) for line in entries[:5]]
    detail = f"{len(entries)} dirty entries"
    nonzero = {name: value for name, value in counts.items() if value}
    if nonzero:
        detail += " (" + ", ".join(f"{name}={value}" for name, value in nonzero.items()) + ")"
    if top_groups:
        detail += "; top_groups: " + ", ".join(f"{item['group']}={item['count']}" for item in top_groups[:5])

    return {
        "entry_count": len(entries),
        "counts": counts,
        "preview": preview,
        "top_groups": top_groups,
        "change_type_preview": {name: paths for name, paths in change_type_preview.items() if paths},
        "review_action": _dirty_review_action(len(entries), counts),
        "detail": detail,
    }


def _porcelain_path(line: str) -> str:
    if len(line) > 3 and line[2] == " ":
        return line[3:]
    if len(line) > 2 and line[1] == " ":
        return line[2:]
    return line.strip()


def _path_group(path: str) -> str:
    normalized = path.replace("\\", "/")
    if " -> " in normalized:
        normalized = normalized.rsplit(" -> ", 1)[-1]
    return normalized.split("/", 1)[0] or "root"


def _dirty_review_action(entry_count: int, counts: dict[str, int]) -> str:
    if entry_count == 0:
        return "none"
    if counts.get("conflicted", 0):
        return "resolve_conflicts_before_gitlink_wiring"
    if entry_count > 50:
        return "split_dirty_worktree_by_group_before_gitlink_wiring"
    if counts.get("untracked", 0):
        return "review_untracked_files_before_gitlink_wiring"
    return "review_dirty_files_before_gitlink_wiring"


def evaluate_submodule_wiring(
    *,
    root: Path,
    required_modules: tuple[str, ...] = DEFAULT_MODULES,
    submodule_status_lines: list[str],
    porcelain_by_module: dict[str, list[str]],
) -> SubmoduleWiringReport:
    root = root.resolve()
    parsed = parse_submodule_status_lines(submodule_status_lines)
    modules: dict[str, ModulePreflight] = {}

    for module in required_modules:
        status = parsed.get(module)
        module_path = root / module
        exists = module_path.exists()
        gitlink = status.gitlink if status else "missing"
        dirty_entries = porcelain_by_module.get(module, [])
        blockers: list[str] = []

        if not exists:
            blockers.append("missing submodule checkout")
        if gitlink != "aligned":
            blockers.append(f"gitlink {gitlink}")
        if dirty_entries:
            blockers.append("dirty worktree")

        modules[module] = ModulePreflight(
            path=module,
            gitlink=gitlink,
            exists=exists,
            dirty_entries=dirty_entries,
            blockers=blockers,
            sha=status.sha if status else "",
            label=status.label if status else "",
        )

    return SubmoduleWiringReport(root=root, modules=modules)


def inspect_submodule_wiring(
    *,
    root: Path = WORKSPACE_ROOT,
    required_modules: tuple[str, ...] = DEFAULT_MODULES,
) -> SubmoduleWiringReport:
    submodule_status_lines = _run_git_lines(
        ["submodule", "status", "--recursive"],
        cwd=root,
    )
    porcelain_by_module: dict[str, list[str]] = {}
    for module in required_modules:
        module_path = root / module
        if module_path.exists():
            porcelain_by_module[module] = _run_git_lines(
                ["status", "--porcelain", "--untracked-files=normal"],
                cwd=module_path,
            )
        else:
            porcelain_by_module[module] = []

    return evaluate_submodule_wiring(
        root=root,
        required_modules=required_modules,
        submodule_status_lines=submodule_status_lines,
        porcelain_by_module=porcelain_by_module,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=WORKSPACE_ROOT)
    parser.add_argument(
        "--module",
        action="append",
        dest="modules",
        help="repeatable submodule path; defaults to eta_engine, firm, mnq_backtest",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    modules = tuple(args.modules) if args.modules else DEFAULT_MODULES
    report = inspect_submodule_wiring(root=args.root, required_modules=modules)
    print(json.dumps(report.as_payload(), indent=2, sort_keys=True))
    return 0 if report.ready else 1


def _run_git_lines(args: list[str], *, cwd: Path) -> list[str]:
    proc = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or f"git {' '.join(args)} failed")
    return [line for line in proc.stdout.splitlines() if line]


if __name__ == "__main__":
    raise SystemExit(main())
