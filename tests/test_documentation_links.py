"""Resolve local Markdown destinations and heading anchors without network access."""

import re
from pathlib import Path
from urllib.parse import unquote, urlsplit

import pytest

ROOT = Path(__file__).resolve().parents[1]


def prose(text):
    fence = None
    for line in text.splitlines():
        marker = re.match(r"^\s*(`{3,}|~{3,})", line)
        if marker:
            token = marker[1]
            if fence is None:
                fence = token
            elif token[0] == fence[0] and len(token) >= len(fence):
                fence = None
            continue
        if fence is None:
            yield line


def anchors(text):
    result = set()
    for line in prose(text):
        heading = re.match(r"^#{1,6}\s+(.+?)(?:\s+#+)?$", line)
        if heading:
            title = re.sub(r"\[([^]]+)\]\([^)]+\)", r"\1", heading[1])
            base = re.sub(r"[^\w -]", "", title.lower()).replace(" ", "-")
            slug = base
            suffix = 0
            while slug in result:
                suffix += 1
                slug = f"{base}-{suffix}"
            result.add(slug)
        result.update(re.findall(r'<a\s+(?:id|name)=["\']([^"\']+)', line))
    return result


def destinations(text):
    lines = list(prose(text))
    references = {}
    for line in lines:
        definition = re.match(r"^\s*\[([^]]+)\]:\s*<?([^>\s]+)>?", line)
        if definition:
            references[definition[1].casefold()] = definition[2]
    for line in lines:
        if re.match(r"^\s*\[[^]]+\]:", line):
            yield from (references[re.match(r"^\s*\[([^]]+)\]", line)[1].casefold()],)
            continue
        line = re.sub(r"`+[^`]*`+", "", line)
        yield from re.findall(r"\[[^]]*\]\(<?([^)\s>]+)>?(?:\s+\"[^\"]*\")?\)", line)
        for label, reference in re.findall(r"\[([^]]+)\]\[([^]]*)\]", line):
            key = (reference or label).casefold()
            assert key in references, f"Undefined link reference: {key}"
            yield references[key]


def link_errors(source, text):
    errors = []
    for destination in destinations(text):
        url = urlsplit(destination)
        if url.scheme or url.netloc:
            continue
        target = (source.parent / unquote(url.path)).resolve() if url.path else source
        if not target.exists():
            errors.append(f"{destination}: missing file")
        elif url.fragment and target.suffix.lower() == ".md":
            if unquote(url.fragment) not in anchors(target.read_text(encoding="utf-8")):
                errors.append(f"{destination}: missing anchor")
    return errors


def test_public_documentation_relative_files_and_anchors_resolve():
    files = [ROOT / name for name in ("README.md", "CHANGELOG.md", "CONTRIBUTING.md")]
    files += sorted((ROOT / "docs").rglob("*.md")) + sorted((ROOT / "reports").rglob("*.md"))
    errors = [
        f"{path.relative_to(ROOT)}: {error}"
        for path in files
        for error in link_errors(path, path.read_text(encoding="utf-8"))
    ]
    assert not errors, "\n".join(errors)


@pytest.mark.parametrize("destination", ["absent.md", "target.md#missing", "#missing"])
def test_link_checker_detects_broken_destinations(tmp_path, destination):
    source = tmp_path / "source.md"
    source.write_text("# Existing\n", encoding="utf-8")
    (tmp_path / "target.md").write_text("# Existing\n", encoding="utf-8")
    assert link_errors(source, f"[broken]({destination})")


def test_link_checker_handles_unicode_duplicates_references_and_fences(tmp_path):
    source = tmp_path / "source.md"
    source.write_text("# Source\n", encoding="utf-8")
    (tmp_path / "target.md").write_text(
        "# 模型数据发送到哪里\n## `Model` config\n## Model config\n```\n# Not a heading\n```\n",
        encoding="utf-8",
    )
    text = (
        "[Chinese](target.md#%E6%A8%A1%E5%9E%8B%E6%95%B0%E6%8D%AE%E5%8F%91%E9%80%81%E5%88%B0%E5%93%AA%E9%87%8C)\n"
        "[duplicate][config]\n[config]: target.md#model-config-1\n"
        "```\n[example](missing.md)\n```\n`[example](missing.md)`\n"
    )
    assert not link_errors(source, text)
    assert "not-a-heading" not in anchors((tmp_path / "target.md").read_text())
