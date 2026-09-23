(() => {
	if (window.__flowImagePaste20260919) return;
	window.__flowImagePaste20260919 = true;

	document.addEventListener("paste", (event) => {
		const input = event.target;
		if (event.defaultPrevented || !input?.matches?.("#flow-root .flow-composer textarea")
			|| input.disabled || input.readOnly) return;
		const clipboard = event.clipboardData;
		if (!clipboard) return;
		let images = Array.from(clipboard.items || [])
			.filter((item) => item.kind === "file" && item.type.startsWith("image/"))
			.map((item) => item.getAsFile()).filter(Boolean);
		// Some browsers expose files without matching clipboard items. Do not use
		// both collections together: they normally contain the same images.
		if (!images.length) images = Array.from(clipboard.files || [])
			.filter((file) => file.type.startsWith("image/"));
		if (!images.length) return;
		const fileInput = input.closest(".flow-composer")?.querySelector('input[type="file"]');
		if (!fileInput || fileInput.disabled) return;
		try {
			const transfer = new DataTransfer();
			const extensions = { "image/png": "png", "image/jpeg": "jpg", "image/gif": "gif",
				"image/webp": "webp", "image/avif": "avif", "image/bmp": "bmp", "image/tiff": "tiff" };
			images.forEach((file) => {
				const extension = extensions[file.type];
				// Clipboard screenshots occasionally have no filename extension. Keep
				// their bytes and MIME type; the native uploader validates support/size.
				if (extension && !/\.[a-z0-9]+$/i.test(file.name || "")) {
					file = new File([file], `${file.name || "pasted-image"}.${extension}`, { type: file.type });
				}
				transfer.items.add(file);
			});
			fileInput.files = transfer.files;
			fileInput.dispatchEvent(new Event("change", { bubbles: true }));
			// Let the browser paste accompanying text at the current selection.
			if (!clipboard.getData("text/plain")) event.preventDefault();
		} catch (error) {
			console.warn("Flow image paste could not use the attachment input", error);
			window.frappe?.show_alert?.({ message: "图片粘贴未成功，请使用附件按钮上传。", indicator: "orange" });
		}
	});
})();
