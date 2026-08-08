#!/usr/bin/env python3
"""平台固定模板，请勿改动。

从 Reward Kit 的逐条判定明细汇总主分（按签名权重池化全题 criterion），
落地一票否决，并显式区分"评分不可用"与"确实得零分"。
"""
import argparse
import json
import math
import pathlib


def load_json(path):
    """读取 JSON；不可读或解析失败返回 None。"""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def load_scores(path):
    data = load_json(path)
    return data if isinstance(data, dict) else None


def finite(value):
    """转成有限浮点数；不可转、NaN、±inf 一律返回 None。

    Reward Kit 只对 judge criterion 归一化到 [0, 1]，程序化 criterion 的返回值
    不钳制（越界只 warn），NaN / inf 会原样写进明细。这类值若直接参与运算会算出
    一个 0 分，看起来像"确实得零分"，必须当成评分异常上报。
    """
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def iter_criteria(details):
    """遍历明细里的全部 criterion。

    details[<维度>] 在该维度只有一个 Reward 时是 dict；judge TOML 与 .py 混用、
    或放了多份 judge TOML 时是 list。两种形状都要处理。
    """
    if not isinstance(details, dict):
        return
    for entry in details.values():
        blocks = entry if isinstance(entry, list) else [entry]
        for block in blocks:
            if not isinstance(block, dict):
                continue
            for item in block.get("criteria") or []:
                if isinstance(item, dict):
                    yield item


def pooled_score(details):
    """全题池化的签名加权分，返回 (分数, 参与条数, 异常条数)。

    正向项：+weight 进分子、weight 进分母。
    negate 项：-weight 进分子、不进分母。明细里的 value 是翻转后的值
              （违规存在 = 0），违规程度需还原为 1 - value。
    异常条目一律不计入、改由 verifier_error 上报，包括：带 error（判官超时会把
    每条都记成 value = 0.0 并保留 negate，若计入会凭空扣分）、weight 非正数、
    value 非有限值、negate 非布尔值。
    无正向条目时分母为 0，主分无定义，返回 (None, ...)。
    """
    numerator = 0.0
    denominator = 0.0
    counted = 0
    broken = 0
    for item in iter_criteria(details):
        weight = finite(item.get("weight"))
        value = finite(item.get("value"))
        negate = item.get("negate")
        if (item.get("error") or weight is None or weight <= 0.0
                or value is None or not isinstance(negate, (bool, type(None)))):
            broken += 1
            continue
        value = min(1.0, max(0.0, value))   # 程序化 criterion 越界返回值的兜底
        if negate:
            numerator -= weight * (1.0 - value)
        else:
            numerator += weight * value
            denominator += weight
        counted += 1
    if denominator <= 0.0:
        return None, counted, broken
    return min(1.0, max(0.0, numerator / denominator)), counted, broken


def count_errors(node):
    """递归统计明细里的 error 字段。judge 超时 / 限额会被记成 0.0 加 error。"""
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
        data = load_json(path)
        if data is None:
            total += 1
        else:
            total += count_errors(data)
    return total


def finite_values(scores):
    """取出 scores 里的全部有限数值；null / NaN / 非数值一律丢弃。"""
    values = []
    for value in (scores or {}).values():
        number = finite(value)
        if number is not None:
            values.append(number)
    return values


def gating_items(reward_path):
    """gating 段的逐条判定值，返回 (钳到 [0,1] 的值列表, 异常条数, 明细是否读到)。

    只看聚合值不够，有两个方向：
      1) all_pass 判的是 value > 0，红线条目若不是 binary，聚合值会把它抹平成"通过"。红线只认"完全没有
         违规"，逐条判定里任何非满值都按违规坐实处理。
      2) 一条 criterion 都没匹配上时聚合值是 0.0，与"确实踩了红线"长得一模一样。
         明细读到了却一条都没有，只说明 gating 段没跑到东西（配置写错），必须报
         不可用，而不是静默否决全部候选。
    明细文件读不到时无从判断，返回 parsed=False，沿用聚合值（文件损坏另由
    detail_errors 兜住）。
    """
    data = load_json(reward_path.with_name("reward-details.json"))
    values = []
    broken = 0
    for entry in iter_criteria(data):
        number = finite(entry.get("value"))
        if number is None:
            broken += 1
        else:
            values.append(min(1.0, max(0.0, number)))
    return values, broken, data is not None


def compute(args):
    """汇总两段评分结果，返回要写进 reward.json 的字典。"""
    graded_path = pathlib.Path(args.graded)
    gating_path = pathlib.Path(args.gating)

    graded = load_scores(graded_path)
    gating = load_scores(gating_path)
    graded_details = load_json(graded_path.with_name("reward-details.json"))

    dims = {}
    for key, value in (graded or {}).items():
        if key == "soft_score":
            continue
        number = finite(value)
        if number is not None:
            dims[key] = number

    # 主分：按签名权重池化全题 criterion（负向项真扣分，空产物下限为 0）。
    pooled, counted, broken = pooled_score(graded_details)
    score = 0.0 if pooled is None else round(pooled, 6)

    # Reward Kit 自己的 [0,1] 归一化聚合值，仅留作审计参照，不作主分。
    soft = finite((graded or {}).get("soft_score"))
    if soft is not None:
        soft = round(soft, 6)

    graded_ok = (args.graded_rc == 0 and bool(dims) and pooled is not None
                 and counted > 0 and broken == 0)

    # 疑罪从无：只有 gating 链路完整跑通、且确证违规时才否决。
    # 判官限额 / 超时 / 评分器异常一律不否决，改由 gating_unavailable 上报平台。
    gating_values = finite_values(gating)
    item_values, gating_broken, gating_parsed = gating_items(gating_path)
    gating_ok = (args.gating_rc == 0 and bool(gating) and bool(gating_values)
                 and detail_errors(gating_path) == 0 and gating_broken == 0
                 and not (gating_parsed and not item_values))
    veto = gating_ok and min(gating_values + item_values) < 1.0

    result = dict(dims)
    result["graded_score"] = score
    result["criteria_counted"] = float(counted)
    if soft is not None:
        result["soft_score"] = soft
    result["gating"] = 0.0 if veto else 1.0
    # 评分不可用时主分一律记 0：宁可保守低估，也不要因为把异常条目排除在分母之外
    # 而把剩下的条目重新归一化成一个虚高的分数。真实分数留在 graded_score 里。
    unavailable = not (graded_ok and gating_ok)
    result["reward"] = 0.0 if (veto or unavailable) else score
    # 平台读取：1 = 本次评分不可信（判官限额/超时/评分器异常），须重评而非记零分。
    result["verifier_error"] = 1.0 if unavailable else 0.0
    result["gating_unavailable"] = 0.0 if gating_ok else 1.0
    return result


parser = argparse.ArgumentParser()
parser.add_argument("--graded", required=True)
parser.add_argument("--graded-rc", type=int, required=True)
parser.add_argument("--gating", required=True)
parser.add_argument("--gating-rc", type=int, required=True)
parser.add_argument("--out", required=True)
args = parser.parse_args()

try:
    result = compute(args)
except Exception:
    # 未预期的异常也必须落地一份结果：缺了 reward.json，平台读到的是"这道题没跑过"，
    # 与"跑出 0 分"无法区分。一律记 verifier_error = 1 交平台重评。
    result = {"graded_score": 0.0, "criteria_counted": 0.0, "gating": 1.0,
              "reward": 0.0, "verifier_error": 1.0, "gating_unavailable": 1.0}

out_path = pathlib.Path(args.out)
out_path.parent.mkdir(parents=True, exist_ok=True)
out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")