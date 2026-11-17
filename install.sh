#!/usr/bin/env bash
# pocket-mcp 安装脚本（Linux：Ubuntu / Debian / Arch / Fedora 通用）
#
# 两种模式：
#   ./install.sh --user      只装给当前用户，不需要 sudo（用户级 systemd）
#   sudo ./install.sh        装成系统服务，专用非 root 用户 + 沙箱（默认）
#
# 只想跑起来、不装服务？直接用 ./run.sh（完全不需要 root）。
#
# 用法：
#   ./install.sh --user [--port 8765] [--workspace ~/mcp] [--allow-shell]
#   sudo ./install.sh [--port 8765] [--workspace /srv/mcp] [--bind 127.0.0.1]
#   ./install.sh --uninstall            # 按之前的模式卸载（--user 需带 --user）
set -euo pipefail

SERVICE_NAME="pocket-mcp"
SERVICE_USER="${SERVICE_USER:-pocketmcp}"
INSTALL_DIR="${INSTALL_DIR:-}"
WORKSPACE="${WORKSPACE:-}"
PORT="${PORT:-8765}"
BIND_HOST="${BIND_HOST:-0.0.0.0}"
ALLOW_SHELL="${ALLOW_SHELL:-0}"
OPEN_FIREWALL="${OPEN_FIREWALL:-0}"
PYTHON_BIN="${PYTHON_BIN:-}"
DO_UNINSTALL=0
USER_MODE=0

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

die() { printf '\033[31m错误：%s\033[0m\n' "$*" >&2; exit 1; }
info() { printf '\033[36m==> %s\033[0m\n' "$*"; }
ok() { printf '\033[32m ok \033[0m %s\n' "$*"; }

usage() {
  sed -n '2,/^set /p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//' | sed '/^set /d'
  exit 0
}

while [ $# -gt 0 ]; do
  case "$1" in
    --user) USER_MODE=1; shift ;;
    --port) PORT="${2:?缺少端口值}"; shift 2 ;;
    --bind|--host) BIND_HOST="${2:?缺少地址}"; shift 2 ;;
    --workspace) WORKSPACE="${2:?缺少目录}"; shift 2 ;;
    --allow-shell) ALLOW_SHELL=1; shift ;;
    --open-firewall) OPEN_FIREWALL=1; shift ;;
    --uninstall) DO_UNINSTALL=1; shift ;;
    -h|--help) usage ;;
    *) die "未知参数：$1（--help 查看用法）" ;;
  esac
done

if [ "$USER_MODE" -eq 1 ]; then
  INSTALL_DIR="${INSTALL_DIR:-$HOME/.local/share/pocket-mcp}"
  WORKSPACE="${WORKSPACE:-$HOME/pocket-mcp-workspace}"
  UNIT_PATH="$HOME/.config/systemd/user/${SERVICE_NAME}.service"
  SYSTEMCTL=(systemctl --user)
else
  INSTALL_DIR="${INSTALL_DIR:-/opt/pocket-mcp}"
  WORKSPACE="${WORKSPACE:-/srv/pocket-mcp-workspace}"
  UNIT_PATH="/etc/systemd/system/${SERVICE_NAME}.service"
  SYSTEMCTL=(systemctl)
fi

need_root() { [ "$(id -u)" -eq 0 ] || die "系统级安装需要 root，请用 sudo；或者用 --user 装成用户级服务（不需要 sudo）"; }

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

uninstall() {
  [ "$USER_MODE" -eq 1 ] || need_root
  info "停止并移除 ${SERVICE_NAME}$( [ "$USER_MODE" -eq 1 ] && echo "（用户级）")"
  "${SYSTEMCTL[@]}" stop "${SERVICE_NAME}" 2>/dev/null || true
  "${SYSTEMCTL[@]}" disable "${SERVICE_NAME}" 2>/dev/null || true
  rm -f "$UNIT_PATH"
  "${SYSTEMCTL[@]}" daemon-reload 2>/dev/null || true
  if [ "$USER_MODE" -eq 0 ] && id "$SERVICE_USER" >/dev/null 2>&1; then
    userdel "$SERVICE_USER" 2>/dev/null || true
    ok "已删除系统用户 $SERVICE_USER"
  fi
  info "已卸载。目录 $INSTALL_DIR 与 $WORKSPACE 保留，确认不需要后手动删除。"
  exit 0
}

[ "$DO_UNINSTALL" -eq 1 ] && uninstall
[ "$USER_MODE" -eq 1 ] || need_root

command -v systemctl >/dev/null 2>&1 || die "没有 systemd，本脚本不适用（直接用 ./run.sh 运行）"
if [ "$USER_MODE" -eq 1 ] && ! "${SYSTEMCTL[@]}" show-environment >/dev/null 2>&1; then
  die "当前用户没有 systemd 用户实例（缺 DBUS_SESSION_BUS_ADDRESS？）。可用 ./run.sh --daemon 代替"
fi

PYTHON="$(find_python)" || die "没找到 Python 3.8+。Ubuntu/Debian: apt install python3；Arch: pacman -S python；Fedora: dnf install python3"
info "Python: $PYTHON ($("$PYTHON" -c 'import sys; print(".".join(map(str, sys.version_info[:3])))'))"
if [ "$USER_MODE" -eq 1 ]; then
  info "模式: 用户级（无 sudo） / 端口: $PORT / 工作区: $WORKSPACE"
else
  info "模式: 系统级 / 用户: $SERVICE_USER（非 root） / 端口: $PORT / 工作区: $WORKSPACE"
fi

# 1. 系统级模式才建专用用户
if [ "$USER_MODE" -eq 0 ]; then
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
fi

# 2. 目录与代码
if [ "$USER_MODE" -eq 1 ]; then
  mkdir -p "$INSTALL_DIR" "$WORKSPACE"
else
  install -d -m 0755 "$INSTALL_DIR"
  install -d -m 0750 -o "$SERVICE_USER" -g "$SERVICE_USER" "$WORKSPACE"
fi
install -m 0644 "$SRC_DIR/server.py" "$INSTALL_DIR/server.py"
[ -f "$SRC_DIR/smoke_test.py" ] && install -m 0644 "$SRC_DIR/smoke_test.py" "$INSTALL_DIR/smoke_test.py"
ok "已安装 server.py 到 $INSTALL_DIR"
ok "工作区 $WORKSPACE（服务只在这棵目录里读写）"

# 3. systemd 单元
if [ "$USER_MODE" -eq 1 ]; then
  UNIT_HEADER="[Unit]
Description=pocket-mcp (MCP server, user service)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$INSTALL_DIR"
  PROTECT_HOME="ProtectHome=read-only"
  # 用户级 systemd 里普通用户没有 CAP_SETPCAP，写 CapabilityBoundingSet= 会让
  # 启动直接失败（status=218/CAPABILITIES）。用户服务本来就没有 capability，跳过这两项。
  EXTRA_HARDENING=""
else
  UNIT_HEADER="[Unit]
Description=pocket-mcp (MCP server for mobile clients)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$SERVICE_USER
Group=$SERVICE_USER
WorkingDirectory=$INSTALL_DIR"
  PROTECT_HOME="ProtectHome=true"
  EXTRA_HARDENING="CapabilityBoundingSet=
AmbientCapabilities="
fi

mkdir -p "$(dirname "$UNIT_PATH")"
cat > "$UNIT_PATH" <<EOF
$UNIT_HEADER
Environment=MCP_WORKSPACE=$WORKSPACE
Environment=MCP_ALLOW_SHELL=$ALLOW_SHELL
ExecStart=$PYTHON -u $INSTALL_DIR/server.py --host $BIND_HOST --port $PORT
Restart=on-failure
RestartSec=2
UMask=0077
# --- 安全加固 ---
NoNewPrivileges=true
PrivateTmp=true
PrivateDevices=true
ProtectSystem=strict
$PROTECT_HOME
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
ProtectClock=true
ProtectHostname=true
ReadWritePaths=$WORKSPACE $INSTALL_DIR
RestrictSUIDSGID=true
RestrictRealtime=true
LockPersonality=true
MemoryDenyWriteExecute=true
$EXTRA_HARDENING
SystemCallArchitectures=native

[Install]
WantedBy=default.target
EOF
chmod 0644 "$UNIT_PATH"
"${SYSTEMCTL[@]}" daemon-reload
"${SYSTEMCTL[@]}" enable --now "$SERVICE_NAME" >/dev/null
ok "systemd 已启用：$SERVICE_NAME$( [ "$USER_MODE" -eq 1 ] && echo "（用户级）")"

# 4. 启动自检
READY=0
for _ in $(seq 1 20); do
  if probe "$PYTHON" >/dev/null 2>&1; then READY=1; break; fi
  sleep 0.5
done
if [ "$READY" -eq 0 ]; then
  echo
  die "服务启动了但自检失败，最近日志：
$("${SYSTEMCTL[@]}" status "$SERVICE_NAME" --no-pager -l 2>/dev/null | tail -20 || true)"
fi
ok "自检通过：POST http://127.0.0.1:$PORT/mcp 能完成 initialize"

# 5. 防火墙（仅系统级模式有意义）
if [ "$USER_MODE" -eq 0 ]; then
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
    echo "防火墙还没动。按需放行（建议只放行内网网段）："
    echo "  ufw:        sudo ufw allow from 192.168.0.0/16 to any port $PORT proto tcp"
    echo "  firewalld:  sudo firewall-cmd --permanent --add-rich-rule='rule family=ipv4 source address=192.168.0.0/16 port port=$PORT protocol=tcp accept' && sudo firewall-cmd --reload"
    echo "  云服务器还需在安全组里放行 $PORT/tcp。更稳：--bind 127.0.0.1 + SSH 隧道/Tailscale。"
  fi
fi

cat <<EOF

安装完成。

  状态：  $("${SYSTEMCTL[@]}" --version >/dev/null 2>&1 && echo "${SYSTEMCTL[*]} status $SERVICE_NAME --no-pager")
  日志：  $( [ "$USER_MODE" -eq 1 ] && echo "journalctl --user -u $SERVICE_NAME -f" || echo "journalctl -u $SERVICE_NAME -f" )
  自检：  $PYTHON $INSTALL_DIR/smoke_test.py http://127.0.0.1:$PORT
  卸载：  $( [ "$USER_MODE" -eq 1 ] && echo "./install.sh --user --uninstall" || echo "sudo ./install.sh --uninstall" )
$( [ "$USER_MODE" -eq 1 ] && printf '\n想让用户服务开机自启（不登录也跑）：loginctl enable-linger %s\n' "$(id -un)" )

手机客户端填：http://<本机IP>:$PORT/mcp
run_command 当前：$( [ "$ALLOW_SHELL" -eq 1 ] && echo "已启用（该用户能执行命令，保持低权限）" || echo "禁用（要启用加 --allow-shell）" )
EOF
