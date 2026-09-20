# 本机 MyGO 案例资源

这是可选的历史资源定位，不是新项目硬依赖；先检查路径是否仍存在。技能本身不依赖这些文件才能指导新作。

工作目录：
`C:/Users/sora/Documents/Codex/2026-09-16/https-www-bilibili-com-video-bv1eybg6ges5`

## 2026-09-17 当前交付

`outputs/MyGO_Purple_FINAL_v14/`

- `MyGO_Seasons_65x98.kicad_pcb`、`MyGO_Purple_FINAL_v14_KiCad_Import.zip`、`MyGO_Seasons_Gerber.zip`。
- `build_final.py` 基于 v12 源图层构建两人的细嘴线；不要脱离依赖目录直接复制执行。
- `fabrication.py` 为案例专用导出器，含 65×98 mm、20 px/mm、3 mm NPTH 及坐标/文字等硬编码。其旧 `compile_side` 不是当前实际构建入口；由各版构建脚本替换。不可当通用新图转换器直接运行。
- `verify_with_gerbonara.py` 独立比对六层、KiCad、板框、钻孔。
- 可用验证命令：`C:/Python314/python.exe outputs/MyGO_Purple_FINAL_v14/verify_with_gerbonara.py --deps work/gerber_validation_v14`。Python 和依赖路径先检查；旧 `work/gerber_validation_deps` 曾发生访问拒绝，不应自动修改 ACL。

v14：紫色阻焊；保留 v11 提亮后的正面素世头发；背面恢复 v9 紫底白字/金框，无后来新增的白标题牌和白外边；原花体 e 以补横笔修复；嘴线名义 0.10 mm。0.12 mm 需求在旧网格上只能近似，此处不是设计规则。

## 可复用桌面工具

`outputs/PCB_Art_Threshold_Studio_20260917/pcb_art_threshold_studio.py`

这是已有的 Tkinter/Pillow 阈值分层工具，可先检查其启动器、自测和实际接口，再用于范围阈值、边缘、黑白边界、前后处理与图层 PNG/SVG 导出。不要把旧记载中的自测通过当作当前运行状态。

## 这次的具体审美反馈（仅供相同任务延续）

- 更喜欢紫色谷子感而非工作电路板观感；并非以后所有用户/作品都必须紫色。
- 喜欢原花体；换成近似字体或单独造 e 被拒绝。
- 过粗圆润的金线、碎白发丝、缺嘴线都不合适。
- 为提亮加入白标题框/外白边被拒绝；恢复已有版式更好。
- 确认一处改动后，后续不应该丢掉此前修复；使用版本组合断言而不是人工记忆猜测。
