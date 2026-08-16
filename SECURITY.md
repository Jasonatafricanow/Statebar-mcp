# Security Policy

## Supported Versions

| Version | Supported          |
| ------- | ------------------ |
| 0.1.x   | :white_check_mark: |

statebar-mcp is a young project (first release 0.1.x). Only the latest
release receives security fixes. No version before 0.1.0 exists.

## Reporting a Vulnerability

Please **do not open a public issue** for security problems.

- Open a private security advisory on GitHub:
  **Security → Report a vulnerability** on the repository, or
- Contact the maintainer directly via the repository owner's GitHub profile.

You can expect:

- an acknowledgement within 5 working days;
- a fix or a documented workaround for confirmed issues in the next release;
- credit in the release notes unless you ask to stay anonymous.

## Threat Model & Trust Boundaries

statebar-mcp stores **personal user state** (health symptoms, sleep, plans)
in a local SQLite database and exposes it over transports. The guarantees
below are the design contract; please report any violation.

1. **Local-first**: the service is designed for a single machine, default
   bind `127.0.0.1`. Exposing it beyond the machine is an operator decision.
2. **Fail-closed REST**: binding a non-loopback address WITHOUT an auth
   token is refused at startup. With a token configured, every endpoint
   (including `/v1/health`) requires `Authorization: Bearer <token>`
   (constant-time compare). Request bodies are capped (default 64 KiB).
3. **No telemetry**: nothing is ever sent anywhere except the operator's
   explicitly configured LLM endpoint (opt-in via `DSH_USER_STATE_LLM_ENABLED`);
   even then only the extractor prompt + the single user message are sent —
   never assistant messages, identifiers, history, or database contents.
4. **LLM cannot write state**: model output passes a deterministic validator
   before reconciliation; provenance is caller-owned; assistant-only content
   can never create canonical state (anti-self-pollution, S1).
5. **Idempotency & ordering**: event/observation-level uniqueness; conflict
   ordering by semantic `observed_at` (delayed messages cannot roll back
   newer state); recoverable event status (pending → sync_committed →
   complete/failed, failed events resume on re-observe).

Out of scope (not guarantees): confidentiality against a malicious local
user with OS-level access to the database file (encrypt at rest yourself),
multi-tenant isolation, and network DoS protection beyond the body cap.
