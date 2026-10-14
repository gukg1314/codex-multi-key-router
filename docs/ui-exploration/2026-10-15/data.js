/* 三套设计共用的虚构内容，不连接管理服务或读取本机配置。 */
window.ROUTER_PREVIEW = Object.freeze({
  title: 'Codex 路由',
  version: '1.2.11',
  page: '概览',
  running: '运行中',
  takeover: '已接管 Codex',
  nav: ['概览', 'Key 与模型', '路由优先级', '设置'],
  actions: ['刷新模型', '启用路由', '停止路由', '切回官方'],
  counts: [
    { label: 'Key 模型', value: 10 },
    { label: '已配置 Key', value: 3 },
    { label: '官方模型', value: 12 },
    { label: '同名模型', value: 3 }
  ],
  sources: [
    { name: '官方账号', count: 12, state: '已登录' },
    { name: '主力 Key', count: 7, state: '已配置', selected: true },
    { name: '备用 Key', count: 3, state: '已配置' },
    { name: '测试 Key', count: 0, state: '已停用' }
  ],
  models: [
    { name: 'gpt-6.1-sol', wire: '原生直通' },
    { name: 'gpt-6-astra', wire: '原生直通' },
    { name: 'gpt-6-luna', wire: '原生直通' },
    { name: 'claude-sonnet-5.5', wire: 'Chat 桥接' },
    { name: 'deepseek-v4.1', wire: 'Chat 桥接' },
    { name: 'glm-5.3-flash', wire: 'Chat 桥接' },
    { name: 'kimi-k3', wire: 'Chat 桥接' }
  ],
  events: [
    { time: '14:32', message: '刷新完成，模型目录已更新' },
    { time: '14:31', message: '主力 Key 拉取完成' },
    { time: '14:30', message: '路由服务已启动' }
  ]
});
