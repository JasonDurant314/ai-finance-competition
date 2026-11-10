(function (global) {
  'use strict';

  var KINDS = [['llm', '模型'], ['tool', '工具'], ['stage', '阶段']];
  var COUNT = { llm: 'llm_calls', tool: 'tool_calls', stage: 'stages' };

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) { n.className = cls; }
    if (text != null) { n.textContent = String(text); }
    return n;
  }

  function nfmt(n) {
    if (n == null) { return '—'; }
    return String(n).replace(/\B(?=(\d{3})+(?!\d))/g, ',');
  }

  function bytes(n) {
    var s = String(n);
    return s.length > 4 ? (s.length / 1000).toFixed(1) + 'k' : s;
  }

  function pretty(t) {
    if (t == null) { return ''; }
    if (typeof t !== 'string') {
      try { return JSON.stringify(t, null, 2); } catch (e) { return String(t); }
    }
    var s = t.trim();
    if (s.length < 200000 && (s.charAt(0) === '{' || s.charAt(0) === '[')) {
      try { return JSON.stringify(JSON.parse(s), null, 2); } catch (e) { /* 原样 */ }
    }
    return t;
  }

  function isImageMarker(s) {
    return typeof s === 'string' && s.indexOf('<图片已省略') === 0;
  }

  function truncation(rec) {
    var box = el('div', 'cutnm small dim');
    var cut = rec.cut || {};
    var keys = Object.keys(cut);
    for (var i = 0; i < keys.length; i++) {
      var m = cut[keys[i]];
      var line = keys[i] + '：';
      if (m.kind === 'image') {
        line += '整页图未记入（' + nfmt(m.total) + ' 字符 base64）';
      } else {
        line += '保留 ' + nfmt(m.kept) + ' / 共 ' + nfmt(m.total) + ' 字符';
      }
      box.appendChild(el('div', null, line));
    }
    var om = rec.omitted || {};
    var oks = Object.keys(om);
    for (var j = 0; j < oks.length; j++) {
      box.appendChild(el('div', null,
        oks[j] + '：载荷过大未记入（' + nfmt(om[oks[j]].chars) + ' 字符）'));
    }
    return box.childNodes.length ? box : null;
  }

  function partNode(content) {
    if (typeof content === 'string') {
      if (isImageMarker(content)) { return el('div', 'imgph dim small', content); }
      return el('pre', 'act-pre', content);
    }
    if (Object.prototype.toString.call(content) === '[object Array]') {
      var box = document.createDocumentFragment();
      for (var i = 0; i < content.length; i++) {
        var pt = content[i] || {};
        var url = pt.image_url;
        if (url && typeof url === 'object') { url = url.url; }
        if (typeof url === 'string') {
          box.appendChild(el('div', 'imgph dim small',
            isImageMarker(url) ? url : '<图片>'));
        } else if (pt.text != null) {
          box.appendChild(el('pre', 'act-pre', pt.text));
        } else {
          box.appendChild(el('pre', 'act-pre', JSON.stringify(pt)));
        }
      }
      return box;
    }
    return el('pre', 'act-pre', JSON.stringify(content, null, 2));
  }

  function header(rec, badgeCls, badge) {
    var h = el('div', 'ah');
    h.appendChild(el('span', 'ab ' + badgeCls, badge));
    h.appendChild(el('span', 'who mono', rec.tool || rec.role || '—'));
    if (rec.model) { h.appendChild(el('span', 'mdl mono dim', rec.model)); }
    if (rec.stage) { h.appendChild(el('span', 'stg dim', rec.stage)); }
    h.appendChild(el('span', 'grow'));
    if (rec.src === 'cache') { h.appendChild(el('span', 'mk mk-o', '·')); }
    else if (rec.ok === false) { h.appendChild(el('span', 'mk mk-x', '×')); }
    else { h.appendChild(el('span', 'mk mk-v', '√')); }
    return h;
  }

  function metaLine(rec) {
    var bits = [];
    if (rec.prompt_tokens != null || rec.completion_tokens != null) {
      bits.push(nfmt(rec.prompt_tokens || 0) + '+' + nfmt(rec.completion_tokens || 0) + ' tok');
    }
    if (rec.cost_cny) { bits.push('¥' + Number(rec.cost_cny).toFixed(4)); }
    if (rec.latency_ms != null) { bits.push(nfmt(rec.latency_ms) + ' ms'); }
    if (rec.cache_hit) { bits.push('缓存命中'); }
    return bits.length ? el('div', 'meta small dim', bits.join(' · ')) : null;
  }

  function fold(summary, body, open) {
    var d = el('details', 'fold2');
    if (open) { d.open = true; }
    d.appendChild(el('summary', null, summary));
    var inner = el('div', 'inner');
    inner.appendChild(body);
    d.appendChild(inner);
    return d;
  }

  function rowLLM(rec) {
    var li = el('li', 'act act-llm');
    li.appendChild(header(rec, 'ab-llm', '模型'));
    var m = metaLine(rec);
    if (m) { li.appendChild(m); }

    if (rec.messages && rec.messages.length) {
      var box = document.createDocumentFragment();
      for (var i = 0; i < rec.messages.length; i++) {
        var msg = rec.messages[i] || {};
        var who = { system: '系统', user: '用户', assistant: '助手' }[msg.role] || msg.role;
        var wrap = el('div', 'msg');
        wrap.appendChild(el('div', 'mrole small dim', who));
        wrap.appendChild(partNode(msg.content));
        box.appendChild(wrap);
      }
      var chars = JSON.stringify(rec.messages).length;
      li.appendChild(fold('提示词 · ' + rec.messages.length + ' 条 · ' + bytes(chars) + ' 字符', box, false));
    } else if (rec.src === 'cache') {
      li.appendChild(el('div', 'dim small', '该 run 早于对话记录器：没有留下提示词'));
    } else {
      li.appendChild(el('div', 'dim small', '这条没有记下提示词'));
    }

    if (rec.text) {
      li.appendChild(fold('回答 · ' + bytes(rec.text.length) + ' 字符',
        partNode(pretty(rec.text)), true));
    }
    if (rec.error) { li.appendChild(el('div', 'errline small', rec.error)); }
    return li;
  }

  function rowTool(rec) {
    var li = el('li', 'act act-tool');
    li.appendChild(header(rec, 'ab-tool', '工具'));
    var m = metaLine(rec);
    if (m) { li.appendChild(m); }

    if (rec.args != null) {
      li.appendChild(fold('入参', partNode(pretty(rec.args)), true));
    }
    if (rec.result != null) {
      li.appendChild(fold('返回', partNode(pretty(rec.result)), false));
    } else if (rec.ok !== false) {
      li.appendChild(el('div', 'dim small', '该调用未记返回值（不经工具注册表的调用）'));
    }
    if (rec.error) { li.appendChild(el('div', 'errline small', rec.error)); }
    return li;
  }

  function rowStage(rec) {
    var li = el('li', 'act act-stage');
    var h = el('div', 'ah');
    h.appendChild(el('span', 'ab ab-stage', '阶段'));
    h.appendChild(el('span', 'who mono', rec.stage || '—'));
    li.appendChild(h);
    return li;
  }

  function render(rec) {
    var li = rec.kind === 'stage' ? rowStage(rec)
           : rec.kind === 'tool' ? rowTool(rec) : rowLLM(rec);
    var cut = truncation(rec);
    if (cut) { li.appendChild(cut); }
    return li;
  }

  function drawChips(box, on, stats, onToggle) {
    box.textContent = '';
    for (var i = 0; i < KINDS.length; i++) {
      var k = KINDS[i][0];
      var n = (stats || {})[COUNT[k]];
      var b = el('button', 'chip' + (on[k] ? ' on' : ''),
        KINDS[i][1] + (n == null ? '' : ' ' + nfmt(n)));
      b.type = 'button';
      b.setAttribute('data-kind', k);
      b.onclick = (function (kind) {
        return function () { onToggle(kind); };
      }(k));
      box.appendChild(b);
    }
  }

  global.Activity = {
    KINDS: KINDS,
    render: render,
    drawChips: drawChips,
    defaultOn: function () { var on = {}; KINDS.forEach(function (k) { on[k[0]] = true; }); return on; },
    activeKinds: function (on) {
      return KINDS.filter(function (k) { return on[k[0]]; }).map(function (k) { return k[0]; });
    }
  };
}(window));
