# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased] — Security Hardening Sprint

*Note: Coffer was born from pryscraper's payment system.*

### Security
- fail-closed pricing
- operation gating
- settlement idempotency
- fsync
- thread lock
- facilitator TTL
- pending_auths cleanup
- network validation
- TRON replay
- webhook HMAC
- SSRF guard
- redirect disable
- empty-secret refuse
- dead-letter redaction
- TRON contract filter
- atomic compare
- caller-binding
- batch caller_key

### Performance
- rate limit degraded cap
- LRU
- lazy loading
- store fsync
- batch expiry
- import fix

### Resilience
- EVM RPC fallback
- Solana payer binding
- health-score routing

## [Unreleased]
