FROM python:3.12-slim
RUN useradd -m -u 1000 agent
RUN pip install --no-cache-dir "harbor-rewardkit[all]==0.1.7"
COPY input_files/ /app/input_files/
RUN chown -R root:root /app/input_files && chmod -R a-w /app/input_files
RUN mkdir -p /app/output && chown -R agent:agent /app/output
WORKDIR /app