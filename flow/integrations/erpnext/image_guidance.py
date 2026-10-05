"""Existing image-review guidance shared by business tool installers."""

SHOW_IMAGE_HINT_EXPLICIT_ONLY = (
	"只有用户明确要求看图、看照片、把图片发出来时，才对那几张图各调用一次 show_image。"
	"只是提到文件名、路径、已绑定图片或列表里的 image 字段时，不要调用 show_image，也不要输出图片。"
)
SHOW_IMAGE_HINT = (
	"用户明确要求看图、看照片、把图片发出来时，对那几张图各调用一次 show_image。"
	"创建客户贴纸流程已授权完成后展示图片核对：专用工具成功且核验图片已登记后，直接调用一次 show_image 展示返回图片，无需另问。"
	"除此流程外，只是提到文件名、路径、已绑定图片或列表里的 image 字段时，不要主动展示图片。"
	"图片由工具自动展示，不再输出图片 Markdown、URL、base64 或重复展示。"
)
