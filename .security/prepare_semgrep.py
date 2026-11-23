"""Fetch the pinned local regression rules without redistributing their bodies.

Run with the scanner environment, which supplies httpx and PyYAML. Upstream
Semgrep Rules License terms apply to the downloaded files.
"""

import argparse
import hashlib
import json
import re
from pathlib import Path, PurePosixPath

import httpx
import yaml

ROOT = Path(__file__).resolve().parents[1]
ORIGIN = "https://raw.githubusercontent.com/semgrep/semgrep-rules"
MAX_BYTES = 1024 * 1024


def digest(data):
    return hashlib.sha256(data).hexdigest()


def prepare(root, client, offline=False):
    directory = root / ".artifacts" / "semgrep-rules"
    directory.mkdir(parents=True, exist_ok=True)
    output = directory / "selected.yaml"
    # A failed refresh must not leave a runnable stale selection behind.
    output.unlink(missing_ok=True)
    lock = json.loads((root / ".security" / "semgrep-rules.lock.json").read_text())
    revision = lock["revision"]
    if not re.fullmatch(r"[a-f0-9]{40}", revision):
        raise ValueError("Rules require a full pinned revision")
    selected = []
    seen = set()
    for entry in lock["rules"]:
        rule_id = entry["id"]
        path = PurePosixPath(entry["path"])
        if (
            not re.fullmatch(r"[a-z0-9-]+", rule_id)
            or rule_id in seen
            or path.is_absolute()
            or ".." in path.parts
            or path.suffix != ".yaml"
        ):
            raise ValueError("Invalid rule identity or source path")
        seen.add(rule_id)
        cached = directory / (rule_id + ".source")
        if cached.exists():
            data = cached.read_bytes()
        elif offline:
            raise ValueError("Pinned rule is missing from the local cache: " + rule_id)
        else:
            with client.stream("GET", f"{ORIGIN}/{revision}/{path}") as response:
                response.raise_for_status()
                chunks = []
                size = 0
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > MAX_BYTES:
                        raise ValueError("Rule source exceeds the download limit")
                    chunks.append(chunk)
                data = b"".join(chunks)
        if len(data) > MAX_BYTES or digest(data) != entry["sha256"]:
            raise ValueError("Rule source checksum mismatch: " + rule_id)
        rules = yaml.safe_load(data)["rules"]
        matches = [rule for rule in rules if rule["id"] == rule_id]
        if len(matches) != 1:
            raise ValueError("Pinned rule is missing or duplicated: " + rule_id)
        rule = matches[0]
        canonical = json.dumps(rule, sort_keys=True, separators=(",", ":")).encode()
        if digest(canonical) != entry["rule_sha256"]:
            raise ValueError("Selected rule differs from the reviewed definition: " + rule_id)
        cached.write_bytes(data)
        selected.append(rule)
    if not selected:
        raise ValueError("Empty rule selection")
    # JSON is also YAML; only explicitly selected rules reach the scanner.
    output.write_text(json.dumps({"rules": selected}, indent=2) + "\n")
    print(f"Prepared {len(selected)} pinned rules at {output}")
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--offline", action="store_true", help="Require verified cached sources")
    args = parser.parse_args()
    with httpx.Client(trust_env=False, follow_redirects=False, timeout=45) as client:
        prepare(ROOT, client, offline=args.offline)


if __name__ == "__main__":
    main()
