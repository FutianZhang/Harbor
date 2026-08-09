#!/bin/bash
set -uo pipefail

# --- 突破 Verifier 容器 1MB 限制的黑客补丁 ---
SITE_PACKAGES=$(python3 -c "import rewardkit, os; print(os.path.dirname(rewardkit.__file__))")
sed -i "s/_MAX_FILE_SIZE = 1024 \* 1024/_MAX_FILE_SIZE = 100 * 1024 * 1024/g" "$SITE_PACKAGES/judges.py"
# ----------------------------------------

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
