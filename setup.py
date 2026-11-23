"""在项目 wheel 的构建目录生成固定分词资源。"""

from __future__ import annotations

import os
import subprocess  # nosec B404
import sys
from pathlib import Path

from setuptools import setup  # type: ignore[import-untyped]
from setuptools.command.build_py import build_py  # type: ignore[import-untyped]

ROOT = Path(__file__).resolve().parent


class BuildPy(build_py):  # type: ignore[misc]
    """从源码随附的固定输入生成资源，不读取宿主安装的运行依赖。"""

    def run(self) -> None:
        super().run()
        dictionary_dir = Path(self.build_lib).resolve() / "semantic_core" / "data"
        # build/lib 会跨版本保留；只清理生成词典，避免旧名字绕过本次重建。
        # unlink 不递归删除目录，也不会删除符号链接所指向的其它资源。
        for cached in dictionary_dir.glob("jieba-prefix-*.json"):
            cached.unlink()
        # Fixed build script/interpreter; the output is one absolute argument.
        subprocess.run(  # nosemgrep: dangerous-subprocess-use-audit
            [
                sys.executable,
                str(ROOT / "scripts" / "build_dictionary.py"),
                "--output-dir",
                str(dictionary_dir),
            ],
            env={
                "PATH": os.defpath,
                "PYTHONHASHSEED": "0",
                "AWS_CONFIG_FILE": os.devnull,
                "AWS_SHARED_CREDENTIALS_FILE": os.devnull,
                "AWS_EC2_METADATA_DISABLED": "true",
            },
            check=True,
            shell=False,  # nosec B603
        )


setup(cmdclass={"build_py": BuildPy})
