//! openai_adapter 交互式 CLI 测试工具
//!
//! 使用方式:
//!   交互模式: cargo run --example openai_adapter_cli
//!   脚本模式: cargo run --example openai_adapter_cli -- source examples/openai_adapter_cli-script.txt
//!
//! 命令:
//!   chat <json_file> [--raw]               - 读取标准 OpenAI JSON body，内部按 stream 字段路由
//!   concurrent <n> <json_file> [--raw]     - 并发 chat
//!   models                                 - 列出可用模型
//!   model <id>                             - 查询单个模型
//!   status                                 - 查看 ds_core 账号池状态
//!   source <file>                          - 从文件读取命令执行
//!   quit | exit                            - 退出并清理

use ai_free_api::{Config, OpenAIAdapter, StreamResponse};
use bytes::Bytes;
use futures::{StreamExt, future::join_all};
use std::io::{self, Read, Write};
use std::path::Path;

/// 读取一行输入，允许无效的 UTF-8
fn read_line_lossy() -> io::Result<String> {
    let mut buf = Vec::new();
    let stdin = io::stdin();
    let mut handle = stdin.lock();

    loop {
        let mut byte = [0u8; 1];
        match handle.read(&mut byte) {
            Ok(0) => break,
            Ok(_) => {
                if byte[0] == b'\n' {
                    break;
                }
                if byte[0] != b'\r' {
                    buf.push(byte[0]);
                }
            }
            Err(e) => return Err(e),
        }
    }

    Ok(String::from_utf8_lossy(&buf).into_owned())
}

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    env_logger::Builder::from_env(env_logger::Env::new().default_filter_or("info")).init();

    let config = Config::load("config.toml")?;
    println!("[初始化中...]");
    let adapter = OpenAIAdapter::new(&config).await?;
    println!(
        "[就绪] 命令: chat <json> [--raw] | concurrent <n> <json> [--raw] | models | model | status | source | quit"
    );

    let mut stdout = io::stdout();

    loop {
        print!("> ");
        stdout.flush()?;

        let line = read_line_lossy()?;
        let line = line.trim();
        if line.is_empty() || line.starts_with('#') {
            continue;
        }

        if handle_line(line, &adapter).await? {
            break;
        }
    }

    println!("[清理中...]");
    adapter.shutdown().await;
    println!("[已关闭]");

    Ok(())
}

/// 解析命令行参数，提取位置参数和 --raw flag
fn parse_args<'a>(parts: &'a [&'a str]) -> (Vec<&'a str>, bool) {
    let raw = parts.iter().any(|p| *p == "--raw" || *p == "-r");
    let positional: Vec<_> = parts
        .iter()
        .filter(|p| **p != "--raw" && **p != "-r")
        .copied()
        .collect();
    (positional, raw)
}

async fn handle_line(line: &str, adapter: &OpenAIAdapter) -> anyhow::Result<bool> {
    let parts: Vec<&str> = line.split_whitespace().collect();
    if parts.is_empty() {
        return Ok(false);
    }

    let cmd = parts[0];
    match cmd {
        "status" => {
            println!("[账号状态]");
            for (i, s) in adapter.account_statuses().iter().enumerate() {
                let email = if s.email.is_empty() { "-" } else { &s.email };
                let mobile = if s.mobile.is_empty() { "-" } else { &s.mobile };
                println!("  [{}] {} / {}", i + 1, email, mobile);
            }
        }

        "chat" if parts.len() >= 2 => {
            let (positional, raw) = parse_args(&parts);
            let file = positional[1];
            if !Path::new(file).exists() {
                eprintln!("[错误] 文件不存在: {}", file);
                return Ok(false);
            }
            let body = std::fs::read_to_string(file)?;
            println!(">>> 请求: {}", file);
            if let Err(e) = run_chat(adapter, body.as_bytes(), raw).await {
                eprintln!("[请求失败] {}", e);
            }
        }

        "concurrent" if parts.len() >= 3 => {
            let (positional, raw) = parse_args(&parts);
            let count: usize = match positional[1].parse() {
                Ok(n) if n > 0 => n,
                _ => {
                    eprintln!("[错误] 并发数必须是正整数");
                    return Ok(false);
                }
            };
            let file = positional[2];
            if !Path::new(file).exists() {
                eprintln!("[错误] 文件不存在: {}", file);
                return Ok(false);
            }
            let body = std::fs::read_to_string(file)?;
            println!(">>> 并发请求: count={}, file={}", count, file);
            run_concurrent(adapter, count, body, raw).await;
        }

        "models" => {
            let json = adapter.list_models();
            println!("{}", String::from_utf8_lossy(&json));
        }

        "model" if parts.len() == 2 => {
            if let Some(json) = adapter.get_model(parts[1]) {
                println!("{}", String::from_utf8_lossy(&json));
            } else {
                println!("null");
            }
        }

        "source" if parts.len() == 2 => {
            let file = parts[1];
            if !Path::new(file).exists() {
                eprintln!("[错误] 文件不存在: {}", file);
                return Ok(false);
            }
            println!("[执行脚本: {}]", file);
            let content = std::fs::read_to_string(file)?;
            for script_line in content.lines() {
                let script_line = script_line.trim();
                if script_line.is_empty() || script_line.starts_with('#') {
                    continue;
                }
                println!(">>> {}", script_line);
                if Box::pin(handle_line(script_line, adapter)).await? {
                    return Ok(true);
                }
            }
            println!("[脚本执行完毕]");
        }

        "quit" | "exit" => {
            println!("[退出]");
            return Ok(true);
        }

        _ => {
            println!(
                "[未知命令: {}] 可用: chat | concurrent | models | model | status | source | quit",
                cmd
            );
        }
    }

    Ok(false)
}

/// 判断请求体是否要求流式
fn is_stream(body: &[u8]) -> bool {
    serde_json::from_slice::<serde_json::Value>(body)
        .ok()
        .and_then(|v| v.get("stream").and_then(|s| s.as_bool()))
        .unwrap_or(false)
}

/// 执行单次 chat，根据 stream 字段路由，raw 控制输出格式
async fn run_chat(adapter: &OpenAIAdapter, body: &[u8], raw: bool) -> anyhow::Result<()> {
    if is_stream(body) {
        let mut stream = adapter.chat_completions_stream(body).await?;
        print_stream(&mut stream, raw).await;
    } else {
        let json = adapter.chat_completions(body).await?;
        if raw {
            println!("{}", String::from_utf8_lossy(&json));
        } else {
            print_chat_summary(&json);
        }
    }
    Ok(())
}

/// 打印非流式响应的简化摘要
fn print_chat_summary(json: &[u8]) {
    let v: serde_json::Value = match serde_json::from_slice(json) {
        Ok(val) => val,
        Err(_) => {
            println!("{}", String::from_utf8_lossy(json));
            return;
        }
    };

    let choice = v.get("choices").and_then(|c| c.get(0));
    let message = choice.and_then(|c| c.get("message"));
    let content = message
        .and_then(|m| m.get("content"))
        .and_then(|c| c.as_str());
    let reasoning = message
        .and_then(|m| m.get("reasoning_content"))
        .and_then(|c| c.as_str());
    let tool_calls = message.and_then(|m| m.get("tool_calls"));
    let finish = choice
        .and_then(|c| c.get("finish_reason"))
        .and_then(|f| f.as_str());
    let usage = v.get("usage");

    let mut summary = serde_json::Map::new();
    if let Some(c) = content {
        summary.insert("content".into(), c.into());
    }
    if let Some(r) = reasoning {
        summary.insert("reasoning_content".into(), r.into());
    }
    if let Some(t) = tool_calls {
        summary.insert("tool_calls".into(), t.clone());
    }
    if let Some(f) = finish {
        summary.insert("finish_reason".into(), f.into());
    }
    if let Some(u) = usage {
        summary.insert("usage".into(), u.clone());
    }

    println!(
        "{}",
        serde_json::to_string_pretty(&summary).unwrap_or_default()
    );
}

/// 消费流式响应，raw 控制输出格式
async fn print_stream(stream: &mut StreamResponse, raw: bool) {
    let mut stdout = io::stdout();
    while let Some(res) = stream.next().await {
        match res {
            Ok(bytes) => {
                if raw {
                    print!("{}", String::from_utf8_lossy(&bytes));
                    stdout.flush().unwrap();
                } else {
                    print_stream_chunk(&bytes);
                }
            }
            Err(e) => {
                eprintln!("\n[流错误] {}", e);
                break;
            }
        }
    }
    if !raw {
        println!();
    }
}

/// 打印单个流式 chunk 的简化摘要
fn print_stream_chunk(bytes: &Bytes) {
    let text = String::from_utf8_lossy(bytes);
    let json_str = text
        .strip_prefix("data: ")
        .and_then(|s| s.strip_suffix("\n\n"))
        .unwrap_or(&text);

    let v: serde_json::Value = match serde_json::from_str(json_str) {
        Ok(val) => val,
        Err(_) => {
            print!("{}", text);
            return;
        }
    };

    let choice = v.get("choices").and_then(|c| c.get(0));
    let delta = choice.and_then(|c| c.get("delta"));
    let content = delta
        .and_then(|d| d.get("content"))
        .and_then(|c| c.as_str());
    let reasoning = delta
        .and_then(|d| d.get("reasoning_content"))
        .and_then(|c| c.as_str());
    let tool_calls = delta.and_then(|d| d.get("tool_calls"));
    let finish = choice
        .and_then(|c| c.get("finish_reason"))
        .and_then(|f| f.as_str());
    let usage = v.get("usage");

    // usage chunk（空 choices）单独处理
    if choice.is_none() || usage.is_some() {
        if let Some(u) = usage {
            println!("[usage] {}", u);
            return;
        }
    }

    let mut parts = Vec::new();
    if let Some(c) = content {
        parts.push(format!("content={:?}", c));
    }
    if let Some(r) = reasoning {
        parts.push(format!("reasoning={:?}", r));
    }
    if let Some(t) = tool_calls {
        parts.push(format!(
            "tool_calls={}",
            serde_json::to_string(t).unwrap_or_default()
        ));
    }
    if let Some(f) = finish {
        parts.push(format!("finish={}", f));
    }

    if !parts.is_empty() {
        println!("[chunk] {}", parts.join(" | "));
    }
}

/// 执行并发请求
async fn run_concurrent(adapter: &OpenAIAdapter, count: usize, body_template: String, raw: bool) {
    let start = std::time::Instant::now();
    let body_bytes = body_template.into_bytes();
    let is_streaming = is_stream(&body_bytes);

    let futures: Vec<_> = (0..count)
        .map(|i| {
            let body = body_bytes.clone();
            async move {
                let req_start = std::time::Instant::now();
                let result = if is_streaming {
                    match adapter.chat_completions_stream(&body).await {
                        Ok(mut stream) => {
                            let mut output = String::new();
                            let mut ok = true;
                            while let Some(chunk) = stream.next().await {
                                match chunk {
                                    Ok(bytes) => {
                                        if raw {
                                            output.push_str(&String::from_utf8_lossy(&bytes));
                                        } else {
                                            let text = String::from_utf8_lossy(&bytes);
                                            let json_str = text
                                                .strip_prefix("data: ")
                                                .and_then(|s| s.strip_suffix("\n\n"))
                                                .unwrap_or(&text);
                                            if let Ok(v) =
                                                serde_json::from_str::<serde_json::Value>(json_str)
                                            {
                                                let delta = v
                                                    .get("choices")
                                                    .and_then(|c| c.get(0))
                                                    .and_then(|c| c.get("delta"));
                                                if let Some(c) = delta
                                                    .and_then(|d| d.get("content"))
                                                    .and_then(|c| c.as_str())
                                                {
                                                    output.push_str(c);
                                                }
                                                if let Some(r) = delta
                                                    .and_then(|d| d.get("reasoning_content"))
                                                    .and_then(|c| c.as_str())
                                                {
                                                    if !output.is_empty() {
                                                        output.push(' ');
                                                    }
                                                    output.push_str(r);
                                                }
                                            }
                                        }
                                    }
                                    Err(e) => {
                                        eprintln!("\n[请求{} 流错误] {}", i, e);
                                        ok = false;
                                        break;
                                    }
                                }
                            }
                            (i, ok, output, req_start.elapsed())
                        }
                        Err(e) => {
                            eprintln!("[请求{} 失败] {}", i, e);
                            (i, false, String::new(), req_start.elapsed())
                        }
                    }
                } else {
                    match adapter.chat_completions(&body).await {
                        Ok(json) => {
                            let output = if raw {
                                String::from_utf8_lossy(&json).to_string()
                            } else {
                                let v: serde_json::Value =
                                    serde_json::from_slice(&json).unwrap_or_default();
                                let mut parts = Vec::new();
                                if let Some(c) = v
                                    .get("choices")
                                    .and_then(|c| c.get(0))
                                    .and_then(|c| c.get("message"))
                                    .and_then(|m| m.get("content"))
                                    .and_then(|c| c.as_str())
                                {
                                    parts.push(c.to_string());
                                }
                                if let Some(r) = v
                                    .get("choices")
                                    .and_then(|c| c.get(0))
                                    .and_then(|c| c.get("message"))
                                    .and_then(|m| m.get("reasoning_content"))
                                    .and_then(|c| c.as_str())
                                {
                                    parts.push(r.to_string());
                                }
                                parts.join(" ")
                            };
                            (i, true, output, req_start.elapsed())
                        }
                        Err(e) => {
                            eprintln!("[请求{} 失败] {}", i, e);
                            (i, false, String::new(), req_start.elapsed())
                        }
                    }
                };
                result
            }
        })
        .collect();

    let results = join_all(futures).await;
    let total_elapsed = start.elapsed();

    println!("\n[并发结果]");
    let success_count = results.iter().filter(|(_, ok, _, _)| *ok).count();
    for (i, ok, output, elapsed) in results {
        let preview: String = output.chars().take(80).collect();
        let status = if ok { "成功" } else { "失败" };
        println!(
            "  [请求{:2}] {} | {:>12?} | {}",
            i,
            status,
            elapsed,
            if preview.is_empty() {
                "(无输出)".to_string()
            } else {
                format!("{}...", preview.replace('\n', " "))
            }
        );
    }
    println!(
        "  总计: {}/{} 成功 | 总耗时 {:?}",
        success_count, count, total_elapsed
    );
}
