"""精确物理设计求解器：分支定界 + 乐观合法下界 + 可复核最优性证明。

目标按字典序最小化：
  1) f1 = sum(weight * 选中方案费用) + alpha * sum(维护费)
  2) 实际存储字节
  3) 升序候选 ID 列表的字典序

下界对未决候选保持乐观：未决候选一律视为“可免费获得”，维护与字节只计已确定
部分。该下界对任意可行完整解都是合法下界。超时/节点耗尽只返回当前可行解与
全局下界，绝不宣称最优。
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from fractions import Fraction

from .model import Problem, closure


@dataclass
class QueryPlanChoice:
    query_id: str
    plan_id: str
    cost: Fraction
    weight: Fraction
    excludes: list  # list[(plan_id, reason)]


@dataclass
class Infeasibility:
    conflicts: list  # 可逐条重验的冲突证据


@dataclass
class SolveResult:
    status: str                 # optimal | incumbent_only | infeasible
    selected: tuple[str, ...]
    f1: Fraction | None
    bytes_used: int
    maintenance_used: Fraction
    storage_budget: int
    maintenance_budget: Fraction
    choices: tuple[QueryPlanChoice, ...]
    global_lb: Fraction | None
    gap: Fraction | None
    proof: dict
    infeasibility: Infeasibility | None = None


def _plan_costs(problem, mask):
    """独立语义：每查询在 mask 下所有依赖满足的最低费用方案（同费按方案 ID）。"""
    choices = []
    total = Fraction(0)
    for q in problem.queries:
        eligible = [p for p in q.plans if p.req_mask & ~mask == 0]
        eligible.sort(key=lambda p: (p.cost, p.id))
        picked = eligible[0]
        choices.append((q, picked))
        total += q.weight * picked.cost
    return total, choices


def evaluate(problem, mask):
    """对一个闭合、满足互斥与预算的候选集合做完整评分。"""
    q_total, choices = _plan_costs(problem, mask)
    bytes_used = 0
    maintenance = Fraction(0)
    for c in problem.candidates:
        if mask & c.bit:
            bytes_used += c.bytes
            maintenance += c.maintenance
    f1 = q_total + problem.alpha * maintenance
    return f1, bytes_used, maintenance, choices


def _mutex_violation(problem, mask):
    seen = {}
    for c in problem.candidates:
        if mask & c.bit and c.group is not None:
            if c.group in seen:
                return seen[c.group], c.id, c.group
            seen[c.group] = c.id
    return None


def mandatory_closure(problem):
    return closure(problem, set(problem.mandatory))


def infeasibility_certificate(problem):
    """必选闭合若违反硬约束，则不存在任何可行解；证据可独立重算。"""
    mc = mandatory_closure(problem)
    by_id = {c.id: c for c in problem.candidates}
    mask = sum(by_id[cid].bit for cid in mc)
    conflicts = []
    mutex = _mutex_violation(problem, mask)
    if mutex:
        a, b, group = mutex
        conflicts.append({
            'type': 'mandatory_mutex',
            'group': group,
            'candidates': [a, b],
            'reason': f'必选闭合同时包含互斥组 {group} 的 {a} 与 {b}'})
    bytes_used = sum(by_id[cid].bytes for cid in mc)
    maintenance = sum((by_id[cid].maintenance for cid in mc), Fraction(0))
    if bytes_used > problem.storage_budget:
        conflicts.append({
            'type': 'mandatory_storage_over_budget',
            'mandatory_closure': sorted(mc),
            'bytes_required': bytes_used,
            'storage_budget': problem.storage_budget,
            'reason': f'必选闭合至少需要 {bytes_used} 字节，预算仅 {problem.storage_budget}'})
    if maintenance > problem.maintenance_budget:
        conflicts.append({
            'type': 'mandatory_maintenance_over_budget',
            'mandatory_closure': sorted(mc),
            'maintenance_required': str(maintenance),
            'maintenance_budget': str(problem.maintenance_budget),
            'reason': '必选闭合维护费超过维护预算'})
    return Infeasibility(conflicts) if conflicts else None


def _optimistic_lb(problem, mask, avail_mask, bytes_so_far, maint_so_far):
    """乐观合法下界：未决候选视为免费可得，忽略其字节与维护费。"""
    allowed = mask | avail_mask
    q_lb = Fraction(0)
    for q in problem.queries:
        best = min(p.cost for p in q.plans if p.req_mask & ~allowed == 0)
        q_lb += q.weight * best
    return q_lb + problem.alpha * maint_so_far, bytes_so_far, maint_so_far


def _exclusion_reasons(problem, mask, choices):
    out = []
    by_pid = {q.id: {p.id: p for p in q.plans} for q in problem.queries}
    for q, picked in choices:
        rows = []
        for p in q.plans:
            if p.id == picked.id:
                continue
            missing = sorted(cid for cid in p.requires if not (mask & problem.candidate(cid).bit))
            if missing:
                reason = '未满足依赖: ' + ', '.join(missing)
            elif (p.cost, p.id) > (picked.cost, picked.id):
                reason = '依赖已满足但费用不优于选中方案（同费按方案 ID 字典序）'
            else:
                reason = '未被选中'
            rows.append((p.id, reason))
        out.append(QueryPlanChoice(q.id, picked.id, picked.cost, q.weight, rows))
    return tuple(out)


def solve(problem, deadline=None, node_limit=None, time_fn=time.monotonic):
    """分支定界精确求解。deadline 为 monotonic 绝对时刻，node_limit 为最大节点数。"""
    cert = infeasibility_certificate(problem)
    if cert is not None:
        return SolveResult(
            status='infeasible', selected=(), f1=None, bytes_used=0,
            maintenance_used=Fraction(0),
            storage_budget=problem.storage_budget,
            maintenance_budget=problem.maintenance_budget,
            choices=(), global_lb=None, gap=None,
            proof={'method': 'mandatory_closure_unsat',
                   'mandatory_closure': sorted(mandatory_closure(problem))},
            infeasibility=cert)

    n = len(problem.candidates)
    cands = [problem.candidate(cid) for cid in problem.topo_order]
    all_mask = (1 << n) - 1

    # 必选闭合预置进根节点：后续只在“含必选闭合”的可行空间里分支，
    # 这样前沿每个节点的乐观下界对真实可行解空间才合法。
    init_ids = mandatory_closure(problem)
    init_mask = sum(problem.candidate(cid).bit for cid in init_ids)
    init_bytes = sum(problem.candidate(cid).bytes for cid in init_ids)
    init_maint = sum((problem.candidate(cid).maintenance for cid in init_ids), Fraction(0))
    f1, b0, m0, ch0 = evaluate(problem, init_mask)
    incumbent = (f1, b0, tuple(sorted(init_ids)), init_mask)

    stats = {'nodes_evaluated': 0, 'bound_pruned': 0, 'feasibility_pruned': 0,
             'branches': 0, 'total_subsets': 2 ** n,
             'effective_subsets': 2 ** (n - len(init_ids))}

    def stopped():
        if node_limit is not None and stats['nodes_evaluated'] >= node_limit:
            return True
        if deadline is not None and time_fn() >= deadline:
            return True
        return False

    def first_undecided(mask, idx):
        j = idx
        while j < n and (mask & cands[j].bit):
            j += 1
        return j

    # 节点：(下一个未决策位序号, 已选 mask, 字节, 维护费)
    root_idx = first_undecided(init_mask, 0)
    root_node = (root_idx, init_mask, init_bytes, init_maint)
    # 根乐观下界是整个可行空间的合法下界，也是前沿清空时的兜底全局下界。
    root_lb, _, _ = _optimistic_lb(
        problem, init_mask, all_mask, init_bytes, init_maint)
    stack = [root_node]
    timed_out = False

    while stack:
        idx, mask, bytes_so_far, maint_so_far = stack.pop()
        stats['nodes_evaluated'] += 1
        if stopped():
            timed_out = True
            break

        if idx == n:
            f1, bu, mu, choices = evaluate(problem, mask)
            key = (f1, bu, tuple(sorted(c.id for c in problem.candidates if mask & c.bit)))
            if key < incumbent[:3]:
                incumbent = (f1, bu, key[2], mask)
            continue

        cand = cands[idx]
        avail_mask = all_mask ^ ((1 << idx) - 1)  # 当前位及之后的候选
        lb1, lb_bytes, _ = _optimistic_lb(
            problem, mask, avail_mask, bytes_so_far, maint_so_far)
        # 字典序剪枝：f1 严格更差，或 f1 相同但字节下界已更大。f3 平局不剪，保证可证最优。
        if (lb1, lb_bytes) > (incumbent[0], incumbent[1]):
            stats['bound_pruned'] += 1
            continue

        # 分支 1：排除当前未决候选。
        stack.append((idx + 1, mask, bytes_so_far, maint_so_far))
        stats['branches'] += 1

        # 分支 2：纳入（含传递依赖），违反硬约束即剪枝。
        prior_ids = {cands[j].id for j in range(idx) if mask & cands[j].bit}
        added = closure(problem, prior_ids | {cand.id})
        new_ids = added - prior_ids
        new_mask = mask
        new_bytes, new_maint = bytes_so_far, maint_so_far
        for cid in new_ids:
            c = problem.candidate(cid)
            new_mask |= c.bit
            new_bytes += c.bytes
            new_maint += c.maintenance
        if _mutex_violation(problem, new_mask) or \
                new_bytes > problem.storage_budget or \
                new_maint > problem.maintenance_budget:
            stats['feasibility_pruned'] += 1
        else:
            stack.append((first_undecided(new_mask, idx + 1),
                          new_mask, new_bytes, new_maint))
            stats['branches'] += 1

    best_f1, best_bytes, best_ids, best_mask = incumbent
    _, _, _, best_choices = evaluate(problem, best_mask)

    if timed_out:
        frontier_lb = root_lb  # 根下界始终合法，保证前沿被取空时仍有全局下界
        for idx, mask, bytes_so_far, maint_so_far in stack:
            avail_mask = all_mask ^ ((1 << idx) - 1)
            lb1, _, _ = _optimistic_lb(problem, mask, avail_mask, bytes_so_far, maint_so_far)
            frontier_lb = min(frontier_lb, lb1)
        global_lb = min(best_f1, frontier_lb)
        status = 'incumbent_only'
        proof = {**stats, 'termination': 'limit_reached',
                 'note': '搜索未完成，global_lb 为前沿节点乐观下界最小值，不宣称最优'}
    else:
        global_lb = best_f1
        status = 'optimal'
        proof = {**stats, 'termination': 'exhausted',
                 'note': '全部子集空间经剪枝后搜索完毕，解为精确最优'}

    gap = best_f1 - global_lb
    return SolveResult(
        status=status,
        selected=best_ids,
        f1=best_f1,
        bytes_used=best_bytes,
        maintenance_used=sum((problem.candidate(cid).maintenance for cid in best_ids), Fraction(0)),
        storage_budget=problem.storage_budget,
        maintenance_budget=problem.maintenance_budget,
        choices=_exclusion_reasons(problem, best_mask, best_choices),
        global_lb=global_lb,
        gap=gap,
        proof=proof)
