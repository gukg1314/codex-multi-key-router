Codex 路由 v1.2.21 使用说明
=================================

默认安装位置：%LOCALAPPDATA%\CodexMultiKey（安装向导可自选目录）
程序不需要另外安装 Python 或任何环境。

一、第一次使用
--------------
1. 双击桌面「Codex 路由」图标打开界面。
2. 到「Key 与模型」页添加你自己的 Key：名称、密钥、上游地址，保存。可以加多个 Key。
3. 点「刷新模型」：每个 Key 各自拉取自己的模型列表；只想拉一部分时，勾选该 Key 要用的模型，没勾的不拉。
4. 到「路由优先级」页：同一个模型名出现在多个 Key 时，默认走列表里第一个勾选它的 Key，可以随时改成官方或其他 Key。
5. 官方模型继续走你的 ChatGPT 账号登录态，不需要配置 Key。

二、两条链路说明
--------------
- 官方模型：请求原样转发到 chatgpt.com/backend-api/codex，用本机 Codex 的 ChatGPT 登录态，不经过任何 Key。
- Key 模型：走各 Key 自己的上游地址，模型清单里不写密钥，转发时按归属取密钥。
- 官方与 Key 出现同名模型时默认 Key 覆盖官方；想走官方就在「路由优先级」页改回官方。
- 首个来源报错会原样返回，不做失败自动切换。
- 自动标题只尝试已启用 Key 中已勾选的模型，优先 /responses，再试 Chat；失败时保留 Codex 的临时标题，不使用个人账号容量。
- 网关任务创建子任务时，子任务继承父任务的网关模型；Chat 和 /responses 都能转发 Codex 的 agent_message。

三、开机自启动
--------------
默认开启：开机后只静默拉起后台服务（不弹窗口）。需要界面时双击桌面图标即可。
在「设置」页可以关闭自启动。

四、日志位置（出问题先看这里）
--------------
所有日志都在安装目录的 runtime\app\runtime\logs\ 文件夹里：
- launcher.log：窗口外壳
- admin.log：管理服务
- router.log、toolctl.log：路由服务
安装、覆盖和卸载日志在 %LOCALAPPDATA%\CodexMultiKeyInstaller\install.log。
日志都是 UTF-8，可以直接用记事本打开。

五、Key 和配置存在哪里
--------------
都在安装目录的 runtime\app\runtime\ 文件夹里：
- keys.json：几个 Key、各自的密钥和勾选的模型
- local-models.json、merged-models.json：模型清单
- settings.json：端口、自启动开关
覆盖安装或更换安装目录不会删这些文件；换电脑时把整个 runtime\app\runtime\ 文件夹拷过去即可。

六、卸载
--------------
「设置 → 应用 → 已安装的应用 → Codex 路由 → 卸载」，
或双击安装目录里的 uninstall.exe。
卸载会删除程序文件及 Key、配置和日志；安装目录里的其他文件会保留。要留 Key 请先备份上面那个文件夹。

七、常见问题
--------------
- 首次打开被 SmartScreen 拦截：点「更多信息」→「仍要运行」。
- 界面打不开：先看 launcher.log 和 admin.log。
- 装完重启电脑后界面显示「运行中」即为自启动正常。
