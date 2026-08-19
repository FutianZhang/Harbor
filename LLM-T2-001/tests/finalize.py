#!/usr/bin/env python3
"""平台固定模板，请勿改动。

从 Reward Kit 的逐条判定明细汇总主分（按签名权重池化全题 criterion），
落地一票否决，并显式区分"评分不可用"与"确实得零分"。

相对基线模板的题包定制（评分地板 / 合规极性）：
  - negate 只扣分、不进分母、不因“未违规”加分
  - 按维度重算分数，避免总分低但 compliance=1.0
  - 核心官方 ID 检查失败时，reward 封顶 0.20（打掉格式像样地板分）
"""
import argparse
import json
import math
import pathlib


CORE_ID_CHECKS = {"P_CORE_IDS", "P_ID_STATUS_PAIRS"}
WRONG_CONTENT_CAP = 0.20

# Programmatic criteria are not in task.toml; map them to Harbor dimension keys.
PROG_DIM = {
    "P_FILE_PDF": "instruction_following",
    "P_FILE_XLSX": "instruction_following",
    "P_FILE_CSV": "instruction_following",
    "P_XLSX_PARSE": "instruction_following",
    "P_CSV_PARSE": "instruction_following",
    "P_CSV_ROWS": "instruction_following",
    "P_PDF_PAGES": "instruction_following",
    "P_PDF_SECTIONS": "instruction_following",
    "P_SHEETS": "instruction_following",
    "P_COMPLIANCE_IDS": "compliance",
}


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


def criterion_id(item):
    return str(item.get("id") or item.get("name") or "")


def iter_criteria(details):
    """遍历明细里的全部 criterion，附带所在维度键（若有）。

    details[<维度>] 在该维度只有一个 Reward 时是 dict；judge TOML 与 .py 混用、
    或放了多份 judge TOML 时是 list。两种形状都要处理。
    扁平 key「reward」表示未按维度分组。
    """
    if not isinstance(details, dict):
        return
    for dim_key, entry in details.items():
        blocks = entry if isinstance(entry, list) else [entry]
        for block in blocks:
            if not isinstance(block, dict):
                continue
            for item in block.get("criteria") or []:
                if isinstance(item, dict):
                    yield dim_key, item


def normalize_dim_key(key, item):
    """把明细维度键 / 程序化 ID 归一到 Harbor 报表维度名。"""
    cid = criterion_id(item)
    if cid in PROG_DIM:
        return PROG_DIM[cid]
    if cid.startswith("P_"):
        return "content_quality"
    mapping = {
        "instruction_following": "instruction_following",
        "content_quality": "content_quality",
        "compliance": "compliance",
        "指令遵循/任务理解": "instruction_following",
        "指令遵循 / 任务理解": "instruction_following",
        "交付物内容质量": "content_quality",
        "交付物内容质量-准确性": "content_quality",
        "交付物内容质量-专业性": "content_quality",
        "交付物内容质量-完整性": "content_quality",
        "反偷懒/反模板": "content_quality",
        "安全合规": "compliance",
        "致命专业错误": "content_quality",
    }
    if key in mapping:
        return mapping[key]
    # Flattened details have no dimension key; leave unset for caller fallback.
    if key == "reward":
        return None
    return key


def pool_items(items):
    """对一组 criterion 做签名加权池化，返回 (分数, 参与条数, 异常条数)。

    正向项：+weight 进分子、weight 进分母。
    negate 项：-weight 进分子、不进分母。明细里的 value 是翻转后的值
              （违规存在 = 0），违规程度需还原为 1 - value。
    """
    numerator = 0.0
    denominator = 0.0
    counted = 0
    broken = 0
    for item in items:
        weight = finite(item.get("weight"))
        value = finite(item.get("value"))
        negate = item.get("negate")
        if (item.get("error") or weight is None or weight <= 0.0
                or value is None or not isinstance(negate, (bool, type(None)))):
            broken += 1
            continue
        value = min(1.0, max(0.0, value))
        if negate:
            numerator -= weight * (1.0 - value)
        else:
            numerator += weight * value
            denominator += weight
        counted += 1
    if denominator <= 0.0:
        return None, counted, broken
    return min(1.0, max(0.0, numerator / denominator)), counted, broken


def pooled_score(details):
    """全题池化的签名加权分，返回 (分数, 参与条数, 异常条数)。"""
    items = [item for _, item in iter_criteria(details)]
    return pool_items(items)


def dimension_scores(details):
    """按维度重算分数（与主分同一套 negate 规则）。"""
    buckets = {}
    for dim_key, item in iter_criteria(details):
        dim = normalize_dim_key(dim_key, item)
        if not dim:
            # Flattened details: still assign by criterion id heuristics.
            dim = normalize_dim_key("", item) or "content_quality"
        buckets.setdefault(dim, []).append(item)
    out = {}
    for dim, items in buckets.items():
        score, counted, _broken = pool_items(items)
        if score is None or counted == 0:
            out[dim] = 0.0
        else:
            out[dim] = round(score, 4)
    return out


def core_id_checks_failed(details):
    """任一核心官方 ID 程序化检查为 0 / False 则视为内容核心失败。"""
    seen = False
    failed = False
    for _dim, item in iter_criteria(details):
        cid = criterion_id(item)
        if cid not in CORE_ID_CHECKS:
            continue
        seen = True
        value = finite(item.get("value"))
        if value is None or value < 1.0:
            failed = True
    return seen and failed


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
    for _dim, entry in iter_criteria(data):
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

    # 主分：按签名权重池化全题 criterion（负向项真扣分，空产物下限为 0）。
    pooled, counted, broken = pooled_score(graded_details)
    score = 0.0 if pooled is None else round(pooled, 6)

    # 维度分：与主分同一套池化，覆盖 Reward Kit 把 negate 当满分的报表。
    dims = dimension_scores(graded_details) if graded_details else {}

    # Reward Kit 自己的 [0,1] 归一化聚合值，仅留作审计参照，不作主分。
    soft = finite((graded or {}).get("soft_score"))
    if soft is not None:
        soft = round(soft, 6)

    graded_ok = (args.graded_rc == 0 and bool(dims) and pooled is not None
                 and counted > 0 and broken == 0)

    # 疑罪从无：只有 gating 链路完整跑通、且确证违规时才否决。
    # 判官限额 / 超时 / 评分器异常一律不否决，改由 gating_unavailable 上报平台。
    if args.gating_absent:
        gating_ok, veto = True, False
    else:
        gating_values = finite_values(gating)
        item_values, gating_broken, gating_parsed = gating_items(gating_path)
        gating_ok = (args.gating_rc == 0 and bool(gating) and bool(gating_values)
                     and detail_errors(gating_path) == 0 and gating_broken == 0
                     and not (gating_parsed and not item_values))
        veto = gating_ok and min(gating_values + item_values) < 1.0

    # 官方 ID 核心检查失败：格式像样也不得超过地板上限。
    if core_id_checks_failed(graded_details):
        score = min(score, WRONG_CONTENT_CAP)
        score = round(score, 6)

    result = dict(dims)
    result["graded_score"] = score
    result["criteria_counted"] = float(counted)
    if soft is not None:
        result["soft_score"] = soft
    result["gating"] = 0.0 if veto else 1.0
    if args.gating_absent:
        result["gating_absent"] = 1.0

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
parser.add_argument("--gating-absent", action="store_true")
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
