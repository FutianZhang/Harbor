#!/usr/bin/env python3
"""Harbor finalize.py — 平台固定模板，逐字使用，不得改动。"""
import argparse
import json
import sys


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--graded", required=True)
    parser.add_argument("--graded-rc", required=True)
    parser.add_argument("--gating", required=True)
    parser.add_argument("--gating-rc", required=True)
    parser.add_argument("--out", required=True)
    return parser.parse_args()


def load_json(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        return {"error": str(e)}


def main():
    args = parse_args()
    graded = load_json(args.graded)
    gating = load_json(args.gating)
    graded_rc = int(args.graded_rc)
    gating_rc = int(args.gating_rc)

    soft_score = 0.0
    if "soft_score" in graded:
        soft_score = graded["soft_score"]

    gating_pass = True
    if gating_rc != 0:
        gating_pass = False
    elif "gating_score" in gating:
        gating_pass = gating["gating_score"] >= 1.0
    elif "score" in gating:
        gating_pass = gating["score"] >= 1.0

    final_score = soft_score if gating_pass else 0.0

    result = {
        "soft_score": soft_score,
        "gating_pass": gating_pass,
        "final_score": final_score,
        "graded_detail": graded,
        "gating_detail": gating,
    }

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
