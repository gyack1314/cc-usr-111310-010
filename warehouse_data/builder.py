"""本地可信 builder 编排与物理目录原子切换。

构建阶段不参与求解，只按既定计划执行：
  * 按依赖拓扑顺序调用本地可信 builder（纯 Python 映射，不执行 shell）；
  * 仅当实际摘要存在且实际字节不超过该候选计划值时才登记为可用；
  * 任一步失败立即阻断，查询不改变路由，旧物理目录保持可用；
  * 全部成功后在短事务中再次核对基准仓库版本、清单摘要与预算，再原子切换；
  * 逻辑表名与内容不变，只切换物理副本目录。
"""
from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from fractions import Fraction

from .model import Problem


class BuildAborted(Exception):
    """构建被阻断；payload 为可复核原因，旧目录保持可用。"""


@dataclass
class BuildStep:
    candidate_id: str
    status: str                 # built | blocked | failed
    actual_bytes: int | None
    digest: str | None
    detail: str


@dataclass
class BuildReport:
    switched: bool
    baseline_catalog: str | None
    new_catalog: str | None
    steps: tuple[BuildStep, ...]
    total_actual_bytes: int
    plan_bytes: int
    storage_budget: int
    reason: str | None


class LocalBuilder:
    """可信本地构建器：按 builder 映射返回实测字节与内容摘要，fail=True 受控失败。"""

    def build(self, problem, candidate_id):
        spec = problem.builders[candidate_id]
        if spec.fail:
            raise BuildAborted(f'builder {candidate_id} 受控失败')
        if spec.actual_bytes < 0:
            raise BuildAborted(f'builder {candidate_id} 实测字节非法')
        return spec.actual_bytes, spec.digest


class PhysicalStore:
    """物理副本目录管理；baseline 为当前在线目录，切换通过同目录 rename 原子完成。"""

    def __init__(self, root):
        self.root = os.path.abspath(root)
        os.makedirs(os.path.join(self.root, 'revisions'), exist_ok=True)
        self.baseline = os.path.join(self.root, 'revisions', 'baseline')
        os.makedirs(self.baseline, exist_ok=True)
        current = os.path.join(self.root, 'current')
        if not os.path.lexists(current):
            os.symlink(self.baseline, current)

    def current_catalog(self):
        return os.path.realpath(os.path.join(self.root, 'current'))

    def write_artifact(self, revision_dir, candidate_id, digest, actual_bytes):
        path = os.path.join(revision_dir, candidate_id)
        payload = f'{candidate_id}\n{digest}\n{actual_bytes}\n'.encode('utf8')
        with open(path, 'wb') as fh:
            fh.write(payload)
        return path, hashlib.sha256(payload).hexdigest()

    def switch(self, revision_dir):
        """同文件系统内原子 rename，失败时 current 仍指向旧目录。"""
        link = os.path.join(self.root, 'current.tmp')
        if os.path.lexists(link):
            os.unlink(link)
        os.symlink(revision_dir, link)
        os.replace(link, os.path.join(self.root, 'current'))


def execute_plan(problem, result, store, builder=None, expected_manifest_sha256=None):
    """按 SolveResult 执行构建并在短事务复核后切换物理目录。"""
    if result.status == 'infeasible':
        raise BuildAborted('计划无解，拒绝构建')
    builder = builder or LocalBuilder()
    by_id = {c.id: c for c in problem.candidates}
    selected = list(result.selected)
    order = [cid for cid in problem.topo_order if cid in selected]
    plan_bytes = sum(by_id[cid].bytes for cid in selected)

    baseline_catalog = store.current_catalog()
    revision_tag = _short(
        f'{problem.warehouse_version}|{problem.manifest_sha256}|{plan_bytes}')
    revision_dir = os.path.join(store.root, 'revisions',
                                f'{problem.warehouse_version}-{revision_tag}')
    os.makedirs(revision_dir, exist_ok=False)

    steps = []
    total_actual = 0
    reason = None
    try:
        for cid in order:
            c = by_id[cid]
            try:
                actual_bytes, digest = builder.build(problem, cid)
            except BuildAborted as exc:
                steps.append(BuildStep(cid, 'failed', None, None, str(exc)))
                reason = f'候选 {cid} 构建失败，计划阻断，查询保持原路由: {exc}'
                raise
            if not digest:
                steps.append(BuildStep(cid, 'blocked', actual_bytes, digest,
                                       '缺少内容摘要，拒绝登记'))
                reason = f'候选 {cid} 无摘要，计划阻断'
                raise BuildAborted(reason)
            if actual_bytes > c.bytes:
                steps.append(BuildStep(cid, 'blocked', actual_bytes, digest,
                                       f'实测 {actual_bytes} 字节超过计划值 {c.bytes}，'
                                       '拒绝登记，不挪用其他未用预算'))
                reason = f'候选 {cid} 实测字节超计划值'
                raise BuildAborted(reason)
            store.write_artifact(revision_dir, cid, digest, actual_bytes)
            total_actual += actual_bytes
            if total_actual > problem.storage_budget:
                steps.append(BuildStep(cid, 'blocked', actual_bytes, digest,
                                       f'累计实际字节 {total_actual} 超过存储预算 '
                                       f'{problem.storage_budget}'))
                reason = '累计实际字节超存储预算，计划阻断'
                raise BuildAborted(reason)
            steps.append(BuildStep(cid, 'built', actual_bytes, digest,
                                   '已登记为可用'))

        # 全部构建完成后再做一次总量复核，防止任何路径绕过逐步骤守卫。
        if total_actual > problem.storage_budget:
            reason = (f'构建后复核：累计实际字节 {total_actual} 超过存储预算 '
                      f'{problem.storage_budget}，计划阻断')
            raise BuildAborted(reason)

        # ---- 短事务：切换前再次核对基准、清单身份与预算 ----------------
        if store.current_catalog() != baseline_catalog:
            raise BuildAborted('切换前复核失败：在线基准目录已漂移，中止切换')
        if expected_manifest_sha256 is not None and \
                expected_manifest_sha256 != problem.manifest_sha256:
            raise BuildAborted('切换前复核失败：清单摘要与规划时不一致，中止切换')
        if total_actual > problem.storage_budget or plan_bytes > problem.storage_budget:
            raise BuildAborted('切换前复核失败：预算占用不合法，中止切换')
        maint = sum((by_id[cid].maintenance for cid in selected), Fraction(0))
        if maint > problem.maintenance_budget:
            raise BuildAborted('切换前复核失败：维护预算超限，中止切换')
        store.switch(revision_dir)
        switched = True
    except BuildAborted as exc:
        switched = False
        reason = reason or str(exc)
        # 未切换的暂存目录保留为证据，但不登记为可用。
        return BuildReport(False, baseline_catalog, None, tuple(steps),
                           total_actual, plan_bytes, problem.storage_budget, reason)

    return BuildReport(True, baseline_catalog, store.current_catalog(),
                       tuple(steps), total_actual, plan_bytes,
                       problem.storage_budget, None)


def _short(value):
    digest = hashlib.sha256(str(value).encode('utf8')).hexdigest()
    return digest[:8]
