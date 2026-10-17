# 04: cook dub measure-rate

**What to build:** 新子命令：输入试听 wav+文本（或 --from-segments 自动配对 translations_dub.txt），VAD 测语音跨度，输出实测 chars/voiced-seconds；打印并可 --json 输出。

**Blocked by:** 02

**Status:** ready-for-agent

- [ ] 对本期已验收 take 目录跑出的实测率与手工测量一致（6.93±0.1）
- [ ] 无语音/静音输入给非零退出与清晰报错
