---
name: Codex 路由工具 · 三种界面风格
description: 同一模型来源工作区，提供浮层、索引与字典三种既定表达
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
  layered-bg: "#eaf0f9"
  layered-panel: "#ffffffda"
  layered-text: "#203652"
  layered-muted: "#425570"
  layered-border: "#d5e0ef"
  layered-accent: "#3562b5"
  layered-selected: "#6952b5"
  layered-dock: "#ffffff91"
  layered-glow-top: "#d1c5f1"
  layered-glow-bottom: "#b9d7f1"
  layered-bg-dark: "#161d2d"
  layered-panel-dark: "#202c40ee"
  layered-text-dark: "#e5efff"
  layered-muted-dark: "#b3c4dc"
  layered-border-dark: "#40506a"
  layered-accent-dark: "#93b7ff"
  layered-selected-dark: "#8a72d5"
  layered-dock-dark: "#202c40d9"
  layered-glow-top-dark: "#342c51"
  layered-glow-bottom-dark: "#163c55"
  layered-accent-ink-dark: "#162138"
  layered-rail-dark: "#182438bb"
  layered-nav-shadow: "#4c6da115"
  layered-sheet-shadow: "#435b8210"
  layered-dock-shadow: "#435b8208"
  index-bg: "#fbfcf8"
  index-panel: "#fff"
  index-text: "#17382f"
  index-muted: "#62746d"
  index-border: "#dbe5dc"
  index-accent: "#235847"
  index-focus: "#478c73"
  index-mint: "#e5f2e9"
  index-native: "#f2f7f1"
  index-source-selected: "#fff"
  index-bg-dark: "#16231e"
  index-panel-dark: "#1b3027"
  index-text-dark: "#e6f2e9"
  index-muted-dark: "#b8cabf"
  index-border-dark: "#476354"
  index-accent-dark: "#9acaaf"
  index-focus-dark: "#b0dfc0"
  index-mint-dark: "#233d31"
  index-native-dark: "#213b2c"
  index-source-selected-dark: "#304c3d"
  index-accent-ink-dark: "#16231e"
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
  layered-title:
    fontFamily: '-apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif'
    fontSize: "30px"
    fontWeight: 650
    lineHeight: 1.2
    letterSpacing: "-.5px"
  index-title:
    fontFamily: '"Songti SC", "STSong", "SimSun", Georgia, serif'
    fontSize: "42px"
    fontWeight: 650
    lineHeight: 1.2
    letterSpacing: "1px"
  index-count:
    fontFamily: 'Georgia, "Times New Roman", serif'
    fontSize: "30px"
    fontWeight: 400
    lineHeight: 1.2
  compact-model:
    fontFamily: '-apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif'
    fontSize: "15px"
    fontWeight: 550
    lineHeight: 1.5
  auxiliary:
    fontFamily: '-apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif'
    fontSize: "12px"
    fontWeight: 400
    lineHeight: 1.5
  group-title:
    fontSize: "21px"
    fontWeight: 600
    lineHeight: 1.25
  small-title:
    fontSize: "18px"
    fontWeight: 600
    lineHeight: 1.25
  service-state:
    fontSize: "16px"
    fontWeight: 600
    lineHeight: 1.5
  lexicon-model-compact:
    fontSize: "32px"
    fontWeight: 400
    lineHeight: 1.15
  index-title-compact:
    fontSize: "36px"
    fontWeight: 650
    lineHeight: 1.2
rounded:
  square: "0"
  layered-sheet: "18px"
  layered-source: "10px"
  layered-control: "8px"
  index-panel: "10px"
  index-model: "8px"
  index-control: "6px"
  protocol: "5px"
  input: "7px"
  layered-nav: "13px"
  dialog: "14px"
  switch: "16px"
spacing:
  page-x: "32px"
  page-y: "24px"
  columns: "44px"
  actions: "8px"
  layered-page-x: "26px"
  layered-columns: "18px"
  index-page-x: "38px"
  index-columns: "28px"
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
  layered-button-primary:
    backgroundColor: "{colors.layered-accent}"
    textColor: "{colors.accent-ink}"
    typography: "{typography.button}"
    rounded: "{rounded.layered-control}"
    padding: "0 12px"
    height: "42px"
  index-button-primary:
    backgroundColor: "{colors.index-accent}"
    textColor: "{colors.accent-ink}"
    typography: "{typography.button}"
    rounded: "{rounded.index-control}"
    padding: "0 12px"
    height: "42px"
---

# Design System: Codex 路由工具 · 三种界面风格

## Overview

**Creative North Star: "浮层工作台 / 工作索引 / 模型字典"**

三种风格分别保留用户选定的构图、排版、材质与信息层级。F 用左导航、双浮层和横向来源 dock；C 用顶部导航、浅绿来源索引和两列模型卡片；I 用大字模型目录、墨线和朱红底部导航。切换仍操作同一份来源与模型数据。

前置 tokens 保留 I 的原名称与值，供旧配置及无风格属性时兼容；`layered-*`、`index-*` 是 F/C 的实装覆盖值，深色使用相同前缀的 `*-dark`。完整施工基准见 [三风格参数](docs/UI施工参数-界面风格.md) 与 [无事件静态图](docs/ui-style-switch/approved/README.md)；I 仍以 [既有参数](docs/UI施工参数-I.md)、[批准图](docs/ui-implementation-I/i-overview-approved.png) 为准。本次验收证据由主流程另行记录，不把既有 I 的结论外推到新风格。

| 设置名称 | 配置 / 根属性 | 已选方向 |
| --- | --- | --- |
| 默认主题 | `layered` / `data-ui-style="layered"` | F「浮层工作台」 |
| 工作索引 | `index` / `data-ui-style="index"` | C「索引工作室」 |
| 模型字典 | `lexicon` / `data-ui-style="lexicon"` | I「模型字典」；旧配置兼容默认 |

“默认主题”是 F 的产品选项名；不改变旧配置的默认风格。风格与浅/深色独立组合，四页与共用控件一起切换。

**Key Characteristics:**

- F 保持 88px 左导航、蓝紫背景、双浮层和 126px 横向来源 dock。
- C 保持顶部横向导航、宋体页标题、左浅绿索引与右两列模型卡片。
- I 保持 66px 英文品牌、44px 模型目录和底部朱红导航。
- 中文、表单与密集数据使用系统 UI 字体，真实状态与操作保持可读。

## Colors

I 浅色使用纸 `bg`、墨 `text`、暖灰 `muted/border` 与朱红 `accent`；深色对应 `bg-dark`、`text-dark`、`muted-dark`、`border-dark`、`accent-dark`，均已在 CSS 的 `data-theme="dark"` 中实现。

朱红用于品牌、选中来源、导航与危险文字；桥接协议使用独立辅助色，成功/警告同时保留文字。深色朱红底使用 `--accent-ink:#201c18`，浅色使用 `#fff`。来源 hover 对应 `--selected`，按钮 hover 对应 `--hover`；完整状态色见施工参数表，主题保存与首屏缓存继续复用。

F 使用 `layered-*`：蓝色用于主操作和焦点，紫色用于选中来源，蓝紫两处光色构成背景，半透明浅色浮层承载正文；深色保留相同层次。C 使用 `index-*`：深绿主操作、薄荷来源索引、纸白目录、浅绿原生直通卡片，深色使用暗绿同构；键盘焦点使用独立的 `index-focus`。两套的正常/选中前景随主题调整，来源选中仍有状态文字或选择标记。

## Typography

**Display Font:** I 使用 Instrument Serif Regular（CSS 名称 `Instrument`），回退 Georgia、serif。正式 HTML 已内嵌原 I 本地字体，并以 `text/plain` 保留版权与完整 SIL OFL 1.1 许可；不依赖外部字体服务。F 使用系统 UI 标题；C 使用本地宋体标题和 Georgia 统计数字。

**Body Font:** macOS/Windows 系统 UI 字体栈；中文、表单、按钮和状态使用它。英文大字用于读模型，控件字号用于完成操作。

| 角色 | 实际值 |
| --- | --- |
| 品牌 / 中文品牌 | Instrument 66px / 系统 UI 24px；间距 12px |
| 模型目录 | `clamp(28px,3.73vw,44px)`，行高 1；Key 表格行高 1.15；≤980px 的 Key 模型为 32px |
| 来源数量 / 顶部数量 | Instrument 30px / 系统 UI 27px，顶部等宽数字 |
| 主标题 / 设置分组 | 26px / 21px；右区标题 14px |
| 优先级模型 / 正文 / 按钮 / 辅助 | 28px / 14px / 13px / 12px |

来源数量是模型数。长名称可以换行或增高行，不能挤掉选择、保存、协议与状态。

上表为 I 的兼容字号。F 页标题为 `layered-title`，统计 22px，模型名为 `compact-model`，来源数量为系统 UI 22px；C 页标题为 `index-title`，顶部统计为 `index-count`、来源数量 Georgia 21px，模型同样 15px。F/C 的 Key 表格和优先级模型都使用系统 UI 15px，不继承 I 的大字字典排版。三套正文 14px、按钮 13px、辅助 12px。

C 的 `Songti SC` / `STSong` 与 Windows 的 `SimSun` 均为平台本地字体；系统字体字面、原生控件和抗锯齿差异如实保留，不声称逐像素一致。

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

上表为 I 的几何。I 顶部与底部高度固定，工作区独立滚动，来源列表另行滚动并给服务留空间；概览左模型右来源与服务，Key 页左编辑右来源，优先级铺满滚动区，设置左主设置右日志诊断。

| 风格 | 基准构图 | 四页延展 |
| --- | --- | --- |
| F 默认主题 | 88px 左导航；主区横内距 26px；实现网格为顶部 96px / 弹性工作区 / 底部 90px。概览浮层从 y=114px 开始：模型/254px 服务两列、间隔18px；上区472px，下区126px 来源 dock | Key 页主编辑区下方148px横向来源 dock，≤980px编辑＋dock区域最小高650px并由主区滚动；优先级主浮层铺满；设置为主设置与254px诊断浮层。保留30px页标题与淡彩背景 |
| C 工作索引 | 顶部76px、标题统计118px、概览控制66px、工作区、页脚54px；横内距38px；工作区左260px来源、列距28px、右两列76px最小高模型卡片 | Key 页左来源/右编辑；优先级全宽；设置分主设置/诊断；无概览控制条的页面直接使用主工作区 |

F 的 96px/90px 是共享 DOM 的网格实现；加上标题与工作区内距后，双浮层和 dock 的可见边界仍对齐静态基准。不能把内部行高差异解释成允许移动已锁定构图。三套概览均无最近事件。

F/C 在非概览页继续于顶部显示真实服务与接管状态，概览则放在各自服务区，避免离开概览后失去状态信息。

F ≤980px 横内距20px、列距14px、服务230px；C ≤980px 横内距24px、左索引220px、列距20px、页标题36px、导航间距20px。≤800px 页内区域纵排，F 来源 dock 仍内部横向滚动，C 导航内部横向滚动；Key 编辑区保留可操作高度，表格内部横滚。完整尺寸和滚动规则见三风格参数。

## Elevation & Depth

I 没有渐变、阴影或玻璃模糊，以实色、1px 分隔线和选中底色区分区域。C 同样使用实色，无渐变和模糊；只有选中来源使用 `0 3px 12px #17382f08` 轻阴影。F 保留两处 radial-gradient 背景、`0 15px 34px #435b8210` 浮层阴影、`0 10px 24px #435b8208` 来源dock阴影及14px标准/带前缀背景模糊；即使 blur 不生效，面板自身的色彩和边线仍承载正文，不依赖系统原生材质。

忙碌转圈是唯一动画：12px、2px 边线、0.7秒线性循环；减少动态模式停用动画与过渡，风格切换不添加布局动画。

## Shapes

I 的按钮、输入、弹窗、来源项和开关均为直角。F 浮层18px、来源10px、按钮8px；C 索引/设置面板10px、模型卡片8px、按钮6px。F/C 输入7px、弹窗与 toast14px。默认边线1px，状态点与忙碌转圈保留圆形。

三套开关外框44×26px、内块18px、内距3px；I 为直角方块，F/C 为16px圆角外框与圆形内块。继续使用 `role="switch"` 和 `aria-checked`。

## Components

- **Buttons:** 共用表单操作最小高42px、13px字号、左右内距12px；sm内距10px。I 主按钮墨底纸色字，F/C 主按钮使用各自 accent。概览紧凑操作 F 最小36px、C 最小34px；来源区添加32px。禁用操作使用真实 disabled。
- **Inputs:** 最小高 42px、内距 8px 10px、1px 边框；复选框 18px，保存与模型下拉始终可达。
- **Navigation:** I 为64px朱红底、14px标签、36px项间距、2px当前页下划线；F 为左侧图标/标签纵排，最小69px项高、13px圆角选中底；C 为76px顶部横排和3px当前页线。均保留 `aria-current`。
- **Source index:** I 为至少52px、15px名称/30px数量、朱红选中；F 为横向dock、至少66px、紫色选中；C 为左浅绿索引、至少76px、白/暗绿选中底及选择标记。均保留 `aria-pressed` 与12px状态文字。
- **Priority table:** 单元格上下内距8px，模型名 I 为28px、F/C 为15px；来源下拉最小42px，表格可内部横向滚动。
- **Refresh icon:** 概览刷新图标沿用 I 原稿的 `stroke-linecap:butt`、`stroke-linejoin:miter`；其他图标保留圆线端。
- **Containers:** I 以标题和细线组织，F/C 按各自浮层/卡片规则。弹窗宽500px，窗口四周至少24px，内容可滚动；toast 底距 I 80px/F 64px/C 70px，避开导航和页脚。
- **Appearance:** 风格和浅/深色分开选择，分段按钮40px；风格固定三选项，保存中显示真实状态，失败沿用错误反馈。只切换根属性和控件状态，保留页面、草稿、搜索、焦点及合理滚动。
- **Focus and feedback:** 键盘焦点为3px对应focus框、offset3px；I/F使用各自accent，C使用独立index-focus；彩色导航和已选分段使用对比前景，F/C分段焦点放在边框内部以免裁切。保留真实任务、空态、错误、删除确认及弹窗键盘行为。

## Do's and Don'ts

### Do

- **Do** 分别保留 F 的左导航/双浮层/dock、C 的顶部导航/左索引/双列目录、I 的大字目录/朱红底部导航。
- **Do** 使用准确的“默认主题 / 工作索引 / 模型字典”名称与配置映射，旧配置继续模型字典。
- **Do** 复用四页业务与主题机制，保留 macOS/Windows 原生窗口、菜单/托盘和平台字体差异。
- **Do** 在核心功能、窄窗和深色最终验收后，由主流程统一记录结果与未验证项。

### Don't

- **Don't** 新增余额、趋势、健康监控、自动轮换或会话功能；正文固定来源，失败不自动切换。
- **Don't** 把三种风格做成同一布局换色，或从旧 F/C 样稿恢复最近事件。
- **Don't** 强行把表单和优先级全部放大为 44px，挤掉下拉、勾选和保存操作。
- **Don't** 写入真实密钥、账号、私人链接或完整 Key 日志；不模拟另一平台窗口按钮。
- **Don't** 把首版截图或静态设计样稿写成功能验收通过；本次不制作安装包。
