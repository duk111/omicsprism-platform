"""构建 OmicsPrism 源码部署包。

把 omicsprism/ 与 omicsprism-platform/ 两个目录打进一个 zip，保留内部目录结构，
排除构建产物、运行时数据与测试残留；包含规则与 omicsprism-platform/.gitignore 一致，
并确保 .gitignore / .gitattributes / .env.example 等配置进包。

用法（任意目录）：
    python omicsprism-platform/scripts/build_deploy_zip.py [输出路径]
默认输出到两个项目目录的上层（D:\\JetBrains\\PythonProjects 同级），
文件名 omicsprism-source-deploy-YYYYMMDD.zip。
"""

from __future__ import annotations

import os
import sys
import zipfile
from datetime import datetime
from pathlib import Path

PLATFORM_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PLATFORM_ROOT.parent
ROOTS = ("omicsprism", "omicsprism-platform")

# 按目录名整棵排除：构建产物、运行时数据、测试残留。
EXCLUDED_DIR_NAMES = {
    ".git", ".venv", "venv", "__pycache__", ".pytest_cache", ".pytest-tmp",
    ".pytest-clean", ".mypy_cache", ".ruff_cache", ".tmp", ".test-tmp",
    ".idea", ".claude", "node_modules", "dist", ".vite",
    "test-results", "playwright-report", "runs", "storage", "auth_data",
}


def _excluded_file(name: str, rel: str) -> bool:
    if name.endswith((".pyc", ".pyo", ".log", ".tsbuildinfo")):
        return True
    if name in {"vite.config.js", "vite.config.d.ts", "skills-lock.json", ".DS_Store", "Thumbs.db"}:
        return True
    if name in {".env"} or (name.startswith(".env.") and name != ".env.example"):
        return True
    # 判的是文件相对路径，永远不会以 "/" 结尾；必须按目录前缀匹配。
    if rel.replace(os.sep, "/").startswith(("omicsprism-platform/frontend/public/examples/",)):
        return True
    return False


def build(out: Path) -> int:
    count = 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for root in ROOTS:
            base = PROJECT_ROOT / root
            for dirpath, dirnames, filenames in os.walk(base):
                dirnames[:] = [
                    d for d in dirnames
                    if d not in EXCLUDED_DIR_NAMES and not d.startswith("dist-check-")
                ]
                for name in filenames:
                    full = os.path.join(dirpath, name)
                    rel = os.path.relpath(full, PROJECT_ROOT)
                    parts = rel.split(os.sep)
                    if any(part in EXCLUDED_DIR_NAMES or part.startswith("dist-check-") for part in parts):
                        continue
                    if _excluded_file(name, rel):
                        continue
                    z.write(full, rel)
                    count += 1
    return count


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    out = Path(args[0]) if args else (
        PROJECT_ROOT / f"omicsprism-source-deploy-{datetime.now():%Y%m%d}.zip"
    )
    count = build(out)
    print(f"wrote {out} ({count} files, {out.stat().st_size / 1024 / 1024:.1f} MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
