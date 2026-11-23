"""Guard the trust store and build/test dependencies against known regressions."""

from __future__ import annotations

import importlib.metadata
import ssl
import unicodedata

import certifi
import pytest
from packaging.version import Version


@pytest.mark.parametrize(
    ("package", "minimum"),
    [("certifi", "2026.10.19"), ("setuptools", "84.1.0"), ("pytest", "9.0.3")],
)
def test_installed_dependencies_meet_security_floors(package: str, minimum: str) -> None:
    assert Version(importlib.metadata.version(package)) >= Version(minimum)


def test_compromised_emsign_root_is_not_trusted() -> None:
    context = ssl.create_default_context(cafile=certifi.where())
    common_names = {
        value
        for certificate in context.get_ca_certs()
        for rdn in certificate["subject"]
        for key, value in rdn
        if key == "commonName"
    }
    assert common_names
    assert "emSign ECC Root CA - G3" not in common_names


def test_manifest_exclusion_matches_decomposed_filename() -> None:
    from setuptools.command.egg_info import FileList

    excluded = "secret_caf\N{LATIN SMALL LETTER E WITH ACUTE}.txt"
    files = FileList()
    files.files = [unicodedata.normalize("NFD", excluded), "public.txt"]
    files.process_template_line(f"global-exclude {excluded}")
    assert files.files == ["public.txt"]
