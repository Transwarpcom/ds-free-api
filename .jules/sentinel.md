## 2025-04-25 - Prevent Timing Attacks in Token Validation
**Vulnerability:** API tokens were being compared using simple string equality (`t == token`), which fails fast on the first mismatched character. This is vulnerable to timing attacks where an attacker could deduce valid API tokens by observing the response times.
**Learning:** For cryptographic secrets like API tokens, passwords, or hashes, timing variations can expose the secret value.
**Prevention:** Always use constant-time comparison algorithms (like `subtle::ConstantTimeEq`) when comparing secrets to prevent leaking information through execution time.

## 2025-04-25 - Completely Eliminate Length-Based Timing Leaks
**Vulnerability:** A length check before a constant-time comparison leaks the length of the tokens, preventing full mitigation against timing attacks.
**Learning:** Comparing tokens using simple length variations and `ct_eq` does not completely hide string patterns, allowing adversaries to discover token lengths through response discrepancies.
**Prevention:** Rather than directly matching strings character-by-character or via padded constraints, we must hash both string tokens (e.g. `sha2::Sha256::digest()`) and execute `ct_eq()` on the resulting constant-length hashes.
