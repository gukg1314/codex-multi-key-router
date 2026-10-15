# 图标设计输入与来源

## 当前候选的共同要求

为多 Key 模型路由桌面工具设计主图标，与已确认的默认主题一致。采用 `#3562b5`、`#6952b5`、`#203652`、`#f8fbff`、`#eaf0f9` 的蓝紫与浅色浮层关系，保留一个主要识别符号。1024×1024 正方形 SVG 源图，圆角浅色底板、真实透明外缘、充分留白、少量柔和渐变，无外部阴影、碎点、文字、真实商标或水印。64/32/16px 均检查识别度。

- A：三路汇流到代码网关。
- B：一分二的路由分支。
- C：三块轻质蓝紫叠层组成代码网关。
- D：钥匙头连接三路分支。

制作方法：按上述概念原创 SVG 几何，使用已有 Sharp 渲染器直接输出对应 PNG；对比板仅排版这些真实 PNG，不重绘图形。

## 废弃的位图探索

曾使用 Codex 内置 image_gen，每个方向分别生成，参考现有主图与默认主题截图。生成关键词为 square desktop app icon、blue/violet frosted tile、bold centered symbol、readable at 32px and 16px、true transparent margin、no text/numbers/watermark/real logos。

随后对每张图定向要求：保持符号拓扑、方向、构图和蓝紫身份，只清理外部白色碎片、彩点与脏边，并减弱高光、鼓包和投影。修正后仍存在透明碎边，因此这些位图未用于当前候选，也未复制到生产资源。

内置生成原文件保留在本次会话的 generated_images 中，未删除；无 CLI/API 回退。
