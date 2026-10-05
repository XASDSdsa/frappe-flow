const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../flow/public/flow_assets/flow_mobile_viewport.js'), 'utf8');
function harness(withViewport = true) {
  const listeners = {}, values = {}, frames = [];
  const target = prefix => ({addEventListener: (name, fn) => {listeners[prefix + name] = fn;}});
  const viewport = Object.assign(target('v:'), {height: 700, offsetTop: 0, scale: 1});
  const window = Object.assign(target('w:'), {innerHeight: 850, visualViewport: withViewport ? viewport : undefined});
  const style = {setProperty: (k, v) => {values[k] = v;}, removeProperty: k => {delete values[k];}};
  const document = Object.assign(target('d:'), {activeElement: null, documentElement: {style}});
  const context = vm.createContext({window, document, requestAnimationFrame: fn => (frames.push(fn), frames.length), Math, Number});
  vm.runInContext(source, context);
  const fire = (key, event = {}) => {listeners[key](event); while(frames.length) frames.shift()(123.4);};
  const textarea = {matches: selector => selector.includes('textarea')};
  const focus = () => {document.activeElement = textarea; fire('d:focusin', {target: textarea});};
  const blur = () => {document.activeElement = null; fire('d:focusout', {target: textarea});};
  return {viewport, window, values, fire, focus, blur, context};
}
const h = harness();
// Without a focused field the CSS 100dvh fallback owns the panel size.
assert.equal(h.values['--flow-mobile-viewport-height'], undefined);
h.viewport.height = 390; h.viewport.offsetTop = 90; h.fire('v:resize');
assert.equal(h.values['--flow-mobile-viewport-height'], undefined);
// The keyboard reduces the panel to the visible area above it.
h.focus();
h.viewport.height = 550; h.viewport.offsetTop = 60; h.fire('v:resize');
assert.equal(h.values['--flow-mobile-viewport-height'], '550px');
assert.equal(h.values['--flow-mobile-viewport-top'], '60px');
// iOS may still report the keyboard-sized height after focusout; it must not stick.
h.blur();
assert.equal(h.values['--flow-mobile-viewport-height'], undefined);
assert.equal(h.values['--flow-mobile-viewport-top'], undefined);
h.fire('v:resize');
assert.equal(h.values['--flow-mobile-viewport-height'], undefined);
// iPad hides the keyboard but keeps focus; its shortcut bar must not shrink the panel.
h.focus();
h.viewport.height = 450; h.viewport.offsetTop = 0; h.fire('v:resize');
assert.equal(h.values['--flow-mobile-viewport-height'], '450px');
h.viewport.height = 780; h.fire('v:resize');
assert.equal(h.values['--flow-mobile-viewport-height'], undefined);
// A tablet's visible area larger than the layout viewport is not a keyboard.
h.window.innerHeight = 910; h.viewport.height = 1000; h.fire('v:resize');
assert.equal(h.values['--flow-mobile-viewport-height'], undefined);
// Pinch zoom and a transient zero height keep the last keyboard geometry.
h.viewport.height = 500; h.fire('v:resize');
assert.equal(h.values['--flow-mobile-viewport-height'], '500px');
h.viewport.scale = 2; h.viewport.height = 215; h.fire('v:resize');
assert.equal(h.values['--flow-mobile-viewport-height'], '500px');
h.viewport.scale = 1; h.viewport.height = 0; h.fire('v:resize');
assert.equal(h.values['--flow-mobile-viewport-height'], '500px');
h.blur();
assert.equal(h.values['--flow-mobile-viewport-height'], undefined);
const fallback = harness(false);
assert.equal(fallback.values['--flow-mobile-viewport-height'], '850px');
fallback.window.innerHeight = 600; fallback.fire('w:resize');
assert.equal(fallback.values['--flow-mobile-viewport-height'], '600px');
vm.runInContext(source, fallback.context);
console.log('Mobile viewport: keyboard sizing, dismissal reset, iPad hidden keyboard, tablet, zoom, fallback and duplicate loading passed.');
