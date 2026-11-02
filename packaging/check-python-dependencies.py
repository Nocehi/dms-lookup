#!/usr/bin/env python3
"""Validate the pinned release/test Python environment and evidence schema."""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
from importlib.metadata import PackageNotFoundError, version as installed_version
from typing import Callable, Mapping, Sequence


SUPPORTED_PYTHON = (3, 11)


def _requirement_api():
    try:
        from packaging.markers import default_environment
        from packaging.requirements import InvalidRequirement, Requirement
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "missing packaging; install the pinned release/test environment"
        ) from exc
    return Requirement, InvalidRequirement, default_environment


def check_requirements(
    requirements_path: pathlib.Path,
    *,
    marker_environment: Mapping[str, str] | None = None,
    version_lookup: Callable[[str], str] = installed_version,
    python_version: tuple[int, int] | None = None,
) -> list[str]:
    """Return reproducibility problems for active requirements."""

    problems: list[str] = []
    running_python = python_version or sys.version_info[:2]
    if running_python < SUPPORTED_PYTHON:
        problems.append(
            "unsupported Python "
            f"{running_python[0]}.{running_python[1]}; "
            f"requires >= {SUPPORTED_PYTHON[0]}.{SUPPORTED_PYTHON[1]}"
        )

    try:
        Requirement, InvalidRequirement, default_environment = _requirement_api()
    except RuntimeError as exc:
        problems.append(str(exc))
        return problems

    environment = default_environment()
    if marker_environment is not None:
        environment.update(marker_environment)

    for line_number, raw_line in enumerate(
        requirements_path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            requirement = Requirement(line)
        except InvalidRequirement as exc:
            problems.append(
                f"{requirements_path}:{line_number}: invalid requirement: {exc}"
            )
            continue

        specifiers = list(requirement.specifier)
        if (
            requirement.url is not None
            or requirement.extras
            or len(specifiers) != 1
            or specifiers[0].operator != "=="
            or "*" in specifiers[0].version
        ):
            problems.append(
                f"{requirements_path}:{line_number}: non-exact requirement: {line}"
            )
            continue

        if requirement.marker is not None and not requirement.marker.evaluate(
            environment=environment
        ):
            continue

        expected = specifiers[0].version
        try:
            actual = version_lookup(requirement.name)
        except PackageNotFoundError:
            problems.append(f"missing {requirement.name}=={expected}")
            continue
        if actual != expected:
            problems.append(f"{requirement.name}: expected {expected}, found {actual}")

    return problems


def check_schema(schema_path: pathlib.Path) -> list[str]:
    try:
        from jsonschema import Draft202012Validator
    except ModuleNotFoundError:
        return ["missing jsonschema; install the pinned release/test environment"]

    try:
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        if schema.get("$schema") != "https://json-schema.org/draft/2020-12/schema":
            return ["sdcv evidence schema does not declare Draft 2020-12"]
        Draft202012Validator.check_schema(schema)
    except Exception as exc:
        return [f"cannot load Draft 2020-12 validator: {exc}"]
    return []


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("requirements", type=pathlib.Path)
    parser.add_argument("schema", type=pathlib.Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    problems = check_requirements(args.requirements)
    if not problems:
        problems.extend(check_schema(args.schema))
    if not problems:
        return 0

    for problem in problems:
        print(f"dependency preflight: {problem}", file=sys.stderr)
    print(
        "install the pinned release/test environment with "
        "python3 -m pip install --requirement requirements-dev.txt",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
