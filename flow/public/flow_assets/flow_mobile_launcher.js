(() => {
	if (window.__flowMobileLauncher20260923) return;
	window.__flowMobileLauncher20260923 = true;

	const start = () => {
		const mobile = window.matchMedia(
			"(max-width: 768px), (max-width: 1024px) and (pointer: coarse)"
		);
		const storageKey = "leya.flow.mobile-launcher-position.v1";
		let position = { side: "right", y: 0.7 };
		try {
			const saved = JSON.parse(localStorage.getItem(storageKey));
			if (["left", "right"].includes(saved?.side) && Number.isFinite(saved.y)) {
				position = { side: saved.side, y: Math.max(0, Math.min(1, saved.y)) };
			}
		} catch (_) {
			// The launcher also works when browser storage is unavailable.
		}

		const style = document.createElement("style");
		style.textContent = `
			#flow-mobile-launcher {
				--flow-safe-top: env(safe-area-inset-top, 0px);
				--flow-safe-bottom: env(safe-area-inset-bottom, 0px);
				--flow-safe-left: env(safe-area-inset-left, 0px);
				--flow-safe-right: env(safe-area-inset-right, 0px);
				position: fixed; z-index: 1039; width: 56px; height: 56px;
				padding: 0; margin: 0; border: 1px solid rgba(255,255,255,.25);
				border-radius: 50%; background: #171717; color: #fff;
				display: flex; flex-direction: column; align-items: center; justify-content: center;
				box-shadow: 0 3px 12px rgba(0,0,0,.22); line-height: 1;
				font: 600 11px/1.1 -apple-system, BlinkMacSystemFont, sans-serif;
				touch-action: none; user-select: none; -webkit-user-select: none;
				-webkit-tap-highlight-color: transparent; cursor: pointer;
			}
			#flow-mobile-launcher[hidden] { display: none !important; }
			#flow-mobile-launcher.is-docking { transition: left 220ms cubic-bezier(.22, 1, .36, 1); }
			@media (prefers-reduced-motion: reduce) {
				#flow-mobile-launcher.is-docking { transition: none; }
			}
			#flow-mobile-launcher:focus-visible { outline: 3px solid #2490ef; outline-offset: 3px; }
			#flow-mobile-launcher svg { width: 22px; height: 22px; margin-bottom: 2px; pointer-events: none; }
			#flow-mobile-launcher span { pointer-events: none; }
		`;
		document.head.appendChild(style);
		const button = document.createElement("button");
		button.id = "flow-mobile-launcher";
		button.type = "button";
		button.hidden = true;
		button.setAttribute("aria-label", "打开 Flow 聊天");
		button.title = "打开 Flow 聊天；拖动可贴靠左侧或右侧";
		button.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true"><path fill="currentColor" d="M12 1l2.8 8.2L23 12l-8.2 2.8L12 23l-2.8-8.2L1 12l8.2-2.8z"/></svg><span>Flow</span>';
		document.body.appendChild(button);

		let root;
		let frame = 0;
		let drag = null;
		let suppressClickUntil = 0;
		const viewport = window.visualViewport;
		const panel = () => window.frappe?.flow?.panel;
		const bounds = () => {
			const css = getComputedStyle(button);
			const safe = (edge) => parseFloat(css.getPropertyValue(`--flow-safe-${edge}`)) || 0;
			const top = viewport?.offsetTop || 0;
			const left = viewport?.offsetLeft || 0;
			const height = viewport ? Math.min(viewport.height, window.innerHeight) : window.innerHeight;
			const width = viewport ? Math.min(viewport.width, window.innerWidth) : window.innerWidth;
			const minY = top + safe("top") + 12;
			const minX = left + safe("left") + 12;
			return {
				minY, maxY: Math.max(minY, top + height - safe("bottom") - 68),
				minX, maxX: Math.max(minX, left + width - safe("right") - 68),
			};
		};
		const place = () => {
			const b = bounds();
			button.style.left = `${position.side === "left" ? b.minX : b.maxX}px`;
			button.style.top = `${b.minY + position.y * (b.maxY - b.minY)}px`;
		};
		const sync = () => {
			frame = 0;
			const nextRoot = document.getElementById("flow-root");
			if (root !== nextRoot) {
				rootObserver.disconnect();
				root = nextRoot;
				if (root) rootObserver.observe(root, { attributes: true, attributeFilter: ["style"] });
			}
			button.hidden = !mobile.matches || !root || !panel() || panel().visible;
			if (button.hidden) drag = null;
			else if (!drag) place();
		};
		const schedule = () => {
			if (!frame) frame = requestAnimationFrame(sync);
		};
		const rootObserver = new MutationObserver(schedule);
		new MutationObserver((records) => {
			if (records.some((record) => [...record.addedNodes, ...record.removedNodes]
				.some((node) => node.id === "flow-root"))) schedule();
		}).observe(document.body, { childList: true });

		button.addEventListener("pointerdown", (event) => {
			if (!event.isPrimary || event.button !== 0) return;
			const rect = button.getBoundingClientRect();
			// Pick up an in-flight docking animation at its visible position.
			button.classList.remove("is-docking");
			button.style.left = `${rect.left}px`;
			button.style.top = `${rect.top}px`;
			drag = { id: event.pointerId, x: event.clientX, y: event.clientY,
				offsetX: event.clientX - rect.left, offsetY: event.clientY - rect.top,
				left: rect.left, moved: false };
			button.setPointerCapture(event.pointerId);
		});
		button.addEventListener("pointermove", (event) => {
			if (!drag || drag.id !== event.pointerId) return;
			if (Math.hypot(event.clientX - drag.x, event.clientY - drag.y) > 6) drag.moved = true;
			if (!drag.moved) return;
			const b = bounds();
			// Follow the finger freely; choose an edge only when the drag ends.
			drag.left = Math.max(b.minX, Math.min(b.maxX, event.clientX - drag.offsetX));
			position.y = Math.max(0, Math.min(1, (event.clientY - drag.offsetY - b.minY) / (b.maxY - b.minY || 1)));
			button.style.left = `${drag.left}px`;
			button.style.top = `${b.minY + position.y * (b.maxY - b.minY)}px`;
		});
		const endDrag = (event) => {
			if (!drag || drag.id !== event.pointerId) return;
			if (drag.moved || event.type === "pointercancel") {
				suppressClickUntil = Date.now() + 500;
				const b = bounds();
				position.side = drag.left < (b.minX + b.maxX) / 2 ? "left" : "right";
				button.classList.add("is-docking");
				place();
				try { localStorage.setItem(storageKey, JSON.stringify(position)); } catch (_) { /* optional */ }
			}
			drag = null;
		};
		button.addEventListener("pointerup", endDrag);
		button.addEventListener("pointercancel", endDrag);
		button.addEventListener("lostpointercapture", endDrag);
		button.addEventListener("transitionend", () => button.classList.remove("is-docking"));
		button.addEventListener("click", (event) => {
			if (Date.now() < suppressClickUntil || button.hidden) {
				event.preventDefault();
				return;
			}
			panel()?.show();
			sync();
		});
		mobile.addEventListener("change", schedule);
		viewport?.addEventListener("resize", schedule);
		viewport?.addEventListener("scroll", schedule);
		window.addEventListener("resize", schedule);
		window.addEventListener("pageshow", schedule);
		window.addEventListener("orientationchange", schedule);
		window.jQuery?.(document).on("app_ready.flow_mobile_launcher", schedule);
		schedule();
	};
	if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start, { once: true });
	else start();
})();
