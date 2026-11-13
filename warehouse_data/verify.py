"""独立验证器：不调用 planner 的任何内部函数，重算全部结论。

提供三类可复核检查：
  1. 评分重算：选中集合闭合性、互斥、预算、每查询方案选择、三级目标值；
  2. 小实例全枚举：枚举全部 2^n 子集，独立证明最优或给出更优反例；
  3. 无解证书：独立重算必选闭合并核对冲突证据；
     incumbent_only：核对 global_lb 对全部未搜索解是合法下界。
"""
from __future__ import annotations

from fractions import Fraction

from .model import Problem, closure


class VerificationFailure(AssertionError):
    """验证未通过；args[0] 为可复核的原因列表。"""


def _feasible(problem, chosen_ids):
    """独立的可行性判定：闭合、互斥、双预算。"""
    chosen = closure(problem, chosen_ids)
    by_id = {c.id: c for c in problem.candidates}
    groups = {}
    nbytes, maint = 0, Fraction(0)
    for cid in chosen:
        c = by_id[cid]
        if c.group is not None:
            if c.group in groups:
                return False, chosen
            groups[c.group] = cid
        nbytes += c.bytes
        maint += c.maintenance
    if nbytes > problem.storage_budget or maint > problem.maintenance_budget:
        return False, chosen
    return True, chosen


def _score(problem, chosen):
    """独立评分，返回 (f1, bytes, maintenance, 每查询选中方案)。"""
    by_id = {c.id: c for c in problem.candidates}
    nbytes = sum(by_id[cid].bytes for cid in chosen)
    maint = sum((by_id[cid].maintenance for cid in chosen), Fraction(0))
    query_total = Fraction(0)
    choices = {}
    for q in problem.queries:
        eligible = [p for p in q.plans if set(p.requires) <= chosen]
        if not eligible:
            raise VerificationFailure([f'查询 {q.id} 在选中集合下无可用方案（应至少有基线）'])
        picked = min(eligible, key=lambda p: (p.cost, p.id))
        choices[q.id] = (picked.id, picked.cost)
        query_total += q.weight * picked.cost
    return query_total + problem.alpha * maint, nbytes, maint, choices


def enumerate_optimum(problem, max_n=20):
    """全枚举 2^n 个闭合子集，独立得到字典序最优；超过 max_n 拒绝执行。"""
    n = len(problem.candidates)
    if n > max_n:
        raise VerificationFailure([f'全枚举规模 {2 ** n} 超过上限 2^{max_n}，拒绝声称穷举'])
    best = None
    mandatory = closure(problem, set(problem.mandatory))
    for bits in range(2 ** n):
        ids = {c.id for c in problem.candidates if bits & c.bit}
        # 必选候选的闭合必须全部包含，这是硬约束。
        if not mandatory <= ids:
            continue
        ok, closed = _feasible(problem, ids)
        if not ok:
            continue
        f1, nbytes, maint, _ = _score(problem, closed)
        key = (f1, nbytes, tuple(sorted(closed)))
        if best is None or key < best[0]:
            best = (key, closed)
    if best is None:
        return None
    return best[0], tuple(sorted(best[1]))


def verify_plan(problem, result, enumerate_if_small=True, max_n=16):
    """复核 SolveResult，全部通过返回 True，否则抛 VerificationFailure。"""
    failures = []
    by_id = {c.id: c for c in problem.candidates}
    selected = set(result.selected)

    if result.status == 'infeasible':
        return _verify_infeasible(problem, result)

    # 1) 必选项与闭合。
    closed = closure(problem, selected)
    if closed != selected:
        failures.append(f'选中集合未依赖闭合，缺少: {sorted(closed - selected)}')
    missing_mandatory = set(problem.mandatory) - selected
    if missing_mandatory:
        failures.append('缺少必选候选: ' + ', '.join(sorted(missing_mandatory)))

    # 2) 互斥与预算。
    ok, _ = _feasible(problem, selected)
    if not ok:
        failures.append('报告的最优解自身不满足互斥或预算约束')
    if result.bytes_used != sum(by_id[cid].bytes for cid in selected):
        failures.append('报告字节数与独立重算不一致')
    maint = sum((by_id[cid].maintenance for cid in selected), Fraction(0))
    if result.maintenance_used != maint:
        failures.append('报告维护费与独立重算不一致')

    # 3) 每查询方案选择。
    f1, nbytes, maint_check, choices = _score(problem, selected)
    if f1 != result.f1:
        failures.append(f'f1 不一致: 报告 {result.f1}，重算 {f1}')
    if nbytes != result.bytes_used:
        failures.append('字节目标值不一致')
    reported_choices = {c.query_id: (c.plan_id, c.cost) for c in result.choices}
    if reported_choices != choices:
        failures.append(f'每查询选中方案不一致: 报告 {reported_choices}，重算 {choices}')
    for row in result.choices:
        for pid, reason in row.excludes:
            if not reason:
                failures.append(f'查询 {row.query_id} 方案 {pid} 缺少排除原因')

    # 4) 预算占用展示。
    if result.bytes_used > result.storage_budget:
        failures.append('存储超预算却被报告为可行')
    if result.maintenance_used > result.maintenance_budget:
        failures.append('维护费超预算却被报告为可行')

    # 5) 最优性 / 下界合法性。
    if result.status == 'optimal' and enumerate_if_small and len(problem.candidates) <= max_n:
        brute = enumerate_optimum(problem)
        if brute is None:
            failures.append('全枚举认为无可行解，与求解器结论矛盾')
        else:
            (bf1, bbytes, bids), bsel = brute
            if (bf1, bbytes, bids) != (result.f1, result.bytes_used, tuple(result.selected)):
                failures.append(
                    f'全枚举得到更优/不同解 {list(bsel)} = ({bf1}, {bbytes})，'
                    f'报告解 = ({result.f1}, {result.bytes_used})')
    if result.status == 'incumbent_only':
        if result.global_lb is None or result.global_lb > result.f1:
            failures.append('global_lb 缺失或大于当前可行解目标值，下界不合法')
        if result.gap != result.f1 - result.global_lb:
            failures.append('最优性差距与 f1-global_lb 不一致')
    else:
        if result.global_lb != result.f1 or result.gap != 0:
            failures.append('最优解的 global_lb 必须等于 f1 且差距为 0')

    if failures:
        raise VerificationFailure(failures)
    return True


def _verify_infeasible(problem, result):
    failures = []
    if result.infeasibility is None or not result.infeasibility.conflicts:
        raise VerificationFailure(['infeasible 状态缺少冲突证据'])
    mc = closure(problem, set(problem.mandatory))
    by_id = {c.id: c for c in problem.candidates}
    nbytes = sum(by_id[cid].bytes for cid in mc)
    maint = sum((by_id[cid].maintenance for cid in mc), Fraction(0))
    groups = [by_id[cid].group for cid in mc if by_id[cid].group is not None]
    real_conflicts = (
        len(groups) != len(set(groups))
        or nbytes > problem.storage_budget
        or maint > problem.maintenance_budget)
    if not real_conflicts:
        failures.append('必选闭合实际满足全部硬约束，无解结论不成立')
    # 证书中的每条证据必须能对上真实数字。
    for ev in result.infeasibility.conflicts:
        t = ev['type']
        if t == 'mandatory_storage_over_budget':
            if ev['bytes_required'] != nbytes or ev['storage_budget'] != problem.storage_budget:
                failures.append('存储冲突证据数字无法重验')
        elif t == 'mandatory_maintenance_over_budget':
            if Fraction(ev['maintenance_required']) != maint or \
                    Fraction(ev['maintenance_budget']) != problem.maintenance_budget:
                failures.append('维护费冲突证据数字无法重验')
        elif t == 'mandatory_mutex':
            if not all(cid in mc and by_id[cid].group == ev['group']
                       for cid in ev['candidates']):
                failures.append('互斥冲突证据无法重验')
        else:
            failures.append(f'未知冲突类型: {t}')
    if failures:
        raise VerificationFailure(failures)
    return True
