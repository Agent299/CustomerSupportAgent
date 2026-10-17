# ADR 0016: Host everything on one Oracle Cloud Always Free VM

- Status: Accepted
- Date: 2026-10-17
- Supersedes: [ADR 0014](0014-render-and-free-datastores.md)

## Context

ADR 0014 placed compute on Render's free tier and state on separate free managed services (Neon, Aiven OpenSearch, Neo4j AuraDB, Cloudflare R2), with ingestion in GitHub Actions. That meant:

- seven vendors;
- a 512 MB / 0.1 CPU API that sleeps after 15 minutes and takes about a minute to wake;
- no free background workers, and quota-bound datastores;
- cross-site cookie problems on `*.onrender.com`.

The owner now has an Oracle Cloud account. Its Always Free tier includes an **Ampere A1 VM (arm64, 2 OCPU / 12 GB RAM)** and 200 GB of block storage. Measured locally on arm64, the full stack idles at about **2.8 GB**.

## Decision

Run the whole system on one A1 VM with Docker Compose (`deploy/oracle/`):

- **Caddy** terminates TLS automatically (domain or `<ip>.sslip.io`), serves the static UI and proxies `/api` on the **same origin**.
- **Postgres, Valkey, Redpanda, OpenSearch and Neo4j** run self-hosted on the internal network with no published ports. Each container has a memory cap; the caps total about 10.5 GB.
- **Ingestion is event-driven for real:** `chunk-worker`, `index-worker` and `relay` (outbox → Redpanda) run continuously. systemd timers run the nightly `schedule`, the Postgres backup and `gc`.
- **CD:** after CI passes on `main`, `.github/workflows/deploy.yml` SSHes in, checks out the exact commit and runs `deploy.sh` (build → migrate → rolling `up --wait` → readiness check). It skips until the `OCI_*` secrets exist.
- CI builds all images on a **native arm64 runner** and validates the production compose file, Caddyfile and scripts.

## Consequences

- The architecture runs as designed: real Kafka-API topics, OpenSearch and Neo4j with no quotas, and no cold starts.
- One origin removes CORS and third-party-cookie issues for Phase 4 auth.
- A single VM is a single point of failure, and Oracle can change Always Free terms (as it did in June 2026). Mitigations:
  - Postgres is backed up nightly;
  - OpenSearch and Neo4j are rebuildable from it (`reindex`);
  - the VM is reproducible from the repo in about an hour (`bootstrap.sh` + `deploy.sh`).
- Redpanda runs in `dev-container` mode (relaxed fsync). This is acceptable because topics are transient work queues regenerable from Postgres.
- The quota-driven sizing worries in ADRs 0005, 0010 and 0012 no longer apply. The designs themselves (chunk text in Postgres, latency budgets, version dedupe) stand on their own merits.
