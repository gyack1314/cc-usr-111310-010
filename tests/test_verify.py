"""独立验证器的反例捕获能力与下界合法性测试。"""
import copy
import unittest
from fractions import Fraction

from warehouse_data.io import load
from warehouse_data.model import parse_problem
from warehouse_data.planner import solve, SolveResult
from warehouse_data.verify import (verify_plan, enumerate_optimum,
                                   VerificationFailure)


def problem_of(name='normal'):
    raw, digest = load(f'data/{name}.json')
    return parse_problem(raw, manifest_sha256=digest)


class VerifyTests(unittest.TestCase):
    def test_tampered_objective_caught(self):
        p = problem_of()
        r = solve(p)
        object.__setattr__(r, 'f1', r.f1 - 1)
        with self.assertRaises(VerificationFailure):
            verify_plan(p, r)

    def test_tampered_selection_caught(self):
        p = problem_of()
        r = solve(p)
        object.__setattr__(r, 'selected', tuple(sorted(set(r.selected) - {'must'})))
        with self.assertRaises(VerificationFailure):
            verify_plan(p, r)

    def test_illegal_lower_bound_caught(self):
        p = problem_of()
        r = solve(p, node_limit=1)
        self.assertEqual(r.status, 'incumbent_only')
        object.__setattr__(r, 'global_lb', r.f1 + 1)  # 下界高于可行解，不合法
        with self.assertRaises(VerificationFailure):
            verify_plan(p, r)

    def test_legitimate_incumbent_passes(self):
        p = problem_of()
        r = solve(p, node_limit=2)
        self.assertTrue(verify_plan(p, r))

    def test_optimistic_lb_never_above_any_feasible_solution(self):
        """下界合法性的直接检查：全枚举每个可行解的 f1 都不小于报告下界。"""
        p = problem_of()
        r = solve(p, node_limit=1)
        n = len(p.candidates)
        for bits in range(2 ** n):
            ids = {c.id for c in p.candidates if bits & c.bit}
            from warehouse_data.model import closure
            if not closure(p, set(p.mandatory)) <= ids:
                continue
            from warehouse_data.verify import _feasible, _score
            ok, closed = _feasible(p, ids)
            if ok:
                f1, *_ = _score(p, closed)
                self.assertGreaterEqual(f1, r.global_lb)

    def test_enumeration_matches_solver_across_rational_variants(self):
        raw = load('data/normal.json')[0]
        # 引入真分数系数与费用，确保全程有理数精确。
        raw['maintenance_coefficient'] = '1/3'
        raw['queries'][0]['plans'][1]['cost'] = '7/2'
        raw['queries'][1]['plans'][1]['cost'] = '10/3'
        p = parse_problem(raw)
        r = solve(p)
        verify_plan(p, r)
        key, _ = enumerate_optimum(p)
        self.assertEqual(key[0], r.f1)
        self.assertIsInstance(r.f1, Fraction)

    def test_infeasible_certificate_numbers_recheck(self):
        p = problem_of('infeasible-mandatory-budget')
        r = solve(p)
        self.assertTrue(verify_plan(p, r))
        ev = r.infeasibility.conflicts[0]
        self.assertEqual(ev['bytes_required'], 1)
        self.assertEqual(ev['storage_budget'], 0)

    def test_forged_infeasibility_caught(self):
        p = problem_of('infeasible-mandatory-budget')
        r = solve(p)
        r.infeasibility.conflicts[0]['bytes_required'] = 99
        with self.assertRaises(VerificationFailure):
            verify_plan(p, r)


if __name__ == '__main__':
    unittest.main()
