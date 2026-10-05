(() => {
	if (window.__flowMobileViewport20260916) return;
	window.__flowMobileViewport20260916 = true;
	const viewport = window.visualViewport;
	let frame = 0;
	let recoveryTimer = 0;
	const update = (recoveringKeyboard = false) => {
		frame = 0;
		// Leave native pinch zoom alone; a zoomed viewport is not a keyboard resize.
		if (viewport && Math.abs(viewport.scale - 1) > 0.01) return;
		// Keep top and height in the same visual viewport. Capping it to the
		// layout height can leave an uncovered strip when browser chrome changes.
		const visualHeight = viewport ? viewport.height : window.innerHeight;
		// During iOS keyboard dismissal Safari can briefly report height=0 and
		// omit the final resize event. At the bounded recovery point, use the
		// full layout height so the stale keyboard-sized root cannot remain.
		const height = recoveringKeyboard && viewport
			? Math.max(visualHeight, window.innerHeight)
			: visualHeight;
		if (!Number.isFinite(height) || height <= 0) return;
		const top = viewport ? Math.max(0, viewport.offsetTop) : 0;
		const style = document.documentElement.style;
		style.setProperty("--flow-mobile-viewport-top", `${top}px`);
		style.setProperty("--flow-mobile-viewport-height", `${height}px`);
	};
	const schedule = () => {
		if (!frame) frame = requestAnimationFrame(update);
	};
	const recoverAfterKeyboard = (event) => {
		const target = event?.target;
		if (!target?.matches?.("textarea, input, [contenteditable='true']")) {
			schedule();
			return;
		}
		schedule();
		if (recoveryTimer) clearTimeout(recoveryTimer);
		// Safari can finish the keyboard animation after the focusout frame and
		// omit a final usable visualViewport resize event.
		recoveryTimer = setTimeout(() => {
			recoveryTimer = 0;
			update(true);
		}, 400);
	};
	viewport?.addEventListener("resize", schedule);
	viewport?.addEventListener("scroll", schedule);
	window.addEventListener("resize", schedule);
	window.addEventListener("orientationchange", schedule);
	window.addEventListener("pageshow", schedule);
	document.addEventListener("focusin", schedule);
	document.addEventListener("focusout", recoverAfterKeyboard);
	update();
})();
