"""求解器精确性、目标字典序、下界合法性与输入错误测试。"""
import copy
import unittest
from fractions import Fraction

from warehouse_data.io import load
from warehouse_data.model import parse_problem, InputError, closure
from warehouse_data.planner import solve, evaluate, infeasibility_certificate
from warehouse_data.verify import enumerate_optimum, verify_plan


def problem_of(name):
    raw, digest = load(f'data/{name}.json')
    return parse_problem(raw, manifest_sha256=digest)


class NormalFixtureTests(unittest.TestCase):
    def setUp(self):
        self.problem = problem_of('normal')
        self.result = solve(self.problem)

    def test_complementary_combo_beats_singles(self):
        """dict+sortA 组合对 q1 的收益不可由单项相加得到，且组合优于两个单项。"""
        by_id = {c.id: c for c in self.problem.candidates}
        mask = lambda ids: sum(by_id[cid].bit for cid in ids)
        must = {'must'}
        f_dict, *_ = evaluate(self.problem, mask(must | {'dict'}))
        f_sortb, *_ = evaluate(self.problem, mask(must | {'sortB'}))
        f_combo, *_ = evaluate(self.problem, mask(must | {'dict', 'sortA'}))
        self.assertLess(f_combo, f_dict)
        self.assertLess(f_combo, f_sortb)
        # 单项 dict 无法启用 q1 的方案 a（同时需要 sortA），证明收益不可简单相加。
        q1 = self.problem.queries[0]
        eligible_dict = {p.id for p in q1.plans
                         if p.req_mask & ~mask(must | {'dict'}) == 0}
        self.assertEqual(eligible_dict, {'base'})

    def test_shared_dict_charged_once(self):
        """同一字典被两个查询共享，存储只收一次。"""
        self.assertEqual(set(self.result.selected), {'dict', 'sortA', 'must'})
        self.assertEqual(self.result.bytes_used, 7)  # 2+4+1，而非按引用重复计费
        choices = {c.query_id: c.plan_id for c in self.result.choices}
        self.assertEqual(choices, {'q1': 'a', 'q2': 'shared'})

    def test_zero_benefit_mandatory_still_selected(self):
        """must 零收益但必选，必须出现在解中并计费。"""
        self.assertIn('must', self.result.selected)
        self.assertTrue(self.result.bytes_used >= 1)
        self.assertEqual(self.result.maintenance_used, Fraction(2))

    def test_budget_used_exactly_equals_limit(self):
        """存储预算 7 恰好用尽（边界相等不算超）。"""
        self.assertEqual(self.result.bytes_used, self.problem.storage_budget)
        self.assertEqual(self.result.status, 'optimal')
        self.assertEqual(self.result.gap, 0)
        self.assertEqual(self.result.global_lb, self.result.f1)

    def test_objective_value_rational(self):
        # q1: 2*2, q2: 1*3, 维护 2 * 1/2 = 1 -> 4+3+1 = 8
        self.assertEqual(self.result.f1, Fraction(8))

    def test_exclusion_reasons_cover_all_non_picked_plans(self):
        rows = {c.query_id: c for c in self.result.choices}
        reasons = dict(rows['q1'].excludes)
        self.assertIn('sortB', reasons['b'])
        self.assertTrue(reasons['b'].startswith('未满足依赖'))
        self.assertIn('费用不优于', reasons['base'])

    def test_independent_verifier_and_brute_force(self):
        self.assertTrue(verify_plan(self.problem, self.result))
        key, ids = enumerate_optimum(self.problem)
        self.assertEqual(tuple(ids), tuple(self.result.selected))
        self.assertEqual(key[0], self.result.f1)
        self.assertEqual(key[1], self.result.bytes_used)

    def test_brute_force_all_small_fixtures(self):
        for name in ('normal', 'builder-over-budget', 'infeasible-mandatory-budget'):
            with self.subTest(file=name):
                p = problem_of(name)
                r = solve(p)
                verify_plan(p, r)


class TieBreakTests(unittest.TestCase):
    def _raw(self):
        return copy.deepcopy(load('data/normal.json')[0])

    def test_lexicographic_id_tie_break(self):
        """f1 与字节完全相同的两个解，选升序 ID 字典序更小者。"""
        raw = self._raw()
        # sortA 与 sortB 调成同字节同维护同方案费用，去掉 dict 依赖使两条路线对称。
        for c in raw['candidates']:
            if c['id'] == 'sortA':
                c['bytes'] = 5
                c['requires'] = []
        q1 = next(q for q in raw['queries'] if q['id'] == 'q1')
        for p in q1['plans']:
            if p['id'] == 'a':
                p['requires'] = ['sortA']
                p['cost'] = '8'
        raw['storage_budget'] = 8
        p = parse_problem(raw)
        r = solve(p)
        verify_plan(p, r)
        # sortA 与 sortB 同费同字节，字典序 sortA < sortB。
        self.assertIn('sortA', r.selected)
        self.assertNotIn('sortB', r.selected)

    def test_second_objective_bytes(self):
        """f1 相同但字节更少的解优先（必选 must 外去掉多余候选）。"""
        raw = self._raw()
        # 让所有方案费用都等于基线，任何副本都无查询收益；最优只含必选闭合。
        for q in raw['queries']:
            base_cost = next(p['cost'] for p in q['plans'] if not p['requires'])
            for p in q['plans']:
                p['cost'] = base_cost
        p = parse_problem(raw)
        r = solve(p)
        verify_plan(p, r)
        self.assertEqual(set(r.selected), set(closure(p, set(p.mandatory))))


class LimitTests(unittest.TestCase):
    def test_node_limit_returns_incumbent_with_valid_lb(self):
        p = problem_of('normal')
        r = solve(p, node_limit=1)
        self.assertEqual(r.status, 'incumbent_only')
        self.assertIsNotNone(r.global_lb)
        self.assertLessEqual(r.global_lb, r.f1)
        self.assertEqual(r.gap, r.f1 - r.global_lb)
        # 不允许宣称最优；证明文本必须说明未搜完。
        self.assertEqual(r.proof['termination'], 'limit_reached')
        verify_plan(p, r)

    def test_deadline_path(self):
        p = problem_of('normal')
        r = solve(p, deadline=0.0, time_fn=lambda: 1.0)
        self.assertEqual(r.status, 'incumbent_only')
        self.assertIsNotNone(r.global_lb)


class InfeasibleTests(unittest.TestCase):
    def test_mandatory_over_budget_certificate(self):
        p = problem_of('infeasible-mandatory-budget')
        r = solve(p)
        self.assertEqual(r.status, 'infeasible')
        self.assertIsNone(r.f1)
        types = [c['type'] for c in r.infeasibility.conflicts]
        self.assertIn('mandatory_storage_over_budget', types)
        verify_plan(p, r)

    def test_mutex_infeasibility(self):
        raw = load('data/normal.json')[0]
        raw['mandatory'] = ['sortA', 'sortB']
        p = parse_problem(raw)
        r = solve(p)
        self.assertEqual(r.status, 'infeasible')
        self.assertEqual(r.infeasibility.conflicts[0]['type'], 'mandatory_mutex')
        verify_plan(p, r)

    def test_certificate_rejects_false_infeasibility(self):
        # 正常夹具不应产生无解证书。
        p = problem_of('normal')
        self.assertIsNone(infeasibility_certificate(p))


class InputErrorTests(unittest.TestCase):
    def _raw(self):
        return copy.deepcopy(load('data/normal.json')[0])

    def test_negative_budget(self):
        with self.assertRaises(InputError) as ctx:
            parse_problem(load('data/invalid-config.json')[0])
        self.assertTrue(any('storage_budget' in x for x in ctx.exception.issues))

    def test_dependency_cycle(self):
        raw = self._raw()
        raw['candidates'][0]['requires'] = ['sortA']   # dict <-> sortA 成环
        with self.assertRaises(InputError) as ctx:
            parse_problem(raw)
        self.assertTrue(any('环' in x for x in ctx.exception.issues))

    def test_unknown_reference(self):
        raw = self._raw()
        raw['candidates'][1]['requires'] = ['ghost']
        with self.assertRaises(InputError) as ctx:
            parse_problem(raw)
        self.assertTrue(any('未知' in x for x in ctx.exception.issues))

    def test_missing_baseline_plan(self):
        raw = self._raw()
        raw['queries'][0]['plans'] = [p for p in raw['queries'][0]['plans']
                                      if p['requires']]
        with self.assertRaises(InputError) as ctx:
            parse_problem(raw)
        self.assertTrue(any('基线' in x for x in ctx.exception.issues))

    def test_float_and_bad_rational_rejected(self):
        raw = self._raw()
        raw['maintenance_coefficient'] = 0.5
        with self.assertRaises(InputError):
            parse_problem(raw)
        raw2 = self._raw()
        raw2['queries'][0]['weight'] = 'not-a-number'
        with self.assertRaises(InputError):
            parse_problem(raw2)

    def test_builder_mapping_must_cover_all(self):
        raw = self._raw()
        del raw['builders']['dict']
        with self.assertRaises(InputError) as ctx:
            parse_problem(raw)
        self.assertTrue(any('builder' in x for x in ctx.exception.issues))


if __name__ == '__main__':
    unittest.main()
