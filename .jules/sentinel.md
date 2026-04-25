## 2025-04-25 - Prevent Timing Attacks in Token Validation
**Vulnerability:** API tokens were being compared using simple string equality (`t == token`), which fails fast on the first mismatched character. This is vulnerable to timing attacks where an attacker could deduce valid API tokens by observing the response times.
**Learning:** For cryptographic secrets like API tokens, passwords, or hashes, timing variations can expose the secret value.
**Prevention:** Always use constant-time comparison algorithms (like `subtle::ConstantTimeEq`) when comparing secrets to prevent leaking information through execution time.
