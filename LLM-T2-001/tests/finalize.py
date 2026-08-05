#!/usr/bin/env python3
"""平台固定模板，请勿改动。
汇总维度得分、落地一票否决，并显式区分"评分不可用"与"确实得零分"。"""
import argparse
import json
import pathlib

def load_scores(path):
    """读取 Reward Kit 的 reward.json；不可读或结构异常返回 None。"""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return data if isinstance(data, dict) else None

def count_errors(node):
    """递归统计明细里的 error 字段。judge 超时 / 限额会被 Reward Kit 记成 0.0 加 error。"""
    total = 0
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "error" and value:
                total += 1
            else:
                total += count_errors(value)
    elif isinstance(node, list):
        for item in node:
            total += count_errors(item)
    return total

def detail_errors(reward_path):
    """扫描 reward.json 同目录下的 *details*.json。"""
    total = 0
    for path in sorted(reward_path.parent.glob("*details*.json")):
        try:
            total += count_errors(json.loads(path.read_text(encoding="utf-8")))
        except Exception:
            total += 1
    return total

def numeric_values(scores):
    values = []
    for value in (scores or {}).values():
        try:
            values.append(float(value))
        except (TypeError, ValueError):
            continue
    return values

parser = argparse.ArgumentParser()
parser.add_argument("--graded", required=True)
parser.add_argument("--graded-rc", type=int, required=True)
parser.add_argument("--gating", required=True)
parser.add_argument("--gating-rc", type=int, required=True)
parser.add_argument("--out", required=True)
args = parser.parse_args()

graded_path = pathlib.Path(args.graded)
gating_path = pathlib.Path(args.gating)
out_path = pathlib.Path(args.out)

graded = load_scores(graded_path)
gating = load_scores(gating_path)
dims = {}

for key, value in (graded or {}).items():
    if key == "soft_score":
        continue
    try:
        dims[key] = float(value)
    except (TypeError, ValueError):
        continue

try:
    soft = float((graded or {})["soft_score"])
except (KeyError, TypeError, ValueError):
    soft = sum(dims.values()) / len(dims) if dims else 0.0
soft = round(soft, 6)

graded_ok = args.graded_rc == 0 and bool(dims) and detail_errors(graded_path) == 0
gating_ok = args.gating_rc == 0 and bool(gating) and detail_errors(gating_path) == 0

# 疑罪从无：只有 gating 链路完整跑通、且确证违规时才否决。
# 判官限额 / 超时 / 评分器异常一律不否决，改由 gating_unavailable 上报平台。
gating_values = numeric_values(gating)
veto = gating_ok and bool(gating_values) and min(gating_values) < 1.0

result = dict(dims)
result["soft_score"] = soft
result["gating"] = 0.0 if veto else 1.0
result["reward"] = 0.0 if veto else soft
# 平台读取：1 = 本次评分不可信（判官限额/超时/评分器异常），须重评而非记零分。
result["verifier_error"] = 0.0 if (graded_ok and gating_ok) else 1.0
result["gating_unavailable"] = 0.0 if gating_ok else 1.0

out_path.parent.mkdir(parents=True, exist_ok=True)
out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")