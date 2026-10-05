(() => {
	if (window.__flowMobileViewport20260916) return;
	window.__flowMobileViewport20260916 = true;
	const viewport = window.visualViewport;
	const EDITABLE = "textarea, input, [contenteditable='true']";
	// An on-screen keyboard covers far more than browser chrome or the iPad
	// shortcut bar that stays visible after the keyboard is hidden with focus kept.
	const KEYBOARD_MIN_HEIGHT = 120;
	let frame = 0;
	const update = () => {
		frame = 0;
		const style = document.documentElement.style;
		const useFullHeight = () => {
			style.removeProperty("--flow-mobile-viewport-top");
			style.removeProperty("--flow-mobile-viewport-height");
		};
		// Only a focused field can raise the on-screen keyboard. Without one, the
		// CSS 100dvh fallback tracks browser chrome natively; a visual height read
		// while the keyboard is still animating closed would leave a blank strip.
		if (viewport && !document.activeElement?.matches?.(EDITABLE)) return useFullHeight();
		// Leave native pinch zoom alone; a zoomed viewport is not a keyboard resize.
		if (viewport && Math.abs(viewport.scale - 1) > 0.01) return;
		const height = viewport ? viewport.height : window.innerHeight;
		if (!Number.isFinite(height) || height <= 0) return;
		// iPad's hide-keyboard key keeps the field focused, so focus alone does not
		// mean the keyboard is still covering the page.
		const layoutHeight = Math.max(window.innerHeight || 0, document.documentElement.clientHeight || 0);
		if (viewport && layoutHeight - height < KEYBOARD_MIN_HEIGHT) return useFullHeight();
		const top = viewport ? Math.max(0, viewport.offsetTop) : 0;
		style.setProperty("--flow-mobile-viewport-top", `${top}px`);
		style.setProperty("--flow-mobile-viewport-height", `${height}px`);
	};
	const schedule = () => {
		if (!frame) frame = requestAnimationFrame(() => update());
	};
	viewport?.addEventListener("resize", schedule);
	viewport?.addEventListener("scroll", schedule);
	window.addEventListener("resize", schedule);
	window.addEventListener("orientationchange", schedule);
	window.addEventListener("pageshow", schedule);
	document.addEventListener("focusin", schedule);
	document.addEventListener("focusout", schedule);
	update();
})();
