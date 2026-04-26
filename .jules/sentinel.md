## 2025-04-25 - Prevent Timing Attacks in Token Validation
**Vulnerability:** API tokens were being compared using simple string equality (`t == token`), which fails fast on the first mismatched character. This is vulnerable to timing attacks where an attacker could deduce valid API tokens by observing the response times.
**Learning:** For cryptographic secrets like API tokens, passwords, or hashes, timing variations can expose the secret value.
**Prevention:** Always use constant-time comparison algorithms (like `subtle::ConstantTimeEq`) when comparing secrets to prevent leaking information through execution time.

## 2025-04-25 - Completely Eliminate Length-Based Timing Leaks
**Vulnerability:** A length check before a constant-time comparison leaks the length of the tokens, preventing full mitigation against timing attacks.
**Learning:** Comparing tokens using simple length variations and `ct_eq` does not completely hide string patterns, allowing adversaries to discover token lengths through response discrepancies.
**Prevention:** Rather than directly matching strings character-by-character or via padded constraints, we must hash both string tokens (e.g. `sha2::Sha256::digest()`) and execute `ct_eq()` on the resulting constant-length hashes.

## 2025-04-25 - Prevent DoS with Timeout and HTTP Headers
**Vulnerability:** Upstream HTTP requests via `reqwest` lacked explicit timeouts, which could lead to unbounded resource consumption (DoS) if the upstream endpoint hangs or becomes unresponsive. Axum handlers were also lacking essential security headers, making the responses vulnerable to Clickjacking and MIME sniffing.
**Learning:** Default HTTP client configurations usually lack robust timeouts. Explicit timeouts (e.g. `connect_timeout` or `timeout`) must always be configured for outbound external API calls. Similarly, Axum doesn't automatically inject security headers; these need to be manually implemented via middleware.
**Prevention:**
1. Configure `connect_timeout()` (and `timeout()` if appropriate for the request lifecycle) when building `reqwest::Client`.
2. Add a global axum middleware using `middleware::from_fn` that sets security headers like `X-Content-Type-Options`, `X-Frame-Options`, `X-XSS-Protection`, and `Strict-Transport-Security`.
