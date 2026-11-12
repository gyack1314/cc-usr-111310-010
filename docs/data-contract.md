# 物理设计成本目录

成本、权重与维护费用用非负有理字符串；bytes为非负整数，预算包含全部已选候选，不按引用次数重复收费。requires为必需候选集合且依赖递归闭合，互斥group中最多选一项，None无互斥。每查询有至少一个空requires基线；选择全部可用方案中cost最低，同费用按plan ID字典序给确定解释。

目标为sum(weight*selected_cost)+maintenance_coefficient*sum(maintenance)，第二目标bytes，第三为升序候选ID列表字典序。mandatory必须计费，即使零收益。构建器是本地可信Python映射，通过逻辑步骤返回actual_bytes、内容摘要及受控失败，不执行清单shell；所测字节超该候选计划值或者总预算任一超出均阻断，不以其他未用预算悄悄更换计划。

本材料dict与sortA组合共同服务q1，dict另服务q2；sortA/sortB互斥，must零收益但必选。storage7恰容dict+sortA+must。初始工具只清点候选、查询、数字预算，不求解最优、构建副本或切换路由。
