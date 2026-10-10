#!/usr/bin/env bash
# cbs 安装脚本 / cbs installer
# 将 cbs.py 与 credits.py 安装到 ~/.cbs/ 并软链到 ~/.local/bin/cbs。
# Installs cbs.py and credits.py into ~/.cbs/ and symlinks ~/.local/bin/cbs.

set -euo pipefail

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET_DIR="${CBS_HOME:-$HOME/.cbs}"
# CBS_BIN_DIR 一并覆盖，避免测试时动到真实的 ~/.local/bin/cbs
BIN_DIR="${CBS_BIN_DIR:-$HOME/.local/bin}"

info() { printf '\033[36m==>\033[0m %s\n' "$1"; }
warn() { printf '\033[33m警告:\033[0m %s\n' "$1"; }
fail() { printf '\033[31m错误:\033[0m %s\n' "$1" >&2; exit 1; }

# --- 前置检查 / prerequisites ---
command -v python3 >/dev/null || fail "需要 python3 / python3 is required"

PY_VER=$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')
case "$PY_VER" in
  3.[7-9]|3.1[0-9]) ;;
  *) warn "检测到 Python $PY_VER，建议 3.7+ / detected $PY_VER, 3.7+ recommended" ;;
esac
info "Python $PY_VER"

[ -f "$SRC_DIR/cbs.py" ] || fail "缺少 cbs.py / cbs.py not found in $SRC_DIR"
[ -f "$SRC_DIR/credits.py" ] || fail "缺少 credits.py（必须与 cbs.py 同目录）/ credits.py must sit next to cbs.py"

# --- 现有安装备份 / back up existing install ---
if [ -f "$TARGET_DIR/cbs.py" ] && ! cmp -s "$SRC_DIR/cbs.py" "$TARGET_DIR/cbs.py"; then
  BACKUP="$TARGET_DIR/cbs.py.bak.$(date +%Y%m%d%H%M%S)"
  cp "$TARGET_DIR/cbs.py" "$BACKUP"
  info "已备份现有 cbs.py -> $BACKUP"
fi

# --- 安装 / install ---
mkdir -p "$TARGET_DIR"
chmod 700 "$TARGET_DIR"
install -m 755 "$SRC_DIR/cbs.py" "$TARGET_DIR/cbs.py"
install -m 644 "$SRC_DIR/credits.py" "$TARGET_DIR/credits.py"
info "已安装到 $TARGET_DIR"

mkdir -p "$BIN_DIR"
ln -sf "$TARGET_DIR/cbs.py" "$BIN_DIR/cbs"
info "已软链 $BIN_DIR/cbs"

# --- PATH 检查 / PATH check ---
if ! echo ":$PATH:" | grep -q ":$BIN_DIR:"; then
  warn "$BIN_DIR 不在 PATH 中 / not in PATH"
  warn "添加这行到 ~/.bashrc / add this line to ~/.bashrc:"
  printf '    export PATH="$HOME/.local/bin:$PATH"\n'
fi

# --- 自检 / smoke test ---
if python3 -c "import ast,sys; [ast.parse(open(p).read()) for p in ('$TARGET_DIR/cbs.py','$TARGET_DIR/credits.py')]" 2>/dev/null; then
  info "语法检查通过 / syntax OK"
else
  fail "语法检查失败 / syntax check failed"
fi

echo
if "$BIN_DIR/cbs" --help >/dev/null 2>&1; then
  info "安装完成，试运行 / installed, try:"
  echo
  echo "    cbs ls          # 查看账号与可用积分 / list accounts and credits"
  echo "    cbs doctor      # 环境自检 / check environment"
else
  warn "自检未通过，但文件已安装。可直接试运行 cbs --help"
fi