#!/bin/bash
# 容器内公共步骤：检出上游源码到指定 commit/tag。
# 由 docker/step-frontend.sh 与 docker/step-backend.sh 共同 source。
# 需要的环境变量：TAG（如 v0.6.19）、REPO、WORK

checkout_upstream() {
    local src="$WORK/src"

    if [ -d "$src/.git" ] && [ "$(git -C "$src" describe --tags --exact-match 2>/dev/null || true)" = "$TAG" ]; then
        echo "[checkout] 复用已有检出：$src @ $TAG"
        return 0
    fi

    # 版本变了就重新浅克隆（便宜；昂贵的 cargo 缓存放在 $WORK/cache，不受影响）
    rm -rf "$src"
    git clone --depth 1 --branch "$TAG" "$REPO" "$src"
    echo "[checkout] 已克隆 $REPO @ $TAG"
    git -C "$src" log -1 --format='[checkout] commit=%H date=%cI subject=%s'
}
