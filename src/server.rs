//! HTTP 服务器层 —— 薄路由壳，暴露 OpenAIAdapter 与 AnthropicCompat 为 HTTP 接口
//!
//! 本模块负责将 adapter / compat 层包装为 axum HTTP 服务。

mod error;
mod handlers;
mod stream;

use axum::{
    Router,
    extract::Request,
    middleware::{self, Next},
    response::{IntoResponse, Response},
    routing::{get, post},
};
use sha2::{Digest, Sha256};
use std::sync::Arc;
use subtle::ConstantTimeEq;
use tokio::net::TcpListener;

use crate::anthropic_compat::AnthropicCompat;
use crate::config::Config;
use crate::openai_adapter::OpenAIAdapter;

use handlers::AppState;

/// 启动 HTTP 服务器
pub async fn run(config: Config) -> anyhow::Result<()> {
    let adapter = Arc::new(OpenAIAdapter::new(&config).await?);
    let anthropic_compat = Arc::new(AnthropicCompat::new(Arc::clone(&adapter)));
    let state = AppState {
        adapter,
        anthropic_compat,
    };
    let router = build_router(state.clone(), &config.server.api_tokens);

    let addr = format!("{}:{}", config.server.host, config.server.port);
    let listener = TcpListener::bind(&addr).await?;
    log::info!(target: "http::server", "openai兼容base_url: http://{}", addr);
    log::info!(target: "http::server", "anthropic兼容base_url: http://{}/anthropic", addr);

    axum::serve(listener, router)
        .with_graceful_shutdown(shutdown_signal())
        .await?;

    log::info!(target: "http::server", "HTTP 服务已停止，正在清理资源");
    state.adapter.shutdown().await;
    log::info!(target: "http::server", "清理完成");

    Ok(())
}

/// 构建路由器
fn build_router(state: AppState, api_tokens: &[crate::config::ApiToken]) -> Router {
    let has_auth = !api_tokens.is_empty();
    let tokens: Vec<String> = api_tokens.iter().map(|t| t.token.clone()).collect();

    let mut router = Router::new()
        .route("/", get(|| async { "ai-free-api" }))
        // OpenAI
        .route("/v1/chat/completions", post(handlers::chat_completions))
        .route("/v1/models", get(handlers::list_models))
        .route("/v1/models/{id}", get(handlers::get_model))
        // Anthropic
        .route("/anthropic/v1/messages", post(handlers::anthropic_messages))
        .route("/anthropic/v1/models", get(handlers::anthropic_list_models))
        .route(
            "/anthropic/v1/models/{id}",
            get(handlers::anthropic_get_model),
        )
        .with_state(state);

    if has_auth {
        router = router.layer(middleware::from_fn(move |req, next| {
            let tokens = tokens.clone();
            async move { auth_middleware(req, next, tokens).await }
        }));
    }

    router = router.layer(middleware::from_fn(security_headers_middleware));

    router
}

/// API Token 鉴权中间件
async fn auth_middleware(req: Request, next: Next, tokens: Vec<String>) -> Response {
    let auth_header = req
        .headers()
        .get("authorization")
        .and_then(|v| v.to_str().ok());

    let valid = match auth_header {
        Some(header) if header.starts_with("Bearer ") => {
            let token = header.strip_prefix("Bearer ").unwrap_or("");
            let token_hash = Sha256::digest(token.as_bytes());
            tokens
                .iter()
                .any(|t| Sha256::digest(t.as_bytes()).ct_eq(&token_hash).into())
        }
        _ => false,
    };

    if !valid {
        log::debug!(target: "http::response", "401 unauthorized request");
        return error::ServerError::Unauthorized.into_response();
    }

    next.run(req).await
}

/// 安全头中间件 —— 添加常见的安全相关响应头
async fn security_headers_middleware(req: Request, next: Next) -> Response {
    let mut response = next.run(req).await;
    let headers = response.headers_mut();

    // 防御 MIME 类型嗅探
    headers.insert(axum::http::header::X_CONTENT_TYPE_OPTIONS, axum::http::HeaderValue::from_static("nosniff"));
    // 防止点击劫持
    headers.insert(axum::http::header::X_FRAME_OPTIONS, axum::http::HeaderValue::from_static("DENY"));
    // XSS 保护
    headers.insert(axum::http::header::X_XSS_PROTECTION, axum::http::HeaderValue::from_static("1; mode=block"));
    // 强制 HTTPS（如果适用）
    headers.insert(axum::http::header::STRICT_TRANSPORT_SECURITY, axum::http::HeaderValue::from_static("max-age=31536000; includeSubDomains"));

    response
}

/// 优雅关闭信号
async fn shutdown_signal() {
    let ctrl_c = async {
        tokio::signal::ctrl_c()
            .await
            .expect("failed to install Ctrl+C handler");
    };

    #[cfg(unix)]
    let terminate = async {
        tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())
            .expect("failed to install SIGTERM handler")
            .recv()
            .await;
    };

    #[cfg(not(unix))]
    let terminate = std::future::pending::<()>();

    tokio::select! {
        _ = ctrl_c => {},
        _ = terminate => {},
    }

    log::info!(target: "http::server", "收到关闭信号，开始优雅关闭");
}
