#!/usr/bin/env bash
# pocket-mcp 直接运行脚本（不需要 root、不需要安装）
#
# 最快用法（一条命令，自动下载并运行）：
#   curl -fsSL https://raw.githubusercontent.com/YYUN64/pocket-mcp/main/run.sh | bash
#
# 本地已 clone 时：
#   ./run.sh                      # 前台运行，默认 0.0.0.0:8765
#   ./run.sh --port 9000
#   ./run.sh --daemon             # 后台运行，日志在 ~/.local/share/pocket-mcp/server.log
#   ./run.sh --stop               # 停止后台实例
#   ./run.sh --workspace ~/notes  # 文件工具只能访问这个目录
#
# 全部以当前用户身份运行：不需要 sudo，也不会创建任何系统用户或写入 /etc。
set -euo pipefail

PORT="${PORT:-8765}"
BIND_HOST="${BIND_HOST:-0.0.0.0}"
WORKSPACE="${WORKSPACE:-$HOME/pocket-mcp-workspace}"
ALLOW_SHELL="${ALLOW_SHELL:-0}"
DATA_DIR="${POCKET_MCP_HOME:-$HOME/.local/share/pocket-mcp}"
REPO_RAW="${REPO_RAW:-https://raw.githubusercontent.com/YYUN64/pocket-mcp/main}"
PYTHON_BIN="${PYTHON_BIN:-}"
MODE="run"

# 被 `curl ... | bash` 调用时 $0 是 "bash"，提示里给个合理的命令名
SELF="${BASH_SOURCE[0]:-$0}"
[ "$SELF" = "bash" ] && SELF="run.sh"

die() { printf '\033[31m错误：%s\033[0m\n' "$*" >&2; exit 1; }
info() { printf '\033[36m==> %s\033[0m\n' "$*"; }

usage() {
  sed -n '2,/^set /p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//' | sed '/^set /d'
  exit 0
}

while [ $# -gt 0 ]; do
  case "$1" in
    --port) PORT="${2:?缺少端口}"; shift 2 ;;
    --bind|--host) BIND_HOST="${2:?缺少地址}"; shift 2 ;;
    --workspace) WORKSPACE="${2:?缺少目录}"; shift 2 ;;
    --allow-shell) ALLOW_SHELL=1; shift ;;
    --daemon) MODE="daemon"; shift ;;
    --stop) MODE="stop"; shift ;;
    -h|--help) usage ;;
    *) die "未知参数：$1（--help 查看用法）" ;;
  esac
done

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

# 定位 server.py：优先用脚本旁边的，否则下载到用户目录
resolve_server() {
  local here
  here="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || true)"
  if [ -n "$here" ] && [ -f "$here/server.py" ]; then
    printf '%s' "$here/server.py"
    return 0
  fi
  mkdir -p "$DATA_DIR"
  if [ ! -f "$DATA_DIR/server.py" ]; then
    info "下载 server.py 到 $DATA_DIR"
    if command -v curl >/dev/null 2>&1; then
      curl -fsSL "$REPO_RAW/server.py" -o "$DATA_DIR/server.py.part" || die "下载失败（检查网络或 REPO_RAW）"
    elif command -v wget >/dev/null 2>&1; then
      wget -qO "$DATA_DIR/server.py.part" "$REPO_RAW/server.py" || die "下载失败（检查网络或 REPO_RAW）"
    else
      die "需要 curl 或 wget 才能下载 server.py"
    fi
    mv "$DATA_DIR/server.py.part" "$DATA_DIR/server.py"
  fi
  printf '%s' "$DATA_DIR/server.py"
}

if [ "$MODE" = "stop" ]; then
  # 只杀监听这个端口的实例，避免误伤别的 pocket-mcp
  pids="$(pgrep -f "server.py --host .*--port $PORT" || true)"
  if [ -z "$pids" ]; then
    echo "端口 $PORT 上没有在运行的后台实例"
  else
    # shellcheck disable=SC2086
    kill $pids && echo "已停止：$pids"
  fi
  exit 0
fi

PYTHON="$(find_python)" || die "需要 Python 3.8+。Ubuntu/Debian: sudo apt install python3；Arch: sudo pacman -S python；Fedora: sudo dnf install python3；Windows: 装 Python 后用它自带的 python"
SERVER="$(resolve_server)"
mkdir -p "$WORKSPACE"

info "Python: $PYTHON / 端口: $PORT / 工作区: $WORKSPACE / 用户: $(id -un)"

if [ "$MODE" = "daemon" ]; then
  mkdir -p "$DATA_DIR"
  LOG="$DATA_DIR/server.log"
  if pgrep -f "server.py --host .*--port $PORT" >/dev/null 2>&1; then
    die "端口 $PORT 上已有实例在跑（先 $SELF --port $PORT --stop）"
  fi
  MCP_WORKSPACE="$WORKSPACE" MCP_ALLOW_SHELL="$ALLOW_SHELL" \
    nohup "$PYTHON" -u "$SERVER" --host "$BIND_HOST" --port "$PORT" >> "$LOG" 2>&1 &
  sleep 1
  info "已在后台启动，日志：$LOG"
  echo "  停止：$SELF --port $PORT --stop"
else
  info "前台运行中，Ctrl+C 停止"
  exec env MCP_WORKSPACE="$WORKSPACE" MCP_ALLOW_SHELL="$ALLOW_SHELL" \
    "$PYTHON" -u "$SERVER" --host "$BIND_HOST" --port "$PORT"
fi
