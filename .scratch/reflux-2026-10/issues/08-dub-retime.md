# 08: cook dub retime 模块与命令

**What to build:** faster-whisper 词时间戳跑每句 _audio_pad 干净人声轨→SequenceMatcher 字符时间锚→两遍式改写双语 SRT 起点（锚点先行/驻留继承/单调钳制/end=下条-0.02/首条-0.05s）→EN 网格吸附亚秒重叠预修剪。真数据回归：leerob 361 pad 重跑结果复现 r12 成片字幕起点 ±50ms。

**Blocked by:** 02

**Status:** ready-for-agent

- [ ] 回归对拍通过（361 句起点全部落在容差内）
- [ ] 混音轨/无 fw 环境给显式报错而非静默错值
