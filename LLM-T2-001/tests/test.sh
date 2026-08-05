#!/bin/bash
set -uo pipefail

# 强行修复 CodeSpace 嵌套沙箱下的 DNS 寻址故障
echo "162.159.140.245 api.openai.com" >> /etc/hosts
echo "185.199.108.133 raw.githubusercontent.com" >> /etc/hosts

mkdir -p /logs/verifier/graded /logs/verifier/gating
rewardkit /tests/graded --workspace /app --output /logs/verifier/graded/reward.json
graded_rc=$?
rewardkit /tests/gating --workspace /app --output /logs/verifier/gating/reward.json
gating_rc=$?
python3 /tests/finalize.py \
  --graded /logs/verifier/graded/reward.json --graded-rc "$graded_rc" \
  --gating /logs/verifier/gating/reward.json --gating-rc "$gating_rc" \
  --out /logs/verifier/reward.json
