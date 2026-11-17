#!/usr/bin/env bash
# pocket-mcp 一键安装脚本（Linux：Ubuntu / Debian / Arch / Fedora 通用）
#
# 设计原则：服务绝不以 root 运行。脚本创建一个专用的系统用户（默认 pocketmcp），
# 用 systemd 的沙箱选项限制它能做的事，工作区之外的文件访问一律拒绝。
#
# 用法：
#   sudo ./install.sh                        # 默认 8765 端口，装成 pocketmcp 用户
#   sudo ./install.sh --port 9000            # 换端口
#   sudo ./install.sh --workspace /srv/mcp   # 指定工作区（文件工具只能读写这里）
#   sudo ./install.sh --bind 127.0.0.1       # 只监听本机（配合 SSH 隧道 / Tailscale）
#   sudo ./install.sh --allow-shell          # 允许 run_command（默认禁用，谨慎）
#   sudo ./install.sh --open-firewall        # 自动放行端口（默认只打印命令，不擅自改防火墙）
#   sudo ./install.sh --uninstall            # 卸载服务
#
# 环境变量与上面参数等价：PORT / WORKSPACE / BIND_HOST / SERVICE_USER / ALLOW_SHELL
set -euo pipefail

SERVICE_NAME="pocket-mcp"
SERVICE_USER="${SERVICE_USER:-pocketmcp}"
INSTALL_DIR="${INSTALL_DIR:-/opt/pocket-mcp}"
WORKSPACE="${WORKSPACE:-/srv/pocket-mcp-workspace}"
PORT="${PORT:-8765}"
BIND_HOST="${BIND_HOST:-0.0.0.0}"
ALLOW_SHELL="${ALLOW_SHELL:-0}"
OPEN_FIREWALL="${OPEN_FIREWALL:-0}"
PYTHON_BIN="${PYTHON_BIN:-}"
DO_UNINSTALL=0

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UNIT_PATH="/etc/systemd/system/${SERVICE_NAME}.service"

die() { printf '\033[31m错误：%s\033[0m\n' "$*" >&2; exit 1; }
info() { printf '\033[36m==> %s\033[0m\n' "$*"; }
ok() { printf '\033[32m ok \033[0m %s\n' "$*"; }

usage() {
  sed -n '2,/^set /p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//' | sed '/^set /d' | sed '/^$/N;/^\n$/D'
  exit 0
}

while [ $# -gt 0 ]; do
  case "$1" in
    --port) PORT="${2:?缺少端口值}"; shift 2 ;;
    --bind|--host) BIND_HOST="${2:?缺少地址}"; shift 2 ;;
    --workspace) WORKSPACE="${2:?缺少目录}"; shift 2 ;;
    --user) SERVICE_USER="${2:?缺少用户名}"; shift 2 ;;
    --allow-shell) ALLOW_SHELL=1; shift ;;
    --open-firewall) OPEN_FIREWALL=1; shift ;;
    --uninstall) DO_UNINSTALL=1; shift ;;
    -h|--help) usage ;;
    *) die "未知参数：$1（--help 查看用法）" ;;
  esac
done

need_root() { [ "$(id -u)" -eq 0 ] || die "需要 root 权限，请用 sudo 运行"; }

find_python() {
  local candidate resolved
  for candidate in "$PYTHON_BIN" python3 python3.13 python3.12 python3.11 python3.10 python3.9 python3.8 python; do
    [ -n "$candidate" ] || continue
    resolved="$(command -v "$candidate" 2>/dev/null || true)"
    [ -n "$resolved" ] || continue
    if "$resolved" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 8) else 1)' 2>/dev/null; then
      printf '%s' "$resolved"
      return 0
    fi
  done
  return 1
}

uninstall() {
  need_root
  info "停止并移除 ${SERVICE_NAME}"
  systemctl stop "${SERVICE_NAME}" 2>/dev/null || true
  systemctl disable "${SERVICE_NAME}" 2>/dev/null || true
  rm -f "$UNIT_PATH"
  systemctl daemon-reload
  if id "$SERVICE_USER" >/dev/null 2>&1; then
    userdel "$SERVICE_USER" 2>/dev/null || true
    ok "已删除用户 $SERVICE_USER"
  fi
  info "已卸载。目录 $INSTALL_DIR 与 $WORKSPACE 保留，确认不需要后手动删除。"
  exit 0
}

probe() {
  local python_bin="$1"
  "$python_bin" - "$PORT" <<'PY'
import json, sys, urllib.request
port = int(sys.argv[1])
body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                   "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                              "clientInfo": {"name": "install-check", "version": "1"}}}).encode()
req = urllib.request.Request(f"http://127.0.0.1:{port}/mcp", data=body,
                             headers={"Content-Type": "application/json"}, method="POST")
with urllib.request.urlopen(req, timeout=5) as resp:
    data = json.loads(resp.read().decode("utf-8"))
print(data["result"]["serverInfo"]["name"])
PY
}

[ "$DO_UNINSTALL" -eq 1 ] && uninstall
need_root

command -v systemctl >/dev/null 2>&1 || die "没有 systemd，本脚本不适用（可用 python3 server.py 手动跑）"
PYTHON="$(find_python)" || die "没找到 Python 3.8+。Ubuntu/Debian: apt install python3；Arch: pacman -S python；Fedora: dnf install python3"

info "Python: $PYTHON ($("$PYTHON" -c 'import sys; print(".".join(map(str, sys.version_info[:3])))'))"
info "服务用户: $SERVICE_USER（非 root） / 端口: $PORT / 工作区: $WORKSPACE"

# 1. 专用系统用户（不允许登录、没有 home 以外的东西）
if id "$SERVICE_USER" >/dev/null 2>&1; then
  ok "用户 $SERVICE_USER 已存在，复用"
else
  if command -v useradd >/dev/null 2>&1; then
    useradd --system --home-dir "$INSTALL_DIR" --no-create-home \
      --shell /usr/sbin/nologin "$SERVICE_USER"
  else
    adduser --system --home "$INSTALL_DIR" --no-create-home --shell /usr/sbin/nologin "$SERVICE_USER"
  fi
  ok "已创建系统用户 $SERVICE_USER"
fi

# 2. 目录与代码
install -d -m 0755 "$INSTALL_DIR"
install -d -m 0750 -o "$SERVICE_USER" -g "$SERVICE_USER" "$WORKSPACE"
install -m 0644 "$SRC_DIR/server.py" "$INSTALL_DIR/server.py"
[ -f "$SRC_DIR/smoke_test.py" ] && install -m 0644 "$SRC_DIR/smoke_test.py" "$INSTALL_DIR/smoke_test.py"
ok "已安装 server.py 到 $INSTALL_DIR"
ok "工作区 $WORKSPACE（服务只在这棵目录里读写）"

# 3. systemd 单元：非 root + 沙箱
cat > "$UNIT_PATH" <<EOF
[Unit]
Description=pocket-mcp (MCP server for mobile clients)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$SERVICE_USER
Group=$SERVICE_USER
WorkingDirectory=$INSTALL_DIR
Environment=MCP_WORKSPACE=$WORKSPACE
Environment=MCP_ALLOW_SHELL=$ALLOW_SHELL
ExecStart=$PYTHON -u $INSTALL_DIR/server.py --host $BIND_HOST --port $PORT
Restart=on-failure
RestartSec=2
UMask=0077
# --- 安全加固：即使被利用也拿不到系统权限 ---
NoNewPrivileges=true
PrivateTmp=true
PrivateDevices=true
ProtectSystem=strict
ProtectHome=true
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
ProtectClock=true
ProtectHostname=true
ReadWritePaths=$WORKSPACE
RestrictSUIDSGID=true
RestrictRealtime=true
LockPersonality=true
MemoryDenyWriteExecute=true
CapabilityBoundingSet=
AmbientCapabilities=
SystemCallArchitectures=native

[Install]
WantedBy=multi-user.target
EOF
chmod 0644 "$UNIT_PATH"
systemctl daemon-reload
systemctl enable --now "$SERVICE_NAME" >/dev/null
ok "systemd 服务已启用：$SERVICE_NAME"

# 4. 启动自检
for _ in $(seq 1 20); do
  if probe "$PYTHON" >/dev/null 2>&1; then
    ok "自检通过：POST http://127.0.0.1:$PORT/mcp 能完成 initialize"
    break
  fi
  sleep 0.5
done
if ! probe "$PYTHON" >/dev/null 2>&1; then
  echo
  die "服务启动了但自检失败，最近日志如下：
$(journalctl -u "$SERVICE_NAME" -n 20 --no-pager 2>/dev/null || true)"
fi

# 5. 防火墙：默认只给提示，不擅自改
if [ "$OPEN_FIREWALL" -eq 1 ]; then
  if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q "Status: active"; then
    ufw allow "$PORT"/tcp >/dev/null && ok "ufw 已放行 $PORT/tcp"
  elif command -v firewall-cmd >/dev/null 2>&1 && firewall-cmd --state >/dev/null 2>&1; then
    firewall-cmd --permanent --add-port="$PORT"/tcp >/dev/null
    firewall-cmd --reload >/dev/null && ok "firewalld 已放行 $PORT/tcp"
  else
    echo "  没检测到启用的 ufw/firewalld，请自行确认云厂商安全组。"
  fi
else
  echo
  echo "防火墙还没动。按需放行（建议只放行内网网段，别对全网暴露）："
  echo "  ufw:        sudo ufw allow from 192.168.0.0/16 to any port $PORT proto tcp"
  echo "  firewalld:  sudo firewall-cmd --permanent --add-rich-rule='rule family=ipv4 source address=192.168.0.0/16 port port=$PORT protocol=tcp accept' && sudo firewall-cmd --reload"
  echo "  云服务器还需在安全组里放行 $PORT/tcp。"
  echo "  更稳的做法：--bind 127.0.0.1 只监听本机，用 SSH 隧道或 Tailscale 访问。"
fi

cat <<EOF

安装完成。

  服务状态：  systemctl status $SERVICE_NAME --no-pager
  实时日志：  journalctl -u $SERVICE_NAME -f
  自检脚本：  $PYTHON $INSTALL_DIR/smoke_test.py http://127.0.0.1:$PORT
  卸载：      sudo ./install.sh --uninstall

手机客户端里填：http://<本机IP>:$PORT/mcp（MCP 地址），端口 $PORT。
run_command 当前为 $( [ "$ALLOW_SHELL" -eq 1 ] && echo "已启用（该用户能执行命令，务必保持低权限）" || echo "禁用（要启用请重跑 --allow-shell）")。
EOF
