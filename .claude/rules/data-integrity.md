---
paths:
  - "data/**"
---

# Data integrity

Do not edit files under data directly. Use the update, process, and compact CLI
commands so schema validation, partitioning, and deduplication remain intact.
Publishing with push-hf always requires an explicit user request.

Use `AGENT_POLICY_AMENDMENT=1` only for an explicitly authorized protected-path
change. Set `AGENT_EXTERNAL_EFFECT_AUTHORITY` only to the exact token printed by
`python scripts/agent_guard.py --print-command-authority "<command>"` after the
current user authorizes that mutation. It authorizes no other command and never
bypasses force-push, secret, destructive, publication, or protected-path rules.
For an MCP/app mutation, pipe the exact hook payload JSON into `python
scripts/agent_guard.py --print-payload-authority`; PowerShell BOM/UTF-16 input is accepted.
