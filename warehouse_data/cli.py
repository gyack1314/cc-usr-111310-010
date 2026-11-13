"""命令行入口：inspect 清点资料，plan 精确规划，verify 独立复核，build 构建切换。"""
import argparse
import json
import sys
import time
from pathlib import Path

from .io import load
from .domain import summarize
from .model import InputError, parse_problem
from .planner import solve
from .verify import verify_plan, VerificationFailure, enumerate_optimum
from .builder import PhysicalStore, execute_plan, BuildAborted
from .report import plan_to_dict, build_to_dict


def _open(path):
    raw, digest = load(path)
    return parse_problem(raw, manifest_sha256=digest)


def _emit(obj, code=0):
    print(json.dumps(obj, ensure_ascii=False, indent=2))
    return code


def cmd_inspect(args):
    v, digest = load(args.file)
    return _emit({'manifest_sha256': digest,
                  'summary': summarize(v, Path(args.file).parent)})


def cmd_plan(args):
    problem = _open(args.file)
    deadline = None if args.timeout is None else time.monotonic() + args.timeout
    result = solve(problem, deadline=deadline, node_limit=args.node_limit)
    out = plan_to_dict(problem, result)
    if args.verify:
        try:
            verify_plan(problem, result)
            out['independent_verification'] = 'passed'
        except VerificationFailure as exc:
            out['independent_verification'] = {'failed': exc.args[0]}
            return _emit(out, 2)
    return _emit(out)


def cmd_verify(args):
    problem = _open(args.file)
    deadline = None if args.timeout is None else time.monotonic() + args.timeout
    result = solve(problem, deadline=deadline, node_limit=args.node_limit)
    try:
        verify_plan(problem, result)
    except VerificationFailure as exc:
        return _emit({'verified': False, 'failures': exc.args[0]}, 2)
    brute = None
    if len(problem.candidates) <= args.enumerate_max:
        opt = enumerate_optimum(problem)
        if opt is None:
            brute = {'exhaustive_subsets': 2 ** len(problem.candidates),
                     'feasible_solution_exists': False}
        else:
            brute = {'exhaustive_subsets': 2 ** len(problem.candidates),
                     'optimum_ids': list(opt[1]),
                     'objective': [str(opt[0][0]), opt[0][1], list(opt[0][2])]}
    return _emit({'verified': True, 'status': result.status,
                  'selected': list(result.selected),
                  'f1': str(result.f1) if result.f1 is not None else None,
                  'global_lower_bound': str(result.global_lb)
                  if result.global_lb is not None else None,
                  'gap': str(result.gap) if result.gap is not None else None,
                  'exhaustive_cross_check': brute})


def cmd_build(args):
    problem = _open(args.file)
    result = solve(problem)
    if result.status == 'infeasible':
        return _emit({'switched': False,
                      'reason': '计划无解，未执行构建',
                      'infeasibility': result.infeasibility.conflicts}, 3)
    store = PhysicalStore(args.catalog_root)
    try:
        report = execute_plan(problem, result, store,
                              expected_manifest_sha256=problem.manifest_sha256)
    except BuildAborted as exc:
        return _emit({'switched': False, 'reason': str(exc)}, 3)
    return _emit(build_to_dict(report), 0 if report.switched else 3)


def main(argv=None):
    parser = argparse.ArgumentParser(prog='warehouse_data')
    sub = parser.add_subparsers(dest='command', required=True)

    p = sub.add_parser('inspect', help='清点原始资料，不求解不构建')
    p.add_argument('file')
    p.set_defaults(func=cmd_inspect)

    p = sub.add_parser('plan', help='精确物理设计规划')
    p.add_argument('file')
    p.add_argument('--timeout', type=float, default=None, help='秒；超时只报当前可行解与全局下界')
    p.add_argument('--node-limit', type=int, default=None)
    p.add_argument('--verify', action='store_true', help='规划后立即独立验证')
    p.set_defaults(func=cmd_plan)

    p = sub.add_parser('verify', help='独立复核，小实例附全枚举对照')
    p.add_argument('file')
    p.add_argument('--timeout', type=float, default=None)
    p.add_argument('--node-limit', type=int, default=None)
    p.add_argument('--enumerate-max', type=int, default=16)
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser('build', help='按计划构建副本并原子切换物理目录')
    p.add_argument('file')
    p.add_argument('--catalog-root', required=True)
    p.set_defaults(func=cmd_build)

    args = parser.parse_args(argv)
    try:
        code = args.func(args)
    except InputError as exc:
        print(json.dumps({'input_error': exc.issues}, ensure_ascii=False, indent=2),
              file=sys.stderr)
        return 4
    return code or 0


if __name__ == '__main__':
    raise SystemExit(main())
