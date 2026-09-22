#!/bin/bash
# 在 node:22-slim 容器里构建 DBX 前端（上游官方做法：pnpm install + pnpm build）。
# 产物：$WORK/out/$VER/dist/
#
# 环境变量（由 build.sh 传入，全部无空格）：
#   TAG  REPO  VER  WORK  PUID  PGID  NPM_REGISTRY
set -euo pipefail

TAG="${TAG:?}"
REPO="${REPO:?}"
VER="${VER:?}"
WORK="${WORK:?}"
PUID="${PUID:-1000}"
PGID="${PGID:-1001}"
NPM_REGISTRY="${NPM_REGISTRY:-https://registry.npmmirror.com}"

mkdir -p "$WORK/logs" "$WORK/out/$VER"
LOG="$WORK/logs/frontend.log"
START="$(date +%s)"

{
    echo "===== DBX 前端构建 $(date '+%F %T') tag=$TAG node=$(node -v) ====="

    # 1) 检出源码（node:22-slim 不带 git，先补上）
    if ! command -v git >/dev/null 2>&1; then
        apt-get update -qq && apt-get install -y --no-install-recommends git ca-certificates
        rm -rf /var/lib/apt/lists/*
    fi
    # shellcheck source=/dev/null
    . /work/docker/lib-checkout.sh
    checkout_upstream
    SRC="$WORK/src"

    # 2) /vol1 上非 root 落盘的文件常常没有权限位（ext4+trim_acl），容器里补一次，
    #    否则 vite/esbuild 这类需要执行位的工具会 EACCES。
    chmod -R a+rX "$SRC" 2>/dev/null || true

    # 3) pnpm：不要用 corepack（它固定去 registry.npmjs.org 拉 pnpm 本体，国内经常连不上 —— 实测踩过）；
    #    改成用镜像装到挂载目录里（跨次复用），版本取自 package.json 的 packageManager 字段。
    export NPM_CONFIG_PREFIX="$WORK/cache/npm-global"
    export PATH="$NPM_CONFIG_PREFIX/bin:$PATH"
    cd "$SRC"
    rm -f "$SRC/.npmrc"     # 上一次运行可能留下的 registry 配置，清掉避免脏状态（不在项目树里，只影响检出副本）
    PM_VERSION="$(sed -n 's/.*"packageManager": *"pnpm@\([0-9.]*\)".*/\1/p' package.json | head -1)"
    [ -n "$PM_VERSION" ] || PM_VERSION="10.27.0"
    if [ "$(pnpm -v 2>/dev/null || echo none)" != "$PM_VERSION" ]; then
        echo "[frontend] 用 $NPM_REGISTRY 安装 pnpm@$PM_VERSION"
        npm i -g "pnpm@$PM_VERSION" --registry="$NPM_REGISTRY" --no-audit --no-fund
    fi
    echo "[frontend] pnpm $(pnpm -v) / node $(node -v) / registry=$NPM_REGISTRY"

    # 4) 依赖 + 构建（只用环境变量传 registry，不在源码树里写 .npmrc）
    export npm_config_registry="$NPM_REGISTRY"
    for attempt in 1 2 3; do
        echo "[frontend] pnpm install 第 $attempt 次…"
        if pnpm install --frozen-lockfile; then
            break
        fi
        [ "$attempt" = 3 ] && { echo "[frontend] 失败：依赖装不上（网络）"; exit 1; }
        sleep 15
    done
    # 依赖里的预编译二进制补执行位（/vol1 落盘丢位）
    find node_modules -type f -size +100k -exec sh -c 'head -c4 "$1" | grep -q ELF && chmod +x "$1"' _ {} \; 2>/dev/null || true

    # 5) 构建。⚠️ 这台机器内存紧张时 vite/rolldown 会偶发「Segmentation fault (core dumped)」
    #    （实测：同样的镜像与代码上一次成功、这一次段错误）→ 清理产物后重试，
    #    仍失败就提示用更大的 DBX_FRONTEND_MEMORY_MB。
    for attempt in 1 2; do
        rm -rf "$SRC/dist"
        echo "[frontend] pnpm build 第 $attempt 次…"
        if pnpm build; then break; fi
        [ "$attempt" = 2 ] && { echo "[frontend] 失败：前端构建不通过（可能是内存不足，试 DBX_FRONTEND_MEMORY_MB=4096）"; exit 1; }
        sleep 10
    done

    # 6) 产物校验（存在 + 有入口 + mtime 新于本次开始时间）
    [ -f "$SRC/dist/index.html" ] || { echo "[frontend] 失败：dist/index.html 不存在"; exit 1; }
    MT=$(stat -c %Y "$SRC/dist/index.html")
    [ "$MT" -ge "$START" ] || { echo "[frontend] 失败：dist 是旧产物（mtime=$MT < $START）"; exit 1; }

    rm -rf "$WORK/out/$VER/dist"
    mkdir -p "$WORK/out/$VER"
    cp -a "$SRC/dist" "$WORK/out/$VER/dist"
    git -C "$SRC" rev-parse HEAD > "$WORK/out/$VER/UPSTREAM_COMMIT"
    git -C "$SRC" describe --tags --always > "$WORK/out/$VER/UPSTREAM_DESCRIBE"

    echo "[frontend] 完成：$(du -sh "$WORK/out/$VER/dist" | cut -f1) / $(find "$WORK/out/$VER/dist" -type f | wc -l) 个文件"
    chown -R "$PUID:$PGID" "$WORK/out" 2>/dev/null || true
    chmod -R a+rX "$WORK/out" 2>/dev/null || true
} 2>&1 | tee -a "$LOG"

exit "${PIPESTATUS[0]}"
