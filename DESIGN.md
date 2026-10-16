---
name: Codex 路由工具 · 同一排版，三套皮肤
description: 默认主题工作区，提供默认、Gen Z 撞色与 Mintlify 外观
colors:
  layered-bg: "#eaf0f9"
  layered-text: "#203652"
  layered-accent: "#3562b5"
  chaos-bg: "#fffcdf"
  chaos-text: "#151515"
  chaos-accent: "#1735df"
  chaos-selected: "#e6007e"
  mintlify-bg: "#ffffff"
  mintlify-text: "#0d0d0d"
  mintlify-accent: "#0d0d0d"
  mintlify-selected: "#d4fae8"
fonts:
  primary: system-ui
---

# Codex 路由工具 · 皮肤规则

用户于2026-10-16手动查看[五候选对比页](docs/ui-skin-exploration/2026-10-16/index.html)后选择03与05并授权实施。正式界面保留默认主题、新增 Gen Z 撞色和 Mintlify；旧工作索引、模型字典退出设置与代码。

| 皮肤 | 配置值 | 外观 |
| --- | --- | --- |
| 默认主题 | `layered` | 蓝紫光色背景、半透明浮层、蓝色操作、紫色来源选中 |
| Gen Z 撞色 | `chaos` | 奶黄、酸橙、蓝、洋红，黑色边线和硬阴影 |
| Mintlify | `mintlify` | 白底、薄边线、薄荷绿选中、近黑主操作、胶囊按钮 |

## 排版与字体

三套共用默认主题的同一组组件、DOM和布局规则。基准客户端1180×820，88px左导航、概览模型/服务双浮层、下方126px来源dock；Key页模型编辑为主、右侧220px来源栏。980px与800px响应断点、字体字号、控件宽高、间距、表格内部滚动全部沿用既有默认主题。

正文、标题、数据使用系统字体，Windows回退Segoe UI/微软雅黑，macOS使用系统UI/苹方。移除旧字典专用字体，不引入网络字体或新增依赖。

## 材质与主题

默认主题原有几何与配色保持。03和05浅色颜色、圆角、阴影沿用确认样张；深色延续各自色彩关系。皮肤选择器不得包含布局、字体、尺寸、间距、顺序或位置声明，只能覆盖颜色、背景、边框颜色、圆角、阴影和模糊。

表单、弹窗、提示、来源列表均使用所选皮肤；保留可读的错误、成功、禁用和焦点状态。减少动态与强制颜色规则保留，无新增动画。源数据与状态文字不因皮肤变化。

完整参数及浅深色值见[当前施工参数](docs/UI施工参数-界面风格.md)。旧参数文档与旧截图只保留为历史证据。

## 行为与兼容

皮肤与浅深色独立保存；设置即时应用、失败回滚、焦点/草稿/搜索/滚动保留与防旧轮询覆盖逻辑不变。旧 `index`/`lexicon`、缺字段与非法值回退 `layered`；后台拒绝写入已移除皮肤。切换不会触发路由、接管或Key操作。

核心日志只记录皮肤名称、结果和共用布局版本，不记录Key、账号或私有链接。macOS与Windows共用HTML和业务接口；不改原生窗口、图标或打包流程。

## 验收边界

用户暂不使用computeruse，因此本轮执行指定测试子agent的非浏览器验证，不声称已完成截图、macOS WKWebView或Windows WebView2实机验收。检查结果见[本轮实施记录](docs/ui-skin-implementation/README.md)。
