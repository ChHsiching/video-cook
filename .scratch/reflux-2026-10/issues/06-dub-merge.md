# 06: cook dub merge 双侧并组

**What to build:** 唯一合法并组入口：输入行号组，en.full.srt 与 translations_dub.txt 同步并组（时间=首句起点+末句终点，文本拼接）；不变式断言（两侧行数、并组集合一致、首词标记匹配）失败即拒写零副作用；写前 .v1 备份。

**Blocked by:** 02

**Status:** ready-for-agent

- [ ] 正常并组双侧一致落盘；构造单侧不匹配场景被断言拦截且文件未动
- [ ] 用本期「flywheel+群聊」错位案例作回归 fixture
