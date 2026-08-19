FROM python:3.12-slim

RUN apt-get update && apt-get install -y curl nodejs npm

RUN apt-get update \
    && apt-get install -y --no-install-recommends bubblewrap \
    && rm -rf /var/lib/apt/lists/*

RUN useradd -m -u 1000 agent

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt


ENV TIKTOKEN_CACHE_DIR=/opt/tiktoken-cache \
    LITELLM_LOCAL_MODEL_COST_MAP=True
RUN mkdir -p "${TIKTOKEN_CACHE_DIR}" \
    && python3 -c "import tiktoken; tiktoken.get_encoding('cl100k_base')" \
    && chmod -R a-w "${TIKTOKEN_CACHE_DIR}"

COPY input_files/ /app/input_files/
RUN chown -R root:root /app/input_files && chmod -R a-w /app/input_files

RUN mkdir -p /app/output /app/solution \
    && chown -R agent:agent /app/output /app/solution

WORKDIR /app

RUN python3 -V \
    && bash --version >/dev/null \
    && rewardkit --help >/dev/null \
    && pip show harbor-rewardkit | grep '^Version: 0.1.7$' \
    && pip check \
    && bwrap --version \
    && id agent \
    && su agent -c "touch /app/output/.w && rm /app/output/.w" \
    && su agent -c "touch /app/solution/.w && rm /app/solution/.w"
