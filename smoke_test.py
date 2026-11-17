#!/usr/bin/env python3
"""Self-test for deepseek-lite-mcp.

Usage:
    python smoke_test.py http://127.0.0.1:8765
    python smoke_test.py http://127.0.0.1:8765 --allow-shell
    python smoke_test.py http://127.0.0.1:8765 --strict-http

The script uses only the Python standard library.  It exits non-zero if a
required check fails.  A failed http_fetch due to missing network access is
reported as SKIP unless --strict-http is given.  run_command is expected to be
disabled unless --allow-shell is given (matching the server's environment).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Tuple


class CheckFailure(Exception):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise CheckFailure(message)


def short(value: Any, limit: int = 300) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    text = text.replace("\r", "\\r").replace("\n", "\\n")
    if len(text) > limit:
        text = text[:limit] + "..."
    return text


class MCPClient:
    def __init__(self, base_url: str, timeout: float = 30.0) -> None:
        base_url = base_url.rstrip("/")
        if base_url.endswith("/mcp"):
            self.endpoint = base_url
            base_url = base_url[: -len("/mcp")]
        else:
            self.endpoint = base_url + "/mcp"
        self.base_url = base_url
        self.timeout = timeout
        self._next_id = 1

    def raw_post(self, body: bytes) -> Tuple[int, Dict[str, str], str]:
        request = urllib.request.Request(
            self.endpoint,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                status = response.getcode()
                headers = dict(response.headers.items())
                data = response.read()
        except urllib.error.HTTPError as exc:
            status = exc.code
            headers = dict(exc.headers.items()) if exc.headers else {}
            data = exc.read()
        return status, headers, data.decode("utf-8", errors="replace")

    def get(self, path: str = "/") -> Tuple[int, str]:
        url = self.base_url + path
        try:
            with urllib.request.urlopen(url, timeout=self.timeout) as response:
                return response.getcode(), response.read().decode(
                    "utf-8", errors="replace"
                )
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8", errors="replace")

    def post_json(self, payload: Dict[str, Any]) -> Tuple[int, str, Any]:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        status, _headers, text = self.raw_post(body)
        try:
            parsed = json.loads(text) if text else None
        except json.JSONDecodeError as exc:
            raise CheckFailure(
                "response is not JSON (status %d): %s (%s)" % (status, short(text), exc)
            )
        return status, text, parsed

    def rpc(
        self,
        method: str,
        params: Optional[Any] = None,
        notification: bool = False,
    ) -> Tuple[int, str, Any, int]:
        request_id = self._next_id
        self._next_id += 1
        payload: Dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        if not notification:
            payload["id"] = request_id
        status, text, parsed = self.post_json(payload)
        return status, text, parsed, request_id

    def tool(self, name: str, arguments: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        status, _text, parsed, request_id = self.rpc(
            "tools/call", {"name": name, "arguments": arguments or {}}
        )
        require(status == 200, "tools/call %s returned HTTP %d" % (name, status))
        require(isinstance(parsed, dict), "tools/call %s response is not an object" % name)
        require(
            parsed.get("id") == request_id,
            "tools/call %s response id mismatch" % name,
        )
        require("error" not in parsed, "tools/call %s returned RPC error: %s" % (name, short(parsed)))
        result = parsed.get("result")
        require(isinstance(result, dict), "tools/call %s missing result object" % name)
        content = result.get("content")
        require(isinstance(content, list) and content, "tools/call %s missing content" % name)
        text = content[0].get("text")
        require(isinstance(text, str), "tools/call %s content text is not a string" % name)
        return {
            "text": text,
            "isError": bool(result.get("isError", False)),
            "raw": result,
        }


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Smoke test a deepseek-lite-mcp server")
    parser.add_argument(
        "base_url",
        nargs="?",
        default="http://127.0.0.1:8765",
        help="服务器地址，默认 http://127.0.0.1:8765（也可写 .../mcp）",
    )
    parser.add_argument(
        "--allow-shell",
        action="store_true",
        help="服务器已设置 MCP_ALLOW_SHELL=1；此时要求 run_command 成功执行 echo hi",
    )
    parser.add_argument(
        "--strict-http",
        action="store_true",
        help="把 http_fetch 网络失败视为测试失败（默认记为 SKIP）",
    )
    parser.add_argument("--timeout", type=float, default=30.0, help="HTTP 超时秒数")
    args = parser.parse_args(argv)

    client = MCPClient(args.base_url, timeout=args.timeout)
    passed = 0
    failed = 0
    skipped = 0

    def run(name: str, function: Any) -> None:
        nonlocal passed, failed, skipped
        try:
            outcome = function()
            if outcome == "SKIP":
                skipped += 1
                print("[SKIP] %s" % name, flush=True)
            else:
                passed += 1
                detail = " - " + short(outcome) if isinstance(outcome, str) and outcome else ""
                print("[PASS] %s%s" % (name, detail), flush=True)
        except CheckFailure as exc:
            failed += 1
            print("[FAIL] %s - %s" % (name, exc), flush=True)
        except Exception as exc:
            failed += 1
            print("[FAIL] %s - %s: %s" % (name, type(exc).__name__, exc), flush=True)

    marker = "SMOKE_MARKER_%d_%d" % (int(time.time()), os.getpid())
    probe_path = ".mcp_smoke/probe_%d.txt" % os.getpid()
    probe_content = "line one %s\nline two\n" % marker

    print("MCP endpoint: %s" % client.endpoint, flush=True)
    print("=" * 70, flush=True)

    def check_http_root() -> str:
        status, text = client.get("/")
        require(status == 200, "GET / returned HTTP %d" % status)
        require("POST /mcp" in text, "GET / does not mention POST /mcp")
        return "HTTP 200"

    def check_initialize() -> str:
        status, _text, parsed, request_id = client.rpc(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "smoke-test", "version": "1.0"},
            },
        )
        require(status == 200, "initialize returned HTTP %d" % status)
        require(isinstance(parsed, dict), "initialize response is not an object")
        require(parsed.get("id") == request_id, "initialize response id mismatch")
        require("error" not in parsed, "initialize returned error: %s" % short(parsed))
        result = parsed.get("result")
        require(isinstance(result, dict), "initialize missing result")
        require(
            isinstance(result.get("protocolVersion"), str)
            and result["protocolVersion"],
            "initialize missing protocolVersion",
        )
        capabilities = result.get("capabilities")
        require(
            isinstance(capabilities, dict) and isinstance(capabilities.get("tools"), dict),
            "initialize missing capabilities.tools",
        )
        server_info = result.get("serverInfo")
        require(isinstance(server_info, dict), "initialize missing serverInfo")
        require(server_info.get("name") == "deepseek-lite-mcp", "wrong server name")
        require(server_info.get("version") == "1.0.0", "wrong server version")
        return "protocolVersion=%s" % result.get("protocolVersion")

    def check_initialized_notification() -> str:
        status, text, parsed, _request_id = client.rpc(
            "notifications/initialized", notification=True
        )
        require(status == 202, "initialized notification returned HTTP %d" % status)
        require(text == "", "initialized notification body is not empty: %r" % text)
        require(parsed is None, "initialized notification unexpectedly returned JSON")
        return "HTTP 202, empty body"

    expected_tools = {
        "get_time",
        "calc",
        "list_dir",
        "read_file",
        "write_file",
        "search_files",
        "run_command",
        "http_fetch",
    }

    def check_tools_list() -> str:
        status, _text, parsed, request_id = client.rpc("tools/list", {})
        require(status == 200, "tools/list returned HTTP %d" % status)
        require(isinstance(parsed, dict), "tools/list response is not an object")
        require(parsed.get("id") == request_id, "tools/list response id mismatch")
        require("error" not in parsed, "tools/list returned error: %s" % short(parsed))
        result = parsed.get("result")
        require(isinstance(result, dict), "tools/list missing result")
        tools = result.get("tools")
        require(isinstance(tools, list), "tools/list tools is not an array")
        names = []
        for tool in tools:
            require(isinstance(tool, dict), "tool definition is not an object")
            name = tool.get("name")
            require(isinstance(name, str) and name, "tool definition missing name")
            require(name == name.lower(), "tool name is not snake_case lowercase: %s" % name)
            require(
                isinstance(tool.get("description"), str) and tool["description"],
                "tool %s missing description" % name,
            )
            schema = tool.get("inputSchema")
            require(
                isinstance(schema, dict) and schema.get("type") == "object",
                "tool %s inputSchema is not an object schema" % name,
            )
            names.append(name)
        missing = expected_tools - set(names)
        require(not missing, "missing tools: %s" % ", ".join(sorted(missing)))
        return "%d tools" % len(names)

    def check_get_time() -> str:
        result = client.tool("get_time")
        require(not result["isError"], "get_time returned isError: %s" % short(result["text"]))
        for token in ("当前时间", "星期", "时区"):
            require(token in result["text"], "get_time text missing %s" % token)
        return result["text"].splitlines()[0]

    def check_calc() -> str:
        result = client.tool("calc", {"expression": "(3+4)*2"})
        require(not result["isError"], "calc returned isError: %s" % short(result["text"]))
        require(result["text"].strip() == "14", "calc result is not 14: %r" % result["text"])

        divide_by_zero = client.tool("calc", {"expression": "1/0"})
        require(divide_by_zero["isError"], "calc 1/0 should return isError=true")
        require(
            "零" in divide_by_zero["text"] or "zero" in divide_by_zero["text"].lower(),
            "calc 1/0 error text is unclear: %s" % short(divide_by_zero["text"]),
        )

        unsafe = client.tool("calc", {"expression": "__import__('os').system('echo x')"})
        require(unsafe["isError"], "calc should reject function calls")
        return "14 (and rejected 1/0 + code execution)"

    def check_write_file() -> str:
        result = client.tool(
            "write_file", {"path": probe_path, "content": probe_content}
        )
        require(
            not result["isError"],
            "write_file returned isError: %s" % short(result["text"]),
        )
        require("已写入" in result["text"], "write_file did not report bytes")
        return result["text"]

    def check_read_file() -> str:
        result = client.tool("read_file", {"path": probe_path, "max_bytes": 200000})
        require(
            not result["isError"],
            "read_file returned isError: %s" % short(result["text"]),
        )
        require(result["text"] == probe_content, "read_file content mismatch")
        return "%d chars" % len(result["text"])

    def check_search_files() -> str:
        result = client.tool(
            "search_files",
            {"pattern": marker, "file_glob": "**/*", "max_results": 10},
        )
        require(
            not result["isError"],
            "search_files returned isError: %s" % short(result["text"]),
        )
        require(marker in result["text"], "search_files result missing marker")
        require(
            probe_path.replace("\\", "/") in result["text"],
            "search_files result missing path: %s" % short(result["text"]),
        )
        require(":1:" in result["text"], "search_files result missing line number")
        return result["text"].splitlines()[0]

    def check_list_dir() -> str:
        result = client.tool("list_dir", {"path": ".mcp_smoke"})
        require(
            not result["isError"],
            "list_dir returned isError: %s" % short(result["text"]),
        )
        require("probe_%d.txt" % os.getpid() in result["text"], "list_dir missing probe file")
        require("[FILE]" in result["text"], "list_dir missing file type")
        return result["text"].splitlines()[0]

    def check_read_outside_workspace() -> str:
        result = client.tool("read_file", {"path": "../../etc/passwd"})
        require(result["isError"], "path traversal was NOT rejected")
        required_words = ("越界", "拒绝", "outside", "reject")
        require(
            any(word in result["text"].lower() for word in required_words),
            "path traversal error text is unclear: %s" % short(result["text"]),
        )
        return "rejected"

    def check_run_command() -> str:
        result = client.tool("run_command", {"command": "echo hi", "timeout": 30})
        if args.allow_shell:
            require(
                not result["isError"],
                "run_command is still disabled but --allow-shell was given: %s"
                % short(result["text"]),
            )
            require("hi" in result["text"], "run_command output missing hi")
            require("退出码：0" in result["text"], "run_command did not exit 0")
            return "enabled, echo hi -> exit 0"
        require(
            result["isError"],
            "run_command must be disabled unless MCP_ALLOW_SHELL=1",
        )
        require(
            "MCP_ALLOW_SHELL" in result["text"],
            "disabled run_command does not explain how to enable it",
        )
        return "correctly disabled (set MCP_ALLOW_SHELL=1 to test it)"

    def check_http_fetch() -> str:
        result = client.tool("http_fetch", {"url": "https://example.com"})
        if result["isError"]:
            if args.strict_http:
                raise CheckFailure("http_fetch returned isError: %s" % short(result["text"]))
            print(
                "       note: http_fetch failed (network unavailable?): %s"
                % short(result["text"], 180),
                flush=True,
            )
            return "SKIP"
        require("Example Domain" in result["text"], "http_fetch content missing Example Domain")
        require("<html" not in result["text"].lower(), "strip_html did not remove HTML tags")
        return "Example Domain, tags stripped"

    def check_unknown_method() -> str:
        status, _text, parsed, _request_id = client.rpc("does/not/exist", {})
        require(status == 200, "unknown method returned HTTP %d" % status)
        require(isinstance(parsed, dict), "unknown method response is not an object")
        error = parsed.get("error")
        require(isinstance(error, dict), "unknown method did not return an error object")
        require(error.get("code") == -32601, "unknown method code is not -32601: %s" % short(parsed))
        return "-32601"

    def check_invalid_json() -> str:
        status, _headers, text = client.raw_post(b"{this is not valid json")
        require(status == 400, "invalid JSON returned HTTP %d, expected 400" % status)
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            raise CheckFailure("invalid JSON response body is not JSON: %s" % short(text))
        error = parsed.get("error") if isinstance(parsed, dict) else None
        require(isinstance(error, dict), "invalid JSON did not return a JSON-RPC error")
        require(error.get("code") == -32700, "invalid JSON code is not -32700: %s" % short(parsed))
        return "HTTP 400, -32700"

    run("GET / help page", check_http_root)
    run("initialize", check_initialize)
    run("notifications/initialized", check_initialized_notification)
    run("tools/list", check_tools_list)
    run("tools/call get_time", check_get_time)
    run("tools/call calc", check_calc)
    run("tools/call write_file", check_write_file)
    run("tools/call read_file", check_read_file)
    run("tools/call search_files", check_search_files)
    run("tools/call list_dir", check_list_dir)
    run("path traversal rejected", check_read_outside_workspace)
    run("tools/call run_command", check_run_command)
    run("tools/call http_fetch", check_http_fetch)
    run("unknown method -> -32601", check_unknown_method)
    run("invalid JSON -> 400/-32700", check_invalid_json)

    print("=" * 70, flush=True)
    print(
        "Summary: %d passed, %d failed, %d skipped" % (passed, failed, skipped),
        flush=True,
    )
    if failed:
        print("RESULT: FAIL", flush=True)
        return 1
    print("RESULT: PASS", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
