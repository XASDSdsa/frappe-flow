# Flow 工具说明格式

`Flow Tool.description` 原文交给模型，不经过额外的 Markdown 转换器。
说明在工具 docstring 或对应安装器中维护，数据库通过源码同步。

## 工具说明

首行用一句话说明具体用途。部分模型兼容路径只保留首个非空行，因此不要以空泛标题开头。
后续按需要使用下列章节；短工具省略无用章节，不重复参数 schema 已准确表达的类型。
原生英文工具使用对应的英文标题。

```markdown
只读核对销售订单资料，返回审核摘要和预检令牌。

### 参数与默认值

- `customer`：明确的客户编号。
- `delivery_days`：未指定时使用实际支持的默认值。

### 返回与下一步

- 根据真实缺项补齐；预检通过后使用返回的令牌正式执行。

### 限制

- 预检不保存订单；正式写入保留原生批准。
```

- 用途、输入、结果、限制各写在对应位置；可增加 `### 用途`。
- 参数名、工具名、返回键用行内代码；示例要与真实签名一致。
- 明确只读或写入、默认值、成功依据、失败原因及下一步。
- 保留权限、审核及业务边界；格式整理不改变工具行为。
- 不用宽表格、嵌套清单、装饰标题或同义重复扩大上下文。
- 不将完整说明同时复制到 docstring 和安装器；沿用各工具现有单一来源。

## Agent 规则

托管业务规则使用 `##` 标题和 `###` 分节，每条规则保持具体可执行。
公共回复规范负责对客服的紧凑展示，各业务段负责自己的流程。
Markdown 分节不能代替准确规则，也不保证模型会遵守全部规则；强制校验仍由原工具实现。

安装器通过 `with_managed_guidance` 替换拥有明确结束标记的完整段落，兼容历史单行说明。
结束标记是 `<!-- /flow-guidance -->`，用于界定管理范围。
自定义说明保留；边界不完整时停止同步，不能猜测并吞掉后续内容。

`deploy/markdown_guidance/metadata.py` 只同步现有工具说明和已有托管规则段落，
不改变启用状态、审批要求、工具路径或 Agent 工具绑定；支持原值精确恢复。

设计参考：[Writing effective tools for agents](https://www.anthropic.com/engineering/writing-tools-for-agents)、
[Effective context engineering for AI agents](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents)。
