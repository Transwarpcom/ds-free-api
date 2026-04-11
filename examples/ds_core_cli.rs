//! ds-core 交互式 CLI 测试工具
//!
//! 使用方式:
//!   交互模式: cargo run --example ds_core_cli
//!   脚本模式: cargo run --example ds_core_cli -- source examples/ds_core_cli-script.txt
//!
//! 命令:
//!   status                              - 查看所有账号状态
//!   model <type>                        - 切换当前模型类型 (默认取 config 第一项)
//!   chat <prompt> [thinking] [search] [--raw]   - 发送对话 (t=thinking, s=search, 默认 false)
//!   concurrent <n> <prompt> [t] [s] [--raw]     - 并发发送 n 个请求
//!   source <file>                                - 从文件读取命令执行
//!   quit | exit                                  - 退出并清理

use ai_free_api::{ChatRequest, Config, DeepSeekCore};
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
            Ok(0) => break, // EOF
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
    let mut current_model_type = config
        .deepseek
        .model_types
        .first()
        .cloned()
        .unwrap_or_else(|| "default".to_string());
    println!("[初始化中...]");
    let core = DeepSeekCore::new(&config).await?;
    println!("[就绪] 命令: status | model | chat | concurrent <n> | source | quit");

    let mut stdout = io::stdout();

    loop {
        print!("> ");
        stdout.flush()?;

        let line = read_line_lossy()?;
        let line = line.trim();
        if line.is_empty() || line.starts_with('#') {
            continue;
        }

        let parts: Vec<&str> = line.split_whitespace().collect();
        if parts.is_empty() {
            continue;
        }
        let cmd = parts[0];

        match cmd {
            "status" => {
                println!("[账号状态]");
                for (i, s) in core.account_statuses().iter().enumerate() {
                    let email = if s.email.is_empty() { "-" } else { &s.email };
                    let mobile = if s.mobile.is_empty() { "-" } else { &s.mobile };
                    println!("  [{}] {} / {}", i + 1, email, mobile);
                }
            }

            "model" if parts.len() >= 2 => {
                current_model_type = parts[1].to_string();
                println!("[当前模型已切换为: {}]", current_model_type);
            }

            "chat" if parts.len() >= 2 => {
                let rest = line.strip_prefix("chat").unwrap().trim();
                let (rest, raw) = extract_raw_flag(rest);
                let (prompt, thinking, search) = parse_chat_args(rest);

                println!(
                    "[请求] prompt={:?}, model_type={}, thinking={}, search={}, raw={}",
                    prompt.chars().take(50).collect::<String>(),
                    current_model_type,
                    thinking,
                    search,
                    raw
                );

                match core
                    .v0_chat(ChatRequest {
                        prompt,
                        thinking_enabled: thinking,
                        search_enabled: search,
                        model_type: current_model_type.clone(),
                    })
                    .await
                {
                    Ok(mut stream) => {
                        if raw {
                            println!("[响应流开始 - raw]");
                            while let Some(chunk) = stream.next().await {
                                let chunk: Result<Bytes, ai_free_api::CoreError> = chunk;
                                match chunk {
                                    Ok(bytes) => {
                                        print!("{}", String::from_utf8_lossy(&bytes));
                                        stdout.flush()?;
                                    }
                                    Err(e) => {
                                        eprintln!("\n[流错误] {}", e);
                                        break;
                                    }
                                }
                            }
                            println!("\n[响应流结束 - raw]");
                        } else {
                            let mut output = String::new();
                            while let Some(chunk) = stream.next().await {
                                match chunk {
                                    Ok(bytes) => output.push_str(&String::from_utf8_lossy(&bytes)),
                                    Err(e) => {
                                        eprintln!("\n[流错误] {}", e);
                                        break;
                                    }
                                }
                            }
                            let preview: String = output.chars().take(300).collect();
                            let truncated = if output.len() > 300 {
                                format!("{}... (完整 {} bytes)", preview, output.len())
                            } else {
                                preview
                            };
                            println!("[响应] {}", truncated);
                        }
                    }
                    Err(e) => {
                        eprintln!("[请求失败] {}", e);
                    }
                }
            }

            "concurrent" if parts.len() >= 3 => {
                let count: usize = match parts[1].parse() {
                    Ok(n) if n > 0 => n,
                    _ => {
                        eprintln!("[错误] 并发数必须是正整数");
                        continue;
                    }
                };

                let rest = line.strip_prefix("concurrent").unwrap().trim();
                let rest = rest.strip_prefix(parts[1]).unwrap().trim();
                let (rest, raw) = extract_raw_flag(rest);
                let (prompt, thinking, search) = parse_chat_args(rest);

                println!(
                    "[并发请求] count={}, model_type={}, prompt={:?}, thinking={}, search={}, raw={}",
                    count,
                    current_model_type,
                    prompt.chars().take(50).collect::<String>(),
                    thinking,
                    search,
                    raw
                );

                run_concurrent(
                    &core,
                    count,
                    prompt,
                    &current_model_type,
                    thinking,
                    search,
                    raw,
                )
                .await;
            }

            "source" if parts.len() == 2 => {
                let file = parts[1];
                if !Path::new(file).exists() {
                    eprintln!("[错误] 文件不存在: {}", file);
                    continue;
                }
                println!("[执行脚本: {}]", file);
                let content = std::fs::read_to_string(file)?;
                for script_line in content.lines() {
                    let script_line = script_line.trim();
                    if script_line.is_empty() || script_line.starts_with('#') {
                        continue;
                    }
                    println!(">>> {}", script_line);
                    if handle_line(script_line, &core, &mut current_model_type).await? {
                        break;
                    }
                }
                println!("[脚本执行完毕]");
            }

            "quit" | "exit" => {
                println!("[退出]");
                break;
            }

            _ => {
                println!(
                    "[未知命令: {}] 可用: status | model | chat | concurrent | source | quit",
                    cmd
                );
            }
        }
    }

    println!("[清理中...]");
    core.shutdown().await;
    println!("[已关闭]");

    Ok(())
}

/// 运行并发请求
async fn run_concurrent(
    core: &DeepSeekCore,
    count: usize,
    prompt: String,
    model_type: &str,
    thinking: bool,
    search: bool,
    raw: bool,
) {
    let start = std::time::Instant::now();

    let futures: Vec<_> = (0..count)
        .map(|i| {
            let prompt = prompt.clone();
            let model_type = model_type.to_string();
            async move {
                let req_start = std::time::Instant::now();
                match core
                    .v0_chat(ChatRequest {
                        prompt,
                        thinking_enabled: thinking,
                        search_enabled: search,
                        model_type,
                    })
                    .await
                {
                    Ok(mut stream) => {
                        let mut output = String::new();
                        while let Some(chunk) = stream.next().await {
                            match chunk {
                                Ok(bytes) => {
                                    output.push_str(&String::from_utf8_lossy(&bytes));
                                }
                                Err(e) => {
                                    eprintln!("\n[请求{} 流错误] {}", i, e);
                                    break;
                                }
                            }
                        }
                        let elapsed = req_start.elapsed();
                        let preview: String = if raw {
                            output.chars().take(200).collect()
                        } else {
                            output.chars().take(80).collect()
                        };
                        (i, true, preview, output.len(), elapsed)
                    }
                    Err(e) => {
                        let elapsed = req_start.elapsed();
                        eprintln!("[请求{} 失败] {}", i, e);
                        (i, false, String::new(), 0, elapsed)
                    }
                }
            }
        })
        .collect();

    let results = join_all(futures).await;
    let total_elapsed = start.elapsed();

    let success_count = results.iter().filter(|(_, ok, _, _, _)| *ok).count();
    for (i, ok, preview, total_len, elapsed) in results {
        let status = if ok { "成功" } else { "失败" };
        let suffix = if ok && !raw && total_len > 80 {
            format!(" ({} bytes)", total_len)
        } else {
            String::new()
        };
        println!(
            "  [请求{:2}] {} | {:>12?} | {}{}",
            i,
            status,
            elapsed,
            if preview.is_empty() {
                "(无输出)".to_string()
            } else {
                format!("{}...", preview.replace('\n', " "))
            },
            suffix
        );
    }
    println!(
        "  总计: {}/{} 成功 | 总耗时 {:?}",
        success_count, count, total_elapsed
    );
}

/// 解析 chat/concurrent 命令参数
fn parse_chat_args(rest: &str) -> (String, bool, bool) {
    let tokens: Vec<&str> = rest.split_whitespace().collect();

    let mut thinking = false;
    let mut search = false;
    let mut prompt_end = tokens.len();

    if let Some(last) = tokens.last() {
        if is_bool(last) && tokens.len() >= 2 {
            search = parse_bool(last);
            prompt_end -= 1;

            if let Some(second_last) = tokens.get(tokens.len() - 2) {
                if is_bool(second_last) {
                    thinking = parse_bool(second_last);
                    prompt_end -= 1;
                }
            }
        } else if is_bool(last) {
            thinking = parse_bool(last);
            prompt_end -= 1;
        }
    }

    let prompt = tokens[..prompt_end].join(" ");
    (prompt, thinking, search)
}

/// 从命令尾部提取 --raw / -r flag
fn extract_raw_flag(rest: &str) -> (&str, bool) {
    let trimmed = rest.trim_end();
    if let Some(prefix) = trimmed.strip_suffix("--raw") {
        (prefix.trim_end(), true)
    } else if let Some(prefix) = trimmed.strip_suffix("-r") {
        (prefix.trim_end(), true)
    } else {
        (rest, false)
    }
}

fn is_bool(s: &str) -> bool {
    matches!(
        s.to_lowercase().as_str(),
        "true" | "false" | "t" | "f" | "1" | "0"
    )
}

fn parse_bool(s: &str) -> bool {
    matches!(s.to_lowercase().as_str(), "true" | "t" | "1")
}

/// 处理单行命令
async fn handle_line(
    line: &str,
    core: &DeepSeekCore,
    model_type: &mut String,
) -> anyhow::Result<bool> {
    let parts: Vec<&str> = line.split_whitespace().collect();
    if parts.is_empty() {
        return Ok(false);
    }

    match parts[0] {
        "status" => {
            println!("[账号状态]");
            for (i, s) in core.account_statuses().iter().enumerate() {
                let email = if s.email.is_empty() { "-" } else { &s.email };
                let mobile = if s.mobile.is_empty() { "-" } else { &s.mobile };
                println!("  [{}] {} / {}", i + 1, email, mobile);
            }
        }

        "model" if parts.len() >= 2 => {
            *model_type = parts[1].to_string();
            println!("[当前模型已切换为: {}]", model_type);
        }

        "chat" if parts.len() >= 2 => {
            let rest = line.strip_prefix("chat").unwrap().trim();
            let (rest, raw) = extract_raw_flag(rest);
            let (prompt, thinking, search) = parse_chat_args(rest);

            println!(
                "[请求] prompt={:?}, model_type={}, thinking={}, search={}, raw={}",
                prompt.chars().take(50).collect::<String>(),
                model_type,
                thinking,
                search,
                raw
            );

            match core
                .v0_chat(ChatRequest {
                    prompt,
                    thinking_enabled: thinking,
                    search_enabled: search,
                    model_type: model_type.clone(),
                })
                .await
            {
                Ok(mut stream) => {
                    if raw {
                        println!("[响应流开始 - raw]");
                        let mut stdout = io::stdout();
                        while let Some(chunk) = stream.next().await {
                            let chunk: Result<Bytes, _> = chunk;
                            match chunk {
                                Ok(bytes) => {
                                    print!("{}", String::from_utf8_lossy(&bytes));
                                    stdout.flush()?;
                                }
                                Err(e) => {
                                    eprintln!("\n[流错误] {}", e);
                                    break;
                                }
                            }
                        }
                        println!("\n[响应流结束 - raw]");
                    } else {
                        let mut output = String::new();
                        while let Some(chunk) = stream.next().await {
                            match chunk {
                                Ok(bytes) => output.push_str(&String::from_utf8_lossy(&bytes)),
                                Err(e) => {
                                    eprintln!("\n[流错误] {}", e);
                                    break;
                                }
                            }
                        }
                        let preview: String = output.chars().take(300).collect();
                        let truncated = if output.len() > 300 {
                            format!("{}... (完整 {} bytes)", preview, output.len())
                        } else {
                            preview
                        };
                        println!("[响应] {}", truncated);
                    }
                }
                Err(e) => {
                    eprintln!("[请求失败] {}", e);
                }
            }
        }

        "concurrent" if parts.len() >= 3 => {
            let count: usize = match parts[1].parse() {
                Ok(n) if n > 0 => n,
                _ => {
                    eprintln!("[错误] 并发数必须是正整数");
                    return Ok(false);
                }
            };

            let rest = line.strip_prefix("concurrent").unwrap().trim();
            let rest = rest.strip_prefix(parts[1]).unwrap().trim();
            let (rest, raw) = extract_raw_flag(rest);
            let (prompt, thinking, search) = parse_chat_args(rest);

            run_concurrent(core, count, prompt, model_type, thinking, search, raw).await;
        }

        "quit" | "exit" => return Ok(true),

        _ => println!("[未知命令: {}]", parts[0]),
    }

    Ok(false)
}
