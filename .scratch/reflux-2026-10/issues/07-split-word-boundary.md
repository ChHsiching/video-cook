# 07: 切词词边界

**What to build:** split_zh 标点阶梯走尽仍超限的子句，用 jieba 求最近词边界再切；1.15x 豁免保留；单测覆盖「技/能」类案例。

**Blocked by:** 01

**Status:** ready-for-agent

- [ ] 必切路径上不再产生词中断口（jieba 词表校验）
- [ ] 豁免路径行为与现状一致（既有豁免测试不回归）
