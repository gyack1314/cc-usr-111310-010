# 查询工作负载驱动的仓库物理设计选择

此项目保存生产与供应链协同的湖仓原始资料，并在固定负载、测量成本与空间限制下给出**可独立验证的物理设计规划**：应建哪些分区布局、排序副本与辅助字典。设备、批次、组织、事件和成本均为合成，不连接真实工厂或生产仓库，也不生成 SQL 优化器或估计器。

Python3.11 以上，无第三方 Python 依赖。项目根目录运行：

```sh
python -m unittest discover -s tests -v
python -m compileall -q warehouse_data tests
python -m warehouse_data.cli inspect data/normal.json
```

隔离模式（忽略用户站点与当前目录注入，显式指定顶层目录）：

```sh
python -I -m unittest discover -s tests -t . -v
```

## 四个子命令

| 命令 | 作用 |
| --- | --- |
| `inspect <file>` | 只读清点候选、查询、数字预算，不求解、不构建 |
| `plan <file> [--timeout 秒] [--node-limit N] [--verify]` | 精确分支定界求解，输出每查询选中方案、排除原因、预算占用与最优性差距 |
| `verify <file>` | 独立验证器复核；小实例附 2^n 全枚举对照 |
| `build <file> --catalog-root DIR` | 按计划依赖顺序调用本地可信 builder，短事务复核后原子切换物理目录 |

```sh
python -m warehouse_data.cli plan data/normal.json --verify
python -m warehouse_data.cli verify data/normal.json
python -m warehouse_data.cli build data/normal.json --catalog-root /tmp/pd-catalog
```

## 模块划分

- `warehouse_data.model`：有理数（`fractions.Fraction`）领域模型与输入校验。依赖环、未知引用、缺空集基线、非有理数字、builder 缺项等先报 `InputError`，原因可逐条复核。
- `warehouse_data.planner`：精确分支定界。硬约束为存储预算、维护预算、必选项与互斥组；目标按字典序最小化 `sum(weight*cost) + α*sum(maintenance)`，再最小化实际字节，再按升序候选 ID 字典序。下界把未决候选乐观视为免费可得，对真实可行解空间始终合法；超时或节点耗尽只报当前可行解与全局下界（`incumbent_only`），不宣称最优。无解时输出必选闭合的可重验冲突证据，不凭贪心失败断言。
- `warehouse_data.verify`：不调用求解器内部函数，独立重算闭合性、互斥、预算与每查询方案；小实例全枚举 2^n 个子集独立证明最优，并能识别被篡改的目标值、选择集合或非法下界。
- `warehouse_data.builder`：本地可信 builder（纯 Python 映射，不执行 shell）。按拓扑顺序构建，仅当摘要存在且实测字节不超该候选计划值、累计不超总预算才登记；任一步失败即阻断，查询保持原路由。全部成功后在短事务再次核对基准目录、清单摘要与预算，再以同目录 `os.replace` 原子切换 `current` 符号链接，逻辑表内容不变。
- `warehouse_data.report`：JSON 序列化，有理数以分数字符串输出，绝不经过 float。

## 夹具场景

- `normal.json`：互补组合（dict+sortA 共同服务 q1，收益不可由单项相加）、共享字典（dict 同时服务 q2，存储只收一次）、零收益必选 `must`、存储预算 7 恰好用尽。
- `builder-over-budget.json`：sortA 实测 8 字节超过计划值 4，构建被阻止，旧访问方案仍可用。
- `infeasible-mandatory-budget.json`：必选闭合需要 1 字节而预算为 0，输出无解冲突证据。
- `invalid-config.json`：基本形状错误，输入阶段拒绝。

[资料约定](docs/data-contract.md)定义字段、类型、时间、数量和版本范围。`inspect` 只读原始资料，不生成仓库新版本或计算领域结果。null 与缺字段分别保留，有理数和 decimal 用字符串存储，不能先经 float 丢精度。

Dockerfile 固定 Linux 镜像，`docker build -t warehouse-material .` 构建；`docker run --rm warehouse-material` 显示摘要；`docker run --rm warehouse-material python -m unittest discover -s tests -v` 运行全部检查。所有资料本地可读，不需要账号、外部数据库或在线数据源。
