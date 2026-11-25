#!/usr/bin/env python3
"""签名鉴权自测：用 openssl 生成 RSA 密钥、造签名，验证服务端的验签逻辑。

用法：
    python3 signature_test.py [--server /opt/pocket-mcp/server.py] [--port 18765]

覆盖：正确签名 200、时间戳过期 401、nonce 重放 401、未知 key id 401、
被篡改 body 401、Bearer token 仍可用 200、无鉴权 401。
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

TOKEN = "sig-test-token"


def run(cmd, **kwargs):
    return subprocess.run(cmd, capture_output=True, text=True, **kwargs)


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical(body: bytes, timestamp: str, nonce: str) -> bytes:
    return ("POST\n/mcp\n" + sha256_hex(body) + "\n" + timestamp + "\n" + nonce).encode()


def sign(openssl: str, key_path: str, message: bytes, tag: str) -> str:
    with tempfile.NamedTemporaryFile(delete=False) as handle:
        handle.write(message)
        msg_path = handle.name
    sig_path = msg_path + ".sig"
    try:
        result = run([openssl, "dgst", "-sha256", "-sign", key_path, "-out", sig_path, msg_path])
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip())
        with open(sig_path, "rb") as handle:
            return base64.b64encode(handle.read()).decode()
    finally:
        for path in (msg_path, sig_path):
            try:
                os.unlink(path)
            except OSError:
                pass


def post(url: str, body: bytes, headers: dict) -> tuple[int, str]:
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return response.getcode(), response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="replace")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--server", default="server.py")
    parser.add_argument("--port", type=int, default=18765)
    parser.add_argument("--openssl", default="openssl")
    parser.add_argument("--python", default=sys.executable)
    args = parser.parse_args(argv)

    workdir = tempfile.mkdtemp(prefix="mcp-sig-")
    workspace = os.path.join(workdir, "ws")
    os.makedirs(workspace, exist_ok=True)
    key_path = os.path.join(workdir, "key.pem")
    pub_path = os.path.join(workdir, "pub.pem")
    auth_path = os.path.join(workdir, "authorized_keys")

    gen = run([args.openssl, "genrsa", "-out", key_path, "2048"])
    if gen.returncode != 0:
        print("openssl genrsa failed:", gen.stderr)
        return 2
    pub = run([args.openssl, "rsa", "-in", key_path, "-pubout", "-out", pub_path])
    if pub.returncode != 0:
        print("openssl rsa -pubout failed:", pub.stderr)
        return 2
    with open(pub_path, "r", encoding="utf-8") as handle:
        lines = [line.strip() for line in handle if line.strip() and "-----" not in line]
    spki_b64 = "".join(lines)
    key_id = hashlib.sha256(base64.b64decode(spki_b64)).hexdigest()[:16]
    with open(auth_path, "w", encoding="utf-8") as handle:
        handle.write("# test keys\n")
        handle.write(spki_b64 + "\n")

    env = dict(os.environ)
    env.update(
        {
            "MCP_PUBKEY_FILE": auth_path,
            "MCP_TOKEN": TOKEN,
            "MCP_WORKSPACE": workspace,
            "MCP_ALLOW_SHELL": "0",
        }
    )
    server = subprocess.Popen(
        [args.python, "-u", args.server, "--host", "127.0.0.1", "--port", str(args.port)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    url = "http://127.0.0.1:%d/mcp" % args.port
    passed = failed = 0

    def check(name: str, actual: int, expected: int, detail: str = "") -> None:
        nonlocal passed, failed
        if actual == expected:
            passed += 1
            print("[PASS] %s (HTTP %d)" % (name, actual))
        else:
            failed += 1
            print("[FAIL] %s: expected %d got %d %s" % (name, expected, actual, detail[:200]))

    def signed_headers(body: bytes, ts: str | None = None, nonce: str | None = None, kid: str | None = None) -> dict:
        timestamp = ts if ts is not None else str(int(time.time()))
        nonce_value = nonce if nonce is not None else base64.b16encode(os.urandom(16)).decode().lower()
        message = canonical(body, timestamp, nonce_value)
        return {
            "Content-Type": "application/json",
            "X-MCP-Key-Id": kid if kid is not None else key_id,
            "X-MCP-Timestamp": timestamp,
            "X-MCP-Nonce": nonce_value,
            "X-MCP-Signature": sign(args.openssl, key_path, message, "req"),
        }

    try:
        # wait for the server to listen
        for _ in range(50):
            try:
                with socket.create_connection(("127.0.0.1", args.port), timeout=0.5):
                    break
            except OSError:
                time.sleep(0.1)

        body = json.dumps(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            ensure_ascii=False,
        ).encode("utf-8")
        check("valid signature", post(url, body, signed_headers(body))[0], 200)

        stale = str(int(time.time()) - 3600)
        check("stale timestamp", post(url, body, signed_headers(body, ts=stale))[0], 401)

        nonce = base64.b16encode(os.urandom(16)).decode().lower()
        headers = signed_headers(body, nonce=nonce)
        check("first use of nonce", post(url, body, headers)[0], 200)
        check("nonce replay", post(url, body, headers)[0], 401)

        check("unknown key id", post(url, body, signed_headers(body, kid="0" * 16))[0], 401)

        tampered = json.dumps(
            {"jsonrpc": "2.0", "id": 2, "method": "initialize", "params": {}},
            ensure_ascii=False,
        ).encode("utf-8")
        headers = signed_headers(body)
        check("tampered body", post(url, tampered, headers)[0], 401)

        check(
            "bearer token still works",
            post(url, body, {"Content-Type": "application/json", "Authorization": "Bearer " + TOKEN})[0],
            200,
        )
        check("no auth", post(url, body, {"Content-Type": "application/json"})[0], 401)
    finally:
        server.terminate()
        try:
            server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server.kill()

    print("Summary: %d passed, %d failed" % (passed, failed))
    print("RESULT:", "PASS" if failed == 0 else "FAIL")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
