"""仓库只跟踪产品、测试和文档所需文件，过程证据保存在仓库外。"""

import subprocess  # nosec B404
from pathlib import Path


def test_no_tracked_process_artifacts() -> None:
    root = Path(__file__).resolve().parents[1]
    tracked = subprocess.check_output(["git", "ls-files", "-z"], cwd=root).split(b"\0")  # nosec B603, B607
    forbidden = (b"investigation/", b"evidence/", b".artifacts/")
    artifacts = [path.decode() for path in tracked if path.startswith(forbidden)]
    assert not artifacts, "过程产物不能被 Git 跟踪：\n" + "\n".join(artifacts)


def test_tracked_documents_have_no_personal_absolute_paths() -> None:
    root = Path(__file__).resolve().parents[1]
    tracked = subprocess.check_output(["git", "ls-files", "-z"], cwd=root).split(b"\0")  # nosec B603, B607
    violations = []
    for raw_path in tracked:
        if not raw_path:
            continue
        path = Path(raw_path.decode())
        if path.parts[0] != "docs" and path.suffix.lower() not in {
            ".md",
            ".markdown",
            ".mdx",
            ".rst",
        }:
            continue
        content = (root / path).read_bytes()
        for number, line in enumerate(content.splitlines(), 1):
            if b"/Users/" in line:
                violations.append(f"{path}:{number}")
    assert not violations, "文档包含不可移植的个人绝对路径：\n" + "\n".join(violations)
