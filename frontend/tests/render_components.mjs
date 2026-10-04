import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { createSSRApp, h } from "vue";
import { compileScript, compileTemplate, parse } from "@vue/compiler-sfc";
import { renderToString } from "@vue/server-renderer";
import { createServer } from "vite";

for (const file of ["ConfirmCard.vue", "ActivityGroup.vue"]) {
	const source = await readFile(new URL(`../src/components/${file}`, import.meta.url), "utf8");
	const { descriptor, errors } = parse(source, { filename: file });
	assert.deepEqual(errors, []);
	assert.equal(compileScript(descriptor, { id: file }).errors?.length ?? 0, 0);
	assert.equal(compileTemplate({ source: descriptor.template.content, id: file }).errors.length, 0);
}

const server = await createServer({ server: { middlewareMode: true, hmr: false, ws: false } });
try {
	const { default: ConfirmCard } = await server.ssrLoadModule(
		"/frontend/src/components/ConfirmCard.vue"
	);
	const { default: ActivityGroup } = await server.ssrLoadModule(
		"/frontend/src/components/ActivityGroup.vue"
	);

	const render = (component, props) =>
		renderToString(createSSRApp({ render: () => h(component, props) }));

	const review = {
		title: "首单贴纸服务提醒",
		message: "这是可信的首单审核正文。",
	};

	const approval = await render(ConfirmCard, {
		question: {
			prompt: "请审核销售订单\n\n" + review.message,
			show_prompt: true,
			options: ["Approve", "Deny"],
		},
		tool: { name: "create_sales_order_draft", arguments: '{"preview_token":"token-123"}' },
	});
	assert.match(approval, /这是可信的首单审核正文。/);
	assert.doesNotMatch(approval, /preview_token/);

	const ordinary = await render(ConfirmCard, {
		question: { prompt: "请批准", options: ["Approve", "Deny"] },
		tool: { name: "create", arguments: '{"doctype":"Sales Order","records":[{"name":"SO-1"}]}' },
	});
	assert.match(ordinary, /Sales Order/);
	assert.match(ordinary, /SO-1/);

	const preview = (result) => ({
		name: "preview_sales_order",
		arguments: "{}",
		result: JSON.stringify(result),
		approval: null,
	});
	const unrelated = {
		name: "other_tool",
		arguments: "{}",
		result: JSON.stringify({ sticker_service_review: review }),
		approval: null,
	};

	for (const live of [true, false]) {
		const history = await render(ActivityGroup, {
			parts: [unrelated, preview({ status: "preview", sticker_service_review: review })],
			live,
			sealed: !live,
		});
		assert.match(history, /这是可信的首单审核正文。/);
	}

	const latestWithoutReview = await render(ActivityGroup, {
		parts: [
			preview({ status: "preview", sticker_service_review: review }),
			preview({ status: "preview", summary: "复购订单" }),
		],
		live: false,
		sealed: true,
	});
	assert.doesNotMatch(latestWithoutReview, /这是可信的首单审核正文。/);

	const latestRunning = await render(ActivityGroup, {
		parts: [
			preview({ status: "preview", sticker_service_review: review }),
			{ ...preview({}), result: null },
		],
		live: true,
		sealed: false,
	});
	assert.doesNotMatch(latestRunning, /这是可信的首单审核正文。/);

	console.log("render_components: all assertions passed");
} finally {
	await server.close();
}
