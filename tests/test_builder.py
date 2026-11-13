"""构建编排：依赖顺序、超预算阻断、失败不切路由、短事务原子切换。"""
import os
import tempfile
import unittest

from warehouse_data.io import load
from warehouse_data.model import parse_problem
from warehouse_data.planner import solve
from warehouse_data.builder import (PhysicalStore, LocalBuilder, execute_plan,
                                    BuildAborted)


def problem_of(name='normal'):
    raw, digest = load(f'data/{name}.json')
    return parse_problem(raw, manifest_sha256=digest)


class BuilderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        self.store = PhysicalStore(self.root)
        self.baseline = self.store.current_catalog()

    def tearDown(self):
        self.tmp.cleanup()

    def test_dependency_order_build_and_switch(self):
        p = problem_of()
        r = solve(p)
        report = execute_plan(p, r, self.store)
        self.assertTrue(report.switched)
        # sortA 依赖 dict，构建顺序中 dict 必须在前。
        order = [s.candidate_id for s in report.steps if s.status == 'built']
        self.assertLess(order.index('dict'), order.index('sortA'))
        self.assertNotEqual(self.store.current_catalog(), self.baseline)
        # 逻辑表内容不变：baseline 目录仍存在且 current 之外无破坏。
        self.assertTrue(os.path.isdir(self.baseline))
        for s in report.steps:
            self.assertEqual(s.status, 'built')
            self.assertIsNotNone(s.digest)

    def test_over_budget_blocks_and_keeps_old_routing(self):
        p = problem_of('builder-over-budget')
        r = solve(p)
        report = execute_plan(p, r, self.store)
        self.assertFalse(report.switched)
        self.assertEqual(self.store.current_catalog(), self.baseline)
        blocked = [s for s in report.steps if s.status == 'blocked']
        self.assertEqual(len(blocked), 1)
        self.assertEqual(blocked[0].candidate_id, 'sortA')
        self.assertTrue(blocked[0].detail.startswith('实测 8 字节超过计划值 4'))
        # 已构建的暂存产物不得登记为可用目录。
        self.assertIsNone(report.new_catalog)

    def test_builder_failure_does_not_reroute_queries(self):
        raw = load('data/normal.json')[0]
        raw['builders']['sortA']['fail'] = True
        p = parse_problem(raw)
        r = solve(p)
        report = execute_plan(p, r, self.store, LocalBuilder())
        self.assertFalse(report.switched)
        self.assertEqual(self.store.current_catalog(), self.baseline)
        self.assertEqual(report.steps[-1].status, 'failed')

    def test_missing_digest_blocks(self):
        raw = load('data/normal.json')[0]
        raw['builders']['dict']['digest'] = ''
        with self.assertRaises(Exception):
            parse_problem(raw)  # 输入阶段即拒绝

    def test_manifest_drift_aborts_short_transaction(self):
        p = problem_of()
        r = solve(p)
        # 规划时清单摘要为 p.manifest_sha256，切换前用不同摘要复核必须中止。
        self.assertIsNotNone(p.manifest_sha256)
        report = execute_plan(
            p, r, self.store,
            expected_manifest_sha256=p.manifest_sha256 + 'drift')
        self.assertFalse(report.switched)
        self.assertEqual(self.store.current_catalog(), self.baseline)
        self.assertIn('清单摘要', report.reason)

    def test_cumulative_actual_bytes_budget_guard(self):
        """单点实测均不超计划值，但实测累计超总预算时仍阻断、不切换。

        两个必选候选各 4 字节、计划预算 8；求解通过后把构建期预算收紧到 7，
        builder 实测 x=4、y=4（均不超自身计划值），累计 8 > 7 必须阻断。
        """
        from warehouse_data.model import Problem, Candidate, Query, Plan, BuilderSpec
        from fractions import Fraction
        cands = (
            Candidate('x', bytes=4, maintenance=Fraction(0), requires=(), group=None, bit=1),
            Candidate('y', bytes=4, maintenance=Fraction(0), requires=(), group=None, bit=2),
        )
        q = Query('q', Fraction(1), (Plan('base', Fraction(10), frozenset(), 0),))
        problem = Problem(
            candidates=cands, queries=(q,), storage_budget=7,
            maintenance_budget=Fraction(1), alpha=Fraction(0),
            mandatory=frozenset({'x', 'y'}), warehouse_version='t',
            workload_version='t',
            builders={'x': BuilderSpec(4, 'dx', False),
                      'y': BuilderSpec(4, 'dy', False)},
            topo_order=('x', 'y'))
        # 预算 7 下必选闭合计划合计 8 已无解；预算 8 放行求解，构建期再收紧到 7。
        problem.storage_budget = 8
        r = solve(problem)
        self.assertEqual(set(r.selected), {'x', 'y'})
        problem.storage_budget = 7   # 构建期预算收紧，触发累计守卫
        report = execute_plan(problem, r, self.store)
        self.assertFalse(report.switched)
        self.assertEqual(self.store.current_catalog(), self.baseline)
        self.assertEqual(report.steps[-1].status, 'blocked')
        self.assertIn('存储预算', report.reason)

    def test_infeasible_plan_refuses_build(self):
        p = problem_of('infeasible-mandatory-budget')
        r = solve(p)
        with self.assertRaises(BuildAborted):
            execute_plan(p, r, self.store)
        self.assertEqual(self.store.current_catalog(), self.baseline)


if __name__ == '__main__':
    unittest.main()
