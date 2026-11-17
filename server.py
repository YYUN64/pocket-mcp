#!/usr/bin/env python3
"""deepseek-lite-mcp: a tiny, dependency-free MCP server.

Transport: streamable HTTP with JSON responses (no SSE, no sessions).
Endpoint:  POST /mcp
Protocol:  JSON-RPC 2.0 / MCP 2025-06-18

Only the Python standard library is used.  The server is intentionally
stateless: every HTTP request is handled independently.

Run:
    python3 server.py --host 0.0.0.0 --port 8765
    python3 server.py 9000
"""

from __future__ import annotations

import argparse
import ast
import datetime
import errno
import html
import json
import locale
import math
import os
import re
import subprocess
import sys
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple


SERVER_NAME = "deepseek-lite-mcp"
SERVER_VERSION = "1.0.0"
DEFAULT_PROTOCOL_VERSION = "2025-06-18"
DEFAULT_PORT = 8765
DEFAULT_HOST = "0.0.0.0"

MAX_REQUEST_BYTES = 2 * 1024 * 1024       # 2 MiB JSON request body
MAX_OUTPUT_CHARS = 200_000                # hard cap for a single tool result
MAX_OUTPUT_LINES = 4_000                  # hard cap for a single tool result
MAX_READ_BYTES = 5_000_000                # max_bytes upper bound for file reads
MAX_FETCH_BYTES = 5_000_000               # max_bytes upper bound for http_fetch
MAX_LIST_ENTRIES = 2_000
MAX_SEARCH_FILES = 10_000
MAX_SEARCH_FILE_BYTES = 2_000_000
MAX_SEARCH_TOTAL_BYTES = 50_000_000
MAX_SEARCH_LINE_CHARS = 500
MAX_CALC_EXPR_CHARS = 4_096
MAX_CALC_POW_EXPONENT = 10_000
HTTP_TIMEOUT_SECONDS = 20.0
USER_AGENT = "deepseek-lite-mcp/1.0.0"

# Configured in main(); all request handlers read these module-level values.
WORKSPACE = Path.cwd().resolve()
ALLOW_SHELL = False


def log(message: str) -> None:
    """Write one diagnostic line to stderr.  stdout stays unused."""
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    try:
        print("[%s] %s" % (stamp, message), file=sys.stderr, flush=True)
    except Exception:
        pass


def one_line(value: Any, limit: int = 160) -> str:
    """Make untrusted text safe for a single stderr line."""
    try:
        text = str(value)
    except Exception:
        text = "<unprintable>"
    text = text.replace("\r", " ").replace("\n", " ")
    if len(text) > limit:
        text = text[:limit] + "..."
    return text


class ToolError(Exception):
    """A tool-level failure.  It becomes an MCP isError result, not RPC error."""


class RequestProblem(Exception):
    """An HTTP/body problem; optionally reported as a JSON-RPC parse error."""

    def __init__(self, status: int, message: str, parse_error: bool = False) -> None:
        Exception.__init__(self, message)
        self.status = status
        self.message = message
        self.parse_error = parse_error


def _is_within_workspace(path: Path) -> bool:
    """Case-aware containment check that also works on Windows."""
    root = os.path.normcase(os.path.abspath(str(WORKSPACE)))
    target = os.path.normcase(os.path.abspath(str(path)))
    if target == root:
        return True
    prefix = root if root.endswith(os.sep) else root + os.sep
    return target.startswith(prefix)


def resolve_workspace_path(raw: Any, label: str = "path") -> Path:
    """Resolve a user path and reject anything outside MCP_WORKSPACE."""
    if not isinstance(raw, str):
        raise ToolError("参数 %s 必须是字符串" % label)
    if not raw:
        raise ToolError("参数 %s 不能为空" % label)
    if "\x00" in raw:
        raise ToolError("参数 %s 包含非法字符 NUL" % label)
    try:
        candidate = Path(raw).expanduser()
        if not candidate.is_absolute():
            candidate = WORKSPACE / candidate
        resolved = candidate.resolve(strict=False)
    except (OSError, RuntimeError, ValueError) as exc:
        raise ToolError("无法解析路径 %s：%s" % (raw, exc))
    if not _is_within_workspace(resolved):
        raise ToolError(
            "路径越界，已拒绝：%s 不在工作区 %s 内" % (raw, WORKSPACE)
        )
    return resolved


def display_path(path: Path) -> str:
    """Return a workspace-relative POSIX-ish path for tool output."""
    try:
        return path.relative_to(WORKSPACE).as_posix()
    except (ValueError, OSError):
        return str(path)


def format_size(size: int) -> str:
    if size < 1024:
        return "%d B" % size
    if size < 1024 * 1024:
        return "%.1f KiB" % (size / 1024.0)
    if size < 1024 * 1024 * 1024:
        return "%.1f MiB" % (size / (1024.0 * 1024.0))
    return "%.1f GiB" % (size / (1024.0 * 1024.0 * 1024.0))


def truncate_output(text: str) -> str:
    """Keep tool output small enough for a phone context window."""
    if not isinstance(text, str):
        text = str(text)
    lines = text.splitlines()
    truncated = False
    if len(lines) > MAX_OUTPUT_LINES:
        text = "\n".join(lines[:MAX_OUTPUT_LINES])
        truncated = True
    if len(text) > MAX_OUTPUT_CHARS:
        text = text[:MAX_OUTPUT_CHARS]
        truncated = True
    if truncated:
        text += (
            "\n\n[输出已截断：最多显示 %d 字符 / %d 行]"
            % (MAX_OUTPUT_CHARS, MAX_OUTPUT_LINES)
        )
    return text


def text_result(text: str, is_error: bool = False) -> Dict[str, Any]:
    return {
        "content": [{"type": "text", "text": truncate_output(text)}],
        "isError": bool(is_error),
    }


def _string_arg(
    arguments: Dict[str, Any],
    name: str,
    default: Optional[str] = None,
    required: bool = False,
    allow_empty: bool = True,
) -> Optional[str]:
    if name not in arguments:
        if required:
            raise ToolError("缺少必填参数 %s" % name)
        return default
    value = arguments[name]
    if not isinstance(value, str):
        raise ToolError("参数 %s 必须是字符串" % name)
    if not allow_empty and not value.strip():
        raise ToolError("参数 %s 不能为空" % name)
    return value


def _int_arg(
    arguments: Dict[str, Any],
    name: str,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    if name not in arguments:
        return default
    value = arguments[name]
    if isinstance(value, bool) or not isinstance(value, int):
        raise ToolError("参数 %s 必须是整数" % name)
    if value < minimum or value > maximum:
        raise ToolError(
            "参数 %s 超出范围：必须在 %d 到 %d 之间" % (name, minimum, maximum)
        )
    return value


def _number_arg(
    arguments: Dict[str, Any],
    name: str,
    default: float,
    minimum: float,
    maximum: float,
) -> float:
    if name not in arguments:
        return default
    value = arguments[name]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ToolError("参数 %s 必须是数字" % name)
    number = float(value)
    if not math.isfinite(number):
        raise ToolError("参数 %s 必须是有限数字" % name)
    if number < minimum or number > maximum:
        raise ToolError(
            "参数 %s 超出范围：必须在 %s 到 %s 之间" % (name, minimum, maximum)
        )
    return number


def _bool_arg(arguments: Dict[str, Any], name: str, default: bool) -> bool:
    if name not in arguments:
        return default
    value = arguments[name]
    if not isinstance(value, bool):
        raise ToolError("参数 %s 必须是布尔值 true/false" % name)
    return value


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


def tool_get_time(arguments: Dict[str, Any]) -> str:
    now = datetime.datetime.now().astimezone()
    weekdays = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]
    offset = now.utcoffset()
    if offset is None:
        offset_text = "未知"
    else:
        total_minutes = int(offset.total_seconds() // 60)
        sign = "+" if total_minutes >= 0 else "-"
        total_minutes = abs(total_minutes)
        offset_text = "UTC%s%02d:%02d" % (
            sign,
            total_minutes // 60,
            total_minutes % 60,
        )
    tz_name = now.tzname() or time.tzname[0] or "本地时区"
    return "\n".join(
        [
            "当前时间：%s" % now.strftime("%Y-%m-%d %H:%M:%S"),
            "日期：%s" % now.strftime("%Y-%m-%d"),
            "星期：%s" % weekdays[now.weekday()],
            "时区：%s (%s)" % (tz_name, offset_text),
            "ISO 8601：%s" % now.isoformat(timespec="seconds"),
        ]
    )


def _eval_calc_node(node: ast.AST, depth: int = 0) -> Any:
    if depth > 200:
        raise ToolError("表达式嵌套过深")

    if isinstance(node, ast.Expression):
        return _eval_calc_node(node.body, depth + 1)

    if isinstance(node, ast.Constant):
        value = node.value
        # bool is a subclass of int, so use exact types here.
        if type(value) is int or type(value) is float:
            if isinstance(value, float) and not math.isfinite(value):
                raise ToolError("数字必须是有限值")
            return value
        raise ToolError("只允许数字常量")

    if isinstance(node, ast.UnaryOp):
        if not isinstance(node.op, ast.USub):
            raise ToolError("只允许一元负号")
        operand = _eval_calc_node(node.operand, depth + 1)
        return -operand

    if isinstance(node, ast.BinOp):
        left = _eval_calc_node(node.left, depth + 1)
        right = _eval_calc_node(node.right, depth + 1)
        op = node.op
        if isinstance(op, ast.Pow):
            if isinstance(right, (int, float)) and abs(right) > MAX_CALC_POW_EXPONENT:
                raise ToolError("指数过大（绝对值不能超过 %d）" % MAX_CALC_POW_EXPONENT)
        try:
            if isinstance(op, ast.Add):
                result = left + right
            elif isinstance(op, ast.Sub):
                result = left - right
            elif isinstance(op, ast.Mult):
                result = left * right
            elif isinstance(op, ast.Div):
                result = left / right
            elif isinstance(op, ast.FloorDiv):
                result = left // right
            elif isinstance(op, ast.Mod):
                result = left % right
            elif isinstance(op, ast.Pow):
                result = left ** right
            else:
                raise ToolError("不支持的运算符")
        except ZeroDivisionError:
            raise ToolError("除数不能为零")
        except OverflowError:
            raise ToolError("计算结果溢出")
        except ValueError as exc:
            raise ToolError("无法计算：%s" % exc)
        if isinstance(result, complex):
            raise ToolError("结果不是实数")
        if isinstance(result, float) and not math.isfinite(result):
            raise ToolError("计算结果不是有限数值")
        return result

    raise ToolError("不支持的语法：只允许数字、+ - * / % ** //、括号和一元负号")


def tool_calc(arguments: Dict[str, Any]) -> str:
    expression = _string_arg(
        arguments, "expression", required=True, allow_empty=False
    )
    assert expression is not None
    if len(expression) > MAX_CALC_EXPR_CHARS:
        raise ToolError("表达式过长（最多 %d 个字符）" % MAX_CALC_EXPR_CHARS)
    try:
        tree = ast.parse(expression, mode="eval")
    except (SyntaxError, ValueError) as exc:
        raise ToolError("表达式语法错误：%s" % exc)
    try:
        node_count = sum(1 for _ in ast.walk(tree))
    except Exception:
        node_count = 0
    if node_count > 2_000:
        raise ToolError("表达式过于复杂")
    result = _eval_calc_node(tree)
    if isinstance(result, float):
        text = repr(result)
    else:
        try:
            text = str(result)
        except ValueError:
            raise ToolError("计算结果过大，无法转换为文本")
    if len(text) > 10_000:
        raise ToolError("计算结果过大")
    return text


def tool_list_dir(arguments: Dict[str, Any]) -> str:
    raw_path = _string_arg(arguments, "path", ".", allow_empty=False)
    assert raw_path is not None
    path = resolve_workspace_path(raw_path, "path")
    if not path.exists():
        raise ToolError("目录不存在：%s" % raw_path)
    if not path.is_dir():
        raise ToolError("不是目录：%s" % raw_path)
    try:
        entries = list(path.iterdir())
    except OSError as exc:
        raise ToolError("无法读取目录 %s：%s" % (raw_path, exc))

    entries.sort(key=lambda entry: entry.name.casefold())
    lines = ["目录：%s（共 %d 项）" % (display_path(path) or ".", len(entries))]
    for entry in entries[:MAX_LIST_ENTRIES]:
        try:
            target = entry.resolve(strict=False)
            if not _is_within_workspace(target):
                lines.append(
                    "[LINK] %10s  %s -> %s（越界，已跳过）"
                    % ("-", entry.name, target)
                )
                continue
            if target.is_dir():
                lines.append("[DIR ] %10s  %s/" % ("-", entry.name))
            else:
                size = target.stat().st_size
                lines.append("[FILE] %10s  %s" % (format_size(size), entry.name))
        except OSError as exc:
            lines.append("[????] %10s  %s（无法读取：%s）" % ("-", entry.name, exc))
        except (RuntimeError, ValueError) as exc:
            lines.append("[LINK] %10s  %s（链接无效：%s）" % ("-", entry.name, exc))
    if len(entries) > MAX_LIST_ENTRIES:
        lines.append(
            "[提示] 仅显示前 %d 项，共 %d 项" % (MAX_LIST_ENTRIES, len(entries))
        )
    return "\n".join(lines)


def tool_read_file(arguments: Dict[str, Any]) -> str:
    raw_path = _string_arg(arguments, "path", required=True, allow_empty=False)
    max_bytes = _int_arg(arguments, "max_bytes", 200_000, 1, MAX_READ_BYTES)
    assert raw_path is not None
    path = resolve_workspace_path(raw_path, "path")
    if not path.exists():
        raise ToolError("文件不存在：%s" % raw_path)
    if not path.is_file():
        raise ToolError("不是普通文件：%s" % raw_path)
    try:
        file_size = path.stat().st_size
        with path.open("rb") as handle:
            data = handle.read(max_bytes + 1)
    except OSError as exc:
        raise ToolError("读取文件失败：%s" % exc)

    truncated = len(data) > max_bytes
    if truncated:
        data = data[:max_bytes]
    if b"\x00" in data:
        raise ToolError(
            "无法按文本读取：文件包含 NUL 字节，可能是二进制文件（共 %d 字节）"
            % file_size
        )
    try:
        if truncated:
            text = data.decode("utf-8-sig", errors="replace")
        else:
            text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ToolError(
            "无法按文本读取：文件不是有效的 UTF-8 文本，可能是二进制文件（共 %d 字节）"
            % file_size
        )
    if not text:
        return "（文件为空，共 %d 字节）" % file_size
    if truncated:
        text += (
            "\n\n[已截断：文件共 %d 字节，仅读取前 %d 字节]"
            % (file_size, max_bytes)
        )
    return text


def tool_write_file(arguments: Dict[str, Any]) -> str:
    raw_path = _string_arg(arguments, "path", required=True, allow_empty=False)
    content = _string_arg(arguments, "content", required=True, allow_empty=True)
    assert raw_path is not None and content is not None
    path = resolve_workspace_path(raw_path, "path")
    if path.exists() and path.is_dir():
        raise ToolError("目标是一个目录，不能写入：%s" % raw_path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        data = content.encode("utf-8")
        with path.open("wb") as handle:
            handle.write(data)
    except OSError as exc:
        raise ToolError("写入文件失败：%s" % exc)
    return "已写入 %d 字节：%s" % (len(data), display_path(path))


def _decode_text_bytes(data: bytes) -> Optional[str]:
    """Decode a text file, preferring UTF-8 and then the OS locale."""
    if b"\x00" in data:
        return None
    encodings = ["utf-8-sig"]
    preferred = locale.getpreferredencoding(False)
    if preferred and preferred.lower() not in ("utf-8", "utf8"):
        encodings.append(preferred)
    for encoding in encodings:
        try:
            return data.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return None


def tool_search_files(arguments: Dict[str, Any]) -> str:
    pattern = _string_arg(arguments, "pattern", required=True, allow_empty=False)
    file_glob = _string_arg(arguments, "file_glob", "**/*", allow_empty=False)
    max_results = _int_arg(arguments, "max_results", 100, 1, 1_000)
    assert pattern is not None and file_glob is not None
    if len(pattern) > 4_096:
        raise ToolError("正则表达式过长（最多 4096 字符）")
    try:
        regex = re.compile(pattern)
    except re.error as exc:
        raise ToolError("正则表达式无效：%s" % exc)

    glob_path = Path(file_glob)
    if glob_path.is_absolute() or glob_path.drive or ".." in glob_path.parts:
        raise ToolError("file_glob 必须是工作区内的相对 glob，不能包含 .. 或绝对路径")
    try:
        candidates = WORKSPACE.glob(file_glob)
    except (OSError, ValueError, NotImplementedError) as exc:
        raise ToolError("file_glob 无效：%s" % exc)

    results: List[str] = []
    scanned_files = 0
    scanned_bytes = 0
    skipped_binary = 0
    skipped_large = 0
    scan_limit_reached = False
    byte_limit_reached = False
    read_errors = 0

    try:
        for candidate in candidates:
            if len(results) >= max_results:
                break
            try:
                resolved = candidate.resolve(strict=False)
            except (OSError, RuntimeError):
                continue
            if not _is_within_workspace(resolved):
                continue
            try:
                if resolved.is_dir() or not resolved.is_file():
                    continue
            except OSError:
                continue

            scanned_files += 1
            if scanned_files > MAX_SEARCH_FILES:
                scan_limit_reached = True
                break
            try:
                size = resolved.stat().st_size
            except OSError:
                read_errors += 1
                continue
            if size > MAX_SEARCH_FILE_BYTES:
                skipped_large += 1
                continue
            if scanned_bytes + size > MAX_SEARCH_TOTAL_BYTES:
                byte_limit_reached = True
                break
            scanned_bytes += size
            try:
                data = resolved.read_bytes()
            except OSError:
                read_errors += 1
                continue
            text = _decode_text_bytes(data)
            if text is None:
                skipped_binary += 1
                continue
            relative = display_path(resolved)
            for line_number, line in enumerate(text.splitlines(), 1):
                if regex.search(line):
                    line_text = line
                    if len(line_text) > MAX_SEARCH_LINE_CHARS:
                        line_text = line_text[:MAX_SEARCH_LINE_CHARS] + "..."
                    results.append("%s:%d: %s" % (relative, line_number, line_text))
                    if len(results) >= max_results:
                        break
    except OSError as exc:
        raise ToolError("搜索失败：%s" % exc)

    notes: List[str] = []
    if len(results) >= max_results:
        notes.append("已达到 max_results=%d，结果可能不完整" % max_results)
    if scan_limit_reached:
        notes.append("已达到最多扫描 %d 个文件的上限" % MAX_SEARCH_FILES)
    if byte_limit_reached:
        notes.append("已达到最多扫描 %d 字节的上限" % MAX_SEARCH_TOTAL_BYTES)
    if skipped_large:
        notes.append("跳过了 %d 个大于 %d 字节的文件" % (skipped_large, MAX_SEARCH_FILE_BYTES))
    if skipped_binary:
        notes.append("跳过了 %d 个二进制/非文本文件" % skipped_binary)
    if read_errors:
        notes.append("有 %d 个文件读取失败" % read_errors)

    if not results:
        message = "未找到匹配：pattern=%r，file_glob=%r（已扫描 %d 个文件）" % (
            pattern,
            file_glob,
            scanned_files,
        )
        if notes:
            message += "\n" + "\n".join("[提示] " + note for note in notes)
        return message

    output = "\n".join(results)
    if notes:
        output += "\n\n" + "\n".join("[提示] " + note for note in notes)
    return output


def _decode_process_output(data: Any) -> str:
    if data is None:
        return ""
    if isinstance(data, str):
        return data
    if not isinstance(data, (bytes, bytearray)):
        return str(data)
    raw = bytes(data)
    encodings = ["utf-8"]
    preferred = locale.getpreferredencoding(False)
    if preferred and preferred.lower() not in ("utf-8", "utf8"):
        encodings.append(preferred)
    encodings.append("gb18030")
    for encoding in encodings:
        try:
            return raw.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")


def tool_run_command(arguments: Dict[str, Any]) -> str:
    if not ALLOW_SHELL:
        raise ToolError(
            "run_command 默认禁用。\n"
            "启用方法：设置环境变量 MCP_ALLOW_SHELL=1，然后重启服务器。\n"
            "Linux/macOS: export MCP_ALLOW_SHELL=1 && python3 server.py --host 0.0.0.0\n"
            "Windows: set MCP_ALLOW_SHELL=1 && py -3 server.py --host 0.0.0.0\n"
            "注意：启用后，能访问该端口的任何人都能在服务器上执行命令。"
        )
    command = _string_arg(arguments, "command", required=True, allow_empty=False)
    timeout = _number_arg(arguments, "timeout", 30.0, 1.0, 300.0)
    assert command is not None
    try:
        completed = subprocess.run(
            command,
            shell=True,
            cwd=str(WORKSPACE),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        partial = _decode_process_output(exc.stdout)
        message = "命令执行超时（%.1f 秒），已终止。" % timeout
        if partial:
            message += "\n已捕获的输出：\n" + partial.rstrip()
        raise ToolError(message)
    except OSError as exc:
        raise ToolError("无法启动命令：%s" % exc)

    output = _decode_process_output(completed.stdout)
    if output:
        output = output.rstrip()
    else:
        output = "（无输出）"
    return "退出码：%d\n输出：\n%s" % (completed.returncode, output)


def _strip_html_markup(text: str) -> str:
    text = re.sub(r"(?is)<script\b[^>]*>.*?</script\s*>", " ", text)
    text = re.sub(r"(?is)<style\b[^>]*>.*?</style\s*>", " ", text)
    text = re.sub(r"(?is)<!--.*?-->", " ", text)
    text = re.sub(r"(?is)<[^>]*>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def tool_http_fetch(arguments: Dict[str, Any]) -> str:
    url = _string_arg(arguments, "url", required=True, allow_empty=False)
    max_bytes = _int_arg(arguments, "max_bytes", 200_000, 1, MAX_FETCH_BYTES)
    strip_html = _bool_arg(arguments, "strip_html", True)
    assert url is not None
    if len(url) > 8_192:
        raise ToolError("URL 过长")
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme.lower() not in ("http", "https"):
        raise ToolError("仅支持 http:// 和 https:// URL")
    if not parsed.netloc:
        raise ToolError("URL 缺少主机名")

    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "*/*",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
            content_type = response.headers.get("Content-Type", "") or ""
            charset = response.headers.get_content_charset()
            raw = response.read(max_bytes + 1)
    except urllib.error.HTTPError as exc:
        try:
            exc.close()
        except Exception:
            pass
        raise ToolError(
            "HTTP 请求失败：HTTP %s %s" % (exc.code, exc.reason)
        )
    except urllib.error.URLError as exc:
        raise ToolError("网络请求失败：%s" % exc.reason)
    except ValueError as exc:
        raise ToolError("URL 无效：%s" % exc)
    except Exception as exc:
        raise ToolError("抓取失败：%s: %s" % (type(exc).__name__, exc))

    truncated = len(raw) > max_bytes
    if truncated:
        raw = raw[:max_bytes]
    lower_type = content_type.lower()
    if (
        lower_type.startswith("image/")
        or lower_type.startswith("audio/")
        or lower_type.startswith("video/")
        or "application/octet-stream" in lower_type
    ):
        return "已获取二进制内容（%d 字节，Content-Type: %s），未作为文本显示。" % (
            len(raw),
            content_type or "未知",
        )

    encoding = charset or "utf-8"
    try:
        if encoding.lower().replace("_", "-") in ("utf-8", "utf8"):
            text = raw.decode("utf-8-sig", errors="replace")
        else:
            text = raw.decode(encoding, errors="replace")
    except (LookupError, UnicodeDecodeError):
        text = raw.decode("utf-8", errors="replace")
    if strip_html:
        text = _strip_html_markup(text)
    if not text:
        text = "（响应正文为空）"
    if truncated:
        text += "\n\n[已截断：仅读取前 %d 字节]" % max_bytes
    return text


TOOL_DEFINITIONS: List[Dict[str, Any]] = [
    {
        "name": "get_time",
        "description": "获取服务器本机（不是手机）的当前日期、时间、星期和时区，无参数。",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
    {
        "name": "calc",
        "description": (
            "安全地计算一个四则运算表达式。支持数字、+ - * / % ** //、括号和一元负号，"
            "不支持变量、函数或任何代码执行。例如：(3+4)*2。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "expression": {
                    "type": "string",
                    "description": "要计算的表达式，例如 (3+4)*2 或 2**10。",
                    "minLength": 1,
                    "maxLength": 4096,
                }
            },
            "required": ["expression"],
            "additionalProperties": False,
        },
    },
    {
        "name": "list_dir",
        "description": "列出工作区内的目录内容，返回名称、类型（目录/文件）和文件大小。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "工作区内的相对路径或绝对路径；省略时使用工作区根目录（.）。",
                    "default": ".",
                }
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "read_file",
        "description": (
            "读取工作区内的 UTF-8 文本文件。二进制文件不会直接返回乱码，而是返回提示。"
            "读取超过 max_bytes 时自动截断并注明。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "工作区内的文件路径（相对或绝对，但必须位于工作区内）。",
                },
                "max_bytes": {
                    "type": "integer",
                    "description": "最多读取的字节数，默认 200000。",
                    "default": 200000,
                    "minimum": 1,
                    "maximum": MAX_READ_BYTES,
                },
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    },
    {
        "name": "write_file",
        "description": (
            "把 UTF-8 文本写入工作区内文件，自动创建父目录，覆盖已有文件，"
            "返回写入的字节数。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "工作区内的目标文件路径（相对或绝对，但必须位于工作区内）。",
                },
                "content": {
                    "type": "string",
                    "description": "要写入的完整文本内容，可以为空字符串。",
                },
            },
            "required": ["path", "content"],
            "additionalProperties": False,
        },
    },
    {
        "name": "search_files",
        "description": (
            "在工作区内按正则表达式搜索文本文件，返回“路径:行号: 行内容”。"
            "会自动跳过二进制文件、超大文件和读取失败的文件。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "Python 正则表达式，例如 TODO|FIXME 或 error\\s+code。",
                },
                "file_glob": {
                    "type": "string",
                    "description": "限定文件范围的相对 glob，默认 **/*，例如 **/*.py。",
                    "default": "**/*",
                },
                "max_results": {
                    "type": "integer",
                    "description": "最多返回的匹配行数，默认 100。",
                    "default": 100,
                    "minimum": 1,
                    "maximum": 1000,
                },
            },
            "required": ["pattern"],
            "additionalProperties": False,
        },
    },
    {
        "name": "run_command",
        "description": (
            "在工作区根目录执行一条 shell 命令，返回合并后的 stdout/stderr 和退出码。"
            "出于安全考虑默认禁用，只有服务器环境变量 MCP_ALLOW_SHELL=1 时才可用，"
            "并受 timeout（默认 30 秒，最大 300 秒）限制。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "要执行的命令字符串，例如 echo hi 或 ls -la。",
                },
                "timeout": {
                    "type": "number",
                    "description": "超时秒数，默认 30，最大 300。",
                    "default": 30,
                    "minimum": 1,
                    "maximum": 300,
                },
            },
            "required": ["command"],
            "additionalProperties": False,
        },
    },
    {
        "name": "http_fetch",
        "description": (
            "用 HTTP/HTTPS GET 抓取网页或接口，自动跟随重定向，设置 User-Agent，"
            "超时 20 秒。strip_html=true 时会去除 script/style/标签并合并空白。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "完整的 http:// 或 https:// URL。",
                },
                "max_bytes": {
                    "type": "integer",
                    "description": "最多下载的响应字节数，默认 200000。",
                    "default": 200000,
                    "minimum": 1,
                    "maximum": MAX_FETCH_BYTES,
                },
                "strip_html": {
                    "type": "boolean",
                    "description": "是否移除 HTML 标签并合并空白，默认 true。",
                    "default": True,
                },
            },
            "required": ["url"],
            "additionalProperties": False,
        },
    },
]

TOOL_HANDLERS: Dict[str, Callable[[Dict[str, Any]], str]] = {
    "get_time": tool_get_time,
    "calc": tool_calc,
    "list_dir": tool_list_dir,
    "read_file": tool_read_file,
    "write_file": tool_write_file,
    "search_files": tool_search_files,
    "run_command": tool_run_command,
    "http_fetch": tool_http_fetch,
}


def call_tool(params: Any) -> Dict[str, Any]:
    if not isinstance(params, dict):
        return text_result("tools/call 的 params 必须是对象", True)
    name = params.get("name")
    if not isinstance(name, str) or not name:
        return text_result("tools/call 缺少字符串参数 name", True)
    arguments = params.get("arguments", {})
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        return text_result("tools/call 的 arguments 必须是对象", True)
    handler = TOOL_HANDLERS.get(name)
    if handler is None:
        return text_result("未知工具：%s" % name, True)
    try:
        result = handler(arguments)
        return text_result(result, False)
    except ToolError as exc:
        return text_result(str(exc), True)
    except Exception as exc:
        log(
            "工具 %s 内部异常：%s\n%s"
            % (one_line(name), one_line(exc), traceback.format_exc())
        )
        return text_result(
            "工具内部错误：%s: %s" % (type(exc).__name__, exc), True
        )


# ---------------------------------------------------------------------------
# JSON-RPC / MCP
# ---------------------------------------------------------------------------


def rpc_error(request_id: Any, code: int, message: str) -> Dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": int(code), "message": message},
    }


def rpc_result(request_id: Any, result: Dict[str, Any]) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def process_rpc(message: Any) -> Tuple[int, Optional[Dict[str, Any]], str]:
    """Return (http_status, json_payload_or_None, rpc_method_for_log)."""
    if not isinstance(message, dict):
        return (
            200,
            rpc_error(None, -32600, "Invalid Request: expected a JSON object"),
            "-",
        )
    if "jsonrpc" in message and message.get("jsonrpc") != "2.0":
        return (
            200,
            rpc_error(message.get("id"), -32600, "Invalid Request: jsonrpc must be 2.0"),
            "-",
        )

    method = message.get("method")
    if not isinstance(method, str) or not method:
        return (
            200,
            rpc_error(message.get("id"), -32600, "Invalid Request: missing method"),
            "-",
        )
    if method == "notifications/initialized":
        return 202, None, method
    if "id" not in message:
        # JSON-RPC notification: no response body.
        return 202, None, method

    request_id = message.get("id")
    if isinstance(request_id, (dict, list, bool)):
        return (
            200,
            rpc_error(None, -32600, "Invalid Request: invalid id type"),
            method,
        )
    params = message.get("params")
    if params is not None and not isinstance(params, (dict, list)):
        return (
            200,
            rpc_error(request_id, -32600, "Invalid Request: params must be object or array"),
            method,
        )

    if method == "initialize":
        protocol_version = DEFAULT_PROTOCOL_VERSION
        if isinstance(params, dict):
            requested = params.get("protocolVersion")
            if isinstance(requested, str) and requested.strip():
                protocol_version = requested
        return (
            200,
            rpc_result(
                request_id,
                {
                    "protocolVersion": protocol_version,
                    "capabilities": {"tools": {}},
                    "serverInfo": {
                        "name": SERVER_NAME,
                        "version": SERVER_VERSION,
                    },
                },
            ),
            method,
        )

    if method == "ping":
        return 200, rpc_result(request_id, {}), method

    if method == "tools/list":
        return 200, rpc_result(request_id, {"tools": TOOL_DEFINITIONS}), method

    if method == "tools/call":
        return 200, rpc_result(request_id, call_tool(params)), method

    return (
        200,
        rpc_error(request_id, -32601, "Method not found: %s" % method),
        method,
    )


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------


class MCPHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    # On Windows SO_REUSEADDR can hide a real port conflict; disable reuse there.
    allow_reuse_address = os.name != "nt"


class MCPRequestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = SERVER_NAME + "/" + SERVER_VERSION
    sys_version = ""

    # BaseHTTPRequestHandler logs through this method; use our own one-line log.
    def log_message(self, fmt: str, *args: Any) -> None:
        return

    def _send_bytes(
        self,
        status: int,
        data: bytes,
        content_type: Optional[str] = None,
    ) -> int:
        try:
            self.send_response(status)
            if content_type:
                self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            if data:
                self.wfile.write(data)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        self.close_connection = True
        return status

    def _send_json(self, status: int, payload: Dict[str, Any]) -> int:
        try:
            data = json.dumps(
                payload, ensure_ascii=False, separators=(",", ":")
            ).encode("utf-8")
        except (TypeError, ValueError) as exc:
            data = json.dumps(
                rpc_error(None, -32603, "Internal error: %s" % exc),
                ensure_ascii=False,
            ).encode("utf-8")
            status = 500
        return self._send_bytes(status, data, "application/json; charset=utf-8")

    def _send_text(
        self,
        status: int,
        text: str,
        extra_headers: Optional[Dict[str, str]] = None,
    ) -> int:
        data = text.encode("utf-8", errors="replace")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            if extra_headers:
                for key, value in extra_headers.items():
                    self.send_header(key, value)
            self.end_headers()
            if data:
                self.wfile.write(data)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        self.close_connection = True
        return status

    def _send_empty(self, status: int) -> int:
        return self._send_bytes(status, b"", None)

    def _log_access(
        self,
        http_method: str,
        path: str,
        status: int,
        rpc_method: str,
        started: float,
    ) -> None:
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        log(
            "%s %s -> %d method=%s %.1fms"
            % (
                one_line(http_method, 16),
                one_line(path, 160),
                status,
                one_line(rpc_method, 120),
                elapsed_ms,
            )
        )

    def do_GET(self) -> None:
        started = time.perf_counter()
        path = urllib.parse.urlsplit(self.path).path or "/"
        if path == "/":
            status = self._send_text(
                200,
                "deepseek-lite-mcp %s\n"
                "MCP endpoint: POST /mcp\n"
                "Transport: JSON-RPC 2.0 over HTTP, JSON responses, stateless\n"
                % SERVER_VERSION,
            )
        elif path == "/mcp":
            status = self._send_text(
                405,
                "GET is not supported here. Send a JSON-RPC POST to /mcp.\n",
                {"Allow": "POST"},
            )
        else:
            status = self._send_text(
                404,
                "Not found. The MCP endpoint is POST /mcp.\n",
            )
        self._log_access("GET", path, status, "-", started)

    def _discard_request_body(self, length: int) -> None:
        """Drain an oversized body so the client can receive our 400 response.

        An immediate close with unread upload data makes many clients see a
        connection reset instead of the HTTP error.  Drain a bounded amount
        (the normal oversize case is only slightly over 2 MiB), then respond.
        """
        remaining = min(length, MAX_REQUEST_BYTES + 4 * 1024 * 1024)
        try:
            self.connection.settimeout(10.0)
            while remaining > 0:
                chunk = self.rfile.read(min(65536, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
        except (OSError, ValueError):
            pass

    def _read_json_body(self) -> Any:
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            raise RequestProblem(400, "缺少 Content-Length 请求头")
        try:
            length = int(raw_length.strip(), 10)
        except (TypeError, ValueError):
            raise RequestProblem(400, "Content-Length 无效")
        if length <= 0:
            raise RequestProblem(400, "请求体为空", parse_error=True)
        if length > MAX_REQUEST_BYTES:
            self._discard_request_body(length)
            raise RequestProblem(
                400,
                "请求体过大：%d 字节，上限为 %d 字节" % (length, MAX_REQUEST_BYTES),
            )
        body = self.rfile.read(length)
        if len(body) != length:
            raise RequestProblem(400, "请求体不完整", parse_error=True)
        try:
            text = body.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise RequestProblem(
                400, "请求体不是有效 UTF-8：%s" % exc, parse_error=True
            )
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise RequestProblem(
                400, "JSON 解析失败：%s" % exc, parse_error=True
            )

    def _process_post(self) -> Tuple[int, str]:
        path = urllib.parse.urlsplit(self.path).path or "/"
        if path != "/mcp":
            return (
                self._send_text(404, "Not found. The MCP endpoint is POST /mcp.\n"),
                "-",
            )
        try:
            message = self._read_json_body()
        except RequestProblem as exc:
            if exc.parse_error:
                return (
                    self._send_json(
                        exc.status,
                        rpc_error(None, -32700, "Parse error: " + exc.message),
                    ),
                    "(parse-error)",
                )
            return self._send_text(exc.status, exc.message + "\n"), "(body)"

        try:
            status, payload, rpc_method = process_rpc(message)
        except Exception as exc:
            log("处理 JSON-RPC 时发生异常：%s\n%s" % (one_line(exc), traceback.format_exc()))
            return (
                self._send_json(
                    500, rpc_error(None, -32603, "Internal error: %s" % exc)
                ),
                "-",
            )

        if status == 202 or payload is None:
            return self._send_empty(202), rpc_method
        return self._send_json(status, payload), rpc_method

    def do_POST(self) -> None:
        started = time.perf_counter()
        path = urllib.parse.urlsplit(self.path).path or "/"
        rpc_method = "-"
        status = 500
        try:
            status, rpc_method = self._process_post()
        except (BrokenPipeError, ConnectionResetError):
            status = 499
        except Exception as exc:
            log(
                "未处理的请求异常：%s\n%s"
                % (one_line(exc), traceback.format_exc())
            )
            try:
                status = self._send_json(
                    500, rpc_error(None, -32603, "Internal server error")
                )
            except Exception:
                status = 500
        finally:
            self._log_access("POST", path, status, rpc_method, started)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args(argv: Optional[List[str]]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="deepseek-lite-mcp - minimal dependency-free MCP server"
    )
    parser.add_argument(
        "port",
        nargs="?",
        type=int,
        help="监听端口（位置参数，默认 8765），例如：python server.py 9000",
    )
    parser.add_argument(
        "--host",
        default=DEFAULT_HOST,
        help="监听地址，默认 0.0.0.0（所有网卡）",
    )
    parser.add_argument(
        "--port",
        dest="port_opt",
        type=int,
        default=None,
        help="监听端口（优先于位置参数，默认 8765）",
    )
    parser.add_argument(
        "--workspace",
        default=None,
        help="工作区根目录；优先使用环境变量 MCP_WORKSPACE，其次此参数，最后当前目录",
    )
    return parser.parse_args(argv)


def configure_workspace(args: argparse.Namespace) -> Path:
    env_workspace = os.environ.get("MCP_WORKSPACE", "")
    raw = env_workspace if env_workspace else args.workspace
    if raw is None:
        raw = os.getcwd()
    try:
        workspace = Path(raw).expanduser().resolve(strict=True)
    except FileNotFoundError:
        raise SystemExit("错误：工作区不存在：%s" % raw)
    except (OSError, RuntimeError, ValueError) as exc:
        raise SystemExit("错误：无法解析工作区 %s：%s" % (raw, exc))
    if not workspace.is_dir():
        raise SystemExit("错误：工作区不是目录：%s" % workspace)
    return workspace


def main(argv: Optional[List[str]] = None) -> int:
    global WORKSPACE, ALLOW_SHELL
    args = parse_args(argv)

    if args.port is not None and args.port_opt is not None and args.port != args.port_opt:
        raise SystemExit("错误：位置参数端口与 --port 不一致，请只用一个")
    if args.port_opt is not None:
        port = args.port_opt
    elif args.port is not None:
        port = args.port
    else:
        port = DEFAULT_PORT
    if port < 1 or port > 65535:
        raise SystemExit("错误：端口必须在 1 到 65535 之间")
    if not args.host or not str(args.host).strip():
        raise SystemExit("错误：--host 不能为空")

    WORKSPACE = configure_workspace(args)
    ALLOW_SHELL = os.environ.get("MCP_ALLOW_SHELL", "") == "1"

    log("deepseek-lite-mcp %s" % SERVER_VERSION)
    log("监听：%s:%d（MCP 端点 http://<服务器IP>:%d/mcp）" % (args.host, port, port))
    log("工作区：%s" % WORKSPACE)
    log("run_command：%s" % ("已启用（MCP_ALLOW_SHELL=1）" if ALLOW_SHELL else "已禁用"))
    log("按 Ctrl+C 停止")

    try:
        httpd = MCPHTTPServer((args.host, port), MCPRequestHandler)
    except OSError as exc:
        code = getattr(exc, "winerror", None) or exc.errno
        if code in (
            errno.EADDRINUSE,
            10048,
            getattr(errno, "WSAEADDRINUSE", 10048),
        ):
            log(
                "错误：端口 %d 已被占用。请换一个端口（例如 --port 8766），"
                "或先关闭占用该端口的旧进程。" % port
            )
        elif code in (errno.EACCES, 10013, getattr(errno, "WSAEACCES", 10013)):
            log(
                "错误：没有权限监听 %s:%d。请换用 1024 以上的端口，"
                "或检查防火墙/端口保留设置。" % (args.host, port)
            )
        else:
            log("错误：无法启动服务器 %s:%d：%s" % (args.host, port, exc))
        return 1

    try:
        httpd.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        log("收到 Ctrl+C，正在停止…")
    finally:
        try:
            httpd.server_close()
        except Exception:
            pass
        log("服务器已停止")
    return 0


if __name__ == "__main__":
    sys.exit(main())
