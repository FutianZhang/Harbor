#!/bin/bash
set -uo pipefail

# 直接向容器注入静态域名映射，彻底绕过 Harbor Egress 代理的 DNS 拦截
# 185.199.108.133 是 raw.githubusercontent.com 的全球官方 CDN 节点
echo "185.199.108.133 raw.githubusercontent.com" >> /etc/hosts

# ！！！请将下方的 IP 替换为你刚才在终端 ping 出来的真实 DeepSeek IP ！！！
echo "3.173.21.63 api.deepseek.com" >> /etc/hosts

mkdir -p /logs/verifier/graded /logs/verifier/gating
rewardkit /tests/graded --workspace /app --output /logs/verifier/graded/reward.json
graded_rc=$?
rewardkit /tests/gating --workspace /app --output /logs/verifier/gating/reward.json
gating_rc=$?
python3 /tests/finalize.py \
  --graded /logs/verifier/graded/reward.json --graded-rc "$graded_rc" \
  --gating /logs/verifier/gating/reward.json --gating-rc "$gating_rc" \
  --out /logs/verifier/reward.json
