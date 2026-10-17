# 10: synth 多 worker 错峰

**What to build:** --workers N（默认1）按序号带切分（主管升序+worker 降序），--stagger S 秒错峰；skip-if-exists 语义不变；90s 测试片小规模冒烟（2 worker）。

**Blocked by:** 02

**Status:** ready-for-agent

- [ ] N worker 并发产物与单进程逐句一致（对拍文件数与大小分布）
- [ ] 错峰间隔在日志可观测；进程退出码聚合正确
