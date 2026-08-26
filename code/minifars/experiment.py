# -*- coding: utf-8 -*-
"""Experiment Swarm 执行引擎（设计文档 §5.3）：按契约沙箱执行实验任务。

机制（逐条移植 FARS）：
1. 实验循环：Orchestrator 按契约顺序执行任务，每任务 = 技能库模板渲染代码
   → subprocess 沙箱执行（墙钟看门狗）→ 指标落盘 results/<task_id>.json
   + run_meta.json（复现五要素：命令/seed/模型版本/token/时间戳）；
2. 策展技能库：代码只由 skills.py 模板组合（禁止从零写评测逻辑）；
3. 有效性评估门：main 完成后对照契约阈值——不支持假设则跳过 analysis
   节省资源，但负结果完整落盘（FARS "算法诚实"，negative result 路线）；
4. 检查点容错：复用 PR2 候选组件 jiuwenswarm.common.checkpoint 的
   CheckpointManager——每任务完成即 git commit + 状态落盘，杀进程后
   从最后未完成任务续跑；
5. 预算硬约束：单任务超时（契约 per_task_timeout_s）→ seeds 降级重试；
   总墙钟超 experiment_wall_clock_min → 剩余 analysis 跳过（记 skipped_budget）。
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from jiuwenswarm.common.checkpoint import CheckpointManager

from .artifacts import run_meta, write_result
from .contract import ContractError, flatten_tasks, load_contract, validate_contract
from .metering import MeteringMiddleware
from .skills import (parse_result_output, parse_seeds, render_task_script,
                     resolve_metric)

#: 合成实验的模型版本标记（run_meta 五要素之一；正式轮次换真实模型名）
SYNTHETIC_MODEL = "synthetic-v0"
#: 沙箱启动重试退避（Windows 杀软扫描新脚本会瞬时 PermissionError）
SANDBOX_RETRY_BACKOFFS = (0.5, 1.5)


def read_result(results_dir: Path | str, task_id: str) -> Dict[str, Any]:
    """读回 results/<task_id>.json 的 metrics（缺文件返回 {}）。"""
    p = Path(results_dir) / f"{task_id}.json"
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8")).get("metrics", {})
    except (json.JSONDecodeError, OSError):
        return {}


def evaluate_effectiveness(contract: Dict[str, Any],
                           results_dir: Path | str) -> Dict[str, Any]:
    """有效性评估门（§5.3 机制 3）：main 指标对照契约阈值/基线。

    metric 引用格式 "<task_id>.<key>"；compare_to 给定基线引用时按
    "主实验 - 基线" 与 threshold 比较，否则主实验绝对值比较。
    返回 {"passed", "reason", "main_value", "baseline_value", ...}。
    """
    gate = (contract.get("tasks") or {}).get("effectiveness_gate") or {}
    metric_ref = str(gate.get("metric") or "")
    direction = gate.get("direction") or "gt"  # 显式 null 缺省 gt（m1）
    threshold = float(gate.get("threshold", 0.0))

    def _value(ref: str) -> Optional[float]:
        parts = ref.split(".", 1)
        if len(parts) != 2:
            return None
        metrics = read_result(results_dir, parts[0])
        key = parts[1]
        v = metrics.get(key)
        if v is None:
            v = metrics.get(resolve_metric(key))  # 语义化指标名 → 合成输出键
        return float(v) if isinstance(v, (int, float)) else None

    main_val = _value(metric_ref)
    if main_val is None:
        return {"passed": False, "reason": f"主实验指标缺失: {metric_ref}",
                "main_value": None, "baseline_value": None}

    compare_to = gate.get("compare_to")
    baseline_val = None
    if isinstance(compare_to, str) and compare_to.startswith("max_baseline."):
        key = resolve_metric(compare_to.split(".", 1)[1])
        scores = [read_result(results_dir, t["id"]).get(key)
                  for t in (contract["tasks"].get("baselines") or [])]
        scores = [float(s) for s in scores if isinstance(s, (int, float))]
        baseline_val = max(scores) if scores else None
    elif compare_to:
        baseline_val = _value(compare_to)

    if compare_to and baseline_val is None:
        # 评审 M1：契约指定对照但基线指标全缺（超时/报错）→ 比较不可判定，
        # 保守判失败（宁停勿带病放行），不静默退化为绝对阈值比较。
        return {"passed": False,
                "reason": f"对照基线指标缺失: {compare_to}，比较不可判定",
                "main_value": main_val, "baseline_value": None}

    def _ops(passed: bool) -> str:
        # reason 的比较符必须与实际判定方向一致（m1：lt 路径勿打 "≥"）
        if direction == "gt":
            return "≥" if passed else "<"
        return "≤" if passed else ">"

    if baseline_val is not None:
        margin = main_val - baseline_val
        if direction == "gt":
            passed = margin >= threshold
        else:
            passed = margin <= -threshold
        reason = (f"main({main_val:.4f}) - baseline({baseline_val:.4f}) = "
                  f"{margin:+.4f} {_ops(passed)} threshold {threshold}"
                  f"（direction={direction}）")
    else:
        if direction == "gt":
            passed = main_val >= threshold
        else:
            passed = main_val <= threshold
        reason = (f"main({main_val:.4f}) {_ops(passed)} threshold {threshold}"
                  f"（direction={direction}）")
    return {"passed": bool(passed), "reason": reason,
            "main_value": main_val, "baseline_value": baseline_val,
            "direction": direction, "threshold": threshold}


class ExperimentOrchestrator:
    """按契约执行实验任务（§5.3 Orchestrator）。

    Args:
        project: workspace/<project_id>/ 根目录
        contract_path: plan/experiment_contract.yaml
        commit_fn: 检查点提交钩子（缺省 workspace.commit）
        metering: 计量中间件（逐任务记编排流水，统一口径）
        python_exe: 沙箱解释器（缺省当前解释器）
    """

    def __init__(self, project: Path | str, contract_path: Path | str,
                 commit_fn=None, metering: Optional[MeteringMiddleware] = None,
                 python_exe: Optional[str] = None):
        self.project = Path(project)
        self.contract_path = Path(contract_path)
        self.commit_fn = commit_fn
        self.metering = metering
        self.python = python_exe or sys.executable
        self.results_dir = self.project / "exp" / "results"
        self.code_dir = self.project / "exp" / "code"
        self.logs_dir = self.project / "exp" / "logs"
        for d in (self.results_dir, self.code_dir, self.logs_dir):
            d.mkdir(parents=True, exist_ok=True)

    # ----------------------------------------------------------------- run
    def run(self) -> Dict[str, Any]:
        """执行全部任务；返回 {"results", "gate", "completed", "skipped"}。"""
        contract = load_contract(self.contract_path)
        tasks = flatten_tasks(contract)
        budget = contract.get("budget") or {}
        timeout_s = int(budget.get("per_task_timeout_s", 60))
        wall_limit_min = float(budget.get("experiment_wall_clock_min", 120))

        ckpt = CheckpointManager(self.project / "exp" / "checkpoints",
                                 commit_fn=self.commit_fn)
        pending_ids = set(ckpt.pending([t["id"] for t in tasks]))
        completed: List[str] = [t["id"] for t in tasks if t["id"] not in pending_ids]
        skipped: Dict[str, str] = {}
        t_start = self._run_started_at()  # 墙钟基准跨进程持久（评审 m3）
        gate_verdict: Dict[str, Any] = {}

        # 评审 C2：main 已在既往进程完成 → 从落盘结果重算门判定，
        # 断点续跑不绕过有效性门（自愈式重评估，连"M1 已 checkpoint
        # 但未及求值即被杀"的崩溃窗口也一并覆盖）。
        main_ids = {t["id"] for t in tasks if t["task_type"] == "main"}
        if main_ids and not (main_ids & pending_ids):
            gate_verdict = evaluate_effectiveness(contract, self.results_dir)

        for task in tasks:
            tid = task["id"]
            if tid not in pending_ids:
                print(f"[experiment] {tid} 已完成（检查点续跑，跳过）")
                continue
            if task["task_type"] == "analysis" and gate_verdict and not gate_verdict["passed"]:
                skipped[tid] = "early_stop_keep_negative"
                print(f"[experiment] {tid} 跳过：有效性门未通过（负结果保留）")
                continue
            elapsed_min = (time.time() - t_start) / 60.0
            if task["task_type"] == "analysis" and elapsed_min > wall_limit_min:
                skipped[tid] = "skipped_budget"
                print(f"[experiment] {tid} 跳过：墙钟预算耗尽（{elapsed_min:.1f}min）")
                continue

            status, metrics = self._run_task(task, timeout_s)
            self._checkpoint(ckpt, task, status)
            completed.append(tid)

            if task["task_type"] == "main":
                gate_verdict = evaluate_effectiveness(contract, self.results_dir)
                verdict_path = self.results_dir / "gate_verdict.json"
                verdict_path.write_text(json.dumps(gate_verdict, ensure_ascii=False,
                                                   indent=2), encoding="utf-8")
                action = "继续 analysis" if gate_verdict["passed"] else \
                    "早停（保留负结果）"
                print(f"[experiment] 有效性门: passed={gate_verdict['passed']} "
                      f"({gate_verdict['reason']}) → {action}")

        if skipped and any(v == "early_stop_keep_negative" for v in skipped.values()):
            self._write_negative_result(contract, gate_verdict)

        summary = {"results": str(self.results_dir), "gate": gate_verdict,
                   "completed": completed, "skipped": skipped}
        (self.results_dir / "run_summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[experiment] 完成 {len(completed)} 任务，跳过 {len(skipped)}，"
              f"总墙钟 {(time.time() - t_start) / 60.0:.1f}min")
        return summary

    # ----------------------------------------------------------- internals
    def _run_started_at(self) -> float:
        """墙钟预算基准（评审 m3）：首跑写入 epoch 标记，续跑读回复用——
        杀进程重启不重置总墙钟，反复重启无法绕过 skipped_budget。"""
        marker = self.results_dir / ".run_started_at"
        if marker.exists():
            try:
                return float(marker.read_text(encoding="utf-8").strip())
            except ValueError:
                pass  # 标记损坏按首跑处理（基准只会后移，安全侧）
        now = time.time()
        marker.write_text(str(now), encoding="utf-8")
        return now

    def _run_task(self, task: Dict[str, Any],
                  timeout_s: int) -> tuple:
        """渲染脚本 → 沙箱执行 → 指标落盘；超时降级 seeds 重试一次。"""
        tid = task["id"]
        seeds = parse_seeds(task.get("seeds"))
        script_path = self.code_dir / f"{tid}.py"
        command = f"{self.python} exp/code/{tid}.py"
        t0 = time.perf_counter()
        t0_wall = time.time()  # 日历时间与单调钟分开记（评审 C1）

        status, metrics, stdout, stderr = self._execute(task, seeds, script_path,
                                                        timeout_s)
        if status == "timeout" and len(seeds) > 1:
            # 预算硬约束下的自动降级（D3：超限任务降级而非作废）
            degraded = seeds[:1]
            print(f"[experiment] {tid} 超时 → seeds 降级 {seeds} -> {degraded} 重试")
            status, metrics, stdout, stderr = self._execute(
                task, degraded, script_path, timeout_s)
            seeds = degraded

        finished = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        meta = run_meta(task_id=tid, task_type=task["task_type"], command=command,
                        seed=seeds[0], model=SYNTHETIC_MODEL,
                        started_at=time.strftime("%Y-%m-%dT%H:%M:%S%z",
                                                 time.localtime(t0_wall)),
                        finished_at=finished, status=status,
                        tokens={"in": 0, "out": 0},
                        extra={"wall_s": round(time.perf_counter() - t0, 2),
                               "seeds": seeds, "skill_template": "benchmark_runner",
                               "task_name": task.get("name", "")})
        write_result(self.results_dir, tid, metrics, meta)
        (self.logs_dir / f"{tid}.log").write_text(
            f"# command: {command}\n# status: {status}\n--- stdout ---\n"
            f"{stdout}\n--- stderr ---\n{stderr}\n", encoding="utf-8")
        self._meter(task, status, t0)
        print(f"[experiment] {tid:<4} {status:<8} "
              f"score={metrics.get('score')!r}")
        return status, metrics

    def _execute(self, task: Dict[str, Any], seeds: List[int],
                 script_path: Path, timeout_s: int) -> tuple:
        """subprocess 沙箱执行（模板渲染 → 运行 → 解析 RESULT_JSON）。"""
        script_task = dict(task)
        script_task["seeds"] = seeds
        script_path.write_text(render_task_script(script_task), encoding="utf-8")
        try:
            proc = self._spawn(script_path, timeout_s)
        except subprocess.TimeoutExpired:
            return "timeout", {}, "", f"超过 {timeout_s}s 墙钟看门狗"
        except OSError as exc:
            return "error", {}, "", str(exc)
        if proc.returncode != 0:
            return "error", {}, proc.stdout, proc.stderr
        metrics = parse_result_output(proc.stdout)
        if not metrics:
            return "error", {}, proc.stdout, proc.stderr or "无 RESULT_JSON 输出"
        return "ok", metrics, proc.stdout, proc.stderr

    def _spawn(self, script_path: Path,
               timeout_s: int) -> subprocess.CompletedProcess:
        """启动子进程；对瞬时 PermissionError（杀软扫描新脚本）短退避重试。"""
        for attempt in range(len(SANDBOX_RETRY_BACKOFFS) + 1):
            try:
                return subprocess.run(
                    [self.python, str(script_path)], capture_output=True,
                    text=True, timeout=timeout_s, cwd=str(self.project),
                    encoding="utf-8", errors="replace")
            except PermissionError:
                if attempt < len(SANDBOX_RETRY_BACKOFFS):
                    time.sleep(SANDBOX_RETRY_BACKOFFS[attempt])
                    continue
                raise

    def _checkpoint(self, ckpt: CheckpointManager, task: Dict[str, Any],
                    status: str) -> None:
        """每任务完成即提交（§5.3 机制 4）：结果与 git 历史同步留痕。"""
        revision = ckpt.mark_done(
            task["id"],
            message=f"[experiment] {task['id']} {status}: {task.get('name', '')}",
            extra={"status": status})
        print(f"[experiment] checkpoint {task['id']} commit={revision}")

    def _meter(self, task: Dict[str, Any], status: str, t0: float) -> None:
        if self.metering is None:
            return
        self.metering.record(stage="experiment", agent="experiment_orchestrator",
                             model=SYNTHETIC_MODEL,
                             latency_ms=int((time.perf_counter() - t0) * 1000),
                             extra={"task_id": task["id"],
                                    "task_type": task["task_type"],
                                    "status": status})

    def _write_negative_result(self, contract: Dict[str, Any],
                               verdict: Dict[str, Any]) -> None:
        """负结果完整落盘（FARS 算法诚实：Writing 阶段按 negative result 成文）。"""
        payload = {
            "schema": "v0",
            "kind": "negative_result",
            "hypothesis": contract.get("hypothesis", ""),
            "predict": contract.get("predict", ""),
            "gate": verdict,
            "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "note": "主实验不支持假设；analysis 已按契约 on_fail 跳过，"
                    "全部已完成结果保留供 Writing 阶段引用。",
        }
        (self.results_dir / "negative_result.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def run_experiment_stage(project: Path | str, contract_path: Path | str,
                         commit_fn=None,
                         metering: Optional[MeteringMiddleware] = None) -> Dict[str, Any]:
    """pipeline 入口封装：校验契约 → Orchestrator 全量执行。"""
    contract = load_contract(contract_path)  # 非法契约拒绝执行（ContractError）
    problems = validate_contract(contract)
    if problems:
        raise ContractError(f"执行前复核失败: {problems}")
    orch = ExperimentOrchestrator(project, contract_path, commit_fn=commit_fn,
                                  metering=metering)
    return orch.run()
