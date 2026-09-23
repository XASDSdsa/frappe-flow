(() => {
	const GUARD = 13;
	if (window.__flowPanelGuardVersion >= GUARD) return;
	const origFetch = (window.__flowOrigFetch || window.fetch).bind(window);
	window.__flowOrigFetch = origFetch;
	window.__flowPanelGuardVersion = GUARD;
	window.__flowPanelGuard = true;

	const STYLE_ID = "flow-image-style";
	const OVERLAY_ID = "flow-image-preview";
	const FLOW_STREAM = /\/api\/method\/flow\.api\.(start_run|resume_run)\b/;
	const MD_IMG = /!\[[^\]]*\]\([^)]+\)/g;

	function ensureStyle() {
		if (document.getElementById(STYLE_ID)) return;
		const style = document.createElement("style");
		style.id = STYLE_ID;
		style.textContent = `
#flow-root .md img:not([alt^="flowimg:"]) {
	display: none !important;
}
#flow-root .md img[alt^="flowimg:"] {
	display: block;
	max-width: min(100%, 360px);
	max-height: 240px;
	width: auto;
	height: auto;
	object-fit: contain;
	border-radius: 8px;
	cursor: zoom-in;
	margin: 8px 0;
	background: var(--surface-gray-1, #f4f4f4);
}
#${OVERLAY_ID} {
	position: fixed;
	inset: 0;
	z-index: 99999;
	display: flex;
	align-items: center;
	justify-content: center;
	background: rgba(0, 0, 0, 0.78);
	cursor: zoom-out;
}
#${OVERLAY_ID} .flow-image-status {
	color: white;
	padding: 16px;
	text-align: center;
}
#${OVERLAY_ID} .flow-image-original {
	position: absolute;
	top: 16px;
	right: 16px;
	color: white;
	background: rgba(0, 0, 0, 0.6);
	padding: 8px 12px;
	border-radius: 6px;
}
#${OVERLAY_ID} img {
	max-width: 92vw;
	max-height: 92vh;
	width: auto;
	height: auto;
	object-fit: contain;
	border-radius: 8px;
	box-shadow: 0 12px 48px rgba(0, 0, 0, 0.45);
	cursor: default;
}
`;
		document.head.appendChild(style);
	}

	function closePreview() {
		const overlay = document.getElementById(OVERLAY_ID);
		if (overlay) overlay.remove();
	}

	async function originalImageUrl(src) {
		const url = new URL(src, window.location.href);
		if (!["http:", "https:"].includes(url.protocol)) throw new Error("Invalid image URL");
		if (url.origin !== window.location.origin || !/^\/(?:private\/)?files\//.test(url.pathname)) {
			return url.href;
		}
		const response = await origFetch(
			"/api/method/flow.api.get_chat_original?file=" +
			encodeURIComponent(url.pathname),
			{ credentials: "same-origin" }
		);
		if (!response.ok) throw new Error("Original image unavailable");
		const result = (await response.json()).message;
		if (!result || !result.url) throw new Error("Original image not found");
		const original = new URL(result.url, window.location.href);
		if (!["http:", "https:"].includes(original.protocol)) throw new Error("Invalid original URL");
		return original.href;
	}

	async function openPreview(src, alt) {
		closePreview();
		const overlay = document.createElement("div");
		overlay.id = OVERLAY_ID;
		overlay.setAttribute("role", "dialog");
		overlay.setAttribute("aria-label", "查看原图");
		const status = document.createElement("div");
		status.className = "flow-image-status";
		status.setAttribute("role", "status");
		status.textContent = "正在加载原图…";
		overlay.appendChild(status);
		overlay.addEventListener("click", closePreview);
		document.body.appendChild(overlay);
		try {
			const original = await originalImageUrl(src);
			if (!overlay.isConnected) return;
			const link = document.createElement("a");
			link.className = "flow-image-original";
			link.textContent = "打开原图";
			link.href = original;
			link.target = "_blank";
			link.rel = "noopener noreferrer";
			link.addEventListener("click", (event) => event.stopPropagation());
			overlay.appendChild(link);
			const img = document.createElement("img");
			img.alt = (alt || "").replace(/^flowimg:/, "");
			img.hidden = true;
			img.addEventListener("load", () => {
				status.hidden = true;
				img.hidden = false;
			});
			img.addEventListener("error", () => {
				img.hidden = true;
				status.hidden = false;
				status.textContent = "浏览器无法显示原图，请点击“打开原图”查看或下载。";
			});
			overlay.appendChild(img);
			img.src = original;
		} catch (error) {
			if (overlay.isConnected) status.textContent = "原图读取失败，请确认文件仍存在且有查看权限。";
		}
	}

	function markdownFromShowImage(name, result) {
		if (name !== "show_image" || !result) return "";
		let data = result;
		if (typeof result === "string") {
			try {
				data = JSON.parse(result);
			} catch (e) {
				return "";
			}
		}
		if (!data || typeof data !== "object" || !data.url) return "";
		let alt = String(data.alt || "image").trim() || "image";
		if (!alt.startsWith("flowimg:")) alt = `flowimg:${alt}`;
		return `![${alt}](${data.url})`;
	}

	function urlFromMarkdown(markdown) {
		const match = markdown && markdown.match(/\]\(([^)]+)\)/);
		return match ? match[1] : "";
	}

	function textFrame(markdown) {
		return `data: ${JSON.stringify({ type: "text", delta: `\n\n${markdown}\n\n` })}\n\n`;
	}

	function rewriteTextBlock(block) {
		const line = block.split("\n").find((item) => item.startsWith("data: "));
		if (!line) return { block, event: null };
		try {
			const event = JSON.parse(line.slice(6));
			if (event.type === "text" && typeof event.delta === "string" && event.delta.includes("![")) {
				event.delta = event.delta.replace(MD_IMG, "");
				block = block
					.split("\n")
					.map((item) => (item.startsWith("data: ") ? `data: ${JSON.stringify(event)}` : item))
					.join("\n");
			}
			return { block, event };
		} catch (e) {
			return { block, event: null };
		}
	}

	function armImage(img) {
		if (!img || img.tagName !== "IMG" || !img.closest("#flow-root .md") || img.dataset.sfArmed) return;
		img.dataset.sfArmed = "1";
		img.decoding = "async";
		img.loading = "lazy";
		img.addEventListener("error", () => {
			img.style.display = "none";
		});
	}

	const observer = new MutationObserver((mutations) => {
		for (const mutation of mutations) {
			for (const node of mutation.addedNodes) {
				if (node.nodeType !== 1) continue;
				if (node.matches && node.matches("#flow-root .md img")) armImage(node);
				else if (node.querySelectorAll) node.querySelectorAll("#flow-root .md img").forEach(armImage);
			}
		}
	});
	observer.observe(document.documentElement, { childList: true, subtree: true });

	window.fetch = async function (input, init) {
		const url = String((input && input.url) || input || "");
		if (!FLOW_STREAM.test(url)) return origFetch(input, init);
		const resp = await origFetch(input, init);
		if (!resp.ok || !resp.body) return resp;
		const reader = resp.body.getReader();
		const decoder = new TextDecoder();
		const encoder = new TextEncoder();
		let leftover = "";
		const seen = new Set();
		const stream = new ReadableStream({
			async pull(controller) {
				const { done, value } = await reader.read();
				if (done) {
					if (leftover) controller.enqueue(encoder.encode(rewriteTextBlock(leftover).block));
					controller.close();
					return;
				}
				leftover += decoder.decode(value, { stream: true });
				const blocks = leftover.split("\n\n");
				leftover = blocks.pop();
				let out = "";
				for (const raw of blocks) {
					const { block, event } = rewriteTextBlock(raw);
					out += `${block}\n\n`;
					if (!event || event.type !== "tool_ended") continue;
					const markdown = markdownFromShowImage(event.name, event.result);
					const imageUrl = urlFromMarkdown(markdown);
					if (!markdown || !imageUrl || seen.has(imageUrl)) continue;
					seen.add(imageUrl);
					out += textFrame(markdown);
				}
				if (out) controller.enqueue(encoder.encode(out));
			},
			cancel(reason) {
				reader.cancel(reason);
			},
		});
		return new Response(stream, { headers: resp.headers, status: resp.status, statusText: resp.statusText });
	};

	document.addEventListener(
		"click",
		(event) => {
			const img = event.target && event.target.closest && event.target.closest("#flow-root .md img");
			if (!img || !img.src) return;
			event.preventDefault();
			event.stopPropagation();
			openPreview(img.currentSrc || img.src, img.alt);
		},
		true
	);

	document.addEventListener("keydown", (event) => {
		if (event.key === "Escape") closePreview();
	});

	if (document.readyState === "loading") {
		document.addEventListener("DOMContentLoaded", ensureStyle, { once: true });
	} else {
		ensureStyle();
	}
})();
