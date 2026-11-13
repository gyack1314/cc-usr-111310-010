"""求解与构建结果的 JSON 序列化；有理数保持分数字符串，不经过 float。"""
from __future__ import annotations

from fractions import Fraction


def q(value):
    """Fraction -> 有理字符串；整数不带分母。"""
    if value is None:
        return None
    if isinstance(value, Fraction):
        return str(value)
    return str(value)


def plan_to_dict(problem, result):
    if result.status == 'infeasible':
        return {
            'status': 'infeasible',
            'warehouse_version': problem.warehouse_version,
            'workload_version': problem.workload_version,
            'manifest_sha256': problem.manifest_sha256,
            'proof': result.proof,
            'infeasibility': {'conflicts': result.infeasibility.conflicts},
        }
    return {
        'status': result.status,
        'warehouse_version': problem.warehouse_version,
        'workload_version': problem.workload_version,
        'manifest_sha256': problem.manifest_sha256,
        'selected_candidates': list(result.selected),
        'objective': {
            'weighted_query_cost_plus_alpha_maintenance': q(result.f1),
            'global_lower_bound': q(result.global_lb),
            'optimality_gap': q(result.gap),
        },
        'budget_usage': {
            'storage': {
                'used_bytes': result.bytes_used,
                'budget_bytes': result.storage_budget,
                'remaining_bytes': result.storage_budget - result.bytes_used,
            },
            'maintenance': {
                'used': q(result.maintenance_used),
                'budget': q(result.maintenance_budget),
                'remaining': q(result.maintenance_budget - result.maintenance_used),
            },
        },
        'query_plans': [
            {
                'query_id': row.query_id,
                'selected_plan': row.plan_id,
                'plan_cost': q(row.cost),
                'weight': q(row.weight),
                'weighted_cost': q(row.weight * row.cost),
                'excluded_plans': [
                    {'plan_id': pid, 'reason': reason}
                    for pid, reason in row.excludes
                ],
            }
            for row in result.choices
        ],
        'proof': result.proof,
    }


def build_to_dict(report):
    return {
        'switched': report.switched,
        'baseline_catalog': report.baseline_catalog,
        'new_catalog': report.new_catalog,
        'total_actual_bytes': report.total_actual_bytes,
        'plan_bytes': report.plan_bytes,
        'storage_budget': report.storage_budget,
        'reason': report.reason,
        'steps': [
            {
                'candidate_id': s.candidate_id,
                'status': s.status,
                'actual_bytes': s.actual_bytes,
                'digest': s.digest,
                'detail': s.detail,
            }
            for s in report.steps
        ],
    }
