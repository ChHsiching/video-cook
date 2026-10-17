# 09: assemble 默认集成 retime

**What to build:** biliteral 之后、ASS 生成之前自动跑 retime（默认开）；--no-retime 退回旧行为；faster-whisper 缺失时降级旧行为+显式警告。附 ADR「retime 为装配默认行为」。

**Blocked by:** 08

**Status:** ready-for-agent

- [ ] 默认路径产物字幕起点与独立 retime 命令一致
- [ ] --no-retime 行为=旧实现；缺 fw 降级警告出现在日志
