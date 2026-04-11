//! Prompt 构建 —— 将 OpenAI messages 转换为 ChatML 格式字符串
//!
//! 若请求包含工具定义或行为指令，会追加到最后一条 user 消息中。
//! 最终以 assistant 角色前缀引导模型生成回复。

use super::tools::ToolContext;
use crate::openai_adapter::types::{ChatCompletionRequest, ContentPart, Message, MessageContent};

const IM_START: &str = "<|im_start|>";
const IM_END: &str = "<|im_end|>";

/// 构建 ChatML 格式的 prompt 字符串
pub fn build(req: &ChatCompletionRequest, tool_ctx: &ToolContext) -> String {
    let mut parts: Vec<String> = req.messages.iter().map(format_message).collect();

    let extra_blocks: Vec<&str> = [
        tool_ctx.defs_text.as_deref(),
        tool_ctx.instruction_text.as_deref(),
    ]
    .into_iter()
    .flatten()
    .collect();

    if !extra_blocks.is_empty() {
        let extra = extra_blocks.join("\n\n");
        if let Some(idx) = req.messages.iter().rposition(|m| m.role == "user") {
            let original = &parts[idx];
            let prefix = format!("{IM_START}user\n");
            let suffix = format!("\n{IM_END}");
            if original.starts_with(&prefix) && original.ends_with(&suffix) {
                let body = &original[prefix.len()..original.len() - suffix.len()];
                let new_body = if body.trim().is_empty() {
                    extra
                } else {
                    format!("{body}\n\n{extra}")
                };
                parts[idx] = format!("{prefix}{new_body}{suffix}");
            }
        } else {
            parts.push(format!("{IM_START}user\n{extra}\n{IM_END}"));
        }
    }

    parts.push(format!("{IM_START}assistant"));
    parts.join("\n")
}

fn format_message(msg: &Message) -> String {
    let body = match msg.role.as_str() {
        "assistant" => format_assistant(msg),
        "tool" => format_tool(msg),
        "function" => format_function(msg),
        _ => format_generic(msg),
    };
    format!("{IM_START}{}\n{}\n{IM_END}", msg.role, body)
}

fn format_generic(msg: &Message) -> String {
    let mut parts = Vec::new();
    if let Some(name) = &msg.name {
        parts.push(format!("(name: {name})"));
    }
    if let Some(content) = &msg.content {
        parts.push(format_content(content));
    }
    parts.join("\n")
}

fn format_assistant(msg: &Message) -> String {
    let mut parts = Vec::new();
    if let Some(content) = &msg.content {
        parts.push(format_content(content));
    }
    if let Some(tool_calls) = &msg.tool_calls {
        for tc in tool_calls {
            if let Some(func) = &tc.function {
                parts.push(format!(
                    "[调用工具: id={}, type={}, function={}] {}",
                    tc.id, tc.ty, func.name, func.arguments
                ));
            } else {
                parts.push(format!("[调用工具: id={}, type={}]", tc.id, tc.ty));
            }
        }
    }
    if let Some(fc) = &msg.function_call {
        parts.push(format!("[调用函数: {}] {}", fc.name, fc.arguments));
    }
    if let Some(refusal) = &msg.refusal {
        parts.push(format!("(refusal: {refusal})"));
    }
    parts.join("\n")
}

fn format_tool(msg: &Message) -> String {
    let mut parts = Vec::new();
    if let Some(id) = &msg.tool_call_id {
        parts.push(format!("(tool_call_id: {id})"));
    }
    if let Some(content) = &msg.content {
        parts.push(format_content(content));
    }
    parts.join("\n")
}

fn format_function(msg: &Message) -> String {
    let mut parts = Vec::new();
    if let Some(name) = &msg.name {
        parts.push(format!("(name: {name})"));
    }
    if let Some(content) = &msg.content {
        parts.push(format_content(content));
    }
    parts.join("\n")
}

fn format_content(content: &MessageContent) -> String {
    match content {
        MessageContent::Text(text) => text.clone(),
        MessageContent::Parts(parts) => {
            parts.iter().map(format_part).collect::<Vec<_>>().join("\n")
        }
    }
}

fn format_part(part: &ContentPart) -> String {
    match part.ty.as_str() {
        "text" => part.text.clone().unwrap_or_default(),
        "refusal" => part.refusal.clone().unwrap_or_default(),
        "image_url" => {
            let detail = part
                .image_url
                .as_ref()
                .and_then(|i| i.detail.as_deref())
                .unwrap_or("auto");
            format!("[图片: detail={detail}]")
        }
        "input_audio" => {
            let fmt = part
                .input_audio
                .as_ref()
                .map(|a| a.format.as_str())
                .unwrap_or("unknown");
            format!("[音频: format={fmt}]")
        }
        "file" => {
            let filename = part
                .file
                .as_ref()
                .and_then(|f| f.filename.as_deref())
                .unwrap_or("unknown");
            format!("[文件: filename={filename}]")
        }
        _ => format!("[未支持的内容类型: {}]", part.ty),
    }
}
