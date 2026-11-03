#!/usr/bin/env python3
"""Prepare an image release version while preserving the recorded Flutter base digest."""
import argparse
import json
from pathlib import Path
import re
import sys


def increase_version(version, increase_type, auto_increase=True):
    if not re.fullmatch(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", version):
        raise ValueError("image_version must be MAJOR.MINOR.PATCH without leading zeroes")
    major, minor, patch = map(int, version.split("."))
    if increase_type == "patch":
        patch += 1
    elif increase_type == "minor":
        minor, patch = minor + 1, 0
    elif increase_type == "major":
        major, minor, patch = major + 1, 0, 0
    elif increase_type != "NONE":
        raise ValueError("increase-type must be patch, minor, major or NONE")
    if auto_increase and increase_type in ("patch", "minor"):
        carry, patch = divmod(patch, 10)
        minor += carry
        carry, minor = divmod(minor, 10)
        major += carry
    return f"{major}.{minor}.{patch}"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("increase_type", choices=("patch", "minor", "major", "NONE"))
    parser.add_argument("--auto-increase", choices=("true", "false"), default="true")
    parser.add_argument("--file", type=Path, default=Path("versions.yml"))
    args = parser.parse_args()
    pin = json.loads(args.file.read_text())
    current_version = pin["image_version"]
    version = increase_version(current_version, args.increase_type,
                               auto_increase=args.auto_increase == "true")
    if version != pin["image_version"]:
        pin["image_version"] = version
        args.file.write_text(json.dumps(pin, indent=2) + "\n")
    print(f"Current version: {current_version}", file=sys.stderr)
    print(f"New version: {version}", file=sys.stderr)
    print(version)


if __name__ == "__main__":
    main()
