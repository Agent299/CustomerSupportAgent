# Deploying to Oracle Cloud (Always Free)

The whole system runs on one **Ampere A1** VM: arm64, **2 OCPU / 12 GB RAM**, which is the current Always Free allowance. It uses Docker Compose with Caddy at the edge. Design rationale: [ADR 0016](adr/0016-oracle-cloud-single-vm.md) and [PLAN §9](PLAN.md#9-deployment-and-hosting).

```
Internet ──80/443──▶ Caddy ──▶ static UI + /api → api
                      (internal) postgres · valkey · redpanda · opensearch · neo4j
                      workers: chunk-worker · index-worker · relay
                      timers:  02:00 schedule · 03:30 backup · 05:00 gc  (UTC)
```

## 1. Create the VM (OCI console)

1. **Compute → Instances → Create instance**
   - **Image:** Canonical Ubuntu **24.04**, the aarch64 build.
   - **Shape:** Ampere → `VM.Standard.A1.Flex`, **2 OCPUs, 12 GB** memory. It should show the "Always Free-eligible" badge.
   - **Networking:** create or choose a VCN with a **public subnet**, and assign a public IPv4 address.
   - **SSH keys:** upload your public key.
   - **Boot volume:** 100 GB is plenty (200 GB is free in total).
   - If you get *"Out of host capacity"*, try another availability domain or retry later.
2. **Open HTTP/HTTPS in the VCN:** go to *Networking → Virtual cloud networks → your VCN → Security lists → Default* and add ingress rules for source `0.0.0.0/0`:
   - TCP **80**
   - TCP **443**
   - UDP **443** (optional, for HTTP/3)
3. Note the instance's **public IP**.

## 2. Pick the public address

- **With a domain:** create an `A` record, e.g. `support.example.com → <public-ip>`, and set `SITE_ADDRESS=support.example.com`.
- **Without a domain:** use `SITE_ADDRESS=<ip-with-dashes>.sslip.io`, e.g. `203-0-113-10.sslip.io`. It resolves to your IP, and Caddy still gets a real certificate.

Caddy obtains and renews certificates automatically once ports 80 and 443 are reachable.

## 3. Bootstrap and first deploy (on the VM)

```bash
ssh ubuntu@<public-ip>
curl -fsSL https://raw.githubusercontent.com/Agent299/CustomerSupportAgent/main/deploy/oracle/bootstrap.sh | bash
exit && ssh ubuntu@<public-ip>            # re-login so the docker group applies

nano /opt/support-agent/deploy/oracle/.env  # fill in SITE_ADDRESS, passwords, OPENAI_API_KEY
/opt/support-agent/deploy/oracle/deploy.sh  # build, migrate, start, readiness check
```

`bootstrap.sh` is idempotent. It does the following:
- installs Docker from Docker's apt repository;
- sets `vm.max_map_count` for OpenSearch;
- adds 4 GB of swap;
- opens 80/443 in the host firewall (Oracle's Ubuntu image rejects all but SSH by default);
- clones the repo to `/opt/support-agent`;
- installs the systemd timers;
- creates `deploy/oracle/.env` (mode 600) from the example.

Generate passwords with `openssl rand -base64 32`.

**Initial knowledge-base load.** This takes about 50 minutes at 2 requests/s, and the full embedding backfill costs about $0.10.

```bash
cd /opt/support-agent/deploy/oracle
docker compose -f docker-compose.prod.yml run --rm --no-deps chunk-worker schedule
docker compose -f docker-compose.prod.yml logs -f chunk-worker index-worker   # watch progress
```

The always-on workers consume the events. The nightly timer then keeps the knowledge base fresh.

## 4. Continuous deployment (GitHub Actions)

After a PR merges and CI passes on `main`, `.github/workflows/deploy.yml` SSHes to the VM, checks out that exact commit, and runs `deploy.sh`. It **skips with a notice** until these secrets exist.

On the VM, create a dedicated deploy key:
```bash
ssh-keygen -t ed25519 -N "" -f ~/.ssh/github_deploy -C github-actions-deploy
cat ~/.ssh/github_deploy.pub >> ~/.ssh/authorized_keys
cat ~/.ssh/github_deploy          # → OCI_SSH_PRIVATE_KEY (then delete this file from the VM)
```
From your laptop, run `ssh-keyscan -t ed25519 <public-ip>` to get `OCI_SSH_KNOWN_HOSTS`.

In GitHub, go to **Settings → Environments → `production`** and add these secrets:

| Secret | Value |
|---|---|
| `OCI_HOST` | VM public IP (or DNS name) |
| `OCI_SSH_USER` | `ubuntu` |
| `OCI_SSH_PRIVATE_KEY` | contents of `github_deploy` |
| `OCI_SSH_KNOWN_HOSTS` | output of `ssh-keyscan` |

Optionally, add *required reviewers* to the `production` environment to approve each deploy. You can also deploy manually: **Actions → Deploy (Oracle Cloud) → Run workflow**.

## 5. Operations

All commands run in `/opt/support-agent/deploy/oracle`, with `c() { docker compose -f docker-compose.prod.yml "$@"; }`.

| Task | Command |
|---|---|
| Status / memory | `c ps` · `docker stats --no-stream` |
| Logs | `c logs -f --tail=100 api` (or `chunk-worker`, `index-worker`, `relay`, `caddy`) |
| Ingest one page now | `curl -X POST https://$SITE/api/ingest/collect -H "X-Ingest-Token: …" -H 'content-type: application/json' -d '{"source_id":"x","url":"https://www.mongodb.com/docs/manual/…/","type":"DOC"}'` (picked up within 60 s) |
| Full re-crawl | `c run --rm --no-deps chunk-worker schedule` |
| Rebuild the search index from Postgres | `c run --rm --no-deps chunk-worker reindex` |
| Timers | `systemctl list-timers 'support-agent-*'` · `journalctl -u support-agent-schedule` |
| Backup now | `./backup.sh` (dumps go to `/opt/support-agent/backups`, 7-day retention) |
| Restore | `c exec -T postgres pg_restore -U support -d support --clean --if-exists < ../../backups/<file>.dump` then `reindex` |
| Rotate a secret | edit `.env`, then `c up -d` (containers are recreated with the new value) |
| Roll back | `git -C /opt/support-agent checkout --detach <sha> && ./deploy.sh` |

**Off-VM backups (recommended).** Copy `/opt/support-agent/backups` somewhere else periodically, e.g. to an Always Free OCI Object Storage bucket with the OCI CLI. The dumps are small: a few MB.

## 6. Things to know

- **Idle reclamation:** Oracle may reclaim an Always Free VM whose CPU, network **and** memory all stay under 20% for 7 days. OpenSearch and Neo4j keep memory well above that.
- **Rebuilding from scratch** takes about an hour: create the VM → `bootstrap.sh` → restore `.env` → `deploy.sh` → restore the latest backup → `reindex`.
- **Not exposed:** the datastores have no published ports. Use `c exec` for admin access (e.g. `c exec postgres psql -U support`).
