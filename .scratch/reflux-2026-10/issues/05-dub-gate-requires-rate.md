# 05: 长度闸门强制实测率

**What to build:** length_gate 并入 cook 命令面；--rate 必填，缺失非零退出；--self-measure 从 dubbed/_full/_segments 现算并打印后按测得率执行；健康带 90-105% 判定保持。

**Blocked by:** 02、04

**Status:** ready-for-agent

- [ ] 无 --rate 拒跑；--self-measure 在 leerob run 上复现 87.9% 覆盖率判定
- [ ] 对本期 r12 译稿跑出 0 短行 0 超限
