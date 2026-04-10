# Core 模块文档

Core 是项目的基础设施层，负责：

- 配置读取与持久化
- 登录态管理
- PoW 计算
- 本地接口鉴权
- 启动期安全提示
- 会话状态存储

## 1. `config.py` - 配置管理

### 当前配置模型

项目支持两类来源：

1. `config.toml`
   适合本地开发或兼容旧配置
2. 环境变量
   适合生产环境，尤其是敏感配置

生产环境推荐三段式运行模型：

```text
runtime/config.toml
runtime/app.env
runtime/deepseek-session.token
```

其中：

- `runtime/config.toml`
  保存监听、CORS、`auth.required`、session pool 等非敏感配置
- `runtime/app.env`
  保存 Bearer token、DeepSeek 账号、请求头指纹等敏感配置
- `runtime/deepseek-session.token`
  保存运行期自动刷新的 DeepSeek 登录态

### 主要能力

- 读取 `CONFIG_PATH` 指向的 TOML 配置
- 兼容 `auth.tokens` 字符串数组与 token table 写法
- 支持 `server.api_key` 旧版单 token 兼容模式
- 支持 `DEEPSEEK_WEB_AUTH_TOKENS_JSON`、`DEEPSEEK_WEB_AUTH_TOKEN`
- 支持 `DEEPSEEK_ACCOUNT_EMAIL`、`DEEPSEEK_ACCOUNT_PASSWORD` 等账号环境变量
- 支持 `DEEPSEEK_BASE_HEADERS_JSON`
- 支持 `DEEPSEEK_BROWSER_IMPERSONATE`
- 支持 `DEEPSEEK_ACCOUNT_TOKEN_PATH` 将登录态单独持久化

### 关键导出

| 方法/变量 | 说明 |
|------|------|
| `CONFIG` | 模块加载时缓存的 TOML 配置 |
| `CONFIG_PATH` | 配置文件路径 |
| `ACCOUNT_TOKEN_PATH` | 登录态 token 文件路径 |
| `load_config()` | 从磁盘读取 TOML |
| `save_config(cfg)` | 将配置写回磁盘 |
| `get_base_headers()` | 获取当前生效的上游请求头 |
| `get_default_impersonate()` | 获取当前生效的浏览器伪装 |
| `get_account_config()` | 汇总 TOML、环境变量与 token 文件后的账号配置 |
| `get_persisted_account_token()` | 获取当前持久化登录态 |
| `persist_account_token(token)` | 持久化登录态 |
| `clear_persisted_account_token()` | 清除持久化登录态 |
| `get_auth_token_entries()` | 获取完整鉴权 token 条目 |
| `get_auth_tokens()` | 获取启用中的本地鉴权 token 值列表 |
| `get_enabled_auth_tokens()` | 同 `get_auth_tokens()` |
| `get_auth_required()` | 读取 `auth.required` |
| `has_effective_auth_tokens()` | 判断是否存在有效 token |
| `get_auth_mode_summary()` | 返回当前鉴权模式摘要 |
| `get_pool_size()` | 读取 session pool 上限 |
| `get_pool_acquire_timeout()` | 读取 session pool 等待超时 |
| `get_server_host()` | 读取监听 host |
| `get_server_port()` | 读取监听 port |
| `get_server_reload()` | 读取 reload 配置 |
| `get_cors_origins()` | 读取 CORS origins |
| `get_cors_origin_regex()` | 读取 CORS origin regex |
| `get_cors_allow_credentials()` | 读取 CORS credentials |
| `get_cors_allow_methods()` | 读取 CORS methods |
| `get_cors_allow_headers()` | 读取 CORS headers |
| `BASE_HEADERS` | 模块级当前请求头快照 |
| `DEFAULT_IMPERSONATE` | 模块级当前浏览器伪装快照 |

## 2. `auth.py` - DeepSeek 登录态管理

### 实现要点

- 懒加载：启动时不主动登录
- 首次调用 `get_token()` 才尝试读取 token 或登录
- 优先顺序：
  1. 内存中的 `_account["token"]`
  2. `DEEPSEEK_ACCOUNT_TOKEN`
  3. `DEEPSEEK_ACCOUNT_TOKEN_PATH` 指向的文件
  4. `config.toml` 中的 `[account].token`
  5. 重新登录
- 登录成功后优先写入 token 文件；没有配置 token 文件时才回写 `config.toml`
- 收到上游认证错误时通过 `invalidate_token()` 清空内存和持久化 token

### 导出方法

| 方法 | 说明 |
|------|------|
| `init_single_account()` | 初始化账号配置，不主动登录 |
| `login()` | 调用 DeepSeek 登录接口获取新 token |
| `invalidate_token()` | 使当前 token 失效 |
| `get_token()` | 获取当前 token，必要时自动登录 |
| `get_auth_headers()` | 获取带 Authorization 的上游请求头 |

## 3. `local_api_auth.py` - 本地接口鉴权

### 实现要点

本模块保护的是 `/v0/*` 和 `/v1/*` 这些“你自己的代理接口”，不是 DeepSeek 上游接口。

支持：

- `Authorization: Bearer <token>`
- `X-API-Key: <token>`

鉴权逻辑：

- 若 `auth.required = false` 且没有任何有效 token，则本地接口开放
- 只要 `auth.required = true`，请求必须带有效 token
- 若配置了 token，即使 `auth.required = false`，带错 token 也会返回 `401`

### 导出方法

| 方法 | 说明 |
|------|------|
| `requires_local_api_auth(path)` | 判断路径是否需要本地接口鉴权 |
| `verify_local_api_auth(request)` | 校验 Bearer token / X-API-Key |

## 4. `server_security.py` - 启动期安全检查

### 实现要点

- 启动时检查监听地址是否为 loopback
- 非 loopback 且无本地接口鉴权时，直接 fail-fast 退出
- 输出当前鉴权模式摘要
- 输出常见风险提示，例如：
  - `auth.required = true` 但没有有效 token
  - CORS 仍为 `*`
  - 服务监听在非 loopback 地址

### 导出方法

| 方法 | 说明 |
|------|------|
| `is_loopback_host(host)` | 判断 host 是否为 loopback |
| `validate_startup_config()` | 启动前做 fail-fast 安全检查 |
| `collect_startup_security_warnings()` | 汇总告警文本 |
| `log_startup_security_warnings()` | 输出启动期安全日志 |

## 5. `pow.py` - PoW 工作量证明

### 实现要点

DeepSeek 网页端 API 要求大部分写操作附带 PoW。

流程：

1. 调用 `/api/v0/chat/create_pow_challenge`
2. 用 WASM 计算答案
3. 组装并 base64 编码后作为 `x-ds-pow-response`

特性：

- 首次加载后缓存 WASM 模块
- 计算时复用缓存，避免重复初始化
- 若获取 challenge 时发现 token 失效，会自动触发 token 刷新并重试

### 导出方法

| 方法 | 说明 |
|------|------|
| `compute_pow_answer(...)` | 纯计算 PoW 答案 |
| `get_pow_response(target_path)` | 获取可直接用于请求头的 PoW 响应 |

## 6. `parent_msg_store.py` - 连续对话状态

### 实现要点

用于维护：

```text
chat_session_id -> parent_message_id
```

场景：

- `/v0/chat/completion` 连续对话
- 根据上一次响应的 `response_message_id` 推导下一次请求的 `parent_message_id`

实现特性：

- 单例模式
- `asyncio.Lock` 保护
- 所有读写接口均为异步

### 导出方法

| 方法 | 说明 |
|------|------|
| `get_instance()` | 获取单例 |
| `aget_instance()` | 异步获取单例 |
| `acreate(session_id)` | 创建 session 记录 |
| `aget_parent_message_id(session_id)` | 获取 parent_message_id |
| `aupdate_parent_message_id(session_id, message_id)` | 更新 parent_message_id |
| `adelete(session_id)` | 删除 session |
| `ahas(session_id)` | 判断 session 是否存在 |
| `aget_all()` | 获取全部 session ID |

## 7. `logger.py` - 日志输出

### 实现要点

- 使用 Python 标准 `logging`
- 自定义彩色 formatter
- 日志级别由 `config.py` 读取

### 导出

| 方法/变量 | 说明 |
|------|------|
| `setup_logger(name, level)` | 初始化 logger |
| `logger` | 默认 logger 实例 |

## 开发约定

1. Core 层只做基础设施，不塞业务编排
2. API/Service 层负责把 Core 能力拼成业务流程
3. 路由层尽量保持轻薄，只做 HTTP 解析和响应封装
