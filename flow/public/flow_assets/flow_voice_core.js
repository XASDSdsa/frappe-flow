(() => {
	"use strict";

	function joinSentences(sentences) {
		return sentences.reduce((text, sentence) => {
			const part = sentence.trim();
			if (!part) return text;
			// Chinese sentences already carry their own punctuation. Latin words
			// at a sentence boundary need a space, including after punctuation.
			const space = /[a-z0-9][.!?,;:)"']*$/i.test(text) && /^[a-z0-9]/i.test(part);
			return text + (space ? " " : "") + part;
		}, "");
	}

	class Transcript {
		constructor() {
			this.sentences = new Map();
		}

		add(message) {
			if (!message || typeof message !== "object") return this.text;
			const rtasr = this._addRtasr(message);
			if (rtasr) return this.text;
			const iflytek = message.data && typeof message.data === "object" ? message.data : null;
			const iflytekResult = iflytek?.result;
			if (iflytekResult && Array.isArray(iflytekResult.ws)) {
				if (iflytekResult.pgs === "rpl" && Array.isArray(iflytekResult.rg)) {
					const start = Number(iflytekResult.rg[0]);
					const end = Number(iflytekResult.rg[1]);
					if (Number.isInteger(start) && Number.isInteger(end) && end >= start) {
						for (let index = start; index <= end; index++) this.sentences.delete(String(index));
					}
				}
				const text = iflytekResult.ws.map((word) => (word?.cw || [])
					.map((candidate) => candidate?.w || "").find(Boolean) || "").join("");
				this.update(iflytekResult.sn, text, Number(iflytek?.status) === 2 || iflytekResult.ls === true);
				return this.text;
			}
			const result = message.result && typeof message.result === "object" ? message.result : message;
			const values = message.sentences ?? result.sentences;
			if (values != null) {
				for (const value of Array.isArray(values) ? values : [values]) {
					if (!value || typeof value !== "object") continue;
					this.update(value.sentence_id, value.sentence ?? value.text, Number(value.sentence_type) === 1);
				}
			}
			return this.text;
		}

		_addRtasr(message) {
			if (message.action !== "result" || Number(message.code || 0) !== 0) return false;
			let data = message.data;
			if (typeof data === "string") {
				try { data = JSON.parse(data); } catch (_) { return true; }
			}
			const state = data?.cn?.st;
			if (!state || !Array.isArray(state.rt)) return true;
			const text = state.rt.flatMap((segment) => segment?.ws || [])
				.flatMap((word) => word?.cw || [])
				.map((candidate) => candidate?.w || "")
				.join("");
			const id = data.seg_id ?? data.segId ?? data.segment_id;
			this.update(id, text, Number(state.type) === 0);
			return true;
		}

		update(id, text, final) {
			if ((typeof id !== "string" && typeof id !== "number") || typeof text !== "string") return;
			const key = String(id);
			const previous = this.sentences.get(key);
			if (previous?.final && !final) return;
			this.sentences.set(key, { text, final });
		}

		get text() {
			const ordered = Array.from(this.sentences.entries()).sort(([left], [right]) => {
				const a = Number(left), b = Number(right);
				if (Number.isFinite(a) && Number.isFinite(b) && a !== b) return a - b;
				return left < right ? -1 : left > right ? 1 : 0;
			});
			return joinSentences(ordered.map(([, value]) => value.text));
		}

		clear() {
			this.sentences.clear();
		}
	}

	const api = { Transcript };
	if (typeof window !== "undefined") window.LeyaFlowVoiceCore = api;
	if (typeof module !== "undefined" && module.exports) module.exports = api;
})();
