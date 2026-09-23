const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');
const corePath = path.join(__dirname, '../flow/public/flow_assets/flow_voice_core.js');
const workletPath = path.join(__dirname, '../flow/public/flow_assets/flow_voice_audio_worklet.js');
function loadCommonJS(file) {
  const module = { exports: {} };
  vm.runInNewContext(fs.readFileSync(file, 'utf8'), { module, exports: module.exports });
  return module.exports;
}
const { Transcript } = loadCommonJS(corePath);
const { Pcm16Resampler } = loadCommonJS(workletPath);

function samplesFrom(buffers) {
  return buffers.flatMap(buffer => {
    const view = new DataView(buffer);
    return Array.from({ length: buffer.byteLength / 2 }, (_, index) => view.getInt16(index * 2, true));
  });
}

function resample(rate, input, blockSize = 128) {
  const buffers = [], levels = [];
  const resampler = new Pcm16Resampler(rate, (buffer, level) => { buffers.push(buffer); levels.push(level); });
  for (let offset = 0; offset < input.length; offset += blockSize) resampler.push(input.subarray(offset, offset + blockSize));
  resampler.finish();
  return { buffers, levels, samples: samplesFrom(buffers), resampler };
}

test('V2 replaces hypotheses, protects final sentences, and sorts IDs numerically', () => {
  const transcript = new Transcript();
  transcript.add({ sentences: { sentence_id: 1, sentence_type: 0, sentence: '你好' } });
  assert.equal(transcript.text, '你好');
  transcript.add({ sentences: { sentence_id: 1, sentence_type: 0, sentence: '你好世界' } });
  assert.equal(transcript.text, '你好世界');
  transcript.add({ sentences: { sentence_id: 1, sentence_type: 1, sentence: '你好，世界。' } });
  transcript.add({ sentences: { sentence_id: 1, sentence_type: 0, sentence: '迟到的草稿' } });
  transcript.add({ sentences: [
    { sentence_id: 10, sentence_type: 1, sentence: '第三句。' },
    { sentence_id: 2, sentence_type: 1, sentence: '第二句。' },
    { sentence_id: 1, sentence_type: 1, sentence: '你好，世界。' },
  ] });
  assert.equal(transcript.text, '你好，世界。第二句。第三句。');
});

test('transcripts support wrapped results, the text variant, empty replacements, and Latin spacing', () => {
  const transcript = new Transcript();
  transcript.add({ result: { sentences: [
    { sentence_id: '0', sentence_type: '1', text: 'Hello.' },
    { sentence_id: '1', sentence_type: 0, sentence: 'world' },
  ] } });
  assert.equal(transcript.text, 'Hello. world');
  transcript.add({ sentences: { sentence_id: 1, sentence_type: 1, sentence: '' } });
  assert.equal(transcript.text, 'Hello.');
  for (const invalid of [null, 'text', {}, { sentences: [null, {}, { sentence_id: 3, sentence: {} }] }]) transcript.add(invalid);
  assert.equal(transcript.text, 'Hello.');
  transcript.clear();
  assert.equal(transcript.text, '');
});

test('iFlytek wpgs results append and replace interim ranges', () => {
  const transcript = new Transcript();
  const result = (sn, text, pgs, rg, status = 1) => transcript.add({ data: {
    status, result: { sn, pgs, rg, ws: [{ cw: [{ w: text }] }] },
  } });
  result(1, '山东绿');
  result(2, '山东绿方', 'apd');
  assert.equal(transcript.text, '山东绿山东绿方');
  result(3, '山东绿方120颗', 'rpl', [1, 2]);
  assert.equal(transcript.text, '山东绿方120颗');
  result(3, '山东绿方120颗。', 'apd', undefined, 2);
  assert.equal(transcript.text, '山东绿方120颗。');
});

for (const rate of [44100, 48000]) {
  test(`${rate} Hz capture produces exactly 16,000 samples per second in 40 ms packets`, () => {
    const result = resample(rate, new Float32Array(rate).fill(0.5));
    assert.equal(result.samples.length, 16000);
    assert.deepEqual(result.buffers.map(buffer => buffer.byteLength), new Array(25).fill(1280));
    assert.ok(result.samples.every(sample => sample === 16384));
    assert.ok(result.levels.every(level => Math.abs(level - 0.5) < 1e-12));
  });

  test(`${rate} Hz phase and sample order are independent of render block boundaries`, () => {
    const input = Float32Array.from({ length: rate * 3 + 37 }, (_, index) =>
      0.65 * Math.sin(index * 2 * Math.PI * 987 / rate) + 0.15 * Math.sin(index * 2 * Math.PI * 97 / rate));
    const blocked = resample(rate, input);
    const together = resample(rate, input, input.length);
    const oddBlocks = resample(rate, input, 73);
    assert.deepEqual(blocked.samples, together.samples);
    assert.deepEqual(blocked.samples, oddBlocks.samples);
    assert.equal(blocked.samples.length, Math.ceil(input.length * 16000 / rate));
    const tail = blocked.buffers.at(-1);
    assert.ok(tail.byteLength > 0 && tail.byteLength < 1280);
  });
}

test('resampling averages before decimation and encodes signed little-endian PCM safely', () => {
  const filtered = resample(48000, Float32Array.from([1, -1, 0, 0.3, 0.6, 0.9]));
  assert.deepEqual(filtered.samples, [0, 19660]);
  const clipped = resample(16000, Float32Array.from([-2, 2, NaN, Infinity, -Infinity]));
  assert.deepEqual(clipped.samples, [-32768, 32767, 0, 0, 0]);
  assert.deepEqual(Array.from(new Uint8Array(clipped.buffers[0]).slice(0, 4)), [0, 128, 255, 127]);
  const count = clipped.buffers.length;
  clipped.resampler.finish();
  clipped.resampler.push(Float32Array.of(1));
  assert.equal(clipped.buffers.length, count);
});

function worklet(rate = 48000) {
  let Processor;
  const messages = [];
  vm.runInNewContext(fs.readFileSync(workletPath, 'utf8'), {
    sampleRate: rate,
    AudioWorkletProcessor: class {
      constructor() { this.port = { postMessage: (message, transfers = []) => messages.push({ message, transfers }) }; }
    },
    registerProcessor(name, implementation) {
      assert.equal(name, 'flow-pcm');
      Processor = implementation;
    },
  });
  const processor = new Processor();
  return {
    processor, messages,
    command(type) { processor.port.onmessage({ data: { type } }); },
    process(input) {
      const output = new Float32Array(128).fill(1);
      const alive = processor.process(input ? [[input]] : [], [[output]]);
      assert.ok(output.every(sample => sample === 0), 'microphone audio must never reach the speakers');
      return alive;
    },
  };
}

test('worklet gates capture until start and flushes residual audio before its stop acknowledgement', () => {
  const harness = worklet();
  const input = new Float32Array(128).fill(-0.5);
  assert.equal(harness.process(input), true);
  assert.equal(harness.messages.length, 0);
  harness.command('start');
  assert.equal(harness.process(), true);
  harness.process(input);
  assert.equal(harness.messages.length, 0, 'a partial packet should wait for more input or stop');
  harness.command('stop');
  assert.deepEqual(harness.messages.map(entry => entry.message.type), ['audio', 'flushed']);
  const audio = harness.messages[0];
  assert.equal(audio.message.buffer.byteLength, Math.ceil(128 / 3) * 2);
  assert.ok(samplesFrom([audio.message.buffer]).every(sample => sample === -16384));
  assert.equal(audio.transfers[0], audio.message.buffer);
  assert.equal(harness.process(input), false);
  harness.command('start');
  harness.command('stop');
  assert.equal(harness.messages.length, 2);
});

test('worklet stop before capture acknowledges without generating silent audio', () => {
  const harness = worklet(44100);
  harness.process();
  harness.command('stop');
  assert.deepEqual(harness.messages.map(entry => entry.message.type), ['flushed']);
});
