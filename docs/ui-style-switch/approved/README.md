# 三种风格施工基准

客户端统一为 **1180×820 CSS px**，不包含原生窗口。

| 风格 | 静态基准 | 依据与唯一结构更新 |
| --- | --- | --- |
| 默认主题（F） | [HTML](f-overview.html) · [PNG](f-overview.png) | 原 F「浮层工作台」，删除服务浮层内的最近事件，保留留白 |
| 工作索引（C） | [HTML](c-overview.html) · [PNG](c-overview.png) | 原 C「索引工作室」，删除目录下方最近事件，保留留白 |
| 模型字典（I） | [已批准 PNG](../../ui-implementation-I/i-overview-approved.png) | 沿用现有正式 I，不重新制作 |

两份 HTML 内嵌同一批虚构数据，可离线打开；未连接管理服务、真实 Key、上游或接管操作。PNG 由隔离 Chrome headless 按 1× 像素密度生成，并已实际看图核对；它们是视觉施工基准，不代表业务、WKWebView 或 WebView2 已验收。

具体尺寸、四页延展、深色和窄窗见 [UI施工参数-界面风格.md](../../UI施工参数-界面风格.md)。图标、原生窗口和系统入口不在本次范围；本目录不含安装包。
