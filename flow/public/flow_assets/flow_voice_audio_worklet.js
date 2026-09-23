(() => {
	"use strict";

	const OUTPUT_RATE = 16000;
	// iFlytek recommends 40 ms / 1280-byte PCM frames for streaming ASR.
	const CHUNK_SAMPLES = 640;

	class Pcm16Resampler {
		constructor(inputRate, emit) {
			if (!Number.isFinite(inputRate) || inputRate <= 0) throw new Error("Invalid audio sample rate");
			this.inputRate = inputRate;
			this.emit = emit;
			this.remaining = inputRate;
			this.area = 0;
			this.finished = false;
			this.resetChunk();
		}

		resetChunk() {
			this.buffer = new ArrayBuffer(CHUNK_SAMPLES * 2);
			this.view = new DataView(this.buffer);
			this.count = 0;
			this.squares = 0;
		}

		push(input) {
			if (this.finished || !input) return;
			for (let i = 0; i < input.length; i++) {
				const value = Number.isFinite(input[i]) ? Math.max(-1, Math.min(1, input[i])) : 0;
				// Integrate the input over each output sample's time window. This
				// box filter reduces aliasing; carrying the area and fractional
				// window across render blocks avoids 44.1 kHz rounding drift.
				// Rate-sized units keep the phase exact for integer sample rates.
				let available = OUTPUT_RATE;
				while (available > 0) {
					const weight = Math.min(available, this.remaining);
					this.area += value * weight;
					this.remaining -= weight;
					available -= weight;
					if (this.remaining <= 0) {
						this.writeSample(this.area / this.inputRate);
						this.remaining = this.inputRate;
						this.area = 0;
					}
				}
			}
		}

		writeSample(value) {
			const sample = Math.max(-1, Math.min(1, value));
			const integer = Math.round(sample * (sample < 0 ? 32768 : 32767));
			this.view.setInt16(this.count * 2, integer, true);
			this.count++;
			this.squares += sample * sample;
			if (this.count === CHUNK_SAMPLES) this.emitChunk();
		}

		emitChunk() {
			if (!this.count) return;
			const buffer = this.count === CHUNK_SAMPLES ? this.buffer : this.buffer.slice(0, this.count * 2);
			const level = Math.sqrt(this.squares / this.count);
			// Replace the working buffer before emit transfers its ownership.
			this.resetChunk();
			this.emit(buffer, level);
		}

		finish() {
			if (this.finished) return;
			this.finished = true;
			const covered = this.inputRate - this.remaining;
			// Preserve a final fraction of an output sample when capture ends
			// between sample boundaries, at most 1/16000 second of padding.
			if (covered > 0) this.writeSample(this.area / covered);
			this.emitChunk();
		}
	}

	if (typeof module !== "undefined" && module.exports) module.exports = { Pcm16Resampler };
	if (typeof AudioWorkletProcessor === "undefined" || typeof registerProcessor !== "function") return;

	class FlowPcmProcessor extends AudioWorkletProcessor {
		constructor() {
			super();
			this.active = false;
			this.stopped = false;
			this.resampler = new Pcm16Resampler(sampleRate, (buffer, level) => {
				this.port.postMessage({ type: "audio", buffer, level }, [buffer]);
			});
			this.port.onmessage = ({ data }) => {
				if (this.stopped) return;
				if (data?.type === "start") this.active = true;
				if (data?.type === "stop") {
					this.active = false;
					this.stopped = true;
					this.resampler.finish();
					this.port.postMessage({ type: "flushed" });
				}
			};
		}

		process(inputs, outputs) {
			// The node is connected to the destination to keep capture running;
			// never send microphone audio to the user's speakers.
			for (const output of outputs) for (const channel of output) channel.fill(0);
			if (this.active && !this.stopped) this.resampler.push(inputs[0]?.[0]);
			return !this.stopped;
		}
	}

	registerProcessor("flow-pcm", FlowPcmProcessor);
})();
