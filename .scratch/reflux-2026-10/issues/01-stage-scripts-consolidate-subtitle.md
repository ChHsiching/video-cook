# 01: 字幕管线脚本物理合并

**What to build:** subtitles.py、make_full_srt.py、make_zh.py、transcribe.py 从 video-subtitle-skill 仓库迁入本仓库 stages/ 布局；cook 的 subtitles/transcribe/burn 相关阶段改为解析包内路径调用；迁移后用本期视频的既有工件冒烟（cook subtitles 在 leerob run 目录上重跑结果与现有产物一致）；pytest 全绿。附 ADR「确定性脚本集中于 cook CLI」。

**Blocked by:** None (can start immediately)

**Status:** ready-for-agent

- [ ] cook subtitles 与 transcribe 在不依赖已安装 skill 的情况下正常完成全流程
- [ ] 包内路径解析有单测（找不到文件时报清晰错误）
- [ ] ADR 合入 docs/adr/
