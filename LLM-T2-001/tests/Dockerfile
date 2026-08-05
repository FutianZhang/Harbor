FROM python:3.12-slim

# 预装核心评分库
RUN pip install --no-cache-dir "harbor-rewardkit[all]==0.1.7"

# Harbor 会以 tests/ 为上下文，这里将所有评分脚本拷贝到容器的 /tests 目录下
COPY . /tests/