/* ============================================================ 控制台页面 JS 的执行夹具
 *
 * 用法（由 `tests/test_panel_js.py` 调用）::
 *
 *     node tests/js/panel_harness.js <page.html> <payloads.json> <static-dir>
 *
 * 输出一份 JSON（stdout），内含：页面脚本是否抛异常、`#rail-state` 与
 * `#activity` 的最终值、以及页面发出过哪些 fetch。
 *
 * ## 为什么要有这个东西
 *
 * 仓库里所有的 Web 门（`tools/web_smoke.py`、`tools/web_pages_check.py`、
 * `tests/test_panel.py`）都走 in-process 渲染 —— **它们不执行 JavaScript**。
 * 于是「页面渲染正确」与「页面能用」之间有一整层没人看过，而右栏正好住在
 * 那一层：实测撞上过一次，`Trail.activeKinds` 返回字符串、调用处 `.join(',')`
 * 抛 `kinds.join is not a function`，**同步**抛出掐掉了同一段 `<script>` 里
 * 后面的右栏 IIFE —— 页眉那行永远停在 `未连接`，而 `/api/agent-log` 本身
 * 完全正常。四个渲染门全绿，右栏一个字都没有。
 *
 * ## 两条设计约束
 *
 * 1. **不联网、不占端口**。payloads.json 是 Python 侧用 TestClient 取回来的
 *    响应，按「路径+查询串」建索引；页面里的 fetch 一律查这张表。缺哪条就
 *    报错 —— 新加一个接口而夹具没跟上，会当场露出来，而不是静默空转。
 * 2. **stub 的缺口必须与页面 bug 分得开**。DOM 是 Proxy 包的：页面读到本夹具
 *    没实现的属性会抛 `StubGap: xxx`，而页面自己的异常不带这个前缀。
 *    实测踩过：夹具少实现一个 `childNodes`，报出来的却是
 *    `Cannot read properties of undefined (reading 'length')`，看着像页面的错。
 */
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const [pagePath, payloadPath, staticDir] = process.argv.slice(2);
const html = fs.readFileSync(pagePath, 'utf-8');
const payloads = JSON.parse(fs.readFileSync(payloadPath, 'utf-8'));

const log = [];
const fetches = [];
const errors = [];

/* ------------------------------------------------------------------ DOM stub */
class El {
  constructor(tag) {
    this.tagName = tag; this.className = ''; this._text = ''; this._html = '';
    this.children = []; this.attrs = {}; this.hidden = false; this.style = {};
    this.value = ''; this.type = ''; this.title = ''; this.disabled = false;
    this.checked = false; this.dataset = {}; this.href = '';
    this._frag = false; this._st = 0;   // 夹具自己的内部标记（要声明，否则被 Proxy 拦下）
    this.classList = {
      _s: new Set(),
      add(c) { this._s.add(c); },
      remove(c) { this._s.delete(c); },
      contains(c) { return this._s.has(c); },
      toggle(c, on) {
        if (on === undefined) { on = !this._s.has(c); }
        if (on) { this._s.add(c); } else { this._s.delete(c); }
      },
    };
  }
  // 与真实 DOM 一致：读的是**自己 + 全部后代**的文字，写会清掉后代。
  // 不这么做的话，「正文到底有没有渲染出来」就只能靠逐个子节点翻 ——
  // 而实测要断的正是「模型的回答原文在不在页面上」这一句。
  get textContent() {
    return this._text + this.children.map((c) => (c && c.textContent) || '').join('');
  }
  set textContent(v) { this._text = String(v); this.children = []; }
  get innerHTML() { return this._html; }
  set innerHTML(v) { this._html = String(v); this.children = []; }
  get firstChild() { return this.children[0] || null; }
  get lastChild() { return this.children[this.children.length - 1] || null; }
  get childNodes() { return this.children; }
  get parentNode() { return null; }
  appendChild(n) {
    if (n && n._frag) { this.children.push(...n.children); } else { this.children.push(n); }
    return n;
  }
  removeChild(n) { const i = this.children.indexOf(n); if (i >= 0) { this.children.splice(i, 1); } return n; }
  insertBefore(n) { this.children.unshift(n); return n; }
  append(...ns) { ns.forEach((n) => this.appendChild(n)); }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  getAttribute(k) { return this.attrs[k] === undefined ? null : this.attrs[k]; }
  removeAttribute(k) { delete this.attrs[k]; }
  addEventListener() {}
  removeEventListener() {}
  querySelector() { return null; }
  querySelectorAll() { return []; }
  closest() { return null; }
  focus() {}
  click() {}
  get scrollHeight() { return 0; }
  set scrollTop(v) { this._st = v; }
  get scrollTop() { return this._st || 0; }
}

function wrap(el) {
  return new Proxy(el, {
    get(t, p, r) {
      if (typeof p === 'symbol' || p === 'then' || p === 'inspect' || p === 'constructor') {
        return Reflect.get(t, p, r);
      }
      if (!(p in t)) { throw new Error('StubGap: ' + String(p)); }
      return Reflect.get(t, p, r);
    },
  });
}

// 只有页面里**真的存在**的 id 才给元素。一律返回万能对象的话，正好会把
// 「这个 id 不存在」这类 bug 盖掉 —— 而那正是要查的东西之一。
const ids = new Set();
for (const m of html.matchAll(/\bid="([^"]+)"/g)) { ids.add(m[1]); }
const byId = new Map();
function getEl(id) {
  if (byId.has(id)) { return byId.get(id); }
  const el = ids.has(id) ? wrap(new El('div')) : null;
  if (!el) { log.push(`getElementById('${id}') → null（页面里没有这个 id）`); }
  byId.set(id, el);
  return el;
}

const sandbox = {
  console,
  setTimeout, clearTimeout, setInterval, clearInterval,
  Promise, JSON, Math, Date, Object, Array, String, Number, Boolean, Error,
  encodeURIComponent, decodeURIComponent, isNaN, parseInt, parseFloat,
  document: {
    getElementById: getEl,
    createElement: (t) => wrap(new El(t)),
    createDocumentFragment: () => { const f = wrap(new El('#fragment')); f._frag = true; return f; },
    querySelectorAll: () => [],
    querySelector: () => null,
    addEventListener: () => {},
    body: wrap(new El('body')),
  },
  localStorage: { getItem: () => null, setItem: () => {}, removeItem: () => {} },
};
sandbox.window = sandbox;

// fetch 查 payloads.json。索引**按路径**（查询串另查一次，有就用精确的那份）：
// 页面的 `kinds` 默认值将来变了不该让夹具失效 —— 那是策略，不是接口。
// 路径缺失才是真缺口（新加接口而夹具没跟上），当场报错。
sandbox.fetch = (url) => {
  const u = String(url);
  fetches.push(u);
  const p = u.split('?')[0];
  const hit = (u in payloads) ? payloads[u] : (p in payloads ? payloads[p] : undefined);
  if (hit === undefined) {
    return Promise.reject(new Error('FixtureMiss: 夹具里没有 ' + p));
  }
  return Promise.resolve({ json: () => Promise.resolve(hit) });
};

/* --------------------------------------------------------------- 页面脚本 */
const inline = [];
for (const m of html.matchAll(/<script(?![^>]*\bsrc=)[^>]*>([\s\S]*?)<\/script>/g)) {
  inline.push(m[1]);
}
const srcFiles = [];
for (const m of html.matchAll(/<script[^>]*\bsrc="\/static\/([^"]+)"/g)) {
  srcFiles.push(m[1]);
}
const parts = srcFiles.map((f) => fs.readFileSync(path.join(staticDir, f), 'utf-8')).concat(inline);

let syncError = null;
try {
  new vm.Script(parts.join('\n;\n'), { filename: 'panel-page.js' }).runInContext(
    vm.createContext(sandbox));
} catch (e) {
  syncError = e.message + '\n' + (e.stack || '').split('\n').slice(0, 3).join('\n');
}
if (syncError) { errors.push('同步异常: ' + syncError); }

/* ------------------------------------------------------------------ 收结果 */
// 页面的取数是异步的，等两拍再收：`Promise.resolve` 的链足够跑完。
setTimeout(() => {
  const state = byId.get('rail-state');
  const list = byId.get('activity');
  const note = byId.get('rail-note');
  const chips = byId.get('rail-chips');
  const out = {
    errors,
    log,
    fetches,
    rail_state: state ? state.textContent : null,
    rail_note_hidden: note ? note.hidden : null,
    rail_note: note ? note.textContent : null,
    activity_rows: list ? list.children.length : null,
    // 每行渲染出来的**正文** —— 「模型的回答原文在不在屏幕上」就断这个。
    activity_text: list ? list.children.map((li) => li.textContent) : null,
    chips: chips ? chips.children.map((b) => ({
      kind: (b.attrs && b.attrs['data-kind']) || '', text: b.textContent,
    })) : null,
  };
  process.stdout.write(JSON.stringify(out));
  process.exit(0);
}, 300);
