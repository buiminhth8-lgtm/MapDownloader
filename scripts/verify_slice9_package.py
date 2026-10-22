"""Slice 9 真实平台验收脚本：离线地图包（Package Builder + Validator）。

**为什么不能只跑 pytest**：``pytest`` 跑在 Qt ``offscreen`` 平台上 —— 那里没有
真实字体、没有 DPI 缩放、没有系统深浅色主题，``QDialog`` 也不是真的窗口。
而"打包"这个功能有三件事**只有实机才看得出来**：

1. 「地图信息 / 地图包内容 / 检查与验证结果」几个区在真实尺寸下有没有被裁掉；
2. 完成后那几行（目录、大小、瓦片、Zoom、SHA256、验证）是不是**真的显示出来**
   而不是停在占位符 ``—``（Slice 9 抓到的正是这类缺陷：结论先画好、紧接着
   被另一句"清空"擦掉，``offscreen`` 下断言的是同一个对象，看不出来）；
3. 生成 GB 级文件时界面会不会冻住。

本脚本是那次"实机看一眼"的可重复版本：

- 真的跑完整条链路（``.mbtiles`` → ``map.mbtiles`` + ``manifest.json`` → 校验）；
- 真的改**一个字节**，然后断言结论从 PASS 变成 FAIL（Spec §35 第 11 步）；
- 真的起 windows 平台插件、真的 ``show()`` 打包对话框、真的点按钮；
- 截图落在 ``docs/slice9_*.png``；
- 全程**只碰临时目录**，不碰你的真实地图与任务记录。

用法::

    python scripts/verify_slice9_package.py
    python scripts/verify_slice9_package.py --offscreen   # 无显示环境（CI）
    python scripts/verify_slice9_package.py --no-shots    # 只跑检查，不截图
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import io
import json
import os
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

DOCS = PROJECT_ROOT / "docs"

#: 连续两个及以上 ASCII 字母 → 判为"漏改的英文按钮"。
_ENGLISH_BUTTON = re.compile(r"[A-Za-z]{2,}")

#: 当前 ``QApplication``；``_shot`` 需要它来空转事件循环（截图前让布局收敛）。
_APP = None

BOUNDS = (116.0, 39.0, 116.5, 39.5)
#: ``BOUNDS`` 在 z12 下只覆盖 7×8 = 56 张瓦片：小到毫秒级，又真的能跑完整条链路。
ZOOM = 12


def _expected_tile_count() -> int:
    """``BOUNDS`` 在 ``ZOOM`` 下的瓦片数 —— **算出来，不写死**。

    这里第一次是写死的，而且写错了：注释写 7×6=42，实际 y 跨 1558~1565 共 8 行，
    是 7×8=56。写死的数字会跟着 ``BOUNDS`` / ``ZOOM`` 一起过期，而真正要守的命题是
    "manifest 里的数字 == 用生产同一套瓦片数学算出来的数字"。
    """
    from app.geo import GeoBounds, calculate_tile_range

    tile_range = calculate_tile_range(GeoBounds(*BOUNDS), ZOOM)
    return (tile_range.max_x - tile_range.min_x + 1) * (
        tile_range.max_y - tile_range.min_y + 1
    )


#: 期望瓦片数：``_build_mbtiles`` 按同一套数学写满，两边必然自洽。
EXPECTED_TILES = _expected_tile_count()


# --------------------------------------------------------------------------- #
# 检查结果收集
# --------------------------------------------------------------------------- #
class Checker:
    """收集检查结果，最后统一汇报并以退出码表达结论。"""

    def __init__(self) -> None:
        self.passed = 0
        self.failures: list[str] = []

    def check(self, ok: bool, label: str, detail: str = "") -> bool:
        if ok:
            self.passed += 1
            print(f"  [OK]   {label}")
        else:
            self.failures.append(f"{label}{f' — {detail}' if detail else ''}")
            print(f"  [FAIL] {label}{f' — {detail}' if detail else ''}")
        return bool(ok)

    def section(self, title: str) -> None:
        print(f"\n=== {title} ===")


def _pump(app, times: int = 8) -> None:
    for _ in range(times):
        app.processEvents()


def _wait_until(app, predicate, timeout_s: float = 60.0) -> bool:
    """轮询 + 空转事件循环。

    不能只 ``worker.wait()``：**线程结束**与**完成信号投递到主线程**之间有一个
    真实的窗口 —— 那时 ``isRunning()`` 已经是 ``False``，但界面上还是空的。
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.02)
    app.processEvents()
    return bool(predicate())


def _shot(widget, name: str, enabled: bool) -> None:
    """把任意控件存成 ``docs/<name>``。

    ``grab()`` **不会**触发布局重算：``resize()`` 之后直接截，截到的是上一轮
    尺寸下的排版。所以先 ``activate()`` 再空转几轮事件循环。
    """
    if not enabled:
        return
    layout = widget.layout()
    if layout is not None:
        layout.activate()
    if _APP is not None:
        _pump(_APP, 12)
    DOCS.mkdir(parents=True, exist_ok=True)
    target = DOCS / name
    if widget.grab().save(str(target)):
        print(f"  [shot] {target.relative_to(PROJECT_ROOT)}")


# --------------------------------------------------------------------------- #
# 造测试数据
# --------------------------------------------------------------------------- #
def _real_png() -> bytes:
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGBA", (32, 32), (30, 120, 200, 255)).save(buffer, format="PNG")
    return buffer.getvalue()


def _build_mbtiles(
    path: Path,
    *,
    name: str = "beijing",
    with_labels: bool = False,
    drop_first: int = 0,
) -> Path:
    """用**生产同一条 Writer** 造一个覆盖完整的 MBTiles。

    Args:
        drop_first: 故意漏掉某个 zoom 上的前 N 张（用来造"覆盖不完整"，
            让 Slice 8 的诊断给出 WARNING）。
    """
    from app.geo import GeoBounds, calculate_tile_range
    from app.mbtiles import MBTilesMetadata, MBTilesWriter
    from app.models import composition_for

    path.parent.mkdir(parents=True, exist_ok=True)
    bounds = GeoBounds(*BOUNDS)
    writer = MBTilesWriter(path)
    writer.open()
    writer.write_metadata(
        MBTilesMetadata.for_bounds(
            bounds,
            ZOOM,
            ZOOM,
            name=name,
            provider="tianditu",
            source_map_type="vector",
            with_labels=with_labels,
            composition=composition_for(with_labels),
        )
    )
    tile_range = calculate_tile_range(bounds, ZOOM)
    data = _real_png()
    dropped = 0
    for x in range(tile_range.min_x, tile_range.max_x + 1):
        for y in range(tile_range.min_y, tile_range.max_y + 1):
            if dropped < drop_first:
                dropped += 1
                continue
            writer.write_tile(ZOOM, x, y, data)
    writer.close()
    return path


def _read_manifest(package_dir: Path) -> dict:
    return json.loads((package_dir / "manifest.json").read_text(encoding="utf-8"))


def _flip_one_byte(path: Path, offset: int = 8192) -> None:
    """把文件里的一个字节取反（**大小不变**）。

    大小不变是重点：只有这样才能证明"大小检查通过、SHA256 仍然挡住了"。
    """
    payload = bytearray(path.read_bytes())
    payload[offset] ^= 0xFF
    path.write_bytes(bytes(payload))


def _whole_file_read_lines(source: str) -> list[str]:
    """源码里**真正调用** ``.read_bytes()`` 的行号。

    不能用 ``"read_bytes" in source`` 这种字符串匹配：``checksum.py`` 的文档
    字符串里恰好写着"别用 ``path.read_bytes()``"（Spec §32 的说明就在那段），
    一搜一个准，扫出来的是注释不是代码。所以这里走 AST，只看真实 Call 节点。
    """
    tree = ast.parse(source)
    lines = [
        str(node.lineno)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "read_bytes"
    ]
    return sorted(set(lines), key=int)


def _has_chunked_read(source: str, function_name: str) -> bool:
    """``function_name`` 里是否有**带参数**的 ``.read(size)`` —— 即流式分块读。"""
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == function_name:
            for inner in ast.walk(node):
                if (
                    isinstance(inner, ast.Call)
                    and isinstance(inner.func, ast.Attribute)
                    and inner.func.attr == "read"
                    and inner.args
                ):
                    return True
    return False


def _layout_problems(widget) -> list[str]:
    """顶层布局里"摆坏了"的地方 —— 用几何量，不靠看截图。

    起因很实在：``docs/slice9_package_dialog.png`` 缩图之后，第一个 group box 的
    标题看着像和别的东西叠在一起。逐行扫像素才发现 y=0~9 完全是空的 —— 是缩图的
    抗锯齿骗了眼睛。**人眼会被骗，``geometry()`` 不会**：

    - 兄弟控件矩形相交 → 一定有问题；
    - 控件 top 小于内容区上边距 / bottom 超出对话框 → 内容被挤出可视区（"被裁掉"）。
    """
    layout = widget.layout()
    if layout is None:
        return []
    entries: list[tuple[object, str]] = []
    for index in range(layout.count()):
        child = layout.itemAt(index).widget()
        if child is not None and child.isVisible():
            title = getattr(child, "title", None)
            entries.append((child.geometry(), title or type(child).__name__))

    problems: list[str] = []
    for a in range(len(entries)):
        for b in range(a + 1, len(entries)):
            if entries[a][0].intersects(entries[b][0]):
                problems.append(f"{entries[a][1]} × {entries[b][1]} 相交")
    top_margin = layout.contentsMargins().top()
    for geometry, name in entries:
        if geometry.top() < top_margin - 1:
            problems.append(f"{name} 越过内容区顶边（top={geometry.top()} < {top_margin}）")
        if geometry.bottom() > widget.height():
            problems.append(f"{name} 越过对话框底边（bottom={geometry.bottom()}）")
    return problems


# --------------------------------------------------------------------------- #
# 场景 1：服务层走完整条链路
# --------------------------------------------------------------------------- #
def scene_pipeline(checker: Checker, work_dir: Path) -> Path:
    """``.mbtiles`` → 地图包 → 校验 → 改一个字节 → FAIL。"""
    from app.services import MapPackageService

    checker.section("场景 A：构建 / 校验 / 篡改检测（服务层）")
    service = MapPackageService()
    source = _build_mbtiles(work_dir / "beijing.mbtiles", with_labels=True)
    source_sha = hashlib.sha256(source.read_bytes()).hexdigest()
    output = work_dir / "packages"

    result = service.build(source, output, package_name="北京离线地图")

    checker.check(result.success is True, "构建成功", str(result.errors))
    package = Path(result.package_dir or "")
    checker.check(package.is_dir(), f"地图包目录已生成：{package.name}")
    checker.check(
        sorted(item.name for item in package.iterdir())
        == ["manifest.json", "map.mbtiles"],
        "目录里恰好是 map.mbtiles + manifest.json",
        str([item.name for item in package.iterdir()]),
    )
    checker.check(
        not package.with_name(package.name + ".building").exists(),
        "没有留下 .building 临时目录",
    )
    checker.check(
        hashlib.sha256(source.read_bytes()).hexdigest() == source_sha,
        "源 .mbtiles 一个字节都没动（copy 不是 move）",
    )

    # --- manifest 的内容 ---
    data = _read_manifest(package)
    payload = (package / "map.mbtiles").read_bytes()
    anchor = {
        "formatVersion": 1,
        "labels": True,
        "composition": "merged",
        "mbtiles.file": "map.mbtiles",
    }
    checker.check(data["formatVersion"] == anchor["formatVersion"], "formatVersion = 1")
    checker.check(data["labels"] is anchor["labels"], "labels = true（中文注记）")
    checker.check(
        data["composition"] == anchor["composition"],
        "composition = merged（Spec §24：与 labels 自洽）",
        data["composition"],
    )
    checker.check(
        data["mbtiles"]["file"] == anchor["mbtiles.file"],
        "mbtiles.file = map.mbtiles（不是绝对路径）",
        data["mbtiles"]["file"],
    )
    checker.check(
        data["mbtiles"]["size"] == len(payload),
        "manifest 里的 size == 磁盘上的字节数",
        f"{data['mbtiles']['size']} vs {len(payload)}",
    )
    checker.check(
        data["mbtiles"]["sha256"] == hashlib.sha256(payload).hexdigest(),
        "manifest 里的 sha256 == 用 hashlib 重算的结果",
    )
    checker.check(
        data["tileCount"] == EXPECTED_TILES,
        f"tileCount == 瓦片数学算出来的张数（{EXPECTED_TILES}）",
        str(data["tileCount"]),
    )
    checker.check(
        (data["minZoom"], data["maxZoom"]) == (ZOOM, ZOOM),
        f"Zoom 区间正确（{ZOOM}）",
    )
    text = (package / "manifest.json").read_text(encoding="utf-8")
    checker.check("D:" not in text and "\\\\" not in text, "manifest 里没有 Windows 路径")
    checker.check("token" not in text.lower(), "manifest 里没有 Token")

    # --- 校验 ---
    validation = service.validate(package)
    checker.check(validation.valid is True, "自洽的包 → PASS", str(validation.errors))
    checker.check(
        validation.manifest_valid and validation.mbtiles_valid and validation.checksum_valid,
        "三行结论都是 PASS",
        f"{validation.manifest_valid}/{validation.mbtiles_valid}/{validation.checksum_valid}",
    )

    # --- 改一个字节（Spec §35 第 11 步）---
    mbtiles = package / "map.mbtiles"
    size_before = mbtiles.stat().st_size
    _flip_one_byte(mbtiles)
    broken = service.validate(package)
    checker.check(mbtiles.stat().st_size == size_before, "只改字节、没改大小（前提成立）")
    checker.check(broken.valid is False, "改一个字节后 → FAIL（不能仍是 PASS）")
    checker.check(
        broken.checksum_valid is False
        and any("SHA256" in message for message in broken.errors),
        "失败理由写的是 SHA256 mismatch",
        str(broken.errors),
    )

    # 恢复原样，供后面的界面场景使用
    _flip_one_byte(mbtiles)
    checker.check(service.validate(package).valid is True, "恢复原样后又是 PASS")

    return package


def scene_edge_cases(checker: Checker, work_dir: Path) -> None:
    """Spec §23 / §16：警告要问、FAIL 禁止打包、覆盖是改名备份。"""
    from app.package import MapPackageBuilder

    checker.section("场景 B：警告确认 / 禁止打包 / 覆盖备份")
    builder = MapPackageBuilder()

    # --- WARNING：不生成任何文件，等用户拍板 ---
    warned = _build_mbtiles(work_dir / "warned.mbtiles", name="warned", drop_first=3)
    first = builder.build(warned, work_dir / "edge")
    checker.check(
        first.needs_confirmation is True and first.confirmation_reason == "warning",
        "缺瓦片的源 → 先问用户（不写任何文件）",
        f"{first.needs_confirmation}/{first.confirmation_reason}",
    )
    checker.check(
        not (work_dir / "edge" / "warned").exists(),
        "等确认期间正式目录不存在",
    )
    second = builder.build(warned, work_dir / "edge", accept_warnings=True)
    checker.check(second.success is True, "用户确认后照常产出", str(second.errors))

    # --- FAIL：禁止打包 ---
    broken = work_dir / "broken.mbtiles"
    broken.write_bytes(b"definitely not a sqlite database" * 64)
    refused = builder.build(broken, work_dir / "edge")
    checker.check(
        refused.diagnostics_status == "FAIL" and refused.success is False,
        "检查 FAIL → 拒绝打包",
        refused.diagnostics_status,
    )

    # --- 已存在 / 覆盖（Spec §16）---
    # 先真的建一个出来。要守的命题是"**再建一次**时要先问用户"，不是"第一次建就要问"；
    # 目标目录还不存在时去断言 exists，测的是空气。
    source = _build_mbtiles(work_dir / "beijing.mbtiles", with_labels=True)
    seeded = builder.build(source, work_dir / "edge", package_name="beijing")
    checker.check(seeded.success is True, "先正常建一个包（给覆盖做铺垫）", str(seeded.errors))
    seeded_dir = Path(seeded.package_dir or "")
    checker.check(seeded_dir.is_dir(), f"铺垫用的包已生成：{seeded_dir.name}")
    seeded_manifest = (seeded_dir / "manifest.json").read_bytes()

    again = builder.build(source, work_dir / "edge", package_name="beijing")
    checker.check(
        again.needs_confirmation is True and again.confirmation_reason == "exists",
        "目标已存在 → 先问用户",
        f"{again.needs_confirmation}/{again.confirmation_reason}",
    )
    checker.check(
        (seeded_dir / "manifest.json").read_bytes() == seeded_manifest,
        "等确认期间旧包一个字节都没被动过",
    )

    overwritten = builder.build(
        source, work_dir / "edge", package_name="beijing", overwrite=True
    )
    checker.check(overwritten.success is True, "确认后覆盖成功", str(overwritten.errors))
    checker.check(
        overwritten.backup_path is not None and Path(overwritten.backup_path).is_dir(),
        "覆盖 = 旧包改名备份，不是删除",
        str(overwritten.backup_path),
    )
    backup_dir = Path(overwritten.backup_path or "")
    checker.check(
        backup_dir.is_dir()
        and (backup_dir / "manifest.json").read_bytes() == seeded_manifest,
        "备份里就是原来那一份（不是空壳）",
    )
    checker.check(
        Path(overwritten.package_dir or "") == seeded_dir,
        "新包还是同一个目录名（是覆盖，不是又开一个）",
        str(overwritten.package_dir),
    )


# --------------------------------------------------------------------------- #
# 场景 3：真窗口
# --------------------------------------------------------------------------- #
def scene_dialog(app, window, checker: Checker, work_dir: Path, package_dir: Path, shots: bool):
    """真的起窗口、真的点按钮、真的截图。"""
    from PySide6.QtCore import QThread

    checker.section("场景 C：打包对话框（真窗口 + 真按钮）")

    source = _build_mbtiles(work_dir / "dialog.mbtiles", name="dialog", with_labels=True)
    window.open_package_dialog(str(source))
    dialog = window._package_dialog
    checker.check(dialog is not None, "「文件 → 生成地图包」打开了对话框")
    if dialog is None:
        return

    # 先抓线程引用再空转事件循环：56 张瓦片的检查是毫秒级的，等 ``_pump`` 转完，
    # 线程很可能已经跑完并把 ``inspection_worker`` 自己置回 ``None`` 了 —— 那时
    # 再去读属性，看到的是"没有线程"，而不是"没有检查"。
    inspection = dialog.inspection_worker
    checker.check(
        inspection is not None or dialog.diagnostics is not None,
        "选中源文件后自动开始后台检查",
        "既没有在跑的检查线程、也没有已经回到主线程的检查结论",
    )

    dialog.resize(900, 720)
    dialog.show()
    _pump(app)

    problems = _layout_problems(dialog)
    checker.check(
        not problems,
        "真实尺寸下各分区没有互相压住 / 被裁掉（几何量，不看图）",
        "；".join(problems),
    )

    if inspection is not None:
        inspection.wait(60_000)
    settled = _wait_until(app, lambda: dialog.diagnostics is not None, 20)
    checker.check(settled, "检查结论已回到主线程")
    checker.check(
        dialog.info_values["tiles"].text() == str(EXPECTED_TILES),
        f"「瓦片」来自真实检查结果（{EXPECTED_TILES}）",
        dialog.info_values["tiles"].text(),
    )
    checker.check(
        dialog.info_values["zoom"].text() == f"{ZOOM}~{ZOOM}",
        "「Zoom」来自真实检查结果",
        dialog.info_values["zoom"].text(),
    )
    checker.check(
        "注记" in dialog.info_values["map_type"].text(),
        "「地图类型」反映出中文注记",
        dialog.info_values["map_type"].text(),
    )
    checker.check(
        dialog.buttons["build"].isEnabled() is True,
        "检查 PASS → [生成地图包] 可点",
    )
    _shot(dialog, "slice9_package_dialog.png", shots)

    # --- 真的点生成 ---
    dialog.set_output_dir(str(work_dir / "gui-packages"))
    dialog.name_edit.setText("北京离线地图")
    dialog.buttons["build"].click()
    worker = dialog.build_worker
    checker.check(worker is not None, "构建跑在后台线程里")
    if worker is not None:
        checker.check(
            worker is not QThread.currentThread(),
            "构建线程不是 Qt 主线程（界面不会冻结）",
        )
        worker.wait(120_000)
    built = _wait_until(app, lambda: dialog.result is not None, 60)
    checker.check(built, "构建结论已回到主线程")

    result = dialog.result
    if result is None:
        checker.check(False, "构建应当有结果")
        return
    checker.check(result.success is True, "从界面构建成功", str(result.errors))
    checker.check(
        dialog.package_values["directory"].text() == result.package_dir,
        "完成视图显示了包目录",
        dialog.package_values["directory"].text(),
    )
    checker.check(
        dialog.package_values["sha256"].text() == result.sha256,
        "完成视图显示了完整 SHA256",
    )
    checker.check(
        dialog.package_values["validation"].text() == "PASS",
        "完成视图的验证结论是 PASS",
        dialog.package_values["validation"].text(),
    )
    checker.check(dialog.progress_bar.value() == 100, "进度条拉满到 100%")
    _shot(dialog, "slice9_package_complete.png", shots)

    # --- [验证地图包] ---
    dialog.start_validation(result.package_dir)
    validate_worker = dialog.validate_worker
    checker.check(validate_worker is not None, "校验也跑在后台线程里")
    if validate_worker is not None:
        validate_worker.wait(120_000)
    _wait_until(
        app, lambda: "Result：" in dialog.diagnostics_text.toPlainText(), 30
    )
    conclusion = dialog.diagnostics_text.toPlainText()
    for line in ("Manifest：PASS", "MBTiles：PASS", "SHA256：PASS", "Result：PASS"):
        checker.check(line in conclusion, f"结论文本区里有「{line}」")
    _shot(dialog, "slice9_package_validate_pass.png", shots)

    # --- 改一个字节后重验 ---
    mbtiles = Path(result.package_dir) / "map.mbtiles"
    _flip_one_byte(mbtiles)
    dialog.start_validation(result.package_dir)
    if dialog.validate_worker is not None:
        dialog.validate_worker.wait(120_000)
    _wait_until(app, lambda: "Result：FAIL" in dialog.diagnostics_text.toPlainText(), 30)
    conclusion = dialog.diagnostics_text.toPlainText()
    checker.check("Result：FAIL" in conclusion, "改过字节后重验 → FAIL")
    checker.check("SHA256：FAIL" in conclusion, "失败理由是 SHA256")
    checker.check(
        dialog.package_values["validation"].text() == "FAIL",
        "「结果」那一行也变成 FAIL",
    )
    _shot(dialog, "slice9_package_verify_fail.png", shots)
    _flip_one_byte(mbtiles)  # 复原

    # --- 弹窗按钮要是中文（标准按钮的默认文案是英文，Slice 7 踩过）---
    labels = [button.text() for button in dialog.buttons.values()]
    english = [text for text in labels if _ENGLISH_BUTTON.search(text)]
    checker.check(not english, "对话框按钮没有残留英文", str(english))

    # --- 主界面那个独立的「验证地图包」入口（Spec §21）---
    window.open_package_validator(str(package_dir))
    validator = window._package_validator_dialog
    checker.check(validator is not None, "「文件 → 验证地图包」打开了独立窗口")
    if validator is not None:
        validator.show()
        _pump(app)
        validator_problems = _layout_problems(validator)
        checker.check(
            not validator_problems,
            "独立验证窗口的布局也没摆坏",
            "；".join(validator_problems),
        )
        validator.validate_button.click()
        if validator.validation_worker is not None:
            validator.validation_worker.wait(120_000)
        done = _wait_until(
            app,
            lambda: validator.result is not None and not validator.is_running,
            60,
        )
        checker.check(done, "独立验证窗口跑完了")
        checker.check(
            validator.result is not None and validator.result.valid is True,
            "独立验证窗口对一个好包给出 PASS",
        )
        _shot(validator, "slice9_validate_dialog.png", shots)
        validator.close()
        _pump(app)

    # --- 关窗要收线程 ---
    checker.check(dialog.close() is True, "打包对话框关得掉")
    _pump(app)
    checker.check(
        dialog.build_worker is None
        and dialog.inspection_worker is None
        and dialog.validate_worker is None,
        "关窗后没有遗留后台线程",
    )


# --------------------------------------------------------------------------- #
# 场景 D：静态守卫
# --------------------------------------------------------------------------- #
def scene_static(checker: Checker) -> None:
    """几条"靠自觉守不住"的约定，扫源码来守。"""
    from PySide6.QtCore import QThread

    from app.ui.package_dialog import PackageBuildWorker, PackageValidateWorker

    checker.section("场景 D：静态守卫")
    dialog_source = (PROJECT_ROOT / "app/ui/package_dialog.py").read_text(encoding="utf-8")
    main_source = (PROJECT_ROOT / "app/ui/main_window.py").read_text(encoding="utf-8")
    validator_source = (PROJECT_ROOT / "app/package/package_validator.py").read_text(
        encoding="utf-8"
    )
    checksum_source = (PROJECT_ROOT / "app/package/checksum.py").read_text(encoding="utf-8")

    checker.check("sqlite3" not in dialog_source, "打包对话框里没有 SQLite 调用")
    checker.check("SELECT " not in dialog_source, "打包对话框里没有 SQL 查询")
    checker.check(
        issubclass(PackageBuildWorker, QThread)
        and issubclass(PackageValidateWorker, QThread),
        "构建 / 校验都是 QThread（Spec §19）",
    )
    checker.check(
        "PackageDialog" in main_source and "PackageValidateDialog" in main_source,
        "主窗口接线了两个入口（Spec §17 / §21）",
    )
    whole_file_reads = _whole_file_read_lines(checksum_source)
    checker.check(
        not whole_file_reads,
        "checksum.py 里没有整文件 read_bytes 调用（Spec §32：500MB/2GB/5GB 不许进内存）",
        f"第 {', '.join(whole_file_reads)} 行",
    )
    checker.check(
        _has_chunked_read(checksum_source, "calculate_sha256"),
        "calculate_sha256 走的是分块读（handle.read(size)）",
    )
    checker.check(
        _has_chunked_read(checksum_source, "copy_file"),
        "copy_file 走的是分块读（reader.read(size)）",
    )
    for keyword in ("INSERT ", "UPDATE ", "DELETE ", "DROP "):
        checker.check(
            keyword not in validator_source,
            f"包校验器里没有 {keyword.strip()}（只读保证）",
        )


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Slice 9 离线地图包实机验收")
    parser.add_argument(
        "--offscreen",
        action="store_true",
        help="使用 Qt offscreen 平台插件（适合无显示环境；中文字体可能缺失）",
    )
    parser.add_argument("--no-shots", action="store_true", help="只跑检查，不截图")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    shots = not args.no_shots

    if args.offscreen:
        os.environ["QT_QPA_PLATFORM"] = "offscreen"

    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QApplication

    from app.main import setup_logging
    from app.tasks import TaskService, TaskStore
    from app.ui.main_window import MainWindow
    from app.ui.preferences import UiPreferences

    setup_logging()

    work_dir = Path(tempfile.mkdtemp(prefix="verify-slice9-"))
    app = QApplication(sys.argv[:1])
    global _APP
    _APP = app
    checker = Checker()

    print(f"platform={app.platformName()}  work_dir={work_dir}")

    try:
        package_dir = scene_pipeline(checker, work_dir)
        scene_edge_cases(checker, work_dir)

        # 主窗口用临时任务记录 + 临时偏好：绝不碰真实数据与注册表
        store = TaskStore(work_dir / "tasks.json")
        preferences = UiPreferences(
            QSettings(str(work_dir / "ui.ini"), QSettings.Format.IniFormat)
        )
        window = MainWindow(task_service=TaskService(store), preferences=preferences)
        window.show()
        _pump(app)

        scene_dialog(app, window, checker, work_dir, package_dir, shots)
        scene_static(checker)

        window._exit_confirmed = True
        window.close()
        _pump(app)
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)

    print("\n" + "=" * 60)
    if checker.failures:
        print(f"结果：{len(checker.failures)} 项失败 / 共 {checker.passed} 项通过")
        for failure in checker.failures:
            print(f"  - {failure}")
        return 1
    print(f"结果：全部通过（{checker.passed} 项）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
