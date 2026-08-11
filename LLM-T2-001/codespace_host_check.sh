#!/usr/bin/env bash
# Codespace 宿主校验：布局 / oracle 部署 / 依赖 / 公开 input 试跑。
#
# 用法:
#   cd /workspaces/Harbor/LLM-T2-001   # Codespace 本题根目录（本地或叫 CFE-OPT-003）
#   bash scripts/codespace_host_check.sh
#   bash scripts/codespace_host_check.sh /workspaces/Harbor/LLM-T2-001
#   bash codespace_host_check.sh .     # 脚本放在任务根时，传 .
#
# 不要把脚本正文粘贴进交互式终端。

set +e
set -u

find_task_root() {
  local start="$1"
  local dir
  dir="$(cd "$start" 2>/dev/null && pwd)" || return 1
  while [[ -n "$dir" && "$dir" != "/" ]]; do
    if [[ -f "$dir/task.toml" && -f "$dir/solution/oracle_source/main.py" && -f "$dir/solution/solve.sh" ]]; then
      printf '%s\n' "$dir"
      return 0
    fi
    dir="$(dirname "$dir")"
  done
  return 1
}

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ "${1:-}" != "" ]]; then
  ROOT="$(cd "$1" && pwd)" || {
    echo "[FAIL] 无法进入参数路径: $1" >&2
    exit 2
  }
elif ROOT="$(find_task_root "$PWD")"; then
  :
elif ROOT="$(find_task_root "$SCRIPT_DIR")"; then
  :
else
  cat >&2 <<'EOF'
[FAIL] 找不到任务根目录（需要同时存在 task.toml、solution/solve.sh、solution/oracle_source/main.py）。

Codespace 上本题目录名可能是 LLM-T2-001（本地可能叫 CFE-OPT-003），请显式传入任务根目录，例如:
  cd /workspaces/Harbor/LLM-T2-001
  bash scripts/codespace_host_check.sh .
  # 或
  bash /workspaces/Harbor/LLM-T2-001/scripts/codespace_host_check.sh /workspaces/Harbor/LLM-T2-001

先确认目录:
  ls /workspaces/Harbor
  ls /workspaces/Harbor/LLM-T2-001
  test -f /workspaces/Harbor/LLM-T2-001/task.toml && echo has_task_toml
  test -f /workspaces/Harbor/LLM-T2-001/solution/oracle_source/main.py && echo has_oracle
EOF
  exit 2
fi

cd "$ROOT" || exit 1

PASS=0
FAIL=0
WARN=0
LOG="$ROOT/codespace_host_check.out"
: >"$LOG"

log() { printf '%s\n' "$*" | tee -a "$LOG"; }
ok()  { PASS=$((PASS + 1)); log "[OK] $*"; }
bad() { FAIL=$((FAIL + 1)); log "[FAIL] $*"; }
wrn() { WARN=$((WARN + 1)); log "[WARN] $*"; }
sec() { log ""; log "======== $* ========"; }

# 避免 `cmd | tee` 时 $? 变成 tee 的退出码
run_capture() {
  local tmp
  tmp="$(mktemp)"
  "$@" >"$tmp" 2>&1
  local rc=$?
  tee -a "$LOG" <"$tmp"
  rm -f "$tmp"
  return "$rc"
}

sec "0) 环境"
log "ROOT=$ROOT"
log "PWD=$(pwd)"
log "invoked_from_cwd_was=${OLDPWD:-unknown}"
log "SHELL=$SHELL"
log "whoami=$(whoami 2>/dev/null || true)"
log "python3=$(command -v python3 2>/dev/null || echo MISSING)"
if [[ ! -f "$ROOT/task.toml" ]]; then
  bad "ROOT 下没有 task.toml —— 当前 ROOT 不是题目目录"
  log "请改用: bash $0 /workspaces/Harbor/LLM-T2-001"
  log "或: cd /workspaces/Harbor/LLM-T2-001 && bash scripts/codespace_host_check.sh ."
  log "PASS=$PASS FAIL=$FAIL WARN=$WARN"
  exit 2
fi
if [[ "$(basename "$ROOT")" != "CFE-OPT-003" && "$(basename "$ROOT")" != "LLM-T2-001" ]]; then
  wrn "当前目录名是 $(basename "$ROOT")；只要含 task.toml + solution/oracle_source 即可，目录名不强制"
fi
log "task_basename=$(basename "$ROOT")"

sec "1) 仓库布局（oracle_source 位置）"
if [[ -f solution/solve.sh ]]; then ok "solution/solve.sh"; else bad "缺少 solution/solve.sh"; fi
if [[ -f solution/oracle_source/main.py ]]; then ok "solution/oracle_source/main.py"; else bad "缺少 solution/oracle_source/main.py"; fi
if [[ -f solution/oracle_source/__init__.py ]]; then ok "solution/oracle_source/__init__.py"; else bad "缺少 solution/oracle_source/__init__.py（相对 import 需要）"; fi
if [[ -d solution/golden_output ]]; then ok "solution/golden_output/"; else bad "缺少 solution/golden_output/"; fi
if [[ -f solution/main.py ]]; then
  wrn "存在 solution/main.py；若 solve.sh 只拷 oracle_source，这个文件不会进 /app/solution"
else
  ok "无 solution/main.py（常见设计）"
fi
log "---- solution/solve.sh (raw) ----"
if [[ -f solution/solve.sh ]]; then
  tee -a "$LOG" <solution/solve.sh
  log "---- solve.sh file meta ----"
  log "size=$(wc -c <solution/solve.sh) lines=$(wc -l <solution/solve.sh)"
  log "grep_oracle_source=$(grep -a -c 'oracle_source' solution/solve.sh 2>/dev/null || echo 0)"
  if grep -a -q 'oracle_source' solution/solve.sh 2>/dev/null; then
    ok "solve.sh 文本含 oracle_source"
  else
    wrn "solve.sh 文本不含 oracle_source —— 下面用改写路径实际执行 solve.sh 验证部署行为"
  fi
else
  bad "无法读取 solution/solve.sh"
fi

sec "2) 模拟容器内 Oracle 部署"
TMP="$(mktemp -d /tmp/cfe-opt-check.XXXXXX)"
log "TMP=$TMP"
mkdir -p "$TMP/solution" "$TMP/app"
cp -R solution/. "$TMP/solution/" 2>>"$LOG"

# 把 solve.sh 里的绝对路径改写到 TMP。
# 必须先替换更长的 /app/solution、/app/output，再替换 /solution，
# 否则 /app/solution 中的 /solution 会被误伤，拷到错误目录却仍 exit 0。
if [[ -f solution/solve.sh ]]; then
  sed \
    -e "s|/app/solution|$TMP/app/solution|g" \
    -e "s|/app/output|$TMP/app/output|g" \
    -e "s|/solution|$TMP/solution|g" \
    -e "s|/app|$TMP/app|g" \
    solution/solve.sh >"$TMP/solve.local.sh"
  # 保证末尾换行，避免个别环境下最后一行不执行
  printf '\n' >>"$TMP/solve.local.sh"
  chmod +x "$TMP/solve.local.sh"
  log "---- rewritten solve.sh ----"
  tee -a "$LOG" <"$TMP/solve.local.sh"
  log "---- executing rewritten solve.sh ----"
  bash "$TMP/solve.local.sh" >"$TMP/solve.local.log" 2>&1
  SOLVE_RC=$?
  tee -a "$LOG" <"$TMP/solve.local.log"
  if [[ "$SOLVE_RC" -eq 0 ]]; then ok "solve.sh 执行成功 (exit=0)"; else bad "solve.sh 执行失败 (exit=$SOLVE_RC)"; fi
  log "---- find under TMP/app after solve ----"
  find "$TMP/app" -maxdepth 3 -type f 2>/dev/null | tee -a "$LOG" || true
else
  bad "无 solve.sh"
  SOLVE_RC=1
fi

NEED=(main.py __init__.py accounting.py attribution.py greeks.py positions.py pricing.py)
MISS=0
for n in "${NEED[@]}"; do
  if [[ -f "$TMP/app/solution/$n" ]]; then
    ok "deployed app/solution/$n"
  else
    bad "未部署 app/solution/$n"
    MISS=1
  fi
done
if [[ "$MISS" -eq 0 ]]; then
  ok "经 solve.sh 后 /app/solution 完整"
else
  bad "solve.sh 未把 oracle_source 部署到 /app/solution（这就是正向自测 /app/solution 空的根因）"
  wrn "回退手工拷贝 oracle_source，仅用于步骤 4 验证引擎本身"
  mkdir -p "$TMP/app/solution"
  cp -R "$TMP/solution/oracle_source/." "$TMP/app/solution/" 2>>"$LOG"
fi
log "---- $TMP/app/solution listing ----"
ls -la "$TMP/app/solution" 2>&1 | tee -a "$LOG"

sec "3) 宿主 Python 依赖（此前八维全 0 的直接原因）"
run_capture python3 - <<'PY'
import importlib.util, sys
print("executable:", sys.executable)
print("version:", sys.version.replace("\n", " "))
missing = []
for m in ["rewardkit", "numpy", "pandas", "scipy", "pyarrow"]:
    ok = importlib.util.find_spec(m) is not None
    print(f"{m}: {'OK' if ok else 'MISSING'}")
    if not ok:
        missing.append(m)
raise SystemExit(1 if missing else 0)
PY
DEP_RC=$?
if [[ "$DEP_RC" -eq 0 ]]; then
  ok "关键依赖已可 import"
else
  wrn "依赖缺失，尝试 pip install -r environment/requirements.txt"
  if [[ -f environment/requirements.txt ]]; then
    run_capture python3 -m pip install -r environment/requirements.txt
    run_capture python3 - <<'PY'
import importlib.util
missing = [m for m in ["numpy", "pandas", "scipy", "pyarrow"] if importlib.util.find_spec(m) is None]
print("after_install_missing:", missing if missing else "none")
raise SystemExit(1 if missing else 0)
PY
    if [[ $? -eq 0 ]]; then ok "pip 安装后依赖可用"; else bad "pip 后仍缺依赖（多 Python/权限/网络问题）"; fi
  else
    bad "缺少 environment/requirements.txt，无法自动安装"
  fi
fi

sec "4) 用公开 input 跑部署后的 oracle（不依赖 Harbor）"
if [[ ! -d environment/input_files/input ]]; then
  bad "缺少 environment/input_files/input"
else
  # oracle 要求 output_dir 尚不存在（mkdir exist_ok=False）；不要预先 mkdir
  OUT_RUN="$TMP/app/output-run"
  rm -rf "$OUT_RUN"
  (
    PYTHONPATH="$TMP/app" python3 -m solution.main \
      --input-dir "$ROOT/environment/input_files/input" \
      --output-dir "$OUT_RUN"
  ) >"$TMP/oracle-run.log" 2>&1
  RUN_RC=$?
  tee -a "$LOG" <"$TMP/oracle-run.log"
  if [[ "$RUN_RC" -eq 0 ]]; then
    ok "oracle 公开 input 运行成功 (exit=0)"
  else
    bad "oracle 公开 input 运行失败 (exit=$RUN_RC)"
  fi
  for f in positions_eod.csv valuation.csv greeks.csv pnl_attribution.csv stress_report.json validation_report.json; do
    if [[ -f "$OUT_RUN/$f" ]]; then ok "产出 $f"; else bad "缺少产出 $f"; fi
  done
  log "---- output-run listing ----"
  ls -la "$OUT_RUN" 2>&1 | tee -a "$LOG"
fi

sec "5) worker 探测（可选；宿主常因非 root / 无 bwrap 失败）"
run_capture python3 - <<'PY'
import importlib.util
print("pyarrow_spec:", importlib.util.find_spec("pyarrow"))
print("can_import_grader_hidden_fixtures_deps: start")
try:
    import pyarrow  # noqa: F401
    print("pyarrow_import: OK", getattr(pyarrow, "__version__", "?"))
except Exception as exc:
    print("pyarrow_import: FAIL", type(exc).__name__, exc)
print("note: full worker needs Linux root + bubblewrap; skip sudo here")
PY

if command -v bwrap >/dev/null 2>&1; then ok "bwrap 可用"; else wrn "宿主无 bwrap（Harbor 镜像里才有也正常）"; fi
if [[ "$(id -u 2>/dev/null || echo 1)" == "0" ]]; then
  wrn "当前是 root，可手动再跑: python3 tests/assets/deterministic/worker.py $TMP/app"
else
  wrn "当前非 root；跳过完整 worker（避免 sudo 卡密码导致终端像假死）"
fi

sec "汇总"
log "PASS=$PASS FAIL=$FAIL WARN=$WARN"
log "详细日志: $LOG"
log "临时目录: $TMP（可保留排查；不需要可 rm -rf $TMP）"
if [[ "$FAIL" -gt 0 ]]; then
  log "结论: 存在失败项，请把本脚本输出或 $LOG 发回"
  exit 1
fi
log "结论: 布局/依赖/公开试跑未见失败（oracle_source 放置看起来正确）"
exit 0
