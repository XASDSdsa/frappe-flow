(() => {
	if (window.__flowVoice20260923) return;
	window.__flowVoice20260923 = true;
	const API = "/api/method/flow.flow_voice.";
	const WORKLET = "/assets/flow/flow_assets/flow_voice_audio_worklet.js";
	let config, configAt = 0, configRequest, controller, root, frame;
	const notice = (message, indicator = "orange") => window.frappe?.show_alert?.({ message, indicator }, 7);
	const RTASR_URL = "wss://rtasr.xfyun.cn/v1/ws?";
	const RTASR_END = Uint8Array.from([123, 34, 101, 110, 100, 34, 58, 116, 114, 117, 101, 125]);
	const settings = () => {
		window.frappe?.flow?.panel?.hide();
		window.frappe?.set_route?.("Form", "Flow Voice Settings", "Flow Voice Settings");
	};

	async function request(method, post = false, signal) {
		const abort = new AbortController();
		const cancel = () => abort.abort();
		if (signal?.aborted) cancel();
		else signal?.addEventListener("abort", cancel, { once: true });
		const timer = setTimeout(cancel, 10000);
		try {
			const response = await fetch(API + method, {
				method: post ? "POST" : "GET", credentials: "same-origin", signal: abort.signal,
				headers: post ? { "Content-Type": "application/json", "X-Frappe-CSRF-Token": window.frappe?.csrf_token || "" } : {},
				...(post ? { body: "{}" } : {}),
			});
			if (!response.ok) {
				throw new Error(response.status === 429 ? "语音使用过于频繁，请稍后再试。"
					: response.status === 403 ? "当前账号没有语音输入权限，请联系管理员。"
						: "语音服务未就绪，请管理员检查科大讯飞语音设置。");
			}
			return (await response.json()).message;
		} finally {
			clearTimeout(timer);
			signal?.removeEventListener("abort", cancel);
		}
	}
	function loadConfig(force = false) {
		if (!force && config && Date.now() - configAt < 30000) return Promise.resolve(config);
		if (!configRequest) configRequest = request("get_config").then((value) => {
			config = value; configAt = Date.now(); return value;
		}).finally(() => { configRequest = null; });
		return configRequest;
	}
	function explainSetup(value) {
		const reason = value?.reason;
		const message = reason === "forbidden" ? "当前账号没有语音输入权限，请联系管理员。"
			: reason === "disabled" ? "语音输入尚未启用，请管理员在“Flow 语音设置”中完成配置并启用。"
				: "语音输入尚未配置科大讯飞账号，请管理员先完成“Flow 语音设置”。";
		if (value?.can_configure) {
			window.frappe?.msgprint?.({ title: "语音输入待配置", message,
				primary_action: { label: "打开语音设置", action: settings } });
		} else notice(message);
	}
	const errors = {
		10105: "科大讯飞实时转写鉴权失败，请检查实时转写 AppID 和 APIKey。",
		10106: "科大讯飞实时转写参数无效，请联系管理员检查配置。",
		10107: "科大讯飞实时转写参数值非法，请联系管理员检查配置。",
		10110: "科大讯飞实时转写服务未开通或额度已用完，请检查控制台。",
		10700: "科大讯飞实时转写引擎异常，请稍后重试。",
		10202: "科大讯飞实时转写连接超时，请检查网络后重试。",
		10204: "科大讯飞实时转写连接异常，请稍后重试。",
		10205: "科大讯飞实时转写请求过于频繁，请稍后重试。",
		10800: "科大讯飞实时转写并发数已满，请稍后重试。",
		16003: "科大讯飞实时转写基础服务异常，请稍后重试。",
		37005: "科大讯飞没有收到有效音频，请重新录音。",
	};

	class VoiceInput {
		constructor(composer) {
			this.composer = composer;
			this.input = composer.querySelector("textarea");
			this.events = new AbortController();
			this.session = null;
			this.button = document.createElement("button");
			this.button.type = "button";
			this.button.className = "flow-voice-button";
			this.button.title = "点击开始语音输入，再次点击停止";
			this.button.setAttribute("aria-label", "开启语音输入");
			this.button.setAttribute("aria-pressed", "false");
			this.button.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true"><rect x="9" y="2" width="6" height="12" rx="3"/><path d="M5 10v2a7 7 0 0 0 14 0v-2M12 19v3M8 22h8"/></svg>';
			const attachment = composer.querySelector('input[type="file"]');
			attachment.parentNode.insertBefore(this.button, attachment);
			this.status = document.createElement("div");
			this.status.className = "flow-voice-status";
			this.status.hidden = true;
			this.status.innerHTML = '<span class="flow-voice-dot" aria-hidden="true"></span><span class="flow-voice-message" role="status" aria-live="polite"></span><button type="button" class="flow-voice-cancel">取消</button>';
			composer.insertBefore(this.status, this.input);
			this.label = this.status.querySelector(".flow-voice-message");
			this.cancelButton = this.status.querySelector("button");
			const listen = (target, event, fn, options = {}) => target.addEventListener(event, fn, { ...options, signal: this.events.signal });
			listen(this.button, "click", (event) => {
				event.preventDefault();
				// Match WeChat: click once to activate, click again to stop.
				if (this.session) {
					if (["recording", "connecting"].includes(this.session.phase)) this.finish();
					else if (["preparing", "finishing"].includes(this.session.phase)) this.cancel();
				} else this.begin({});
			});
			listen(this.cancelButton, "click", () => {
				// Cancellation is immediate and keeps whatever has already been
				// recognized in the composer.  Gray the control while cleanup runs.
				this.cancelButton.disabled = true;
				this.cancelButton.classList.add("is-cancelled");
				this.cancel();
			});
			listen(composer, "click", (event) => {
				if (this.session && !this.button.contains(event.target) && !this.cancelButton.contains(event.target)) {
					event.preventDefault(); event.stopImmediatePropagation();
				}
			}, { capture: true });
			listen(composer, "keydown", (event) => {
				if (!this.session) return;
				if (event.key === "Escape") { event.preventDefault(); this.cancel(); }
				if (event.key === "Enter" && event.target !== this.button && event.target !== this.cancelButton) {
					event.preventDefault(); event.stopImmediatePropagation();
				}
			}, { capture: true });
			listen(document, "visibilitychange", () => { if (document.hidden) this.cancel(); });
			listen(window, "pagehide", () => this.cancel());
			this.inputObserver = new MutationObserver(() => {
				this.button.disabled = this.input.disabled;
				if (this.input.disabled) this.cancel();
			});
			this.inputObserver.observe(this.input, { attributes: true, attributeFilter: ["disabled"] });
			this.button.disabled = this.input.disabled;
			loadConfig().catch(() => {});
		}
		showPhase() {
			const s = this.session;
			if (!s) return;
			this.label.textContent = s.phase === "recording"
				? "正在录音，再次点击麦克风结束 · Esc 取消"
				: s.phase === "finishing" ? "正在确认最后的文字…再次点击麦克风可取消" : "正在准备麦克风，请稍候…";
		}
		async begin({ pointer = null, y = 0 }) {
			if (this.input.disabled || this.input.readOnly || this.session) return;
			if (!config?.enabled) {
				try {
					const value = await loadConfig(true);
					if (!value?.enabled) explainSetup(value);
					else notice("语音已准备好，请再次点击麦克风开始。", "blue");
				} catch (_) { notice("暂时无法检查语音配置，请确认网络正常后重试。"); }
				return;
			}
			const AudioContext = window.AudioContext || window.webkitAudioContext;
			if (!window.isSecureContext || !navigator.mediaDevices?.getUserMedia || !AudioContext || !window.AudioWorkletNode) {
				notice("当前浏览器不支持网页语音输入，请使用最新版 Safari 或 Chrome 打开 HTTPS 页面。"); return;
			}
			const s = this.session = { pointer, pointerY: y, phase: "preparing", cancelArmed: false, audioStarted: false,
				before: this.input.value, written: this.input.value,
				start: this.input.selectionStart ?? this.input.value.length,
				end: this.input.selectionEnd ?? this.input.value.length,
				readOnly: this.input.readOnly, abort: new AbortController(),
				transcript: new window.LeyaFlowVoiceCore.Transcript(), timers: [], ended: false, endSent: false };
			this.input.readOnly = true;
			this.status.hidden = false;
			this.cancelButton.disabled = false;
			this.cancelButton.classList.remove("is-cancelled");
			this.composer.classList.add("flow-voice-busy");
			this.button.setAttribute("aria-pressed", "true");
			this.button.setAttribute("aria-label", "关闭语音输入");
			this.showPhase();
			try {
				// Resume in the original user gesture; Safari otherwise suspends capture.
				s.context = new AudioContext();
				const resume = s.context.resume();
				const media = navigator.mediaDevices.getUserMedia({ audio: {
					channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true,
				} }).then((stream) => {
					if (this.session !== s) { stream.getTracks().forEach((track) => track.stop()); return; }
					s.stream = stream;
				});
				s.timers.push(setTimeout(() => this.fail(s, "麦克风准备超时，请检查浏览器权限后重试。"), 30000));
				const [auth] = await Promise.all([request("create_session", true, s.abort.signal), media,
					resume, s.context.audioWorklet.addModule(WORKLET)]);
				if (this.session !== s) return;
				if (!auth?.url?.startsWith(RTASR_URL)) throw new Error("语音服务返回了无效的实时转写连接地址。");
				s.source = s.context.createMediaStreamSource(s.stream);
				s.node = new AudioWorkletNode(s.context, "flow-pcm", {
					numberOfInputs: 1, numberOfOutputs: 1, outputChannelCount: [1],
					channelCount: 1, channelCountMode: "explicit",
				});
				s.gain = s.context.createGain(); s.gain.gain.value = 0;
				s.source.connect(s.node); s.node.connect(s.gain); s.gain.connect(s.context.destination);
				s.node.onprocessorerror = () => this.fail(s, "麦克风音频处理失败，请重新录音。");
				s.context.onstatechange = () => {
					if (s.phase === "recording" && s.context.state !== "running") this.fail(s, "录音被系统中断，已停止麦克风。");
				};
				s.node.port.onmessage = ({ data }) => {
					if (this.session !== s) return;
					if (data.type === "audio" && ["recording", "finishing"].includes(s.phase)) {
						if (s.socket.readyState !== WebSocket.OPEN || s.socket.bufferedAmount > 64000) {
							this.fail(s, "网络传输过慢，语音已停止。请检查已识别文字后重试。"); return;
						}
						s.socket.send(data.buffer);
						s.audioStarted = true;
					} else if (data.type === "flushed" && s.phase === "finishing" && !s.ended) {
						s.ended = true;
						s.endSent = true;
						if (s.socket.readyState === WebSocket.OPEN) s.socket.send(RTASR_END);
					}
				};
				s.phase = "connecting";
				s.socket = new WebSocket(auth.url);
				s.socket.onopen = () => {
					if (this.session !== s) return;
					s.timers.forEach(clearTimeout); s.timers = [];
					s.phase = "recording";
					s.node.port.postMessage({ type: "start" });
					s.timers.push(setTimeout(() => { if (this.session === s) this.finish(); }, Math.max(1, Number(auth.max_seconds) || 60) * 1000));
					this.showPhase();
				};
				s.socket.onmessage = ({ data }) => {
					if (this.session !== s) return;
					let message;
					try { message = JSON.parse(data); } catch (_) { this.fail(s, "语音服务返回了无法识别的结果，请重新录音。"); return; }
					if (message.action === "started") return;
					if (Number(message.code || 0) !== 0) {
						this.fail(s, errors[message.code] || `语音识别服务暂时不可用（${Number(message.code)}），请稍后重试。`); return;
					}
					const previousText = s.transcript.text;
					const text = s.transcript.add(message);
					if (text !== previousText && !this.write(s, text)) return;
				};
				s.socket.onerror = () => this.fail(s, "无法连接科大讯飞语音服务，请检查网络或联系管理员。");
				s.socket.onclose = () => {
					if (this.session !== s) return;
					if (s.endSent || s.phase === "finishing") {
						const text = s.transcript.text;
						this.cleanup(s);
						notice(text ? "语音已转成文字，请检查后发送。" : "没有识别到语音，请靠近麦克风再试一次。", text ? "green" : "orange");
					} else this.fail(s, "语音连接已中断，请检查已识别文字后重试。");
				};
			} catch (error) {
				if (this.session !== s) return;
				const text = error.name === "NotAllowedError" ? "未获得麦克风权限，请在浏览器网站设置中允许麦克风。"
					: error.name === "NotFoundError" ? "没有找到可用麦克风，请检查设备。"
						: error.name === "NotReadableError" ? "麦克风被其他应用占用，请释放后重试。"
							: error.name === "AbortError" ? "语音服务连接超时，请检查网络后重试。"
								: "语音输入未能启动，请检查配置、网络和麦克风后重试。";
				this.fail(s, /讯飞|语音|当前账号/.test(error.message || "") ? error.message : text);
			}
		}
		write(s, text) {
			if (this.input.value !== s.written) { this.fail(s, "输入内容已发生变化，语音已停止，保留你当前的文字。"); return false; }
			s.written = text ? s.before.slice(0, s.start) + text + s.before.slice(s.end) : s.before;
			this.input.value = s.written;
			this.input.dispatchEvent(new Event("input", { bubbles: true }));
			return true;
		}
		finish() {
			const s = this.session;
			if (!s || s.phase === "finishing") return;
			if (s.phase !== "recording") {
				this.cancel(); return;
			}
			s.phase = "finishing";
			s.timers.forEach(clearTimeout); s.timers = [];
			this.showPhase();
			s.node.port.postMessage({ type: "stop" });
			s.stream.getTracks().forEach((track) => track.stop());
			s.timers.push(setTimeout(() => this.fail(s, "最后一段识别超时，已识别文字已保留，请检查后发送。"), 8000));
		}
		cancel() {
			const s = this.session;
			if (!s) return;
			this.cleanup(s);
		}
		fail(s, message) {
			if (this.session !== s) return;
			this.cleanup(s);
			notice(message);
		}
		cleanup(s) {
			if (this.session !== s) return;
			this.session = null;
			s.phase = "closed";
			s.timers.forEach(clearTimeout);
			s.abort.abort();
			s.stream?.getTracks().forEach((track) => track.stop());
			if (s.socket) { s.socket.onclose = s.socket.onerror = s.socket.onmessage = null; s.socket.close(); }
			s.source?.disconnect(); s.node?.disconnect(); s.gain?.disconnect();
			if (s.node) { s.node.port.onmessage = null; s.node.port.close(); }
			if (s.context) { s.context.onstatechange = null; s.context.close().catch(() => {}); }
			this.input.readOnly = s.readOnly;
			this.status.hidden = true;
			this.status.classList.remove("is-cancelling");
			this.composer.classList.remove("flow-voice-busy");
			this.button.setAttribute("aria-pressed", "false");
			this.button.setAttribute("aria-label", "开启语音输入");
		}
		destroy() {
			this.cancel(); this.events.abort(); this.inputObserver.disconnect();
			this.button.remove(); this.status.remove();
		}
	}

	function schedule() {
		if (frame) return;
		frame = requestAnimationFrame(() => {
			frame = 0;
			const composer = document.querySelector("#flow-root .flow-composer");
			if (controller?.composer !== composer) {
				controller?.destroy(); controller = null;
				if (composer?.querySelector("textarea") && composer.querySelector('input[type="file"]')) controller = new VoiceInput(composer);
			}
			if (window.frappe?.flow?.panel?.visible === false) controller?.cancel();
		});
	}
	function bindRoot() {
		const next = document.getElementById("flow-root");
		if (root === next) return;
		observer.disconnect();
		root?.removeEventListener("click", onHeaderClick, true);
		root = next;
		if (root) {
			observer.observe(root, { childList: true, subtree: true, attributes: true, attributeFilter: ["style"] });
			root.addEventListener("click", onHeaderClick, true);
		}
		schedule();
	}
	function onHeaderClick(event) { if (event.target.closest("header button")) controller?.cancel(); }
	const observer = new MutationObserver((records) => {
		if (!controller?.composer.isConnected || records.some((record) => record.target === root ||
			[...record.addedNodes].some((node) => node.nodeType === 1 && (node.matches(".flow-composer") || node.querySelector(".flow-composer"))))) schedule();
	});
	function start() {
		new MutationObserver(bindRoot).observe(document.body, { childList: true });
		bindRoot();
	}
	if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start, { once: true });
	else start();
})();
