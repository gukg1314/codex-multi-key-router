# 图标定稿与导出规格 v1.0

2026-10-15，用户最终选择原始 A 的蓝紫三路汇流图，放弃强化玻璃版本；随后单独确认状态栏稿，并明确允许用代码蒙版只清理主图外缘。主图与状态栏现已接入程序资源。

## 冻结素材

以下路径均相对项目根目录。

| 用途 | 冻结文件 | 规格 |
| --- | --- | --- |
| 主图母版 | `packaging/assets/app-icon.png` | 1024×1024，sRGB，RGBA8，真实透明外缘 |
| macOS 兼容母版 | `packaging/assets/app-icon-macos.png` | 1024×1024；透明轮廓边界 (100,100)…(924,924)，避免系统二次缩小与套底板 |
| 主图原始参考 | `docs/icon-exploration/2026-10-15/statusbar/reference-main-a.png` | 用户最后选择的原图，1254×1254 |
| 固定 Alpha 蒙版 | `docs/icon-exploration/2026-10-15/final/app-icon-alpha-mask.svg` | 1254×1254；路径边界 x=84…1170、y=102…1154 |
| 清理后的原尺寸图 | `docs/icon-exploration/2026-10-15/final/app-icon-cleaned-source.png` | 保留原 RGB，只将原 Alpha 与固定路径覆盖率相乘 |
| 状态栏矢量母版 | `docs/icon-exploration/2026-10-15/statusbar/statusbar-master.svg` | 18pt；具体几何与留白见同目录 `SPEC.md` |
| 状态栏白色母版 | `packaging/assets/statusbar-source.png`、`statusbar-source@2x.png` | 18/36px；sRGB，RGBA8，白色线条、透明镂空 |

主图先按已确认的外轮廓清理外侧杂点，再使用 LANCZOS 等比缩至 1024px，冻结为主母版。没有重画、换色、补底板或加阴影。原尺寸清理前后所有 RGB 字节一致，内部矩形 (220,220)…(1030,1045) 的 RGBA 一致。以后只从冻结母版缩放与编码，不再运行外缘清理或重新生图。

`icons-preview.png` 来自正式母版，包含主图、小尺寸和状态栏设计示意；菜单栏部分并非实机截图。状态栏单独确认图保留在 `statusbar/statusbar-confirmation.png`。

## 统一导出与引用

使用既有标准库导出器 `python3 packaging/icons.py --force`，Windows/macOS 共用一份流程；构建机器不新增 Pillow 或 Sharp 依赖。

| 资源入口 | 来源与规格 |
| --- | --- |
| `packaging/assets/app.ico` | 主图等比缩放；16/24/32/48/64/128/256px，32 位 ARGB；Windows EXE/安装器已有引用 |
| `launcher/assets/tray.ico` | 主图等比缩放；16/32/48px；沿用现有 Windows 托盘入口 |
| `ui/logo.png` | 主图等比缩放；60×60px；侧栏与 favicon 共用 |
| macOS `AppIcon.icns` | 构建时从 macOS 兼容母版生成标准 iconset 共 10 槽位，16…1024px；Finder/Dock 共用；1024px 槽位像素保持不变 |
| `launcher/assets/statusbar.png`、`statusbar@2x.png` | 两份白色母版仅转换 RGB 为黑色，逐像素保留 Alpha；不缩放、不阈值抠图 |

主图和状态栏来源分离，删除旧根目录图片回退及自动补底、裁切、824 比例重排。母版缺失、尺寸错误或状态栏可见像素非纯白时明确中止，避免误用旧资源。日志记录文件名、尺寸与处理步骤，不记录个人绝对路径。

AppKit 显式加载 18/36px 两个 `NSBitmapImageRep`，各自与 `NSImage` 都固定 18×18pt，并设置 `isTemplate=true`。资源缺失或解码失败记录日志并返回空图像，不自动换成彩色主图；菜单安装代码仍会执行。

macOS 图标实际显示偏小与打开/退出切换的问题已在后续修复：使用独立的系统兼容母版，并移除运行时 `setApplicationIconImage:` 覆盖；详见 `../macos-render-fix/README.md`。共享主图、Windows、界面 logo 与状态栏资源保持不变。

## 验证

- `test_packager.py`：24 项通过；macOS 专用 Objective-C 从 Windows ASCII 检查范围移出，实际 Windows 源码检查保留。
- Objective-C 语法检查通过；无界面 CLI 调用真实状态栏加载函数，两个密度的 18pt 尺寸、template 标志和缺资源分支通过。
- 主图来源像素、PNG/ICO 规格、状态栏纯色与 Alpha 保持检查通过。
- 临时导出标准 10 槽位 iconset 与 ICNS 成功；未制作 app、DMG 或安装器。
- 未使用 computeruse，未验证 Windows 原生运行或实际 Dock、菜单栏截图。

## 母版 SHA-256

```text
50350bda885a1e844e20fff6f8920ea29e68274e149f3c062b0c3946fef6a7b9  packaging/assets/app-icon.png
a5c2256c2b12943f98635ad221f775926ffb80ca77bf8a14fc61d41c646d7a39  packaging/assets/app-icon-macos.png
896461923a0af3e43b9b37b0e243499e188412185b7f1585de88401af03daea7  packaging/assets/statusbar-source.png
cc83637aa297862ef7375bee13770a6a2b2b3e9b1ba30897664a06cf8bdfcf8d  packaging/assets/statusbar-source@2x.png
9880a03753704105c362cc5f7592119d82f0ccf49e91d27f412e5a3432445dfd  docs/icon-exploration/2026-10-15/statusbar/statusbar-master.svg
5770ccaca160cd2a50bd5b0e19cc969843bb4b2b14e5854ea8fffdfeb8e3f0fb  docs/icon-exploration/2026-10-15/final/app-icon-alpha-mask.svg
```
