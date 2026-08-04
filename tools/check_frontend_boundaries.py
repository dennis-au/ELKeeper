#!/usr/bin/env python3
"""Report private cross-feature and legacy-facade imports in the React tree."""

from __future__ import annotations

import argparse
import re
from pathlib import Path
import sys


IMPORT_RE = re.compile(r"(?:from|import)\s+['\"]([^'\"]+)['\"]")
PAGE_IMPLEMENTATION_RE = re.compile(
    r"\b(?:import|use[A-Z]\w*|fetch|api\.|useQuery|useMutation)\b"
)


def find_violations(root: Path) -> list[str]:
    root = root.resolve()
    source = root / "src"
    features = source / "features"
    violations: list[str] = []
    if not source.exists():
        return violations
    for path in source.rglob("*.ts*"):
        text = path.read_text(encoding="utf-8")
        for target in IMPORT_RE.findall(text):
            # `api.ts` and `types.ts` at the source root are deliberately
            # compatibility facades. New code must use feature-owned contracts.
            if path not in {source / "api.ts", source / "types.ts"} and target.startswith("."):
                resolved = (path.parent / target).resolve()
                if resolved in {source / "api.ts", source / "types.ts"}:
                    violations.append(f"{path.relative_to(root)} imports legacy facade {target}")

            if not features.exists() or not path.is_relative_to(features):
                continue
            relative = path.relative_to(features)
            source_feature = relative.parts[0]
            marker = "/features/"
            if marker not in target:
                continue
            target_feature = target.split(marker, 1)[1].split("/", 1)[0]
            if target_feature != source_feature and target_feature:
                violations.append(f"{path.relative_to(root)} imports {target}")
    return sorted(set(violations))


def find_route_page_violations(root: Path) -> list[str]:
    """Ensure legacy route files remain feature-composition facades only."""

    root = root.resolve()
    pages = root / "src" / "pages"
    if not pages.exists():
        return []
    violations: list[str] = []
    for path in sorted(pages.glob("*Page.tsx")):
        text = path.read_text(encoding="utf-8")
        if PAGE_IMPLEMENTATION_RE.search(text):
            violations.append(
                f"{path.relative_to(root)} contains route-page implementation; move it to its feature workspace"
            )
        if "export" not in text or "/features/" not in text:
            violations.append(
                f"{path.relative_to(root)} is not a feature compatibility facade"
            )
    return violations


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1] / "frontend")
    parser.add_argument("--strict", action="store_true")
    args = parser.parse_args(argv)
    violations = find_violations(args.root) + find_route_page_violations(args.root)
    if violations:
        print("Frontend module boundary report:")
        print("\n".join(f"- {item}" for item in violations))
    else:
        print("Frontend module boundary report: no private cross-feature imports found.")
    return 1 if args.strict and violations else 0


if __name__ == "__main__":
    sys.exit(main())
