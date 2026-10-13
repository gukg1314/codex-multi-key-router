const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const html = fs.readFileSync(path.join(__dirname, '..', 'ui', 'index.html'), 'utf8');
const script = html.split('<script>', 2)[1].split('</script>', 1)[0];

class Element {
  constructor() {
    this.innerHTML = '';
    this.textContent = '';
    this.className = '';
    this.scrollTop = 0;
    this.scrollLeft = 0;
    this.children = [];
    this.classList = { toggle() {} };
  }
  appendChild(child) { this.children.push(child); }
  addEventListener() {}
  contains() { return false; }
  remove() {}
}

const state = {
  ok: true,
  app_version: 'test',
  platform: 'macos',
  taken_over: false,
  keys: [
    {
      id: 'key-1', name: 'Synthetic Key', enabled: true, has_key: true,
      masked_key: 'sk-te******0001', upstream_base_url: 'https://upstream.example/v1',
      included_count: 1, discovered_at: '2026-09-30 10:00:00',
      selected_models: null, discovered_models: ['kimi-k3'],
    },
    {
      id: 'key-2', name: 'Second Synthetic Key', enabled: true, has_key: true,
      masked_key: 'sk-te******0002', upstream_base_url: 'https://second.example/v1',
      included_count: 1, discovered_at: '2026-09-30 10:01:00',
      selected_models: null, discovered_models: ['glm-5.3'],
    },
  ],
  models: [
    { slug: 'local/kimi-k3', source: 'key-1', upstream_model: 'kimi-k3', wire_api: 'chat', input_modalities: ['text'] },
    { slug: 'local/glm-5.3', source: 'key-2', upstream_model: 'glm-5.3', wire_api: 'responses', input_modalities: ['text'] },
  ],
  conflicts: [],
  official: {
    logged_in: true, count: 1, models: [{ slug: 'gpt-5.6-terra' }],
  },
  router: { running: true, port: 8790, pid: null },
  settings: {
    theme: 'light', autostart: false, runtime: '/synthetic/data', admin_port: 8791,
    keys_file: '/synthetic/data/keys.json', autostart_command: '/synthetic/launch',
  },
  task: { status: 'done', kind: 'idle', detail: {} },
  events: [{ time: '10:00', message: 'Synthetic event' }],
};

const elements = new Map();
const listeners = {};
let responseQueue = [];
let fetchOverride = null;
const document = {
  documentElement: { setAttribute() {} },
  activeElement: null,
  getElementById(id) {
    if (!elements.has(id)) elements.set(id, new Element());
    return elements.get(id);
  },
  addEventListener(type, listener) {
    (listeners[type] ||= []).push(listener);
  },
  querySelectorAll() { return []; },
  createElement() { return new Element(); },
};

const context = vm.createContext({
  document,
  location: { search: '?page=keys' },
  history: { replaceState() {} },
  localStorage: { getItem() { return null; }, setItem() {} },
  URL,
  URLSearchParams,
  console: { info() {}, debug() {}, warn() {} },
  fetch: (...args) => {
    if (fetchOverride) return fetchOverride(...args);
    const next = responseQueue.shift() || state;
    return Promise.resolve({ ok: true, status: 200, json: async () => next });
  },
  setTimeout(callback) { callback(); return 1; },
  setInterval() { return 1; },
  window: {},
  confirm() { return true; },
});
context.__fixture = state;

function clickTarget(action, attrs = {}) {
  return {
    closest(selector) { return selector === '[data-action]' ? this : null; },
    getAttribute(name) { return name === 'data-action' ? action : (attrs[name] ?? null); },
  };
}

async function click(action, attrs = {}) {
  const handler = listeners.click && listeners.click[0];
  assert.ok(handler, 'document click handler is registered');
  await handler({ target: clickTarget(action, attrs) });
}

async function main() {
  vm.runInContext(script, context);
  assert.equal(vm.runInContext('SELECTED_KEY', context), undefined,
    'undefined means no source has been selected yet');

  await vm.runInContext('loadState(false)', context);
  assert.equal(vm.runInContext('SELECTED_KEY', context), 'key-1');

  await click('select-official');
  assert.equal(vm.runInContext('SELECTED_KEY', context), null,
    'null records an intentional official-account selection');

  await vm.runInContext('loadState(true)', context);
  assert.equal(vm.runInContext('SELECTED_KEY', context), null,
    'a silent state poll preserves the official selection');
  assert.match(document.getElementById('page').innerHTML, /data-action="select-official"/);
  assert.match(document.getElementById('page').innerHTML, /keyitem on/);

  responseQueue = [{
    ...state,
    task: { status: 'done', kind: 'discover_official', detail: { discovered: 2, restart_pending: true } },
  }, state];
  await vm.runInContext('pollTask()', context);
  assert.equal(vm.runInContext('SELECTED_KEY', context), null,
    'official discovery completion keeps the current Key-page selection');
  assert.ok(document.getElementById('toast').children.some(item => item.textContent.includes('手动重启 Codex')),
    'the UI explains when the official model update needs a manual restart');

  await click('select-key', { 'data-id': 'key-2' });
  await vm.runInContext('loadState(true)', context);
  assert.equal(vm.runInContext('SELECTED_KEY', context), 'key-2',
    'a silent state poll preserves a selected Key');

  responseQueue = [{
    ...state,
    task: { status: 'done', kind: 'discover', detail: { key_id: 'key-1', discovered: 3 } },
  }, state];
  await vm.runInContext('pollTask()', context);
  assert.equal(vm.runInContext('SELECTED_KEY', context), 'key-2',
    'a different Key discovery completion does not steal the selected Key');

  responseQueue = [{ ...state, keys: [state.keys[0]], models: [state.models[0]] }];
  await vm.runInContext('loadState(true)', context);
  assert.equal(vm.runInContext('SELECTED_KEY', context), 'key-1',
    'when a selected Key disappears, selection falls back to a remaining Key');

  let releaseDelete;
  const pendingDelete = new Promise(resolve => { releaseDelete = resolve; });
  fetchOverride = (url, options) => {
    if (url === '/api/keys/key-1' && options.method === 'DELETE') return pendingDelete;
    return Promise.resolve({ ok: true, status: 200, json: async () => ({ ...state, keys: [state.keys[1]], models: [state.models[1]] }) });
  };
  const deletePromise = listeners.click[0]({ target: clickTarget('delete-key') });
  await click('select-official');
  releaseDelete({ ok: true, status: 200, json: async () => ({ ok: true }) });
  await deletePromise;
  fetchOverride = null;
  assert.equal(vm.runInContext('SELECTED_KEY', context), null,
    'finishing an in-flight delete does not steal a newer official selection');

  vm.runInContext("STATE=__fixture; PAGE='overview'; OVERVIEW_SOURCE='official'; render();", context);
  let overview = document.getElementById('page').innerHTML;
  assert.match(overview, /class="sources-layout"/);
  assert.match(overview, /class="source-list scroll"/);
  assert.match(overview, /class="[^"]*overview-model-table/);
  assert.match(overview, /gpt-5\.6-terra/);
  assert.match(overview, /<details class="card recent-events"[^>]*>/);
  assert.doesNotMatch(overview, /<details class="card recent-events"[^>]*\bopen\b/,
    'recent events start collapsed');

  const toggle = listeners.toggle && listeners.toggle[0];
  assert.ok(toggle, 'the recent events toggle listener is registered');
  toggle({ target: { id: 'recent-events', open: true } });
  responseQueue = [{ ...state, events: [{ time: '10:01', message: 'Changed synthetic event' }] }];
  await vm.runInContext('loadState(true)', context);
  overview = document.getElementById('page').innerHTML;
  assert.match(overview, /<details class="card recent-events"[^>]*\bopen\b/,
    'an open event panel stays open when a state poll redraws the overview');

  await click('select-overview-source', { 'data-source': 'key-1' });
  overview = document.getElementById('page').innerHTML;
  assert.match(overview, /Synthetic Key/);
  assert.match(overview, /local\/kimi-k3/);
  assert.doesNotMatch(overview, /gpt-5\.6-terra/);

  const footer = document.getElementById('foot').innerHTML;
  assert.doesNotMatch(footer, /\/synthetic\/data/);
  const settings = vm.runInContext('pageSettings()', context);
  assert.match(settings, /\/synthetic\/data/);
  assert.match(settings, /隐藏程序坞图标/);
  assert.match(settings, /data-action="toggle-hide-dock" role="switch" aria-checked="true"/);
  assert.match(settings, /aria-labelledby="hide-dock-label" aria-describedby="hide-dock-description"/);

  const dockMessages = [];
  context.window.webkit = {
    messageHandlers: { codexmkDockVisibility: { postMessage(value) { dockMessages.push(value); } } },
  };
  await vm.runInContext('loadState(true)', context);
  assert.deepEqual(dockMessages, [true], 'a missing setting uses the enabled macOS default');
  vm.runInContext("PAGE='settings'; render(); render();", context);
  await vm.runInContext('loadState(true)', context);
  assert.deepEqual(dockMessages, [true], 'rendering and unchanged state polls do not repeat the native update');

  let releaseDockSave;
  let dockSaveCount = 0;
  const pendingDockSave = new Promise(resolve => { releaseDockSave = resolve; });
  fetchOverride = (url, options) => {
    assert.equal(url, '/api/settings');
    assert.equal(options.method, 'PUT');
    assert.deepEqual(JSON.parse(options.body), { hide_dock_icon: false });
    dockSaveCount += 1;
    return pendingDockSave;
  };
  const dockSavePromise = click('toggle-hide-dock');
  assert.match(document.getElementById('page').innerHTML, /data-action="toggle-hide-dock"[^>]*\bdisabled/);
  assert.deepEqual(dockMessages, [true], 'the native setting stays unchanged while saving');
  await click('toggle-hide-dock');
  assert.equal(dockSaveCount, 1, 'an in-flight setting save cannot be submitted twice');
  releaseDockSave({ ok: true, status: 200, json: async () => ({ ok: true, settings: { ...state.settings, hide_dock_icon: false } }) });
  await dockSavePromise;
  assert.equal(vm.runInContext('STATE.settings.hide_dock_icon', context), false);
  assert.deepEqual(dockMessages, [true, false], 'the saved false value reaches the native bridge');
  assert.match(document.getElementById('page').innerHTML, /data-action="toggle-hide-dock" role="switch" aria-checked="false"/);
  assert.doesNotMatch(document.getElementById('page').innerHTML, /data-action="toggle-hide-dock"[^>]*\bdisabled/);

  fetchOverride = () => Promise.resolve({ ok: false, status: 500, json: async () => ({ ok: false, code: 'operation_failed' }) });
  await click('toggle-hide-dock');
  assert.equal(vm.runInContext('STATE.settings.hide_dock_icon', context), false);
  assert.deepEqual(dockMessages, [true, false], 'a failed save never applies a native setting');
  assert.equal(vm.runInContext('SAVING_DOCK_VISIBILITY', context), false);

  context.__windowsDockFixture = { ...state, platform: 'windows' };
  vm.runInContext('STATE=__windowsDockFixture; syncDockVisibility();', context);
  assert.doesNotMatch(vm.runInContext('pageSettings()', context), /toggle-hide-dock|隐藏程序坞图标/);
  await click('toggle-hide-dock');
  assert.deepEqual(dockMessages, [true, false], 'Windows does not apply the macOS-only setting');

  context.__unbridgedDockFixture = { ...state, settings: { ...state.settings, hide_dock_icon: true } };
  vm.runInContext('STATE=__unbridgedDockFixture;', context);
  context.window.webkit = undefined;
  fetchOverride = () => Promise.resolve({ ok: true, status: 200, json: async () => ({ ok: true, settings: { ...state.settings, hide_dock_icon: false } }) });
  await click('toggle-hide-dock');
  assert.equal(vm.runInContext('STATE.settings.hide_dock_icon', context), false,
    'the setting can still be saved when the UI runs without a native bridge');
  fetchOverride = null;

  process.stdout.write('UI behavior checks passed\n');
}

main().catch(error => {
  process.stderr.write(String(error.stack || error) + '\n');
  process.exitCode = 1;
});
