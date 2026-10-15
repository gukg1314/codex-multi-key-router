# 三种界面风格 · 实施验收

后续局部调整：[默认主题 Key 页列表空间](keys-room/README.md)（2026-10-15）；下方保留三风格首次验收记录。

2026-10-15。用户指定 F/C/I 三套既有方向，整合进现有工具；不改图标、原生窗口或路由业务，不打包、不 push。

## 交付

| 设置名称 | 风格值 | 结构 |
| --- | --- | --- |
| 默认主题 | `layered` | F 浮层工作台：88px 左导航、双浮层、横向来源 dock |
| 工作索引 | `index` | C 索引工作室：顶部导航、左来源索引、两列模型卡片 |
| 模型字典 | `lexicon` | I 模型字典：纸底、墨线、大字目录、朱红底部导航 |

- 三套共用业务节点、接口和数据，通过 `data-ui-style` 重排布局；没有复制三份业务实现。四页、按钮、表单、弹窗和提示均随风格切换。
- 浅/深色仍独立保存。`ui_style` 沿用 settings API，后端白名单校验；旧配置缺字段与非法缓存回退模型字典。“默认主题”仅为 F 的选项名称。
- 选择立即预览，成功后写入后台和首屏缓存；失败或旧服务丢弃字段时恢复之前风格并提示。中文日志只记录风格与结果。
- 切换不重建当前页面/导航，不发路由启停、接管或来源切换请求；保留搜索、模型/图片/优先级草稿、端口与弹窗输入、焦点和可用滚动偏移。进出设置保留搜索与优先级草稿。
- 保存期间忽略重复风格提交，并保护预览免受晚到轮询覆盖；风格按钮用可聚焦的 `aria-disabled` 保留焦点，路由任务按钮仍使用真实 `disabled`。
- 概览保持不展示最近事件。F/C 使用本地系统字体和内联 SVG，I 继续使用内嵌 Instrument Serif 与 OFL 许可；无新增运行时依赖。

[施工参数](../UI施工参数-界面风格.md) · [设计系统](../../DESIGN.md) · [F/C 去事件静态基准](approved/README.md)

## 视觉证据

同一套虚构数据，客户端 1180×820；另检查 900×700、浅/深色、空态、忙碌、Key 弹窗和键盘焦点。截图全部来自隔离 Chrome 与假 API，未读取真实配置/Key，未连接上游或执行真实接管。

| 证据 | 链接 |
| --- | --- |
| 四页 · 浅色 · 1180×820 | [拼图](captures/sheet-light-desktop.png) |
| 四页 · 深色 · 1180×820 | [拼图](captures/sheet-dark-desktop.png) |
| 四页 · 浅色 · 900×700 | [拼图](captures/sheet-light-narrow.png) |
| 四页 · 深色 · 900×700 | [拼图](captures/sheet-dark-narrow.png) |
| 弹窗、忙碌、空态 | [拼图](captures/sheet-states-dark-narrow.png) |
| F / C / I 概览 | [F](captures/layered-overview-light-desktop.png) · [C](captures/index-overview-light-desktop.png) · [I](captures/lexicon-overview-light-desktop.png) |
| 实际几何 | [geometry.json](captures/geometry.json) |

独立评审两轮完成：修复 F 窄窗 Key 页模型表被挤光及 dock 裁切、C 顶部品牌未居中、F/C 已选风格焦点不清楚三项。

- F 基准：模型 `(114,114,768,472)`，服务 `(900,114,254,472)`，来源 dock `(114,604,1040,126)`，与锁参一致；Key 页 dock 增至 148px 以容纳添加按钮与完整来源项。
- C 基准：顶部导航 76px、标题统计 118px、服务控制 66px；来源索引 `(38,284,260,468)`，目录 x326、宽816、列距28。≤980px 保持 24px 外距与20px列距。
- I 保留既有排版和主要几何。新增外观行使设置页按内容滚动；模型字典的纸底是已选方向，机械检测的通用米色提示不改写该方向。

## 验证与边界

代码验证由指定 `gpt-6-luna / max` 测试子 agent 执行，最终代码检查通过：

- `node tests/test_ui_behavior.js`：UI 行为回归通过。
- `python3 -m unittest discover -s tests -p 'test_admin.py'`：48 项管理服务测试通过。
- `python3 -m py_compile app/admin.py tests/test_admin.py`：语法检查通过。
- 隔离 Playwright：三风格×浅深主题、刷新缓存/后台恢复、真实点击焦点、保存前与保存中发起的旧轮询晚到、失败回滚、端口草稿/滚动/节点保留、导航往返保留搜索/模型/图片/优先级草稿全部通过。保存中风格按钮仍是焦点目标，`disabled=false`、`aria-disabled=true`；重复提交被保存锁拦截。
- 后端新增核心用例覆盖旧配置、三种合法值与主题组合、服务重读、原字段保留、无路由启停副作用，以及非法值在其他设置副作用前拒绝。
- 最后只读代码审阅发现自启动/程序坞旧响应可能全量覆盖风格；已改为只合并自身字段，由新建 `gpt-6-luna / max` 子 agent 补充两条并发路径并再次运行 Node 回归通过。

最终独立视觉复核通过，证据为 65 张单图、5 张四页/状态拼图及几何记录。

未使用 computeruse。未验证原生 macOS WKWebView、Windows WebView2、Windows 实机或安装包；系统 UI/宋体、原生 select、滚动条和抗锯齿可能有平台差异。未改任何图标、系统状态栏或打包脚本。
