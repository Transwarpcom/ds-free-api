//! OpenAI 模型列表响应生成
//!
//! 基于 DeepSeek model_types 静态生成 OpenAI /models 响应。

use crate::openai_adapter::types::{Model, ModelList};

const MODEL_CREATED: u64 = 1_090_108_800;
const MODEL_OWNED_BY: &str = "deepseek-web (proxied by https://github.com/NIyueeE)";

/// 根据 model_types 生成模型列表 JSON
pub fn list(model_types: &[String]) -> Vec<u8> {
    let data: Vec<Model> = model_types
        .iter()
        .map(|ty| Model {
            id: format!("deepseek-{}", ty),
            object: "model",
            created: MODEL_CREATED,
            owned_by: MODEL_OWNED_BY,
        })
        .collect();

    serde_json::to_vec(&ModelList {
        object: "list",
        data,
    })
    .unwrap_or_else(|_| br#"{"object":"list","data":[]}"#.to_vec())
}

/// 查询单个模型
pub fn get(model_types: &[String], id: &str) -> Option<Vec<u8>> {
    let target = id.to_lowercase();
    model_types
        .iter()
        .find(|ty| format!("deepseek-{}", ty).to_lowercase() == target)
        .map(|ty| {
            serde_json::to_vec(&Model {
                id: format!("deepseek-{}", ty),
                object: "model",
                created: MODEL_CREATED,
                owned_by: MODEL_OWNED_BY,
            })
            .unwrap_or_else(|_| br#"{}"#.to_vec())
        })
}
