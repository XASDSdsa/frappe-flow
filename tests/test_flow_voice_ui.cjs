const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const test = require('node:test');
const corePath = path.join(__dirname, '../flow/public/flow_assets/flow_voice_core.js');
function loadCommonJS(file) {
  const module = { exports: {} };
  vm.runInNewContext(fs.readFileSync(file, 'utf8'), { module, exports: module.exports });
  return module.exports;
}
const { Transcript } = loadCommonJS(corePath);
const source = fs.readFileSync(path.join(__dirname, '../flow/public/flow_assets/flow_voice.js'), 'utf8');
const settle = () => new Promise(resolve => setImmediate(resolve));
function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

class UIEvent {
  constructor(type, properties = {}) { Object.assign(this, { type, bubbles: false, defaultPrevented: false }, properties); }
  preventDefault() { this.defaultPrevented = true; }
  stopImmediatePropagation() { this.stopped = true; }
}
class Target {
  constructor() { this.handlers = new Map(); }
  addEventListener(type, listener, options = {}) {
    const listeners = this.handlers.get(type) || [];
    listeners.push({ listener, capture: options === true || options.capture, signal: options.signal });
    this.handlers.set(type, listeners);
  }
  removeEventListener(type, listener) {
    this.handlers.set(type, (this.handlers.get(type) || []).filter(entry => entry.listener !== listener));
  }
  invoke(event, capture) {
    for (const entry of this.handlers.get(event.type) || []) {
      if (!!entry.capture !== capture || entry.signal?.aborted || event.stopped) continue;
      entry.listener(event);
    }
  }
  dispatchEvent(event) {
    event.target ||= this;
    const ancestors = [];
    for (let parent = this.parentNode; parent; parent = parent.parentNode) ancestors.push(parent);
    for (const ancestor of ancestors.toReversed()) ancestor.invoke(event, true);
    this.invoke(event, true);
    this.invoke(event, false);
    if (event.bubbles) for (const ancestor of ancestors) ancestor.invoke(event, false);
    return !event.defaultPrevented;
  }
}
class Element extends Target {
  constructor(tag) {
    super(); this.tagName = tag; this.children = []; this.attributes = {}; this.classes = new Set();
    this.disabled = this.readOnly = false; this.hidden = false; this.nodeType = 1;
    this.classList = {
      add: (...names) => names.forEach(name => this.classes.add(name)),
      remove: (...names) => names.forEach(name => this.classes.delete(name)),
      toggle: (name, active) => active ? this.classes.add(name) : this.classes.delete(name),
      contains: name => this.classes.has(name),
    };
  }
  set className(value) { this.classes = new Set(value.split(/\s+/)); }
  set innerHTML(value) {
    if (value.includes('flow-voice-message')) {
      const label = new Element('span'); label.className = 'flow-voice-message'; this.appendChild(label);
      const button = new Element('button'); button.className = 'flow-voice-cancel'; this.appendChild(button);
    }
  }
  setAttribute(name, value) { this.attributes[name] = value; }
  appendChild(child) { this.insertBefore(child, null); }
  insertBefore(child, next) {
    child.parentNode = this;
    const at = next ? this.children.indexOf(next) : this.children.length;
    this.children.splice(at, 0, child);
  }
  remove() { this.parentNode.children = this.parentNode.children.filter(child => child !== this); this.parentNode = null; }
  contains(target) { return target === this || this.children.some(child => child.contains(target)); }
  get isConnected() { return this.tagName === 'body' || !!this.parentNode?.isConnected; }
  matches(selector) {
    if (selector.startsWith('.')) return this.classes.has(selector.slice(1));
    if (selector === 'input[type="file"]') return this.tagName === 'input' && this.type === 'file';
    return this.tagName === selector;
  }
  querySelector(selector) {
    for (const child of this.children) {
      if (child.matches(selector)) return child;
      const found = child.querySelector(selector); if (found) return found;
    }
    return null;
  }
  closest(selector) {
    if (selector === 'header button') return this.tagName === 'button' && this.parentNode?.tagName === 'header' ? this : null;
    return this.matches(selector) ? this : this.parentNode?.closest(selector);
  }
  setPointerCapture() {}
}

async function harness(options = {}) {
  const requests = [], notices = [], dialogs = [], contexts = [], nodes = [], sockets = [], timers = new Map(), observers = [];
  let timerId = 0, mediaCalls = 0, submitted = 0;
  const document = new Target();
  document.body = new Element('body'); document.readyState = 'complete'; document.hidden = false;
  document.createElement = tag => new Element(tag);
  const root = new Element('div'); document.body.appendChild(root);
  const composer = new Element('div'); composer.className = 'flow-composer'; root.appendChild(composer);
  const input = new Element('textarea'); input.value = options.draft ?? '原草稿';
  input.selectionStart = options.start ?? input.value.length; input.selectionEnd = options.end ?? input.selectionStart;
  composer.appendChild(input);
  const attachment = new Element('input'); attachment.type = 'file'; composer.appendChild(attachment);
  const send = new Element('button'); composer.appendChild(send);
  send.addEventListener('click', () => { submitted++; });
  composer.addEventListener('keydown', event => { if (event.key === 'Enter') submitted++; });
  document.getElementById = id => id === 'flow-root' ? root : null;
  document.querySelector = selector => selector === '#flow-root .flow-composer' ? root.querySelector('.flow-composer') : null;
  const track = { stops: 0, stop() { this.stops++; } };
  const stream = { getTracks: () => [track] };
  class Connection {
    connect() {} disconnect() { this.disconnected = true; }
  }
  class AudioContext {
    constructor() {
      this.state = 'running'; this.destination = {}; contexts.push(this);
      this.audioWorklet = { addModule: () => Promise.resolve() };
    }
    resume() { return Promise.resolve(); }
    close() { this.closed = true; this.state = 'closed'; return Promise.resolve(); }
    createMediaStreamSource() { return this.source = new Connection(); }
    createGain() { this.gain = new Connection(); this.gain.gain = { value: 1 }; return this.gain; }
  }
  class AudioWorkletNode extends Connection {
    constructor() {
      super(); nodes.push(this); this.commands = [];
      this.port = { postMessage: message => this.commands.push(message.type), close: () => { this.port.closed = true; } };
    }
    emit(data) { this.port.onmessage?.({ data }); }
  }
  class WebSocket {
    static OPEN = 1;
    constructor(url) { this.url = url; this.readyState = 0; this.bufferedAmount = 0; this.sent = []; sockets.push(this); }
    send(value) { this.sent.push(value); }
    close() { this.closed = true; this.readyState = 3; }
    open() { this.readyState = 1; this.onopen?.(); }
    emit(value) { this.readyState = 1; this.onmessage?.({ data: JSON.stringify(value) }); }
  }
  const window = new Target();
  Object.assign(window, {
    isSecureContext: true, AudioContext, AudioWorkletNode, LeyaFlowVoiceCore: { Transcript },
    frappe: { csrf_token: 'test-csrf', flow: { panel: { visible: true } },
      show_alert: value => notices.push(value), msgprint: value => dialogs.push(value), set_route() {} },
  });
  vm.runInNewContext(source, {
    window, document, Event: UIEvent, AudioWorkletNode, WebSocket, AbortController,
    navigator: { mediaDevices: { getUserMedia() { mediaCalls++; return options.media?.promise || Promise.resolve(stream); } } },
    fetch(url, request) {
      requests.push({ url, ...request });
      const value = url.endsWith('get_config') ? (options.config || { enabled: true })
        : (options.auth?.promise || { url: 'wss://rtasr.xfyun.cn/v1/ws?appid=IFLYTEKAPP&ts=1&signa=test&lang=cn', app_id: 'IFLYTEKAPP', max_seconds: 60 });
      return Promise.resolve(value).then(message => ({ ok: true, json: async () => ({ message }) }));
    },
    MutationObserver: class {
      constructor(callback) { this.callback = callback; observers.push(this); }
      observe(target) { this.target = target; } disconnect() { this.target = null; }
    },
    setTimeout(callback, delay) { const id = ++timerId; timers.set(id, { callback, delay }); return id; },
    clearTimeout(id) { timers.delete(id); },
    requestAnimationFrame(callback) { Promise.resolve().then(callback); return ++timerId; },
    btoa: value => Buffer.from(value, 'binary').toString('base64'), Buffer, console,
  });
  await settle();
  const button = composer.querySelector('.flow-voice-button');
  const status = composer.querySelector('.flow-voice-status');
  const click = (target, detail = 0) => target.dispatchEvent(new UIEvent('click', { bubbles: true, detail }));
  const api = { window, document, root, composer, input, button, status, track, stream, requests, notices, dialogs,
    contexts, nodes, sockets, timers, observers, send, click,
    get mediaCalls() { return mediaCalls; }, get submitted() { return submitted; },
    cancel() { click(status.querySelector('button')); },
    async start() { click(button); await settle(); },
    async record() { await api.start(); sockets.at(-1).open(); await settle(); },
    // RTASR wraps the transcript as a JSON string in data. type=1 is interim;
    // type=0 is final for a segment. Keep a stable segment id so interim text
    // replaces itself before the final result arrives.
    result(text, type = 1, segId = 0) {
      sockets.at(-1).emit({ action: 'result', code: 0,
        data: JSON.stringify({ seg_id: segId, cn: { st: { type, rt: [{ ws: [{ cw: [{ w: text }] }] }] } } }) });
    },
    key(key) { input.dispatchEvent(new UIEvent('keydown', { bubbles: true, key })); },
  };
  return api;
}

function assertReleased(h) {
  assert.equal(h.input.readOnly, false);
  assert.equal(h.status.hidden, true);
  assert.equal(h.contexts.at(-1).closed, true);
  if (h.sockets.length) assert.equal(h.sockets.at(-1).closed, true);
  if (h.nodes.length) {
    assert.equal(h.nodes.at(-1).disconnected, true);
    assert.equal(h.nodes.at(-1).port.closed, true);
  }
}

test('interim text replaces the original selection; cancel keeps the recognized draft', async () => {
  const h = await harness({ draft: '前缀选中后缀', start: 2, end: 4 });
  await h.record();
	h.result('你好'); assert.equal(h.input.value, '前缀你好后缀');
	h.result('你好世界'); assert.equal(h.input.value, '前缀你好世界后缀');
	h.cancel();
	assert.equal(h.input.value, '前缀你好世界后缀');
  assert.ok(h.track.stops > 0); assertReleased(h);
  assert.equal(h.submitted, 0);
});

test('socket open starts capture and permits the first audio frame without a server message', async () => {
  const h = await harness(); await h.start();
  h.sockets[0].open();
  assert.deepEqual(h.nodes[0].commands, ['start']);
  h.nodes[0].emit({ type: 'audio', buffer: new ArrayBuffer(20) });
  assert.equal(h.sockets[0].sent.length, 1);
  assert.equal(new Uint8Array(h.sockets[0].sent[0]).byteLength, 20);
  h.cancel(); assertReleased(h);
});

test('finish flushes audio before end; final text stays in the draft without sending', async () => {
  const h = await harness(); await h.record(); h.result('识别中');
  h.click(h.send); h.key('Enter'); assert.equal(h.submitted, 0);
  h.click(h.button);
  assert.deepEqual(h.nodes[0].commands, ['start', 'stop']);
  assert.ok(h.track.stops > 0);
  assert.equal(h.sockets[0].sent.length, 0);
  const audio = new ArrayBuffer(20); h.nodes[0].emit({ type: 'audio', buffer: audio });
  h.nodes[0].emit({ type: 'flushed' });
  assert.equal(new Uint8Array(h.sockets[0].sent[0]).byteLength, 20);
  assert.equal(new TextDecoder().decode(h.sockets[0].sent[1]), '{"end":true}');
  h.result('识别完成。', 0);
  h.sockets[0].onclose();
  assert.equal(h.input.value, '原草稿识别完成。');
  assertReleased(h); assert.equal(h.submitted, 0);
});

test('cancel ignores old socket messages and stops a microphone granted after cancellation', async () => {
  const h = await harness(); await h.record();
  const oldMessage = h.sockets[0].onmessage;
  h.cancel();
  oldMessage({ data: JSON.stringify({ code: 0, sentences: { sentence_id: 0, sentence_type: 1, sentence: '迟到内容' } }) });
  assert.equal(h.input.value, '原草稿');
  const media = deferred(), pending = await harness({ media });
  await pending.start(); pending.cancel(); assertReleased(pending);
  media.resolve(pending.stream); await settle();
  assert.equal(pending.track.stops, 1); assert.equal(pending.sockets.length, 0);
  assert.equal(pending.input.value, '原草稿');
});

test('cancel aborts signing and ignores a late signing response', async () => {
  const auth = deferred(), h = await harness({ auth });
  await h.start(); h.cancel();
  const signing = h.requests.find(request => request.url.endsWith('create_session'));
  assert.equal(signing.method, 'POST'); assert.equal(signing.headers['X-Frappe-CSRF-Token'], 'test-csrf');
  assert.equal(signing.signal.aborted, true); assertReleased(h);
  auth.resolve({ url: 'wss://rtasr.xfyun.cn/v1/ws?appid=IFLYTEKAPP&ts=1&signa=late&lang=cn', app_id: 'IFLYTEKAPP' }); await settle();
  assert.equal(h.sockets.length, 0); assert.ok(h.track.stops > 0);
});

test('microphone permission denial closes the context and restores an editable draft', async () => {
  const media = deferred(), h = await harness({ media });
  await h.start(); media.reject(Object.assign(new Error('permission denied'), { name: 'NotAllowedError' }));
  await settle(); assertReleased(h);
  assert.equal(h.input.value, '原草稿');
  assert.match(h.notices.at(-1).message, /麦克风权限/);
  assert.equal(h.sockets.length, 0);
});

test('Escape, page hiding, and document hiding cancel and release recording resources', async () => {
  for (const cancel of [h => h.key('Escape'), h => h.window.dispatchEvent(new UIEvent('pagehide')),
    h => { h.document.hidden = true; h.document.dispatchEvent(new UIEvent('visibilitychange')); }]) {
    const h = await harness(); await h.record(); h.result('临时文本'); cancel(h);
    assert.equal(h.input.value, '原草稿临时文本'); assertReleased(h); assert.ok(h.track.stops > 0);
  }
});

test('click toggles continuous recording; second click stops, and a pending start can be cancelled', async () => {
  const h = await harness();
  await h.record(); h.result('持续识别');
  h.click(h.button);
  assert.deepEqual(h.nodes[0].commands, ['start', 'stop']);
  assert.equal(h.input.value, '原草稿持续识别');
  assert.equal(h.submitted, 0);
  const media = deferred(), pending = await harness({ media });
  await pending.start(); pending.click(pending.button);
  assertReleased(pending);
  media.resolve(pending.stream); await settle();
  assert.equal(pending.track.stops, 1); assert.equal(pending.sockets.length, 0);
});

test('second microphone click while finalizing cancels immediately without timeout notice', async () => {
  const h = await harness(); await h.record(); h.result('识别中');
  h.click(h.button);
  assert.equal(h.status.hidden, false);
  assert.ok([...h.timers.values()].some(entry => entry.delay === 8000));
  h.click(h.button);
  assertReleased(h);
	assert.equal(h.input.value, '原草稿识别中');
  assert.equal(h.notices.some(entry => /最后一段识别超时/.test(entry.message)), false);
});

test('cancel button stops immediately, turns gray, and keeps recognized text', async () => {
  const h = await harness(); await h.record(); h.result('已识别');
  h.cancel();
  assert.equal(h.input.value, '原草稿已识别');
  assert.equal(h.status.hidden, true);
  assert.equal(h.button.attributes['aria-pressed'], 'false');
  assertReleased(h);
});

test('external draft changes are preserved and stop recognition', async () => {
  const h = await harness(); await h.record(); h.result('识别中');
  h.input.value = '用户新的草稿'; h.result('迟到更新');
  assert.equal(h.input.value, '用户新的草稿'); assertReleased(h);
  assert.match(h.notices.at(-1).message, /内容已发生变化/);
});

test('unconfigured speech keeps its button visible and explains setup without asking for microphone permission', async () => {
  const h = await harness({ config: { enabled: false, reason: 'disabled', can_configure: true } });
  assert.ok(h.button); assert.equal(h.button.disabled, false);
  await h.start();
  assert.equal(h.mediaCalls, 0); assert.equal(h.contexts.length, 0);
  assert.equal(h.dialogs.length, 1); assert.match(h.dialogs[0].message, /尚未启用/);
  assert.equal(h.dialogs[0].primary_action.label, '打开语音设置');
});

test('slow transport and finalization timeout release resources and retain recognized text', async () => {
  const slow = await harness(); await slow.record(); slow.result('已识别');
  slow.sockets[0].bufferedAmount = 64001;
  slow.nodes[0].emit({ type: 'audio', buffer: new ArrayBuffer(20) });
  assert.equal(slow.input.value, '原草稿已识别'); assertReleased(slow);
  assert.match(slow.notices.at(-1).message, /网络传输过慢/);
  const timeout = await harness(); await timeout.record(); timeout.result('已识别'); timeout.click(timeout.button);
  const timer = [...timeout.timers.values()].find(entry => entry.delay === 8000); assert.ok(timer); timer.callback();
  assert.equal(timeout.input.value, '原草稿已识别'); assertReleased(timeout);
  assert.equal(timeout.submitted, 0);
});
