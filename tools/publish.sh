#!/usr/bin/env bash
# =============================================================================
# 把「本机开发目录」里该公开的部分，同步/提交/推送到 GitHub 的分享源仓库。
#
#   用法：
#     bash tools/publish.sh --remote git@github.com:tfgzs/FnDepot.git            # 提交并推送
#     bash tools/publish.sh --remote ... --dry-run                             # 只看会提交哪些文件
#     bash tools/publish.sh --remote ... --tag v0.6.19                         # 顺带推 tag（CI 会出包+发 Release）
#
#   它做的事：
#     1) 按白名单挑出可公开的文件（本机专用文件如 sync.sh / local.cfg 永不发布）
#     2) 泄漏闸门：扫本机 IP / 主机名 / 账号名 / 家目录路径，命中就拒绝推送
#     3) 在临时目录 git init + commit，推到远端 main（不影响本机开发目录）
# =============================================================================
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REMOTE=""
TAG=""
DRY=0
BRANCH="${PUBLISH_BRANCH:-main}"

while [ $# -gt 0 ]; do
    case "$1" in
        --remote) REMOTE="$2"; shift 2 ;;
        --tag) TAG="$2"; shift 2 ;;
        --branch) BRANCH="$2"; shift 2 ;;
        --dry-run) DRY=1; shift ;;
        -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
        *) echo "未知参数：$1" >&2; exit 2 ;;
    esac
done
[ -n "$REMOTE" ] || { echo "必须给 --remote <git 地址>" >&2; exit 2; }

# 公开白名单（相对 ROOT；目录会整棵带上）
PUBLIC=(
    build.sh README.md LICENSE NOTICE .gitignore fnpack.json
    docker lib fpk tools docs .github dbx
)
# 永不发布（双保险，即使白名单写错）
NEVER=(sync.sh local.cfg build dist)

STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT

copy_one() {
    local rel="$1"
    [ -e "$ROOT/$rel" ] || return 0
    local base
    base="$(basename "$rel")"
    for n in "${NEVER[@]}"; do
        [ "$base" = "$n" ] && return 0
    done
    mkdir -p "$STAGE/$(dirname "$rel")"
    if [ -d "$ROOT/$rel" ]; then
        ( cd "$ROOT" && tar cf - --exclude=__pycache__ --exclude='*.pyc' "$rel" ) | tar xf - -C "$STAGE"
    else
        cp -p "$ROOT/$rel" "$STAGE/$rel"
    fi
}
for rel in "${PUBLIC[@]}"; do copy_one "$rel"; done

# ⚠️ 飞牛数据卷是 trimacl：从那里拷出来的文件权限位常被抹成 000，git 连读都读不了。
#    暂存目录在 /tmp（无 trimacl），这里统一补权限位再提交。
#    注意：目录本身可能是 000，find 进不去 → 按层加深逐轮补（每轮让下一层可进入）。
for depth in 1 2 3 4 5 6; do
    chmod u+rwx "$STAGE" 2>/dev/null || true
    find "$STAGE" -maxdepth "$depth" -exec chmod u+rwx {} + 2>/dev/null || true
done
find "$STAGE" -type f -exec chmod u+rw {} + 2>/dev/null || true
find "$STAGE" -name '*.sh' -exec chmod u+x {} +
find "$STAGE" -name '*.py' -exec chmod u+x {} +
[ -d "$STAGE/fpk/cmd" ] && chmod u+x "$STAGE"/fpk/cmd/* 2>/dev/null || true
echo "[publish] 待发布文件：$(find "$STAGE" -type f | wc -l) 个"

# ---------------------------------------------------------------- 泄漏闸门
LOCAL_IPS="$(ip -4 -o addr 2>/dev/null | awk '{print $4}' | cut -d/ -f1 | grep -v '^127\.' | paste -sd'|' - || true)"
HOSTNAME_SHORT="$(hostname 2>/dev/null || echo '')"
PAT="(192\.168\.[0-9]+\.[0-9]+|10\.[0-9]+\.[0-9]+\.[0-9]+|/home/[a-z0-9_-]+|/vol[0-9]+/|$(whoami)|${HOSTNAME_SHORT})"
HITS="$(grep -rInE "$PAT" "$STAGE" 2>/dev/null | grep -vE 'localhost|127\.0\.0\.1|example\.com' | head -20 || true)"
if [ -n "$HITS" ]; then
    echo "[publish] ✗ 检出疑似本机信息，拒绝发布：" >&2
    echo "$HITS" >&2
    exit 1
fi
echo "[publish] ✓ 泄漏闸门通过"

if [ "$DRY" = 1 ]; then
    echo "[publish] --dry-run：只列文件，不推送"
    find "$STAGE" -type f | sed "s|$STAGE/||" | sort
    exit 0
fi

# ---------------------------------------------------------------- git 提交 + 推送
cd "$STAGE"
git init -q -b "$BRANCH"
git add -A
git -c user.name="${PUBLISH_GIT_NAME:-fnos-packager}" \
    -c user.email="${PUBLISH_GIT_EMAIL:-packager@example.com}" \
    commit -q -m "chore: 同步打包工具与源清单（$(date '+%Y-%m-%d %H:%M')）"
git remote add origin "$REMOTE"
echo "[publish] 推送 → $REMOTE ($BRANCH)"
git push -f origin "$BRANCH"
if [ -n "$TAG" ]; then
    echo "[publish] 推 tag → $TAG（CI 将出包并发布 Release）"
    git -c user.name="${PUBLISH_GIT_NAME:-fnos-packager}" \
        -c user.email="${PUBLISH_GIT_EMAIL:-packager@example.com}" tag -f "$TAG"
    git push -f origin "$TAG"
fi
echo "[publish] ✅ 完成"
