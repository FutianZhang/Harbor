# 统一以官方 python 镜像为准。内网加速地址（registry mirror / pip index）由平台在
# 项目启动时下发，不要写死私有镜像名。
FROM python:3.12-slim

RUN useradd -m -u 1000 agent

RUN pip install --no-cache-dir \
        "harbor-rewardkit[all]==0.1.7" \
        python-docx pypdf chardet matplotlib

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

COPY input_files/ /app/input_files/
RUN chown -R root:root /app/input_files && chmod -R a-w /app/input_files

RUN mkdir -p /app/output && chown -R agent:agent /app/output

WORKDIR /app
