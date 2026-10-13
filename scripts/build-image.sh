#!/usr/bin/env bash
#
# build-image.sh — 固化的 TuriX 镜像构建 + 推送流程
#
# 目标仓库: turix-registry.cn-heyuan.cr.aliyuncs.com/turix/xhs-ops
# 平台:     linux/amd64（虚拟机为 x86_64；Apple Silicon 上经 QEMU 交叉构建，
#           Dockerfile 已配国内源，实测 amd64 全程约 15 分钟）
#
# 用法:
#   bash scripts/build-image.sh              # latest + <commit短哈希> 双标签
#   bash scripts/build-image.sh v1.2.0       # 额外追加版本标签
#
# 部署侧更新（虚拟机上）:
#   docker compose -f docker-compose.prod.yml pull && \
#   docker compose -f docker-compose.prod.yml up -d
#
# 回滚（虚拟机上）:
#   把 docker-compose.prod.yml 里 image 的 latest 换成历史 commit 短哈希
#   （推送结束时本脚本会列出当前可回滚的 tag），然后 up -d。

set -euo pipefail

REGISTRY="turix-registry.cn-heyuan.cr.aliyuncs.com"
REPO="${REGISTRY}/turix/xhs-ops"

# ── 仓库定位 ────────────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${PROJECT_DIR}"

if ! git rev-parse --git-dir >/dev/null 2>&1; then
  echo "错误: ${PROJECT_DIR} 不是 git 仓库，无法确定 commit 标签" >&2
  exit 1
fi

GIT_SHA="$(git rev-parse --short HEAD)"
BRANCH="$(git branch --show-current)"
if git status --porcelain | grep -q .; then
  echo "⚠ 工作区有未提交改动，commit 标签 ${GIT_SHA} 不完全代表当前代码"
  echo "  （构建包含未提交内容，回溯时请留意）"
fi

# ── 标签组装 ────────────────────────────────────────────────────────────────
TAGS=("${REPO}:latest" "${REPO}:${GIT_SHA}")
if [[ $# -ge 1 && -n "${1}" ]]; then
  TAGS+=("${REPO}:${1}")
fi

# ── 前置检查 ────────────────────────────────────────────────────────────────
command -v docker >/dev/null 2>&1 || { echo "错误: docker 不可用" >&2; exit 1; }

echo "▸ 检查镜像仓库登录状态..."
if ! docker pull "${REPO}:latest" >/dev/null 2>&1 \
   && ! docker manifest inspect "${REPO}:latest" >/dev/null 2>&1; then
  echo "  未登录或无权限。请先执行:" >&2
  echo "    docker login ${REGISTRY}" >&2
  exit 1
fi
echo "  登录有效"

# ── 构建 + 推送 ─────────────────────────────────────────────────────────────
TAG_ARGS=()
for tag in "${TAGS[@]}"; do
  TAG_ARGS+=(-t "${tag}")
done

echo "▸ 构建 linux/amd64 镜像（commit: ${GIT_SHA}, branch: ${BRANCH:-detached}）..."
echo "  Apple Silicon 上为 QEMU 交叉构建，全程约 10-20 分钟，请耐心等待"
docker buildx build \
  --platform linux/amd64 \
  "${TAG_ARGS[@]}" \
  --push \
  "${PROJECT_DIR}"

# ── 结果摘要 ────────────────────────────────────────────────────────────────
DIGEST="$(docker buildx imagetools inspect "${REPO}:${GIT_SHA}" 2>/dev/null \
  | grep -m1 '^Digest:' | awk '{print $2}')"

echo
echo "✓ 推送完成:"
for tag in "${TAGS[@]}"; do
  echo "    ${tag}"
done
[[ -n "${DIGEST}" ]] && echo "    digest: ${DIGEST}"

echo
echo "▸ 虚拟机更新:"
echo "    docker compose -f docker-compose.prod.yml pull && \\"
echo "    docker compose -f docker-compose.prod.yml up -d"
echo
echo "▸ 可回滚标签（保留最近 5 个 commit 标签供参考）:"
git log --oneline -5 --format='    %h' HEAD
