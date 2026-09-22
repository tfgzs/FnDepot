#!/bin/bash
# =============================================================================
# DBX 飞牛版（fnOS）构建管线
#
# 一条命令完成：查上游最新版本 → 取/编译 dbx-web（静态 musl）→ 组装 fpk → 自检 → 出包
#
#   用法示例：
#     bash build.sh                     # 上游最新版，本机（x86）源码编译并打包
#     bash build.sh --mode release      # 秒级：用上游官方 static 产物（校验 sha256 后打包）
#     bash build.sh --version v0.6.19   # 指定版本
#     bash build.sh --arch arm64        # 给 ARM 飞牛出包
#     bash build.sh --check             # 只回答「上游有没有新版本」，供定时任务用
#
#   两种模式都产出**同一种产物**：全静态 musl 的 dbx-web + 前端 dist，装进 fnOS fpk。
#     release = 用上游官方 static-browser 发布物（sha256 校验 + ELF 静态校验后重打包）
#     source  = 自己用官方流水线同款参数编译（cargo-zigbuild + crt-static）
# =============================================================================
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORK="$ROOT/build/work"
PKG_ROOT="$ROOT/build/pkg"
DIST_DIR="$ROOT/dist"
STATE="$ROOT/build/state.json"
LOGDIR="$ROOT/build/logs"

REPO_URL="https://github.com/t8y2/dbx.git"
REPO_SLUG="t8y2/dbx"
APPNAME="dbx"
DEFAULT_PORT=4224
PUID="${DBX_BUILD_PUID:-$(id -u)}"
PGID="${DBX_BUILD_PGID:-$(id -g)}"

MODE="source"
ARCH="x64"
VERSION=""
SKIP_BUILD=0
NO_PACKAGE=0
CHECK_ONLY=0
DO_INSTALL=0
INSTALL_PORT="$DEFAULT_PORT"
FORCE=0
PACKER="${DBX_PACKER:-auto}"

while [ $# -gt 0 ]; do
    case "$1" in
        --mode) MODE="$2"; shift 2 ;;
        --arch) ARCH="$2"; shift 2 ;;
        --version) VERSION="$2"; shift 2 ;;
        --skip-build) SKIP_BUILD=1; shift ;;
        --no-package) NO_PACKAGE=1; shift ;;
        --check) CHECK_ONLY=1; shift ;;
        --install) DO_INSTALL=1; shift ;;
        --install-port) INSTALL_PORT="$2"; shift 2 ;;
        --force) FORCE=1; shift ;;
        --packer) PACKER="$2"; shift 2 ;;
        -h|--help) sed -n '2,25p' "$0"; exit 0 ;;
        *) echo "未知参数：$1（--help 看用法）" >&2; exit 2 ;;
    esac
done

case "$MODE" in auto|release|source) ;; *) echo "--mode 只能是 auto|release|source" >&2; exit 2 ;; esac
case "$PACKER" in auto|fnpack|native) ;; *) echo "--packer 只能是 auto|fnpack|native" >&2; exit 2 ;; esac
case "$ARCH" in
    x64)   FPK_PLATFORM="x86";  RUST_TARGET="x86_64-unknown-linux-musl"; ELF_ARCH="x86_64" ;;
    arm64) FPK_PLATFORM="arm";  RUST_TARGET="aarch64-unknown-linux-musl"; ELF_ARCH="aarch64" ;;
    *) echo "--arch 只能是 x64|arm64" >&2; exit 2 ;;
esac

mkdir -p "$WORK" "$LOGDIR" "$DIST_DIR" "$PKG_ROOT"

log() { printf '[build] %s\n' "$*"; }
die() { printf '[build] 失败：%s\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------- 编译缓存位置
# ⚠️ cargo 的 target 目录**不能**放在 /vol1（本项目目录）里：
#    飞牛所有数据卷都是 ext4+trimacl 挂载，进程新建的文件权限位会被抹成 000，
#    rustc 会因为「output file ... is not writeable」直接编译失败（已最小化复现：
#    同一个 crate 换个 target 目录就正常）。所以缓存放到根文件系统上的 /tmp（无 trimacl），
#    用 bind mount 挂进容器；里面的文件在构建结束时会 chown 回 NAS 属主，随时可删。
CARGO_CACHE_HOST="${DBX_CARGO_CACHE_HOST:-/tmp/dbx-cargo}"
CARGO_CACHE_MIN_FREE_GB="${DBX_CARGO_CACHE_MIN_FREE_GB:-12}"

prepare_cargo_cache() {
    mkdir -p "$CARGO_CACHE_HOST" 2>/dev/null || die "无法创建编译缓存目录 $CARGO_CACHE_HOST"
    chmod 755 "$CARGO_CACHE_HOST" 2>/dev/null || true
    local avail_gb
    avail_gb="$(df -BG --output=avail / | tail -1 | tr -dc '0-9')"
    if [ -n "$avail_gb" ] && [ "$avail_gb" -lt "$CARGO_CACHE_MIN_FREE_GB" ]; then
        log "根分区可用仅 ${avail_gb}G（< ${CARGO_CACHE_MIN_FREE_GB}G）→ 清掉编译缓存腾地方"
        rm -rf "$CARGO_CACHE_HOST"/* 2>/dev/null || true
    fi
    # 缓存目录里如果有 root 属主的文件（上次构建留下的），需要特权才能删 —— 交给容器里的脚本处理
}

# ---------------------------------------------------------------- 1. 版本解析
resolve_version() {
    if [ -n "$VERSION" ]; then
        TAG="$VERSION"
        case "$TAG" in v*) ;; *) TAG="v$TAG" ;; esac
        VER="${TAG#v}"
        return
    fi
    local json
    json="$(curl -fsSL --max-time 60 "https://api.github.com/repos/$REPO_SLUG/releases/latest")" \
        || die "拉取上游最新版本失败（网络？）"
    TAG="$(printf '%s' "$json" | python3 -c 'import json,sys;print(json.load(sys.stdin)["tag_name"])')"
    VER="${TAG#v}"
}

# 记录 tag 对应的 commit（溯源用）。注意注解 tag：/git/ref/tags/<tag> 返回的是 tag 对象，
# 要再取一层才是 commit（否则溯源文件里会写 unknown）。
resolve_commit() {
    UPSTREAM_COMMIT="$(curl -fsSL --max-time 60 \
        "https://api.github.com/repos/$REPO_SLUG/git/ref/tags/$TAG" 2>/dev/null \
        | python3 -c '
import json, sys, urllib.request
try:
    d = json.load(sys.stdin)
except Exception:
    print(""); raise SystemExit
o = d.get("object") or {}
sha, kind = o.get("sha", ""), o.get("type", "")
if kind == "commit":
    print(sha)
elif kind == "tag" and o.get("url"):
    try:
        t = json.load(urllib.request.urlopen(o["url"], timeout=30))
        print((t.get("object") or {}).get("sha", ""))
    except Exception:
        print("")
else:
    print("")
' || true)"
}

read_state() {  # 输出：<version> <arch> <mode> <fpk_sha256>
    [ -f "$STATE" ] || { echo ""; return; }
    python3 - "$STATE" <<'PY'
import json,sys
try:
    d=json.load(open(sys.argv[1]))
except Exception:
    print(""); raise SystemExit
print(d.get("version",""), d.get("arch",""), d.get("mode",""), d.get("fpk_sha256",""))
PY
}

log "上游版本解析…"
resolve_version
log "上游最新：$TAG（DBX $VER）｜ 模式=$MODE 架构=$ARCH（fpk platform=$FPK_PLATFORM）"

if [ "$CHECK_ONLY" = 1 ]; then
    read -r SVER SARCH SMODE SSHA _rest < <(read_state; echo)
    printf 'upstream=%s installed_state=%s arch_state=%s\n' "$TAG" "${SVER:-none}" "${SARCH:-none}"
    if [ -n "$SVER" ] && [ "$SVER" = "$VER" ] && [ "$SARCH" = "$ARCH" ]; then
        exit 10   # 10 = 已是最新（cron 据此静默）
    fi
    exit 0        # 0 = 有新版本
fi

resolve_commit

# ------------------------------------------------- 2. 取得可打包的产物（bin+dist）
OUT="$WORK/out/$VER"
have_artifacts() {
    [ -x "$OUT/bin/dbx-web-bin" ] && [ -f "$OUT/dist/index.html" ] && [ -f "$OUT/UPSTREAM_COMMIT" ]
}

fetch_release_assets() {
    local asset="DBX_${VER}_${ARCH}-browser-static.tar.gz"
    local url="https://github.com/$REPO_SLUG/releases/download/$TAG/$asset"
    local dl="$WORK/dl"
    mkdir -p "$dl" "$OUT"

    log "release 模式：下载官方静态产物 $asset"
    # 大文件走 GitHub 会偶发断流 → 重试；已有完整文件（官方 sha256 对得上）就直接复用。
    # ⚠️ 不要在「文件已完整」时用 -C - 续传：GitHub 会挂住连接，白等十几分钟（踩过）。
    local have=0
    if [ -f "$dl/$asset" ]; then
        if curl -fsSL --http1.1 --retry 3 --retry-delay 3 --max-time 120 \
                -o "$dl/$asset.sha256" "$url.sha256" 2>/dev/null \
           && ( cd "$dl" && sha256sum -c "$asset.sha256" >/dev/null 2>&1 ); then
            log "本地已有该版本产物且官方 sha256 校验通过，跳过下载"
            have=1
        else
            rm -f "$dl/$asset"
        fi
    fi
    if [ "$have" != 1 ]; then
        local i=0
        while :; do
            i=$((i + 1))
            if curl -fL --http1.1 --connect-timeout 20 --max-time 600 \
                    --retry 3 --retry-delay 5 --retry-all-errors \
                    -o "$dl/$asset" "$url"; then
                break
            fi
            rm -f "$dl/$asset"
            [ "$i" -ge 6 ] && die "下载 $url 失败（已重试 $i 次）"
            log "下载中断，第 $i 次重试…"
            sleep 5
        done
        [ -f "$dl/$asset.sha256" ] || curl -fsSL --http1.1 --retry 3 --retry-delay 3 --max-time 120 \
            -o "$dl/$asset.sha256" "$url.sha256" 2>/dev/null || true
    fi
    if [ -f "$dl/$asset.sha256" ]; then
        ( cd "$dl" && grep -q "$asset" "$asset.sha256" && sha256sum -c "$asset.sha256" ) \
            || die "官方 sha256 校验失败（$asset）"
        log "官方 sha256 校验通过"
    else
        log "警告：官方未提供 .sha256，跳过官方校验（仍会做 ELF 静态校验）"
    fi

    rm -rf "$WORK/release-extract"
    mkdir -p "$WORK/release-extract"
    tar -xzf "$dl/$asset" -C "$WORK/release-extract"
    chmod -R a+rX "$WORK/release-extract" 2>/dev/null || true   # /vol1 落盘常丢权限位

    local root
    root="$(find "$WORK/release-extract" -maxdepth 4 -name dbx-web-bin -printf '%h\n' | head -1)"
    [ -n "$root" ] || die "官方产物里找不到 bin/dbx-web-bin"
    local pkgdir
    pkgdir="$(dirname "$root")"

    rm -rf "$OUT"
    mkdir -p "$OUT/bin"
    cp -f "$root/dbx-web-bin" "$OUT/bin/dbx-web-bin"
    chmod 755 "$OUT/bin/dbx-web-bin"
    cp -a "$pkgdir/dist" "$OUT/dist"
    [ -f "$OUT/dist/index.html" ] || die "官方产物缺 dist/index.html"
    echo "$TAG" > "$OUT/UPSTREAM_TAG"
    [ -n "${UPSTREAM_COMMIT:-}" ] && echo "$UPSTREAM_COMMIT" > "$OUT/UPSTREAM_COMMIT" || echo "unknown" > "$OUT/UPSTREAM_COMMIT"
    echo "release" > "$OUT/BUILD_MODE"
    echo "$asset" > "$OUT/SOURCE_ASSET"
    sha256sum "$OUT/bin/dbx-web-bin" | cut -d' ' -f1 > "$OUT/BIN_SHA256"
}

build_from_source() {
    log "source 模式：容器内编译（node:22-slim 前端 + rust:1.97.1-bookworm 后端）"
    mkdir -p "$WORK/logs" "$OUT"
    rm -f "$OUT/bin/dbx-web-bin"

    if [ "${DBX_SKIP_FRONTEND:-0}" = 1 ] && [ -f "$OUT/dist/index.html" ]; then
        log "按要求跳过前端构建（复用已有 $OUT/dist）"
    else
    python3 "$ROOT/lib/fnosdocker.py" run \
        --image node:22-slim --name "$APPNAME-build-front" \
        --mount "$ROOT:/work" --bash /work/docker/step-frontend.sh \
        --env "TAG=$TAG" --env "REPO=$REPO_URL" --env "VER=$VER" --env "WORK=/work/build/work" \
        --env "PUID=$PUID" --env "PGID=$PGID" --env "NPM_REGISTRY=${NPM_REGISTRY:-https://registry.npmmirror.com}" \
        --memory "${DBX_FRONTEND_MEMORY_MB:-3072}" --timeout 3600 \
        || die "前端构建失败，日志：$WORK/logs/frontend.log"
    fi

    prepare_cargo_cache
    rust_image="${DBX_RUST_IMAGE:-rust:1.97.1-bookworm}"   # 与上游 CI 同版本；可用 DBX_RUST_IMAGE 换更新版试
    log "后端容器镜像：$rust_image"
    python3 "$ROOT/lib/fnosdocker.py" run \
        --image "$rust_image" --name "$APPNAME-build-back" \
        --mount "$ROOT:/work" --mount "$CARGO_CACHE_HOST:/cargo" \
        --bash /work/docker/step-backend.sh \
        --env "TAG=$TAG" --env "REPO=$REPO_URL" --env "VER=$VER" --env "WORK=/work/build/work" \
        --env "TARGET=$RUST_TARGET" --env "PUID=$PUID" --env "PGID=$PGID" --env "JOBS=${DBX_CARGO_JOBS:-3}" \
        --env "CARGO_TARGET_DIR=/cargo/target" --env "CARGO_HOME=/cargo/cargo" \
        --memory "${DBX_BUILD_MEMORY_MB:-3072}" --timeout "${DBX_BUILD_TIMEOUT:-10800}" \
        || die "后端编译失败，日志：$WORK/logs/backend-$RUST_TARGET.log"

    [ -f "$OUT/UPSTREAM_TAG" ] || echo "$TAG" > "$OUT/UPSTREAM_TAG"
    echo "source" > "$OUT/BUILD_MODE"
}

if [ "$SKIP_BUILD" = 1 ]; then
    have_artifacts || die "--skip-build 但 $OUT 下没有可用产物"
    log "跳过构建，复用 $OUT"
elif [ "$MODE" = "release" ]; then
    fetch_release_assets
elif [ "$MODE" = "source" ]; then
    build_from_source
else   # auto
    if build_from_source; then
        :
    else
        log "source 模式失败 → 退回 release 模式（会用官方产物，报告里会标明）"
        MODE="release-fallback"
        fetch_release_assets
    fi
fi

[ -x "$OUT/bin/dbx-web-bin" ] || die "缺少 dbx-web 二进制"
[ -f "$OUT/dist/index.html" ] || die "缺少前端 dist/index.html"

log "校验二进制必须是全静态（无解释器 / 无动态库）"
python3 "$ROOT/lib/elfcheck.py" "$OUT/bin/dbx-web-bin" --expect-arch "$ELF_ARCH" || die "ELF 静态校验不通过"
[ "$(cat "$OUT/BUILD_MODE")" = "release" ] || [ "$(cat "$OUT/BUILD_MODE")" = "release-fallback" ] \
    || sha256sum "$OUT/bin/dbx-web-bin" | cut -d' ' -f1 > "$OUT/BIN_SHA256"

[ "$NO_PACKAGE" = 1 ] && { log "按要求只准备产物，不打包"; exit 0; }

# ------------------------------------------------------------------ 3. 组装 fpk
PKGDIR="$PKG_ROOT/${APPNAME}-${VER}-${ARCH}"
log "组装 fpk 目录：$PKGDIR"

rm -rf "$PKGDIR"
mkdir -p "$PKGDIR"
cp -a "$ROOT/fpk/." "$PKGDIR/"

UPSTREAM_COMMIT_FILE="$(cat "$OUT/UPSTREAM_COMMIT" 2>/dev/null || echo unknown)"
CHANGELOG="${VER}: 同步上游 DBX ${TAG}（commit ${UPSTREAM_COMMIT_FILE:0:10}，构建模式 $(cat "$OUT/BUILD_MODE")）"
sed -e "s/@VERSION@/$VER/g" \
    -e "s/@PLATFORM@/$FPK_PLATFORM/g" \
    -e "s|@CHANGELOG@|$CHANGELOG|g" \
    "$ROOT/fpk/manifest.tpl" > "$PKGDIR/manifest"
rm -f "$PKGDIR/manifest.tpl"

mkdir -p "$PKGDIR/app/bin"
cp -f "$OUT/bin/dbx-web-bin" "$PKGDIR/app/bin/dbx-web-bin"
chmod 755 "$PKGDIR/app/bin/dbx-web-bin"
rm -rf "$PKGDIR/app/dist"
cp -a "$OUT/dist" "$PKGDIR/app/dist"

# 图标：ICON.PNG(64) / ICON_256.PNG(256) + ui/images/{64,256,icon_64,icon_256}.png
cp -f "$ROOT/fpk/app/ui/images/256.png" "$PKGDIR/ICON_256.PNG"
cp -f "$ROOT/fpk/app/ui/images/64.png"  "$PKGDIR/ICON.PNG"

# /vol1（ext4+trim_acl）上落盘的文件常常没有权限位，fnpack 会照搬源目录 mode → 打包前统一修
find "$PKGDIR" -type d -exec chmod 755 {} +
find "$PKGDIR" -type f -exec chmod 644 {} +
chmod 755 "$PKGDIR/cmd/"* "$PKGDIR/app/bin/dbx-web-bin"

# ---------------------------------------------------------------- 4. 自检 + 打包
log "打包前自检"
python3 "$ROOT/lib/fpkcheck.py" "$PKGDIR" --expect-version "$VER" --expect-platform "$FPK_PLATFORM" || die "打包前自检不通过"

# 打包器：fnpack（飞牛自带）或原生 fpkpack.py（纯 Python，任何 Linux/CI 都能跑）
if [ "$PACKER" = "auto" ]; then
    if command -v fnpack >/dev/null 2>&1; then PACKER=fnpack; else PACKER=native; fi
fi
FPK_NAME="${APPNAME}_${VER}_${FPK_PLATFORM}.fpk"
case "$PACKER" in
    fnpack)
        log "打包器：fnpack（飞牛自带）"
        ( cd "$PKGDIR" && fnpack build -d . ) || die "fnpack 打包失败"
        FPK_SRC="$(find "$PKGDIR" -maxdepth 1 -name '*.fpk' | head -1)"
        [ -n "$FPK_SRC" ] || die "找不到 fnpack 产出的 fpk"
        cp -f "$FPK_SRC" "$DIST_DIR/$FPK_NAME"
        ;;
    native)
        log "打包器：原生 fpkpack.py（不依赖飞牛工具）"
        python3 "$ROOT/lib/fpkpack.py" "$PKGDIR" -o "$DIST_DIR/$FPK_NAME" || die "原生打包失败"
        ;;
esac

log "出包后自检（解包核对）"
python3 "$ROOT/lib/fpkcheck.py" --fpk "$DIST_DIR/$FPK_NAME" --expect-version "$VER" \
    --expect-platform "$FPK_PLATFORM" || die "出包后自检不通过"

FPK_SHA="$(sha256sum "$DIST_DIR/$FPK_NAME" | cut -d' ' -f1)"
FPK_SIZE="$(du -h "$DIST_DIR/$FPK_NAME" | cut -f1)"

# 溯源文件（给上架/复核用：这个包到底是哪个上游 commit、什么模式、什么校验和）
PROV="$DIST_DIR/PROVENANCE-${VER}-${ARCH}.txt"
{
    echo "app            = $APPNAME (fnOS 应用名)"
    echo "app_version    = $VER"
    echo "fpk            = $FPK_NAME"
    echo "fpk_size       = $FPK_SIZE"
    echo "fpk_sha256     = $FPK_SHA"
    echo "build_mode     = $(cat "$OUT/BUILD_MODE")"
    echo "upstream_repo  = $REPO_URL"
    echo "upstream_tag   = ${TAG}"
    echo "upstream_commit= $UPSTREAM_COMMIT_FILE"
    echo "source_asset   = $(cat "$OUT/SOURCE_ASSET" 2>/dev/null || echo '(源码编译)')"
    echo "binary_sha256  = $(sha256sum "$OUT/bin/dbx-web-bin" | cut -d' ' -f1)"
    echo "binary_static  = yes (no PT_INTERP, no DT_NEEDED)"
    if [ -f "$OUT/GIT_DEP_PATCHED" ]; then
        echo "git_dep_patch  = yes（上游 git 依赖抓取失败 → 用本地镜像 + --config path 补丁，只改取源方式）"
    fi
    echo "frontend_files = $(find "$OUT/dist" -type f | wc -l)"
    echo "built_at       = $(date '+%Y-%m-%dT%H:%M:%S%z')"
    # 不写主机名（溯源文件会随包交给审核/别人看，不夹带本机信息）
    echo "built_on       = $(uname -m) / $(cat /etc/fnos-release 2>/dev/null || sed -n 's/^PRETTY_NAME=//p' /etc/os-release)"
} > "$PROV"

python3 - "$STATE" <<PY
import json
json.dump({
    "version": "$VER", "tag": "${TAG}", "arch": "$ARCH", "mode": "$(cat "$OUT/BUILD_MODE")",
    "upstream_commit": "$UPSTREAM_COMMIT_FILE", "fpk": "$FPK_NAME", "fpk_sha256": "$FPK_SHA",
    "built_at": "$(date '+%Y-%m-%dT%H:%M:%S%z')",
}, open("$STATE", "w"), ensure_ascii=False, indent=2)
PY

log "完成 ✅  $DIST_DIR/$FPK_NAME  ($FPK_SIZE, sha256=${FPK_SHA:0:16}…)"
log "溯源：$PROV"

if [ "$DO_INSTALL" = 1 ]; then
    log "安装到本机（应用中心接口，向导参数 port=$INSTALL_PORT）"
    python3 "$ROOT/tools/install-fpk.py" "$DIST_DIR/$FPK_NAME" --port "$INSTALL_PORT"
fi
