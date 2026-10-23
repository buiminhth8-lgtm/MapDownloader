"""检查 ``docs/`` 的结构一致性：没有杂物、没有断链、没有孤儿文档。

为什么需要它：文档混乱的四种典型形态都**不会自己报错** ——
截图躺在 ``docs/`` 根目录、图片引用指向已移动的文件、文档之间互不链接、
新写的文档没人知道存在。人工检查一次要几分钟且容易漏，脚本几毫秒。

用法::

    python scripts/check_docs.py          # 有问题时退出码 1
    python scripts/check_docs.py -v       # 顺带列出检查过的文件

检查项：

1. ``docs/`` 根目录只允许 ``*.md`` 与 ``images/``（截图必须进 images/）；
2. 所有图片引用都能解析到真实文件（跳过 ``*`` / ``{}`` 这类通配写法）；
3. 所有文档间链接都能解析（含相对路径与 ``../``）；
4. ``docs/`` 下每份文档都被索引或别的文档链接到（孤儿文档）；
5. 索引 ``docs/README.md`` 本身存在。

**不检查**内容是否正确 —— 那是评审的事。这里只保证"结构没散"。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

#: 仓库根
ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
IMAGES = DOCS / "images"
INDEX = DOCS / "README.md"

#: 图片引用：允许 ``docs/images/x.png`` / ``images/x.png`` / 裸 ``x.png``。
#: 名字里带 ``*`` / ``{}`` / ``,`` 的是说明性写法（"截图落在 slice16_*.png"、
#: ``slice9_package_{a,b}.png``），跳过 —— 它们不是真实的可点击引用。
IMAGE_RE = re.compile(r"([\w./{},-]*[\w}]\.png)")

#: 文档里明确"不要引用"的临时产物，以及 ``sliceNN_xxx.png`` 这类命名占位
SKIP_IMAGE_NAMES = {"ui_main_window.png"}
PLACEHOLDER_MARKERS = ("NN", "xxx", "场景", "<", ">")

#: markdown 链接：``](target.md)`` / ``](target.md#anchor)``
MD_LINK_RE = re.compile(r"\]\(([^)\s#]+\.md)(#[^)\s]*)?\)")


def _markdown_files() -> list[Path]:
    """全部参与检查的文档：根 README + docs/ 下所有 md。"""
    return [ROOT / "README.md", *sorted(DOCS.glob("*.md"))]


def check_stray_files() -> list[str]:
    """docs/ 根目录只允许 md 与 images/。"""
    problems = []
    for entry in DOCS.iterdir():
        if entry.name == "images":
            if not entry.is_dir():
                problems.append("docs/images 应该是目录，实际是文件")
            continue
        if entry.is_file() and entry.suffix != ".md":
            hint = ""
            if entry.suffix.lower() in {".png", ".jpg", ".jpeg", ".gif", ".svg"}:
                hint = "（截图请放到 docs/images/）"
            problems.append(f"docs/{entry.name} 是杂物{hint}")
    return problems


def _resolve(target: str, base: Path) -> Path | None:
    """把引用解析成绝对路径；认不出的（URL 等）返回 None。"""
    if target.startswith(("http://", "https://", "mailto:")):
        return None
    return (base / target).resolve()


def check_image_links(files: list[Path]) -> tuple[list[str], int]:
    """图片引用是否都能落地。返回（问题, 检查过的条数）。"""
    problems = []
    checked = 0
    for path in files:
        text = path.read_text(encoding="utf-8")
        for match in IMAGE_RE.finditer(text):
            raw = match.group(1)
            if "*" in raw or "{" in raw or "$" in raw or "," in raw:
                continue
            if Path(raw).name in SKIP_IMAGE_NAMES:
                continue
            if any(marker in raw for marker in PLACEHOLDER_MARKERS):
                continue
            checked += 1
            # 三种写法都可能出现：仓库根相对、docs 相对、docs/images 相对
            candidates = [
                (ROOT / raw).resolve(),
                (path.parent / raw).resolve(),
                (IMAGES / Path(raw).name).resolve(),
            ]
            if not any(c.is_file() for c in candidates):
                problems.append(f"{path.relative_to(ROOT)} 引用了不存在的图片：{raw}")
    return problems, checked


def check_md_links(files: list[Path]) -> tuple[list[str], set[Path]]:
    """文档间链接是否都能落地；顺带返回被链接到的文档集合。"""
    problems = []
    linked: set[Path] = set()
    for path in files:
        text = path.read_text(encoding="utf-8")
        for match in MD_LINK_RE.finditer(text):
            resolved = _resolve(match.group(1), path.parent)
            if resolved is None:
                continue
            if not resolved.is_file():
                problems.append(
                    f"{path.relative_to(ROOT)} 链接到不存在的文档：{match.group(1)}"
                )
            else:
                linked.add(resolved)
    return problems, linked


def check_orphans(files: list[Path], linked: set[Path]) -> list[str]:
    """docs/ 下每份文档都该被索引或别的文档提到。"""
    problems = []
    for path in files:
        if path.name == "README.md" and path.parent == DOCS:
            continue  # 索引自己不需要被索引
        if path.resolve() not in linked:
            problems.append(
                f"docs/{path.name} 是孤儿文档：没有从 docs/README.md 或任何其它文档链接过来"
            )
    return problems


def main(argv: list[str] | None = None) -> int:
    verbose = "-v" in (argv if argv is not None else sys.argv[1:])
    files = _markdown_files()

    problems: list[str] = []
    problems += check_stray_files()
    image_problems, image_count = check_image_links(files)
    problems += image_problems
    md_problems, linked = check_md_links(files)
    problems += md_problems
    problems += check_orphans(files, linked)

    if not INDEX.is_file():
        problems.append("docs/README.md（文档地图）不存在")

    if verbose:
        print(f"检查了 {len(files)} 份文档、{image_count} 处图片引用")
        for path in files:
            print(f"  {path.relative_to(ROOT)}")

    if problems:
        print(f"发现 {len(problems)} 个结构问题：")
        for item in problems:
            print(f"  - {item}")
        return 1

    print(f"文档结构 OK：{len(files)} 份文档、{image_count} 处图片引用，无杂物 / 无断链 / 无孤儿")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
