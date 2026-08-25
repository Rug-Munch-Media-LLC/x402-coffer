# Coffer — FAQ

**What is Coffer?** A self-hosted x402 payment facilitator: add a one-line HTTP 402 paywall to any API and let AI agents pay per call.

**What is x402?** An open HTTP-native payments protocol (402 Payment Required) — machine-to-machine micropayments without accounts or API keys.

**Which chains are supported?** EVM + Solana verification built in; scheme handlers are pluggable.

**How do I paywall my endpoint?** Wrap your route with the Coffer middleware and declare a price — see README quickstart. Agents receive a 402 challenge, pay on-chain, retry with proof.

**Does it need a facilitator service?** Coffer IS the facilitator — self-hosted, no third party. External verify mode also supported.

**How does it relate to rmi-mcp-x402?** Coffer settles payments; rmi-mcp-x402 is the MCP actor marketplace that consumes Coffer for agent payments.
