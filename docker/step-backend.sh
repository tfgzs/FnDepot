#!/bin/bash
# 在 rust:1.97.1-bookworm 容器里把 dbx-web 编成 **完全静态的 musl 二进制**
# （与上游官方 static-browser 发布物同一套做法：cargo-zigbuild + crt-static）。
# 产物：$WORK/out/$VER/bin/dbx-web-bin
#
# 为什么坚持静态 musl：
#   * 飞牛是 Debian 12（glibc 2.36），但别人家的飞牛/发行版未必 —— 静态二进制不挑 glibc；
#   * 同一个产物能直接放进 x86 与 arm 两种 fpk（本脚本按 TARGET 参数化）。
#
# 环境变量（由 build.sh 传入，全部无空格）：
#   TAG REPO VER WORK TARGET PUID PGID JOBS
set -euo pipefail

TAG="${TAG:?}"
REPO="${REPO:?}"
VER="${VER:?}"
WORK="${WORK:?}"
TARGET="${TARGET:-x86_64-unknown-linux-musl}"
PUID="${PUID:-1000}"
PGID="${PGID:-1001}"
JOBS="${JOBS:-3}"

# ★ 缓存目录由 build.sh 通过 env 指定（挂在 /cargo，位于根文件系统的无 trimacl 路径）。
#   没给就退回容器内路径 —— 那样每次都是全量重编，慢但也能出包。
export CARGO_HOME="${CARGO_HOME:-/cargo-home}"
export CARGO_TARGET_DIR="${CARGO_TARGET_DIR:-/cargo-target}"
export CARGO_NET_GIT_FETCH_WITH_CLI=true
export CARGO_NET_RETRY=10
export CARGO_BUILD_JOBS="$JOBS"
# ★ 必须加大 rustc 的线程栈：dbx-drivers 这种「巨型枚举 + 深递归类型」会让 rustc 爆栈，
#   表现是 SIGSEGV + "rustc interrupted by SIGSEGV, printing backtrace" +
#   第一次给 32MB 不够（rustc 会再要 64MB），实测 128MB 才过。
#   不加这个，本机编译到 dbx-drivers 必挂（实测：JOBS=2/1 都一样挂，跟内存无关）。
export RUST_MIN_STACK="${RUST_MIN_STACK:-1073741824}"

# 上游 `crates/*` 依赖一个 fork 的固定 commit（zipg/mysql_async）——
# GitHub 偶发 TLS 断流会让 cargo 直接放弃。兜底：本地整仓克隆后用 --config 打 path 补丁
# （只改「怎么取源码」，不改源码本身，也不动仓库里的任何文件；正常路径不会启用）。
GIT_DEP_URL="https://github.com/zipg/mysql_async.git"
GIT_DEP_NAME="mysql_async"
GIT_DEP_MIRROR="$WORK/cache/git-deps/$GIT_DEP_NAME"

mkdir -p "$WORK/logs" "$WORK/out/$VER/bin" "$CARGO_TARGET_DIR" "$CARGO_HOME"
LOG="$WORK/logs/backend-$TARGET.log"
START="$(date +%s)"

prepare_git_dep_mirror() {
    local sha
    sha="$(grep -B2 -A3 "source = \"git+$GIT_DEP_URL" "$1/Cargo.lock" 2>/dev/null \
          | sed -n 's/.*rev=\([0-9a-f]\{7,40\}\).*/\1/p' | head -1)"
    [ -n "$sha" ] || { echo "[backend] 无法从 Cargo.lock 读出 git 依赖的 rev"; return 1; }
    echo "[backend] git 依赖兜底：$GIT_DEP_NAME @ $sha"

    if [ ! -d "$GIT_DEP_MIRROR/.git" ]; then
        rm -rf "$GIT_DEP_MIRROR"
        local i
        for i in 1 2 3; do
            if git clone -q "$GIT_DEP_URL" "$GIT_DEP_MIRROR"; then break; fi
            rm -rf "$GIT_DEP_MIRROR"; sleep 10
        done
    fi
    [ -d "$GIT_DEP_MIRROR/.git" ] || { echo "[backend] 兜底失败：克隆不下来 $GIT_DEP_URL"; return 1; }

    git -C "$GIT_DEP_MIRROR" fetch -q --tags --force origin 2>/dev/null || true
    if ! git -C "$GIT_DEP_MIRROR" checkout -qf "$sha" 2>/dev/null; then
        git -C "$GIT_DEP_MIRROR" fetch -q origin "$sha" 2>/dev/null || true
        git -C "$GIT_DEP_MIRROR" checkout -qf "$sha" || { echo "[backend] 兜底失败：取不到 commit $sha"; return 1; }
    fi
    GIT_DEP_PATCH="--config"
    GIT_DEP_PATCH_VALUE="patch.\"$GIT_DEP_URL\".$GIT_DEP_NAME.path=\"$GIT_DEP_MIRROR\""
    return 0
}

{
    echo "===== DBX 后端静态编译 $(date '+%F %T') tag=$TAG target=$TARGET rustc=$(rustc -V) jobs=$JOBS ====="

    # 1) 工具链：官方发布流水线用的就是 zig cc 作 linker（cargo-zigbuild 在 PyPI 上有包）
    if ! command -v cargo-zigbuild >/dev/null 2>&1; then
        apt-get update -qq
        apt-get install -y --no-install-recommends build-essential cmake pkg-config perl python3-pip
        pip3 install --break-system-packages --quiet ziglang cargo-zigbuild
        rm -rf /var/lib/apt/lists/*
    fi
    rustup target add "$TARGET" >/dev/null 2>&1 || true

    # 2) 检出源码
    # shellcheck source=/dev/null
    . /work/docker/lib-checkout.sh
    checkout_upstream
    SRC="$WORK/src"
    chmod -R a+rX "$SRC" 2>/dev/null || true
    cd "$SRC"

    # 3) 依赖：先按正常路径抓（带重试），失败才上兜底补丁
    BUILD_EXTRA=()
    FETCH_OK=0
    for attempt in 1 2 3; do
        echo "[backend] cargo fetch 第 $attempt 次…"
        if cargo fetch --locked --target "$TARGET"; then FETCH_OK=1; break; fi
        sleep 20
    done
    if [ "$FETCH_OK" != 1 ]; then
        echo "[backend] 正常抓取依赖失败 → 启用 git 依赖兜底"
        if prepare_git_dep_mirror "$SRC"; then
            BUILD_EXTRA=("$GIT_DEP_PATCH" "$GIT_DEP_PATCH_VALUE")
            cargo fetch --target "$TARGET" "${BUILD_EXTRA[@]}" || true
        else
            echo "[backend] 失败：依赖抓不下来（网络）"; exit 1
        fi
    fi

    # 4) 编译（参数照抄上游 .github/workflows/release.yml 的 static-browser 任务）
    RUSTFLAGS="-C target-feature=+crt-static" \
        cargo zigbuild --release -p dbx-web --target "$TARGET" \
        --no-default-features --features "duckdb-sidecar,dynamodb,mq-admin,dbx-core/sqlite-bundled" \
        "${BUILD_EXTRA[@]}"

    BIN="$CARGO_TARGET_DIR/$TARGET/release/dbx-web"
    [ -x "$BIN" ] || { echo "[backend] 失败：找不到产物 $BIN"; exit 1; }
    MT=$(stat -c %Y "$BIN")
    [ "$MT" -ge "$START" ] || { echo "[backend] 失败：产物是旧的（mtime=$MT < $START）"; exit 1; }

    # 5) 静态自证：不能有 ELF interpreter，也不能有 DT_NEEDED 依赖
    if readelf -l "$BIN" | grep -q 'Requesting program interpreter'; then
        readelf -l "$BIN" | grep 'Requesting program interpreter' || true
        echo "[backend] 失败：产物不是全静态（有 program interpreter）"; exit 1
    fi
    if readelf -d "$BIN" 2>/dev/null | grep -q 'Shared library:'; then
        readelf -d "$BIN" | grep 'Shared library:' || true
        echo "[backend] 失败：产物不是全静态（有动态库依赖）"; exit 1
    fi

    cp -f "$BIN" "$WORK/out/$VER/bin/dbx-web-bin"
    chmod 755 "$WORK/out/$VER/bin/dbx-web-bin"
    git -C "$SRC" rev-parse HEAD > "$WORK/out/$VER/UPSTREAM_COMMIT"
    git -C "$SRC" describe --tags --always > "$WORK/out/$VER/UPSTREAM_DESCRIBE"
    [ "${#BUILD_EXTRA[@]}" -gt 0 ] && echo "git_dep_patched=yes" > "$WORK/out/$VER/GIT_DEP_PATCHED"
    file "$WORK/out/$VER/bin/dbx-web-bin" | tee -a "$LOG"
    echo "[backend] 完成：$(du -h "$WORK/out/$VER/bin/dbx-web-bin" | cut -f1)"
    # 产物还给 NAS 属主；编译缓存（在根文件系统的 /tmp 下）也一起 chown，方便用户随时删
    chown -R "$PUID:$PGID" "$WORK/out" 2>/dev/null || true
    chmod -R a+rX "$WORK/out" 2>/dev/null || true
    for d in "$CARGO_TARGET_DIR" "$CARGO_HOME"; do
        case "$d" in
            /work/*) : ;;   # 在挂载目录里的不用管（build.sh 已处理）
            *) chown -R "$PUID:$PGID" "$d" 2>/dev/null || true ;;
        esac
    done
} 2>&1 | tee -a "$LOG"

exit "${PIPESTATUS[0]}"
