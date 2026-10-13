# TuriX 小红书运营平台 — 常用命令
# ==========================================
# make help          查看全部目标
#
# 镜像构建（linux/amd64，推送到阿里云仓库）:
#   make build        构建 amd64 latest + commit 标签并推送
#   make build V=v1.2.0  额外追加版本标签
#   make rollback-list   列出最近可回滚的 commit 标签
#
# 本地开发:
#   make dev          启动后端 + Vite 前端开发服务
#   make test         运行后端测试

REGISTRY    := turix-registry.cn-heyuan.cr.aliyuncs.com
REPO        := $(REGISTRY)/turix/xhs-ops
IMAGE       := $(REPO):latest

.PHONY: build build-push dev test rollback-list help

# 构建 linux/amd64 镜像（latest + commit 短哈希双标签）并推送。
# 可选 V=v1.2.0 追加版本标签。
build:
	@if [ -n "$(V)" ]; then \
		bash scripts/build-image.sh "$(V)"; \
	else \
		bash scripts/build-image.sh; \
	fi

# build 的别名：强调构建后立即推送
build-push: build

# 本地起前后端开发服务（宿主机直跑，非容器）
dev:
	python3.11 main.py --with-frontend

# 运行后端测试
test:
	python3.11 -m pytest tests/backend/ -q

# 列出最近 10 个 commit 对应的镜像标签（用于回滚选择）
rollback-list:
	@git log --oneline -10 --format='  %h  %s' HEAD
	@echo "  用法: 把 docker-compose.prod.yml 里 image tag 的 latest 换成上面的短哈希"

# 帮助
help:
	@echo "可用目标:"
	@echo "  make build            构建 amd64 镜像（latest + commit 标签）并推送到阿里云"
	@echo "  make build V=v1.2.0   同上，额外打版本标签"
	@echo "  make rollback-list    列出最近可回滚的 commit 标签"
	@echo "  make dev              本地开发模式（前后端热更新）"
	@echo "  make test             运行后端测试"
