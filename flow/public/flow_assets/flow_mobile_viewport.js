(() => {
	if (window.__flowMobileViewport20260916) return;
	window.__flowMobileViewport20260916 = true;
	const viewport = window.visualViewport;
	let frame = 0;
	const update = () => {
		frame = 0;
		// Leave native pinch zoom alone; a zoomed viewport is not a keyboard resize.
		if (viewport && Math.abs(viewport.scale - 1) > 0.01) return;
		// Keep top and height in the same visual viewport. Capping it to the
		// layout height can leave an uncovered strip when browser chrome changes.
		const height = viewport ? viewport.height : window.innerHeight;
		if (!Number.isFinite(height) || height <= 0) return;
		const top = viewport ? Math.max(0, viewport.offsetTop) : 0;
		const style = document.documentElement.style;
		style.setProperty("--flow-mobile-viewport-top", `${top}px`);
		style.setProperty("--flow-mobile-viewport-height", `${height}px`);
	};
	const schedule = () => {
		if (!frame) frame = requestAnimationFrame(update);
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
