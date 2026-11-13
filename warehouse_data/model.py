"""物理设计问题的有理数领域模型与输入校验。

所有金额、权重、费用均以非负有理字符串输入，内部统一使用 Fraction，
绝不经过 float。校验失败抛出 InputError，issues 中保留可逐条复核的原因。
"""
from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction

from .io import integer, unique


class InputError(ValueError):
    """输入清单形状或引用不合法；issues 为可重验的中文原因列表。"""

    def __init__(self, issues):
        self.issues = [str(x) for x in issues]
        super().__init__('；'.join(self.issues))


def rational(value, name):
    """解析非负有理字符串，拒绝 bool/float/缺失。"""
    if not isinstance(value, str) or not value:
        raise InputError([f'{name} 必须是非空有理字符串，收到 {value!r}'])
    try:
        parsed = Fraction(value)
    except (ValueError, ZeroDivisionError):
        raise InputError([f'{name} 不是合法有理数: {value!r}'])
    if parsed < 0:
        raise InputError([f'{name} 不能为负数: {value!r}'])
    return parsed


def _string_list(value, name, issues):
    if not isinstance(value, list):
        issues.append(f'{name} 必须是字符串列表')
        return []
    out = []
    for item in value:
        if not isinstance(item, str) or not item:
            issues.append(f'{name} 存在非字符串或空标识: {item!r}')
        elif item in out:
            issues.append(f'{name} 存在重复引用: {item}')
        else:
            out.append(item)
    return out


@dataclass(frozen=True)
class Candidate:
    id: str
    bytes: int
    maintenance: Fraction
    requires: tuple[str, ...]
    group: str | None
    bit: int = 0
    req_mask: int = 0


@dataclass(frozen=True)
class Plan:
    id: str
    cost: Fraction
    requires: frozenset[str]
    req_mask: int


@dataclass(frozen=True)
class Query:
    id: str
    weight: Fraction
    plans: tuple[Plan, ...]


@dataclass(frozen=True)
class BuilderSpec:
    actual_bytes: int
    digest: str
    fail: bool


@dataclass
class Problem:
    candidates: tuple[Candidate, ...]
    queries: tuple[Query, ...]
    storage_budget: int
    maintenance_budget: Fraction
    alpha: Fraction
    mandatory: frozenset[str]
    warehouse_version: str
    workload_version: str
    builders: dict[str, BuilderSpec]
    topo_order: tuple[str, ...]
    manifest_sha256: str | None = None

    @property
    def candidate_ids(self):
        return tuple(c.id for c in self.candidates)

    def candidate(self, cid):
        return self._by_id[cid]

    def __post_init__(self):
        self._by_id = {c.id: c for c in self.candidates}
        self._q_by_id = {q.id: q for q in self.queries}


def _topo_order(candidates, issues):
    """Kahn 拓扑排序；存在依赖环时把残余节点写进 issues。"""
    ids = [c.id for c in candidates]
    req = {c.id: set(c.requires) for c in candidates}
    resolved, remaining = [], set(ids)
    while remaining:
        ready = sorted(cid for cid in remaining if not req[cid] & remaining)
        if not ready:
            issues.append('候选依赖存在环，涉及节点: ' + ', '.join(sorted(remaining)))
            return tuple(resolved)
        # 一次取一个，保持 ID 字典序确定。
        pick = ready[0]
        resolved.append(pick)
        remaining.remove(pick)
    return tuple(resolved)


def parse_problem(raw, manifest_sha256=None):
    """把原始 JSON dict 校验并解析为 Problem；任何形状错误抛 InputError。"""
    issues = []
    if not isinstance(raw, dict):
        raise InputError(['顶层必须为对象'])

    required = ['candidates', 'queries', 'storage_budget', 'maintenance_budget',
                'maintenance_coefficient', 'mandatory', 'warehouse_version',
                'workload_version', 'builders']
    for key in required:
        if key not in raw:
            issues.append(f'缺少顶层字段: {key}')
    if issues:
        raise InputError(issues)

    # ---- 候选 ----------------------------------------------------------
    if not isinstance(raw['candidates'], list) or not raw['candidates']:
        issues.append('candidates 必须是非空列表')
    seen_ids = set()
    if isinstance(raw['candidates'], list):
        try:
            seen_ids = unique(raw['candidates'], 'id')
        except ValueError as exc:
            issues.append(f'候选 ID 重复或缺失: {exc}')
    candidates = []
    if isinstance(raw['candidates'], list):
        for row in raw['candidates']:
            if not isinstance(row, dict):
                issues.append(f'候选不是对象: {row!r}')
                continue
            cid = row.get('id')
            if not isinstance(cid, str) or not cid:
                issues.append(f'候选 ID 非法: {cid!r}')
                continue
            try:
                nbytes = integer(row.get('bytes'))
            except (ValueError, TypeError):
                issues.append(f'候选 {cid} 的 bytes 必须是非负整数')
                nbytes = 0
            maint = _safe_rational(row.get('maintenance'), f'候选 {cid} maintenance', issues)
            group = row.get('exclusive_group')
            if group is not None and not isinstance(group, str):
                issues.append(f'候选 {cid} 的 exclusive_group 必须是字符串或 null')
                group = None
            req = _string_list(row.get('requires', []), f'候选 {cid} requires', issues)
            candidates.append(Candidate(cid, nbytes, maint, tuple(req), group))

    for c in candidates:
        unknown = [r for r in c.requires if r not in seen_ids]
        if unknown:
            issues.append(f'候选 {c.id} 引用未知依赖: {", ".join(unknown)}')

    topo = _topo_order(candidates, issues) if candidates else ()

    # ---- 查询与方案 ----------------------------------------------------
    queries = []
    if not isinstance(raw['queries'], list) or not raw['queries']:
        issues.append('queries 必须是非空列表')
    else:
        try:
            unique(raw['queries'], 'id')
        except ValueError as exc:
            issues.append(f'查询 ID 重复或缺失: {exc}')
        for qrow in raw['queries']:
            if not isinstance(qrow, dict):
                issues.append(f'查询不是对象: {qrow!r}')
                continue
            qid = qrow.get('id')
            if not isinstance(qid, str) or not qid:
                issues.append(f'查询 ID 非法: {qid!r}')
                continue
            weight = _safe_rational(qrow.get('weight'), f'查询 {qid} weight', issues)
            plans_raw = qrow.get('plans')
            if not isinstance(plans_raw, list) or not plans_raw:
                issues.append(f'查询 {qid} 至少需要一个访问方案')
                continue
            plans, plan_ids = [], set()
            has_baseline = False
            for prow in plans_raw:
                if not isinstance(prow, dict):
                    issues.append(f'查询 {qid} 的方案不是对象: {prow!r}')
                    continue
                pid = prow.get('id')
                if not isinstance(pid, str) or not pid:
                    issues.append(f'查询 {qid} 存在非法方案 ID: {pid!r}')
                    continue
                if pid in plan_ids:
                    issues.append(f'查询 {qid} 方案 ID 重复: {pid}')
                    continue
                plan_ids.add(pid)
                cost = _safe_rational(prow.get('cost'), f'查询 {qid} 方案 {pid} cost', issues)
                req = _string_list(prow.get('requires', []),
                                   f'查询 {qid} 方案 {pid} requires', issues)
                unknown = [r for r in req if r not in seen_ids]
                if unknown:
                    issues.append(
                        f'查询 {qid} 方案 {pid} 引用未知候选: {", ".join(unknown)}')
                if not req:
                    has_baseline = True
                plans.append((pid, cost, req))
            if not has_baseline:
                issues.append(f'查询 {qid} 缺少 requires 为空集的基线方案')
            built = []
            for pid, cost, req in plans:
                mask = 0
                for r in req:
                    idx = next((i for i, c in enumerate(candidates) if c.id == r), None)
                    if idx is not None:
                        mask |= 1 << idx
                built.append(Plan(pid, cost, frozenset(req), mask))
            queries.append(Query(qid, weight, tuple(built)))

    # ---- 预算、系数、必选 ----------------------------------------------
    try:
        storage_budget = integer(raw['storage_budget'])
    except (ValueError, TypeError):
        issues.append('storage_budget 必须是非负整数')
        storage_budget = 0
    maintenance_budget = _safe_rational(raw['maintenance_budget'],
                                        'maintenance_budget', issues)
    alpha = _safe_rational(raw['maintenance_coefficient'],
                           'maintenance_coefficient', issues)

    mandatory = frozenset(_string_list(raw.get('mandatory', []), 'mandatory', issues))
    unknown_mandatory = sorted(mandatory - seen_ids)
    if unknown_mandatory:
        issues.append('mandatory 引用未知候选: ' + ', '.join(unknown_mandatory))

    for key in ('warehouse_version', 'workload_version'):
        if not isinstance(raw[key], str) or not raw[key]:
            issues.append(f'{key} 必须是非空字符串')

    # ---- 构建器映射 ----------------------------------------------------
    builders = {}
    braw = raw['builders']
    if not isinstance(braw, dict):
        issues.append('builders 必须是以候选 ID 为键的对象')
    else:
        extra = sorted(set(braw) - seen_ids)
        if extra:
            issues.append('builders 包含未知候选: ' + ', '.join(extra))
        for cid in sorted(seen_ids):
            spec = braw.get(cid)
            if not isinstance(spec, dict):
                issues.append(f'候选 {cid} 缺少 builder 配置')
                continue
            try:
                actual = integer(spec.get('actual_bytes'))
            except (ValueError, TypeError):
                issues.append(f'builder {cid} actual_bytes 必须是非负整数')
                actual = 0
            digest = spec.get('digest')
            if not isinstance(digest, str) or not digest:
                issues.append(f'builder {cid} digest 必须是非空字符串')
                digest = ''
            fail = spec.get('fail', False)
            if not isinstance(fail, bool):
                issues.append(f'builder {cid} fail 必须是布尔值')
                fail = False
            builders[cid] = BuilderSpec(actual, digest, fail)

    if issues:
        raise InputError(issues)

    # 依赖位掩码在全部 ID 合法后补全。
    index = {c.id: i for i, c in enumerate(candidates)}
    candidates = tuple(
        Candidate(c.id, c.bytes, c.maintenance, c.requires, c.group,
                  bit=1 << index[c.id],
                  req_mask=sum(1 << index[r] for r in c.requires))
        for c in candidates)

    return Problem(
        candidates=candidates,
        queries=tuple(queries),
        storage_budget=storage_budget,
        maintenance_budget=maintenance_budget,
        alpha=alpha,
        mandatory=mandatory,
        warehouse_version=raw['warehouse_version'],
        workload_version=raw['workload_version'],
        builders=builders,
        topo_order=topo,
        manifest_sha256=manifest_sha256)


def _safe_rational(value, name, issues):
    try:
        return rational(value, name)
    except InputError as exc:
        issues.extend(exc.issues)
        return Fraction(0)


def closure(problem, ids):
    """候选 ID 集合的递归依赖闭合（输入已校验，不会出现未知引用）。"""
    out, stack = set(), list(ids)
    by_id = {c.id: c for c in problem.candidates}
    while stack:
        cid = stack.pop()
        if cid in out:
            continue
        out.add(cid)
        stack.extend(by_id[cid].requires)
    return out
