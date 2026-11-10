(function (global) {
  'use strict';
  var KINDS = [
    ['tool_call', '工具', true],
    ['stage', '阶段', true],
    ['assertion', '断言', true],
    ['error', '错误', true],
    ['llm_call', '模型', false],
    ['routing', '路由', false],
    ['file_access', '文件访问', false]
  ];
  function defaultOn() {
    var on = {};
    KINDS.forEach(function (k) { on[k[0]] = k[2]; });
    return on;
  }
  function activeKinds(on) {
    return KINDS.filter(function (k) { return on[k[0]]; })
                .map(function (k) { return k[0]; });
  }
  function markOf(ev) {
    var extra = ev.extra || {};
    if (ev.kind === 'error') { return '×'; }
    if (ev.kind === 'assertion') {
      if (extra.status === 'skipped') { return '○'; }
      if (extra.status === 'pass') { return '√'; }
      return ev.ok === false ? '×' : '√';
    }
    if (ev.kind === 'stage') {
      if (extra.status === 'start') { return '·'; }
      return ev.ok === false ? '×' : '√';
    }
    return ev.ok === false ? '×' : '√';
  }
  function markClass(m) {
    if (m === '×') { return 'mk mk-x'; }
    if (m === '√') { return 'mk mk-v'; }
    return 'mk mk-o';
  }
  function kindLabel(kind) {
    for (var i = 0; i < KINDS.length; i++) {
      if (KINDS[i][0] === kind) { return KINDS[i][1]; }
    }
    return kind || '?';
  }
  function clockOf(ts) {
    if (!ts) { return ''; }
    var d = new Date(ts);
    if (isNaN(d.getTime())) { return ''; }
    return d.toTimeString().slice(0, 8);
  }
  function where(ev) {
    var extra = ev.extra || {};
    switch (ev.kind) {
      case 'tool_call': return ev.tool || '';
      case 'llm_call': return ev.prompt_id || '';
      case 'stage': return ev.stage || '';
      case 'routing': return '第 ' + extra.page + ' 页';
      case 'assertion': return ev.tool || '';
      case 'error': return ev.tool || '';
      case 'file_access': return ev.path || '';
      default: return '';
    }
  }
  function detail(ev) {
    var extra = ev.extra || {};
    switch (ev.kind) {
      case 'tool_call':
        return ev.error ? ev.error
                        : '入参指纹 ' +
                          String(ev.args_hash || '').slice(0, 12);
      case 'llm_call':
        return (ev.role || '') + ' · ' + (ev.model || '') +
               ' · v' + (ev.prompt_version || '?');
      case 'stage': return extra.detail || extra.status || '';
      case 'routing': return (extra.route || '') + ' · ' + (extra.reason || '');
      case 'assertion': return (extra.name || '') +
               (extra.detail ? ' · ' + extra.detail : '');
      case 'error': return ev.error || '';
      case 'file_access': return ev.mode || '';
      default: return '';
    }
  }
  function metrics(ev) {
    var bits = [];
    if (ev.kind === 'llm_call') {
      bits.push(((ev.prompt_tokens || 0) + (ev.completion_tokens || 0)) + ' tok');
      if (ev.cache_hit) { bits.push('缓存'); }
      if (ev.cost_cny) { bits.push('¥' + Number(ev.cost_cny).toFixed(4)); }
    }
    if (ev.latency_ms) { bits.push(ev.latency_ms + 'ms'); }
    return bits.join(' · ');
  }
  function render(ev, tpl, opts) {
    opts = opts || {};
    var node = tpl.content.cloneNode(true);
    var put = function (f, txt) {
      var el = node.querySelector('[data-f="' + f + '"]');
      if (el) { el.textContent = txt == null ? '' : String(txt); }
    };
    var mark = markOf(ev);
    var mkEl = node.querySelector('[data-f="mark"]');
    if (mkEl) { mkEl.className = markClass(mark); }
    put('mark', mark);
    put('time', clockOf(ev.ts));
    put('kind', kindLabel(ev.kind));
    put('detail', detail(ev));
    put('metrics', metrics(ev));
    var whEl = node.querySelector('[data-f="where"]');
    var land = (opts.landing || {})[ev.tool];
    if (whEl && land) {
      var a = document.createElement('a');
      a.href = land + (opts.rid ? '?run=' + encodeURIComponent(opts.rid) : '');
      a.textContent = where(ev);
      whEl.appendChild(a);
    } else {
      put('where', where(ev));
    }
    return node;
  }
  function drawChips(box, on, counts, onToggle) {
    box.textContent = '';
    KINDS.forEach(function (k) {
      var b = document.createElement('button');
      b.type = 'button';
      b.className = 'chip' + (on[k[0]] ? ' on' : '') +
                    (counts && counts[k[0]] === 0 ? ' zero' : '');
      b.textContent = k[1] + ' ' + (counts ? (counts[k[0]] || 0) : '–');
      b.addEventListener('click', function () { onToggle(k[0]); });
      box.appendChild(b);
    });
  }
  global.Trail = {
    KINDS: KINDS,
    defaultOn: defaultOn,
    activeKinds: activeKinds,
    markOf: markOf,
    markClass: markClass,
    kindLabel: kindLabel,
    clockOf: clockOf,
    where: where,
    detail: detail,
    metrics: metrics,
    render: render,
    drawChips: drawChips
  };
}(window));
