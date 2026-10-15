# Codex 路由工具

一个本机小服务，让 Codex 同时用两类模型：**ChatGPT 官方账号里的模型**，以及**多个第三方 Key 自己的模型**。
界面是可视化四页，macOS 出 dmg，Windows 出安装 exe，目标电脑不用装 Python 或任何环境。

施工过程文档见 [施工计划历史](施工计划历史/)。

## 界面更新

- 设置提供三种界面风格：默认主题（浮层工作台）、工作索引、模型字典；与浅色/深色独立组合，保存后刷新或重启仍保持。旧配置继续使用模型字典。
- 默认主题的“Key 与模型”页以模型列表为主：常规窗口将来源放在右侧，1180×820 可完整显示7行；900×700窄窗可显示4行，来源底栏可滚动查看。
- 四页共用业务、数据与接口，切换风格保留页面、来源、搜索、勾选/图片/优先级草稿、焦点和合理滚动位置；不触发路由启停或接管。
- 概览展示模型来源、服务状态和操作，不显示最近事件；设置保留启动、路由、外观和日志诊断。
- [三风格施工参数](docs/UI施工参数-界面风格.md) · [截图与验收](docs/ui-style-switch/README.md) · [Key页空间优化](docs/ui-style-switch/keys-room/README.md)。

## 父子代理同源修复

- 子代理保留所选模型型号，继承父代理的实际 Key、上游或官方账号；同来源没有该子模型时明确提示，不切换其他来源。标题和图片请求也不能绕过检查。
- 同名模型被全局优先级隐藏时，仍可作为该 Key 的子代理候选；官方模型刷新后，子代理使用最新官方目录。
- 原生 Responses 保留加密的代理消息；Chat 桥接只转换明文，遇到密文明确提示不兼容，不删除或猜测正文。
- 升级安装后点一次「刷新模型」生成新清单并加载新路由。路由重启后需先在父会话继续一轮，重新建立来源绑定。旧会话已有的损坏密文需在原机恢复，不会被本修复自动改写。
- `router.log` 记录父子会话 ID、所选模型、实际来源和拒绝原因，不记录密钥或消息正文。

## v1.2.11 子任务路由修复

- 网关任务创建子任务时，按 Codex 提供的父任务 ID 继承父任务使用的网关模型，不会因子任务默认模型改走个人账号。
- Chat 桥接和 `/responses` 直通都能转换 Codex 的 `agent_message`，避免本地路由在模型收到请求前报 `unsupported_input_type`。
- 软件界面直接读取后台版本；安装器、外壳、后台与包名增加一致性测试，统一显示为 `1.2.11`。
- `router.log` 记录请求 ID、父子路由结果、模型和接入方式，不记录密钥或正文。

## v1.2.10 自动标题路由

- 自动标题只使用已启用、已勾选且探测通过的网关模型；优先 `/responses`，再试 Chat，按 Key 顺序接力，整次最多 20 秒。即使正文用官方 GPT，标题也不使用个人账号。
- 网关全部失败时，Codex 保留创建会话时设置的临时标题；标题失败不影响会话创建。工具调用续接使用同一 Key 和模型。
- `router.log` 记录标题请求 ID、模型、Key 来源、接入方式和失败原因，不记录密钥或正文。

## v1.2.9 修复

- 旧 Astra 等模型记录会重新验证接入方式：Chat 工具调用失败且 `/responses` 成功时才升级；临时失败保留原记录。
- Chat 桥接能识别 `tool_search`，从请求自带的延迟加载工具中展开可用函数。
- Chat 桥接暂时跳过 Codex 内置 `web_search` 及相关选择、历史，避免阻断对话；其他工具保留，原生 `/responses` 搜索不受影响。日志会记录跳过数量。
- 公司 Key 的 `/responses` 图片工具改用网关可接受的顶层别名，调用结果还原给 Codex；实际图片生成请求按回合 ID 继续使用同一个公司 Key，官方模型仍走官方账号。
- 关键路由日志保留请求 ID、模型、接入方式和改写结果，不再记录请求、响应或工具参数正文。macOS 与 Windows 使用同一份核心代码。

## v1.2.8 改进

- **官方账号请求可溯源**：请求没匹配到公司 Key、被转发到个人 ChatGPT 账号时，`router.log` 会写一条 `official account usage`，记下 `rid`、`model`、`path`、`target=chatgpt.com`、登录态类型和 `x-codex-*` 头名（只记头名与 `thread_source`，不记凭据、不记正文）。排查「已经切到网关，个人账号容量还在掉」时，直接搜这一行，就能确认到底哪些模型还在走个人账号。
- **启动时列出走个人账号的模型**：启动日志新增 `official priority models=...`，一眼看到当前被标记为 official 的名单（例如标题生成固定使用的 `gpt-5.6-luna`）。Windows 回传日志路径：`%LOCALAPPDATA%\CodexMultiKey\runtime\app\runtime\logs\router.log`。
- 本次只重打 Windows 安装包，macOS 包仍为 1.2.5。

## v1.2.7 改进

- **上游拒绝日志再补内容**：`local upstream rejected` 现在带 `messages=`、`payload_chars=`、`last_user=`、`last_tool=`，被上游 4xx/5xx 拒绝时能直接看到本次请求的最后一条用户输入和最后一条工具输出（各前 500 字符）。上游只说「敏感词」不说哪个词时，用这两段就能对照出被拒内容。
- 本次只重打 Windows 安装包，macOS 包仍为 1.2.5。

## v1.2.6 改进

- **排障日志更完整**：Local 桥接的上游返回 4xx/5xx 时，`local upstream rejected` 会把上游响应体前 500 字符写进路由日志；工具调用因参数不合法被降级时，`local bridge downgraded tool call` 会把参数前 200 字符写进日志。以后再遇到「high demand」或工具调用被跳过，直接看日志里的上游原文即可定位。
- 本次只重打 Windows 安装包，macOS 包仍为 1.2.5。

## v1.2.5 修掉的问题

- **macOS 上「Key 与模型」页的删除按钮点了没反应**：mac 外壳的网页视图没有接管 JavaScript 的 `confirm()`，系统直接按「否」返回，删除请求根本没发出去（Windows 外壳本来就有接管，所以只有 mac 看起来像坏了）。现在 mac 外壳补上 `WKUIDelegate`，`confirm()` 和 `alert()` 都会弹系统原生确认框：点「确定」才删除，点「取消」原样保留，方便以后界面里再用这两个方法。

## v1.2.4 修掉的问题

- **退出工具后 Python 进程还在**：以前点「退出」只把路由挂起，后台管理服务（`pythonw.exe`）继续留在内存里；Windows 覆盖安装时它锁着 `runtime\python\_bz2.pyd`，安装程序只能报「文件正被另一个进程使用」，必须先手动杀进程。现在退出会先停路由，再关掉整个后台服务，工具退干净、不留 Python 进程；Codex 依旧留在本地路由上，下次开机自启或手动打开工具都会把服务重新拉起来。

## v1.2.3 修掉的问题

- **模型列表被自动刷回顶部**：界面每 5 秒拉一次后台状态，之前不管数据有没有变都整页重画，模型列表是独立滚动框，一重画就被打回顶部，正在看的模型、勾选和搜索词都被打断。现在内容没变就完全不碰界面，内容确实变了才重建，并且会把列表滚动位置原样搬回去。

## v1.2.2 修掉的问题

- **关闭开机自启动弹 Load failed**：1.2.1 关闭自启时用 `launchctl bootout` 卸载登录项，而登录项里正跑着工具自己的进程，卸载动作把后台服务一起杀掉，请求收不到响应，界面只能报英文 `Load failed`，plist 还残留在磁盘上。现在关闭自启只禁用登录项并删除 plist，当前进程继续服务到用户主动退出；启动时若发现 plist 和登录项状态对不上，会自动补一次加载把系统登录项数据库修一致。
- **连接失败提示中文化**：本地服务连不上时，界面不再直接弹 `Load failed`，改成中文提示。

## v1.2.1 修掉的问题

- **Windows 桌面闪黑框**：后台服务是在无控制台的 `pythonw.exe` 里跑的，调用 netstat / tasklist / taskkill / icacls / PowerShell / zstd 这类短命控制台程序时，Windows 每次都会新开一个黑框。现在这些调用统一带 `CREATE_NO_WINDOW`，桌面不再每隔几秒闪一下。
- **开机自启只起了后台服务**：登录项改成带 `--silent` 静默启动桌面程序，开机后后台服务和菜单栏 / 托盘图标一起恢复；同时把「用户是否期望路由运行」记进 `settings.json`，重启后不会再出现「工具开着但路由是停止的」。

## 两条链路（核心）

- **官方模型**：请求原样转发到 `https://chatgpt.com/backend-api/codex`，用本机 Codex 的 ChatGPT 账号登录态，不经过任何 Key。
- **Key 模型**：每个 Key 各自配名称、密钥、上游地址、启停和模型白名单；刷新时多个 Key 并发拉取（固定 3 个并发），某个 Key 撞限流或 5xx 时只对它自己串行重试一次，其他 Key 不受影响；转发时按归属取密钥，清单里不写密钥。
- **接入方式**：上游模型声明 `openai-response` 端点时走 `/responses` 原生直通，图片工具名按网关要求改写（推理强度滑块可用）；只声明 `openai` 的走 Chat 桥接，若该模型在 Chat 上带不了工具调用（例如上游提示必须改用 `/responses`），刷新时会自动改走直通，不再误判成不兼容。概览页和「Key 与模型」页都能看到每个模型当前是哪一种。
- **同名模型**：默认 Key 覆盖官方，可在「路由优先级」页改回官方或指定别的 Key；固定走优先级第 1 个来源，不做失败自动切换。

## 端口

| 用途 | 地址 |
| --- | --- |
| 路由（Codex 连这个） | `127.0.0.1:8790` |
| 管理界面 / API | `127.0.0.1:8791`（设置页可改） |

## 安装使用

### macOS（Apple Silicon）

1. 双击 `release/<版本>/CodexRouter-<版本>-mac-arm64.dmg`。
2. 把「Codex 路由.app」拖进「应用程序」。
3. 第一次打开要 **右键 → 打开**（包未签名，直接双击会被拦）。

### Windows（x64）

1. 双击 `release/<版本>/CodexRouter-<版本>-win-x64.exe`，按向导选择安装目录；默认是 `%LOCALAPPDATA%\CodexMultiKey`，只为当前用户安装。
2. 桌面和开始菜单会出现「Codex 路由」图标；完成页可选择立即运行。
3. 第一次运行可能被 SmartScreen 拦，点「更多信息」→「仍要运行」。

### 装好之后

- 「Key 与模型」页加 Key（可以加多个）；把示例上游地址换成自己的 OpenAI-compatible API 地址，再点「刷新模型」。
- 「Key 与模型」选中「ChatGPT 官方账号」，点「拉取官方模型」获取当前账号可用的最新 Codex 模型，完成后自动重启 Codex 使目录生效；登录过期时会提示重新登录，原列表保留。
- 官方账号或 Key 的选择会保留，不会被后台状态轮询自动切走。
- 「概览」查看路由状态、来源及模型，执行刷新、启用、停止或切回官方；三种风格使用各自布局，地址和数据目录在设置中查看。
- 「路由优先级」页决定同名模型走哪个 Key，或者改回官方。
- 开机自启静默启动桌面程序：后台服务、菜单栏 / 托盘图标一起恢复，主窗口不弹；要看界面双击图标即可。
- macOS 设置里的「隐藏程序坞图标」默认开启、即时生效：手动启动照常显示窗口，Dock 和 Cmd+Tab 不显示工具；关闭窗口或按 Cmd+Q 继续后台运行，状态栏「退出」才停止工具。关闭开关恢复原来的程序坞和 Cmd+Q 行为。
- 关窗口不退出：macOS 缩进菜单栏图标，Windows 会让你选「最小化」到托盘还是「关闭工具」。
- 「关闭工具」会停掉路由并关掉整个后台服务（不留 Python 进程），Codex 的本地接管状态保留、不切回官方直连；下次开机会按这个状态自动恢复；开机自启开关不受影响。

## 数据与日志位置

两端都只有**一个数据文件夹**（窗口外壳和 Python 服务写同一个地方）：

| 平台 | 文件夹 |
| --- | --- |
| macOS | `~/Library/Application Support/CodexOfficialLocalRouter/` |
| Windows | `<安装目录>\runtime\app\runtime\`（默认安装目录为 `%LOCALAPPDATA%\CodexMultiKey`） |

里面固定这几个文件：

- `keys.json`：多个 Key、密钥、勾选的模型、同名模型优先级
- `local-models.json`、`merged-models.json`：模型清单（不含密钥）
- `settings.json`：端口、接管开关、自启动开关、macOS 程序坞图标开关、界面风格和浅深色主题
- `logs/`：`launcher.log`（窗口外壳）、`admin.log`（管理服务）、`router.log`、`toolctl.log`

全部 UTF-8 无 BOM，记事本可直接打开。

macOS 和老工具共用这个数据文件夹，所以老工具里已经配好的 Key 和模型会直接沿用；
Windows 的安装目录里，重装或更换安装目录会保留并迁移数据文件夹；卸载会连它一起删（要留就先备份）。

## 开机自启动

| 平台 | 落地位置 |
| --- | --- |
| macOS | `~/Library/LaunchAgents/com.codexmultikey.router.admin.plist` |
| Windows | `HKCU\Software\Microsoft\Windows\CurrentVersion\Run` 的 `CodexMultiKeyRouter` |

注册的动作只有一个：带 `--silent` 启动桌面程序，由它静默拉起后台管理服务，所以开机后菜单栏 / 托盘图标也在。工具每次启动都会把已经打开的登录项指向当前安装位置的桌面程序，1.2.0 及更早写下的旧条目在下次登录时自动改写；启动时还会顺手清掉老工具的自启动项，避免两个程序抢 8790 端口。
「设置」页可以随时开关；关闭时只禁用登录项并删掉 plist，正在运行的服务和菜单栏不会被中断。

## 一键打包（Apple Silicon Mac）

双击根目录的 `一键打包.command`。它会把补丁版本号加 1，同步 Mac、Windows 和软件界面的版本，依次构建并校验两包，然后放进 `release/<新版本>/`。例如当前 `1.2.11` 会输出 `release/1.2.12/`；任一步失败则恢复原版本，不发布半成品。

也可以单独构建当前版本（不会自动加版本号）：

```bash
python3 packaging/build_windows.py   # -> dist-output/CodexRouter-Setup-<当前版本>.exe
python3 packaging/build_macos.py     # -> dist-output/CodexRouter-<当前版本>-arm64.dmg
```

- Windows：Go 交叉编译窗口外壳，NSIS 生成带目录选择、进度和完成页的安装器；内嵌 `python-3.12.10-embed-amd64` 与 zstd 解码器，最后自检 + 冒烟跑一次打包好的 `admin.py`。
- macOS：cgo 编译 WKWebView 外壳，内嵌 python-build-standalone，ad-hoc 签名后 `hdiutil` 出 dmg，并挂载回验。
- 需要 Go 工具链、NSIS 的 `makensis`；macOS 打包还需要 `hdiutil` / `codesign`。首次联网下载运行时，缓存在 `dist-output/.cache`。
- 图标：应用图标（macOS icns、Windows exe/桌面/托盘 ico）优先用根目录 `icondock.png`（成品白底圆角卡片，生成时把卡片外的白边抠成透明），没有就退回 `icon圆角.png` 合成白底，再退回 `icon.png`；菜单栏剪影和界面 logo 始终用透明版 `icon.png`，不会出现白块。

## 测试

```bash
python3 -m unittest discover -s tests -q
node tests/test_ui_behavior.js
cd launcher && go test ./...
cd installer && go test ./...
```

## Windows 编码兼容（硬约束）

PowerShell 和 .bat 的代码页问题是这套工具的头号风险，所以按下面的规矩做，并且有测试守着：

1. 全项目**不产生任何 `.bat` / `.cmd` / `.ps1`**（`tests/test_packager.py` 扫全仓，有就直接失败）。
2. 必须调用 PowerShell 时只走 `-EncodedCommand`（UTF-16LE + Base64），中文永远不进命令行。
3. 会被 Windows 解析的字符串一律纯 ASCII：Go 源码、`packaging/build_windows.py`、`launcher/*.m` 都断言无 BOM 全 ASCII。
4. 拉起 Python 时强制 `PYTHONUTF8=1`、`PYTHONIOENCODING=utf-8`、`-X utf8`，不依赖系统代码页。
5. 配置、日志、打包文本统一 UTF-8（日志无 BOM，Windows 下发给用户的说明文件是 UTF-8 BOM + CRLF，记事本不会乱码）。
6. 默认安装路径纯 ASCII：`%LOCALAPPDATA%\CodexMultiKey`；界面文案只走 HTTP/JSON，不走控制台。
7. 不硬编码 `cp936` / `gbk`；Windows 侧路径走 UTF-16 API，Python 侧用 `os.fsdecode`。

## 卸载

- macOS：先在「设置」页关掉自启动，再删掉「应用程序」里的 .app；自启残留可删 `~/Library/LaunchAgents/com.codexmultikey.router.admin.plist`。
- Windows：「设置 → 应用 → 已安装的应用 → Codex 路由 → 卸载」，或双击安装目录里的 `uninstall.exe`。

## 已知限制

- macOS 只出 arm64 未签名 dmg；Windows 安装包未做代码签名，SmartScreen 会提示一次。
- 同名模型固定走选中的来源，不做失败自动切换。
- 关闭窗口后工具留在菜单栏 / 托盘里；开机自启静默启动，不弹主窗口。

## 目录结构

| 路径 | 作用 |
| --- | --- |
| `app/` | Python 核心：`router.py` 路由、`admin.py` 管理 API、`toolctl.py` 工具链、`keys_store.py` 多 Key 存储、`autostart.py` 自启动 |
| `ui/index.html` | 四页界面（概览 / Key 与模型 / 路由优先级 / 设置） |
| `launcher/` | Go 窗口外壳（macOS WKWebView / Windows WebView2 一份主流程） |
| `installer/` | Windows 安装器与卸载器 |
| `packaging/` | 两个打包脚本和共用工具 |
| `tests/` | 必要单测 |
| `dist-output/` | 打包产物与下载缓存（不进版本库） |
