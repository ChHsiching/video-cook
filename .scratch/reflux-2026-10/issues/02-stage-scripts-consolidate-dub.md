# 02: 配音管线脚本物理合并

**What to build:** full_dub.py、loudness_match.py、scan_reference.py、extract_reference.py、make_zh_dub_srt.py、build_merge.py、length_gate.py 从 video-dubbing-skill 仓库迁入 stages/；cook dub 的 separate/synth/assemble/full 阶段改解析包内路径（重阶段仍经 --python venv 子进程执行包内文件）；删除 cook 里对已安装 skill 的 --keep-subs 版本探针（不再需要）。

**Blocked by:** None (can start immediately)

**Status:** ready-for-agent

- [ ] cook dub assemble 在 leerob run 上 --keep-subs 重跑产物与现有成片时长/字幕一致
- [ ] 版本探针代码删除且探针场景不再可能（路径只在包内）
- [ ] pytest 全绿
