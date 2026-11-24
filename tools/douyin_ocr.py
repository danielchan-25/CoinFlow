#!/usr/bin/env python3
"""OCR 助手（跨平台，保持原有 CLI 与 JSON 结构）。

命令：
    douyin_ocr.py ocr <png>
    douyin_ocr.py crop <png> <x> <y> <w> <h>
    douyin_ocr.py probe <png> <size> <x,y,w,h> ...
    douyin_ocr.py tsv <png>          # x\ty\tw\th\ttext（像素坐标，按 y 排序）

ocr/crop 返回 {"width","height","lines":[{"text","x","y","w","h"}]}，坐标归一化到整张截图。
probe 返回 {"patches":[<size*size 灰度值>|null,...]}。

后端：
    macOS    -> Vision 框架（经 osascript/JXA，与原实现行为一致）
    Windows  -> Windows.Media.Ocr（winsdk，使用系统已安装的中文 OCR 引擎）
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

JXA = r"""
ObjC.import('Cocoa');
ObjC.import('Vision');

function fail(message) { throw new Error(message); }

function loadRep(path) {
  const rep = $.NSBitmapImageRep.imageRepWithContentsOfFile(path);
  if (!rep) fail('无法读取手机截图: ' + path);
  return rep;
}

function recognize(cg, offsetX, offsetY, scaleX, scaleY, keepSpaces) {
  const request = $.VNRecognizeTextRequest.alloc.init;
  request.recognitionLevel = 0;
  request.usesLanguageCorrection = false;
  request.recognitionLanguages = $(['zh-Hans', 'en-US']);
  const handler = $.VNImageRequestHandler.alloc.initWithCGImageOptions(cg, $());
  handler.performRequestsError($([request]), $());
  const lines = [];
  const total = request.results.count;
  for (let i = 0; i < total; i++) {
    const observation = request.results.objectAtIndex(i);
    const candidate = observation.topCandidates(1).objectAtIndex(0);
    if (!candidate) continue;
    let text = ObjC.unwrap(candidate.string);
    text = keepSpaces ? text.replace(/^\s+|\s+$/g, '') : text.replace(/\s+/g, '');
    if (!text) continue;
    const box = observation.boundingBox;
    lines.push({
      text: text,
      x: offsetX + Number(box.origin.x) * scaleX,
      y: offsetY + (1 - Number(box.origin.y) - Number(box.size.height)) * scaleY,
      w: Number(box.size.width) * scaleX,
      h: Number(box.size.height) * scaleY
    });
  }
  return lines;
}

function makeRep(size) {
  return $.NSBitmapImageRep.alloc.initWithBitmapDataPlanesPixelsWidePixelsHighBitsPerSampleSamplesPerPixelHasAlphaIsPlanarColorSpaceNameBytesPerRowBitsPerPixel(
    null, size, size, 8, 4, true, false, $.NSDeviceRGBColorSpace, 0, 0);
}

function graySamples(rep, width, height, spec, size) {
  const parts = spec.split(',').map(Number);
  if (parts.length !== 4 || parts.some(isNaN)) return null;
  const px = parts[0] * width, py = parts[1] * height;
  const pw = parts[2] * width, ph = parts[3] * height;
  if (px < 0 || py < 0 || pw < 1 || ph < 1) return null;
  if (px + pw > width + 0.5 || py + ph > height + 0.5) return null;
  const rx = Math.floor(px), ry = Math.floor(py);
  const rw = Math.ceil(px + pw) - rx, rh = Math.ceil(py + ph) - ry;
  if (rw < 1 || rh < 1 || rx + rw > width || ry + rh > height) return null;

  const small = makeRep(size);
  const image = $.NSImage.alloc.initWithSize($.NSMakeSize(width, height));
  image.addRepresentation(rep);
  const context = $.NSGraphicsContext.graphicsContextWithBitmapImageRep(small);
  $.NSGraphicsContext.setCurrentContext(context);
  const source = $.NSMakeRect(rx, height - ry - rh, rw, rh);
  image.drawInRectFromRectOperationFraction(
    $.NSMakeRect(0, 0, size, size), source, $.NSCompositingOperationCopy, 1.0);

  const values = [];
  for (let gy = 0; gy < size; gy++) {
    for (let gx = 0; gx < size; gx++) {
      const color = small.colorAtXY(gx, gy);
      values.push(Math.round(255 * (0.299 * color.redComponent + 0.587 * color.greenComponent + 0.114 * color.blueComponent)));
    }
  }
  return values;
}

function run(argv) {
  if (argv.length < 2) fail('用法: douyin_ocr.py ocr|crop|probe|tsv <png> ...');
  const command = argv[0];
  const path = argv[1];
  const rep = loadRep(path);
  const width = Number(rep.pixelsWide);
  const height = Number(rep.pixelsHigh);

  if (command === 'ocr') {
    const lines = recognize(rep.CGImage, 0, 0, 1, 1, false);
    return JSON.stringify({width: width, height: height, lines: lines});
  }

  if (command === 'crop') {
    if (argv.length !== 6) fail('用法: douyin_ocr.py crop <png> <x> <y> <w> <h>');
    const x = Number(argv[2]), y = Number(argv[3]), w = Number(argv[4]), h = Number(argv[5]);
    if ([x, y, w, h].some(isNaN)) fail('裁切参数必须是数字');
    const rx = Math.floor(x), ry = Math.floor(y);
    const rw = Math.ceil(x + w) - rx, rh = Math.ceil(y + h) - ry;
    if (rx < 0 || ry < 0 || rw < 1 || rh < 1 || rx + rw > width || ry + rh > height) {
      fail('裁切区域超出截图范围');
    }
    const crop = $.CGImageCreateWithImageInRect(rep.CGImage, $.CGRectMake(rx, ry, rw, rh));
    if (!crop) fail('裁切失败');
    const lines = recognize(crop, rx / width, ry / height, rw / width, rh / height, false);
    return JSON.stringify({width: width, height: height, lines: lines});
  }

  if (command === 'probe') {
    if (argv.length < 4) fail('用法: douyin_ocr.py probe <png> <size> <x,y,w,h> ...');
    const size = Number(argv[2]);
    if (!(size > 0)) fail('probe size 必须大于 0');
    const patches = [];
    for (let i = 3; i < argv.length; i++) {
      patches.push(graySamples(rep, width, height, argv[i], size));
    }
    return JSON.stringify({patches: patches});
  }

  if (command === 'tsv') {
    const lines = recognize(rep.CGImage, 0, 0, 1, 1, true);
    lines.sort((a, b) => a.y - b.y);
    return lines.map(function (line) {
      return Math.round(line.x * width) + '\t' + Math.round(line.y * height) + '\t' +
        Math.round(line.w * width) + '\t' + Math.round(line.h * height) + '\t' + line.text;
    }).join('\n');
  }

  fail('未知命令: ' + command);
}
"""


# ---- macOS 后端 -----------------------------------------------------------

def helper_path() -> Path:
    return Path.home() / ".droidfleet" / "bin" / "douyin_ocr.js"


def ensure_script() -> Path:
    path = helper_path()
    digest = hashlib.sha256(JXA.encode("utf-8")).hexdigest()
    marker = path.with_suffix(".sha256")
    current = marker.read_text(encoding="utf-8").strip() if marker.exists() else ""
    if not path.exists() or current != digest:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(JXA, encoding="utf-8")
        marker.write_text(digest, encoding="utf-8")
    return path


def run_macos(argv: list[str]) -> int:
    script = ensure_script()
    proc = subprocess.run(["osascript", "-l", "JavaScript", str(script), *argv],
                          capture_output=True, text=True)
    if proc.returncode != 0:
        detail = proc.stderr.strip() or proc.stdout.strip() or "OCR 执行失败"
        sys.stderr.write(detail + "\n")
        return 1
    sys.stdout.write(proc.stdout)
    return 0


# ---- Windows 后端（Windows.Media.Ocr） ------------------------------------

_engine = None

_CJK_SPACE = re.compile("(?<=[\u3000-\u303f\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uff00-\uffef])\\s+|\\s+(?=[\u3000-\u303f\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uff00-\uffef])")


def _windows_engine():
    """按 zh-Hans -> 用户配置 -> 任意可用顺序挑选 OCR 引擎。"""
    global _engine
    if _engine is not None:
        return _engine

    from winsdk.windows.globalization import Language
    from winsdk.windows.media.ocr import OcrEngine

    for tag in ("zh-Hans-CN", "zh-Hans", "zh-CN"):
        try:
            engine = OcrEngine.try_create_from_language(Language(tag))
        except OSError:
            engine = None
        if engine is not None:
            _engine = engine
            return engine

    engine = OcrEngine.try_create_from_user_profile_languages()
    if engine is None:
        available = list(OcrEngine.available_recognizer_languages)
        if available:
            engine = OcrEngine.try_create_from_language(available[0])
    if engine is None:
        raise RuntimeError("系统未安装可用的 OCR 语言包（设置 → 时间和语言 → 语言，添加中文语言包）")
    _engine = engine
    return engine


def _to_software_bitmap(image):
    from winsdk.windows.graphics.imaging import BitmapAlphaMode, BitmapPixelFormat, SoftwareBitmap
    from winsdk.windows.storage.streams import DataWriter

    if image.mode != "RGBA":
        image = image.convert("RGBA")
    width, height = image.size
    writer = DataWriter()
    writer.write_bytes(image.tobytes("raw", "BGRA"))
    buffer = writer.detach_buffer()
    return SoftwareBitmap.create_copy_from_buffer(
        buffer, BitmapPixelFormat.BGRA8, width, height, BitmapAlphaMode.PREMULTIPLIED)


def _wait(operation, timeout: float = 20.0):
    """阻塞等待 IAsyncOperation（winsdk 的异步对象不接受 await）。"""
    import time

    from winsdk.windows.foundation import AsyncStatus

    started = time.monotonic()
    while operation.status == AsyncStatus.STARTED:
        if time.monotonic() - started > timeout:
            raise TimeoutError("Windows OCR 超时")
        time.sleep(0.005)
    if operation.status != AsyncStatus.COMPLETED:
        raise RuntimeError(f"Windows OCR 失败：{operation.status} {operation.error_code}")
    return operation.get_results()


def _windows_lines(path: Path, rect: tuple[int, int, int, int] | None, keep_spaces: bool):
    from PIL import Image

    with Image.open(path) as source:
        image = source.convert("RGB")
    full_w, full_h = image.size
    if rect is None:
        rx = ry = 0
        work = image
    else:
        rx, ry, rw, rh = rect
        work = image.crop((rx, ry, rx + rw, ry + rh))

    engine = _windows_engine()
    bitmap = _to_software_bitmap(work)
    result = _wait(engine.recognize_async(bitmap))

    lines = []
    for line in result.lines:
        words = list(line.words)
        if not words:
            continue
        left = min(word.bounding_rect.x for word in words)
        top = min(word.bounding_rect.y for word in words)
        right = max(word.bounding_rect.x + word.bounding_rect.width for word in words)
        bottom = max(word.bounding_rect.y + word.bounding_rect.height for word in words)
        text = line.text
        if keep_spaces:
            # Windows OCR 会在中文之间插空格，去掉紧邻汉字的空白，保持与 Vision 一致
            text = _CJK_SPACE.sub("", text.strip())
        else:
            text = "".join(text.split())
        if not text:
            continue
        lines.append({
            "text": text,
            "x": (rx + left) / full_w,
            "y": (ry + top) / full_h,
            "w": (right - left) / full_w,
            "h": (bottom - top) / full_h,
        })
    return full_w, full_h, lines


def _windows_gray_patch(image, width, height, spec: str, size: int):
    from PIL import Image

    parts = spec.split(",")
    try:
        values = [float(part) for part in parts]
    except ValueError:
        return None
    if len(values) != 4:
        return None
    px, py, pw, ph = (values[0] * width, values[1] * height, values[2] * width, values[3] * height)
    if px < 0 or py < 0 or pw < 1 or ph < 1:
        return None
    if px + pw > width + 0.5 or py + ph > height + 0.5:
        return None
    rx, ry = math.floor(px), math.floor(py)
    rw, rh = math.ceil(px + pw) - rx, math.ceil(py + ph) - ry
    if rw < 1 or rh < 1 or rx + rw > width or ry + rh > height:
        return None
    patch = image.crop((rx, ry, rx + rw, ry + rh)).resize((size, size), Image.BILINEAR)
    return list(patch.convert("L").tobytes())


def _windows_number_lines(path: Path, rect: tuple[int, int, int, int] | None, scale: int = 2):
    """用英文引擎专读数字：中文引擎在彩色大号数字上经常整段漏读。"""
    from PIL import Image
    from winsdk.windows.globalization import Language
    from winsdk.windows.media.ocr import OcrEngine

    with Image.open(path) as source:
        image = source.convert("RGB")
    full_w, full_h = image.size
    if rect is None:
        rx = ry = 0
        work = image
    else:
        rx, ry, rw, rh = rect
        work = image.crop((rx, ry, rx + rw, ry + rh))
    if scale > 1:
        work = work.resize((work.width * scale, work.height * scale), Image.LANCZOS)

    engine = None
    for tag in ("en-US", "en-GB"):
        try:
            engine = OcrEngine.try_create_from_language(Language(tag))
        except OSError:
            engine = None
        if engine is not None:
            break
    if engine is None:
        engine = _windows_engine()

    bitmap = _to_software_bitmap(work)
    result = _wait(engine.recognize_async(bitmap))
    lines = []
    for line in result.lines:
        text = "".join(line.text.split())
        if not text:
            continue
        words = list(line.words)
        if words:
            left = min(word.bounding_rect.x for word in words)
            top = min(word.bounding_rect.y for word in words)
            right = max(word.bounding_rect.x + word.bounding_rect.width for word in words)
            bottom = max(word.bounding_rect.y + word.bounding_rect.height for word in words)
        else:
            left = top = right = bottom = 0
        lines.append({
            "text": text,
            "x": (rx + left / scale) / full_w,
            "y": (ry + top / scale) / full_h,
            "w": (right - left) / scale / full_w,
            "h": (bottom - top) / scale / full_h,
        })
    return full_w, full_h, lines


def run_windows(argv: list[str]) -> int:
    from PIL import Image

    if len(argv) < 2:
        sys.stderr.write("用法: douyin_ocr.py ocr|crop|probe|tsv <png> ...\n")
        return 1
    command, raw_path = argv[0], argv[1]
    path = Path(raw_path)
    if not path.exists():
        sys.stderr.write(f"无法读取手机截图: {path}\n")
        return 1

    if command == "ocr":
        width, height, lines = _windows_lines(path, None, False)
        sys.stdout.write(json.dumps({"width": width, "height": height, "lines": lines}))
        return 0

    if command == "crop":
        if len(argv) != 6:
            sys.stderr.write("用法: douyin_ocr.py crop <png> <x> <y> <w> <h>\n")
            return 1
        try:
            x, y, w, h = (float(value) for value in argv[2:6])
        except ValueError:
            sys.stderr.write("裁切参数必须是数字\n")
            return 1
        with Image.open(path) as source:
            full_w, full_h = source.size
        rx, ry = math.floor(x), math.floor(y)
        rw, rh = math.ceil(x + w) - rx, math.ceil(y + h) - ry
        if rx < 0 or ry < 0 or rw < 1 or rh < 1 or rx + rw > full_w or ry + rh > full_h:
            sys.stderr.write("裁切区域超出截图范围\n")
            return 1
        width, height, lines = _windows_lines(path, (rx, ry, rw, rh), False)
        sys.stdout.write(json.dumps({"width": width, "height": height, "lines": lines}))
        return 0

    if command == "num":
        if len(argv) != 6:
            sys.stderr.write("用法: douyin_ocr.py num <png> <x> <y> <w> <h>\n")
            return 1
        try:
            x, y, w, h = (float(value) for value in argv[2:6])
        except ValueError:
            sys.stderr.write("裁切参数必须是数字\n")
            return 1
        with Image.open(path) as source:
            full_w, full_h = source.size
        rx, ry = math.floor(x), math.floor(y)
        rw, rh = math.ceil(x + w) - rx, math.ceil(y + h) - ry
        if rx < 0 or ry < 0 or rw < 1 or rh < 1 or rx + rw > full_w or ry + rh > full_h:
            sys.stderr.write("裁切区域超出截图范围\n")
            return 1
        width, height, lines = _windows_number_lines(path, (rx, ry, rw, rh))
        sys.stdout.write(json.dumps({"width": width, "height": height, "lines": lines}))
        return 0

    if command == "probe":
        if len(argv) < 4:
            sys.stderr.write("用法: douyin_ocr.py probe <png> <size> <x,y,w,h> ...\n")
            return 1
        try:
            size = int(float(argv[2]))
        except ValueError:
            sys.stderr.write("probe size 必须大于 0\n")
            return 1
        if size <= 0:
            sys.stderr.write("probe size 必须大于 0\n")
            return 1
        with Image.open(path) as source:
            image = source.convert("RGB")
        width, height = image.size
        patches = [_windows_gray_patch(image, width, height, spec, size) for spec in argv[3:]]
        sys.stdout.write(json.dumps({"patches": patches}))
        return 0

    if command == "tsv":
        width, height, lines = _windows_lines(path, None, True)
        lines.sort(key=lambda line: line["y"])
        rows = [
            "%d\t%d\t%d\t%d\t%s" % (
                round(line["x"] * width), round(line["y"] * height),
                round(line["w"] * width), round(line["h"] * height), line["text"])
            for line in lines
        ]
        sys.stdout.write("\n".join(rows))
        return 0

    sys.stderr.write("未知命令: " + command + "\n")
    return 1


# ---- 入口 -----------------------------------------------------------------

def run(argv: list[str]) -> int:
    if sys.platform == "win32":
        return run_windows(argv)
    if argv and argv[0] == "num":
        # macOS 的 Vision 引擎直接读裁切区域即可
        argv = ["crop", *argv[1:]]
    return run_macos(argv)


def main(argv: list[str] | None = None) -> int:
    if sys.platform == "win32":
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (AttributeError, OSError):
                pass
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in {"-h", "--help"}:
        sys.stdout.write(__doc__ or "")
        return 0
    try:
        return run(args)
    except FileNotFoundError:
        sys.stderr.write("缺少 osascript，OCR 只能在 macOS 上运行\n")
        return 1
    except OSError as error:
        sys.stderr.write(f"OCR 助手启动失败：{error}\n")
        return 1


if __name__ == "__main__":
    sys.exit(main())
