# macOS 图标缩小与 Dock 切换修复

## 已复现的原因

用户视频中，软件退出后图案缩小约三成，并多出灰色底板；启动后恢复较大的原图。已安装 ICNS 与打包资源逐字节一致，1024px 槽位也与共享母版一致，排除了混用旧资源。

在 macOS 27.0.1 使用 `NSWorkspace.icon(forFile:)` 读取真实系统渲染，复现了二次套底板和约 0.675 倍的内缩。单独调整留白、尺寸或仅裁切旧 Alpha 都不能消除该现象。原图的透明轮廓和旧底板透明缝需要一起适配。

启动代码此前又调用 `setApplicationIconImage:` 用直接解码的 ICNS 覆盖系统图标；退出后覆盖消失，因而出现两种外观。[Apple 文档](https://developer.apple.com/documentation/appkit/nsapplication/applicationiconimage) 明确这是运行时覆盖图标的接口。

## 修复与冻结资源

- 删除运行时覆盖函数及调用；应用列表、Dock 与应用切换器统一使用 `CFBundleIconFile` 指向的包内图标。
- 新增 `packaging/assets/app-icon-macos.png`，只用于 macOS；共享主图、Windows ICO、界面 logo 和状态栏资源未修改。
- macOS 构建从 `macos_source` 导出 ICNS。缺少此母版时停止构建，避免默默退回错误图标。
- 构建只读取冻结 PNG 并缩放、编码；没有新增依赖，也不在每次构建时重画或重新生成图案。

兼容母版为 1024×1024 sRGB RGBA8。原图按 Alpha 可见边界 (66,81)…(958,945) 取出图案，保持宽高比缩至 824×798，置于 (100,113)。用匹配的 `#F8FBFF` 浅色填平旧轮廓外的透明缝，再使用完整的连续圆角 Alpha；可见边界为 (100,100)…(924,924)。图案和配色来自已确认主图。

本次只复用了本机正常显示的传统 ICNS 的通用 Alpha 几何，没有复制其 RGB 图案。最终母版已独立保存，构建不依赖任何其他 App。824px 是本次实测通过的兼容版式，不能将它当作所有 macOS 版本的固定识别阈值。

```text
PNG SHA-256:   a5c2256c2b12943f98635ad221f775926ffb80ca77bf8a14fc61d41c646d7a39
Alpha SHA-256: ff7c503c07deb1bf37a4e7f254aeb5c669d9ae222ecc0326aa949f69481a9378
```

## 验证与证据

- 25 项 `test_packager.py` 测试通过，包括 macOS 确实选择平台母版的回归测试。
- Objective-C 语法检查和既有无界面 Dock 显隐、退出行为测试通过。
- 使用真实打包函数生成临时 ICNS，10 槽位正确；1024px 槽与 macOS 母版像素相同。
- 在具有唯一标识、图标名和有效占位可执行文件的临时包中，经系统图标服务验证无额外灰底和二次缩小；没有启动应用。
- `system-before.png`、`system-after.png` 是同一系统的实际资源渲染；`comparison.png` 只将两张真实输出并排展示。

用户明确选择仅做代码与资源检查；本轮未调用 computeruse、未验证真实打开/退出 UI，也未重新打包或替换已安装 App。
