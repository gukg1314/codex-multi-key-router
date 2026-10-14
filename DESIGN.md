---
name: Codex 路由工具 · I 模型字典
description: 以纸色、墨线和朱红导航组织本机模型来源
colors:
  bg: "#f7f3ec"
  text: "#29251f"
  muted: "#65594e"
  border: "#cec3b6"
  accent: "#bd3022"
  accent-ink: "#fff"
  bg-dark: "#201c18"
  text-dark: "#f4eee4"
  muted-dark: "#c4b4a3"
  border-dark: "#584a3e"
  accent-dark: "#e46a58"
typography:
  wordmark:
    fontFamily: "Instrument, Georgia, serif"
    fontSize: "66px"
    fontWeight: 400
    lineHeight: 1
    letterSpacing: "-.025em"
  model:
    fontFamily: "Instrument, Georgia, serif"
    fontSize: "clamp(28px,3.73vw,44px)"
    fontWeight: 400
    lineHeight: 1
    letterSpacing: "-.012em"
  body:
    fontFamily: '-apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif'
    fontSize: "14px"
    fontWeight: 400
    lineHeight: 1.5
  button:
    fontFamily: '-apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif'
    fontSize: "13px"
    fontWeight: 400
  title:
    fontSize: "26px"
    fontWeight: 600
    lineHeight: 1.25
rounded:
  square: "0"
spacing:
  page-x: "32px"
  page-y: "24px"
  columns: "44px"
  actions: "8px"
components:
  button-primary:
    backgroundColor: "{colors.text}"
    textColor: "{colors.bg}"
    typography: "{typography.button}"
    rounded: "{rounded.square}"
    padding: "0 12px"
    height: "42px"
  input:
    backgroundColor: "{colors.bg}"
    textColor: "{colors.text}"
    rounded: "{rounded.square}"
    padding: "8px 10px"
    height: "42px"
  navigation:
    backgroundColor: "{colors.accent}"
    textColor: "{colors.accent-ink}"
    height: "64px"
  source-active:
    backgroundColor: "{colors.accent}"
    textColor: "{colors.accent-ink}"
    rounded: "{rounded.square}"
    padding: "0 10px"
    height: "52px"
---

# Design System: Codex 路由工具 · I「模型字典」

## Overview

**Creative North Star: "模型字典"**

模型名称是主要阅读对象。左边读模型，右边选来源和处理路由，顶部确认服务与接管状态；纸色画布、墨色细线和朱红导航保持清楚的阅读顺序。

用户已选 I，并授权删除概览最近事件、补齐排版后施工。四页已实现，**代码回归与独立截图评审通过**；完整实际参数以 [正式 CSS](ui/index.html) 为准，详见 [UI施工参数-I.md](docs/UI施工参数-I.md)。施工参考为 [批准后的效果图](docs/ui-implementation-I/i-overview-approved.png) 和 [参考 HTML](docs/ui-exploration/2026-10-15/round-3/i-overview-approved.html)；[验收记录](docs/ui-implementation-I/README.md) 说明证据及未验证项。上一版 `docs/UI施工参数.md` 保留作历史资料。

**Key Characteristics:**

- 66px 英文品牌与 44px 模型目录形成主要记忆点。
- 左工作区、右来源索引和底部朱红导航组织操作。
- 中文、表单与密集数据使用系统 UI 字体，真实状态与操作保持可读。

## Colors

浅色使用纸 `bg`、墨 `text`、暖灰 `muted/border` 与朱红 `accent`；深色对应 `bg-dark`、`text-dark`、`muted-dark`、`border-dark`、`accent-dark`，均已在 CSS 的 `data-theme="dark"` 中实现。

朱红用于品牌、选中来源、导航与危险文字；桥接协议使用独立辅助色，成功/警告同时保留文字。深色朱红底使用 `--accent-ink:#201c18`，浅色使用 `#fff`。来源 hover 对应 `--selected`，按钮 hover 对应 `--hover`；完整状态色见施工参数表，主题保存与首屏缓存继续复用。

## Typography

**Display Font:** Instrument Serif Regular（CSS 名称 `Instrument`），回退 Georgia、serif。正式 HTML 已内嵌原 I 本地字体，并以 `text/plain` 保留版权与完整 SIL OFL 1.1 许可；不依赖外部字体服务。

**Body Font:** macOS/Windows 系统 UI 字体栈；中文、表单、按钮和状态使用它。英文大字用于读模型，控件字号用于完成操作。

| 角色 | 实际值 |
| --- | --- |
| 品牌 / 中文品牌 | Instrument 66px / 系统 UI 24px；间距 12px |
| 模型目录 | `clamp(28px,3.73vw,44px)`，行高 1；Key 表格行高 1.15；≤980px 的 Key 模型为 32px |
| 来源数量 / 顶部数量 | Instrument 30px / 系统 UI 27px，顶部等宽数字 |
| 主标题 / 设置分组 | 26px / 21px；右区标题 14px |
| 优先级模型 / 正文 / 按钮 / 辅助 | 28px / 14px / 13px / 12px |

来源数量是模型数。长名称可以换行或增高行，不能挤掉选择、保存、协议与状态。

## Layout

| 参数 | 基准 |
| --- | --- |
| 客户端 | 1180×820 CSS px；原生标题栏不计入 |
| 页面三段 | masthead 108px / 工作区 648px / 导航 64px |
| 工作区 | 上下 24px、左右 32px；左 802px、列距 44px、右 270px |
| 7 行模型 | 每行 72px；标题 58 + UL 顶线 1 + 模型 504 + 脚注 37 = 内区 600px |
| 右侧来源与服务 | 来源至少 52px，项间 6px；服务前间距 23px，保留 I 原稿右侧几何 |
| 980px 断点 | 右区 230px、列距 24px、顶部组距 20px、数量间距 18px、品牌 54px、数量 24px；Key 搜索独占一行 |
| 800px 断点 | 页内左右区纵排；横边距 24px、顶部组距 14px、数量间距 12px、品牌 44px、中文品牌 20px、数量 21px、导航间距 24px；隐藏导航右侧辅助文字 |

顶部与底部高度固定，工作区独立滚动，来源列表另行滚动并给服务留空间。概览左模型右来源与服务；Key 页左编辑右来源；优先级铺满滚动区；设置左主设置右日志诊断。其他页延续同一顶部、导航与列网格。

## Elevation & Depth

没有渐变、阴影或玻璃模糊。以实色、1px 分隔线和选中底色区分区域；弹窗使用现有遮罩及实色内容框。忙碌转圈是唯一动画：12px、2px 边线、0.7 秒线性循环；减少动态模式停用动画与过渡。

## Shapes

按钮、输入、弹窗、来源项和开关均为直角。默认边线 1px；状态点与忙碌转圈保留圆形。开关为 44×26px 外框、18px 内方块、3px 内距，继续使用 `role="switch"` 和 `aria-checked`。

## Components

- **Buttons:** 最小高 42px、13px 字号、左右内距 12px；sm 内距 10px。主按钮墨底纸色字，次按钮透明底，危险按钮朱红字/边；禁用操作使用真实 disabled。来源区添加按钮 32px，主题分段按钮 40px。
- **Inputs:** 最小高 42px、内距 8px 10px、1px 边框；复选框 18px，保存与模型下拉始终可达。
- **Navigation:** 64px 朱红底、14px 标签、36px 项间距；当前项 700 字重，距底 14px 的 2px 纸色 `--bg` 下划线，保留 `aria-current`。
- **Source index:** 至少 52px，左右内距 10px，名称 15px、状态 12px、数量 30px；选中朱红实底，保留 `aria-pressed`。
- **Priority table:** 单元格上下内距 8px，模型名 28px；来源下拉最小 42px，七行在基准窗口完整显示。
- **Refresh icon:** 概览刷新图标沿用 I 原稿的 `stroke-linecap:butt`、`stroke-linejoin:miter`；其他图标保留圆线端。
- **Containers:** 内容区以标题和细线组织，取消旧卡片阴影与材质。弹窗宽 500px，窗口四周至少 24px，内容可滚动；toast 底距 80px，避开导航。
- **Focus and feedback:** 键盘焦点为 3px 朱红框、offset 3px；朱红导航使用对比前景。保留真实任务、空态、错误、删除确认及弹窗键盘行为；轮询保留草稿、搜索、焦点与滚动位置。

## Do's and Don'ts

### Do

- **Do** 保持批准图的左模型右来源、108/648/64 三段和朱红导航。
- **Do** 复用四页业务与主题机制，保留 macOS/Windows 原生窗口、菜单/托盘和平台字体差异。
- **Do** 在核心功能、窄窗和深色最终验收后，由主流程统一记录结果与未验证项。

### Don't

- **Don't** 新增余额、趋势、健康监控、自动轮换或会话功能；正文固定来源，失败不自动切换。
- **Don't** 强行把表单和优先级全部放大为 44px，挤掉下拉、勾选和保存操作。
- **Don't** 写入真实密钥、账号、私人链接或完整 Key 日志；不模拟另一平台窗口按钮。
- **Don't** 把首版截图或静态设计样稿写成功能验收通过；本次不制作安装包。
