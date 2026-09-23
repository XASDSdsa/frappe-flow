const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../flow/public/flow_assets/flow_panel_guard.js'), 'utf8');
function harness() {
  const listeners = {}, requests = [], imageLoads = [];
  let completeRequest;
  class Element {
    constructor(tag) { this.tagName = tag.toUpperCase(); this.children = []; this.handlers = {}; }
    appendChild(child) { child.parent = this; this.children.push(child); }
    setAttribute(k, v) { this[k] = v; }
    addEventListener(k, fn) { this.handlers[k] = fn; }
    get isConnected() { return this === document.body || this === document.head || !!this.parent?.isConnected; }
    set src(v) { this._src = v; imageLoads.push(v); }
    get src() { return this._src; }
    remove() { this.parent.children = this.parent.children.filter(c => c !== this); this.parent = null; }
  }
  const document = {
    readyState: 'complete', documentElement: {},
    createElement: tag => new Element(tag),
    addEventListener: (name, fn) => { listeners[name] = fn; },
    getElementById: id => [...document.body.children, ...document.head.children].find(c => c.id === id),
  };
  document.head = new Element('head'); document.body = new Element('body');
  const window = { location: new URL('https://erp.example.com/app'), fetch: (...args) => {
    requests.push(args); return new Promise(resolve => { completeRequest = resolve; });
  } };
  vm.runInNewContext(source, { window, document, URL, MutationObserver: class { observe() {} } });
  return { document, listeners, requests, imageLoads,
    click() { const img = {src: 'https://erp.example.com/private/files/chat-preview-abc123def4.jpg', alt: 'flowimg:sample'};
      listeners.click({target: {closest: () => img}, preventDefault() {}, stopPropagation() {}}); },
    respond(ok=true) { completeRequest({ok, json: async () => ({message: {url: '/private/files/original.png'}})}); },
    overlay() { return document.getElementById('flow-image-preview'); },
  };
}
const flush = () => new Promise(resolve => setImmediate(resolve));
(async () => {
  const normal = harness();
  assert.equal(normal.requests.length, 0); assert.equal(normal.imageLoads.length, 0);
  normal.click();
  assert.equal(normal.requests.length, 1); assert.equal(normal.imageLoads.length, 0);
  assert.match(normal.requests[0][0], /get_chat_original\?file=/);
  normal.respond(); await flush();
  assert.deepEqual(normal.imageLoads, ['https://erp.example.com/private/files/original.png']);
  const img = normal.overlay().children.find(c => c.tagName === 'IMG');
  img.handlers.load(); assert.equal(img.hidden, false);
  assert.equal(normal.overlay().children[0].hidden, true);
  normal.listeners.keydown({key: 'Escape'}); assert.equal(normal.overlay(), undefined);
  const closed = harness(); closed.click(); closed.listeners.keydown({key: 'Escape'});
  closed.respond(); await flush(); assert.equal(closed.imageLoads.length, 0);
  const denied = harness(); denied.click(); denied.respond(false); await flush();
  assert.equal(denied.imageLoads.length, 0);
  assert.match(denied.overlay().children[0].textContent, /读取失败/);
  const unsupported = harness(); unsupported.click(); unsupported.respond(); await flush();
  unsupported.overlay().children.find(c => c.tagName === 'IMG').handlers.error();
  assert.match(unsupported.overlay().children[0].textContent, /打开原图/);
  assert.equal(unsupported.overlay().children.find(c => c.tagName === 'A').href, 'https://erp.example.com/private/files/original.png');
  console.log('PASS: no eager request; click loads original; close cancels pending display; denied/missing image is explicit; unsupported image retains original link.');
})().catch(error => { console.error(error); process.exitCode = 1; });
