# Development Plan: homelab-apps (feat/aftertouch-deployment branch)

*Generated on 2026-05-11 by Vibe Feature MCP*
*Workflow: [epcc](https://codemcp.github.io/workflows/workflows/epcc)*

## Goal
Deploy **AfterTouch** (Bose SoundTouch local cloud replacement) to the homelab Kubernetes cluster as a new Pulumi app under `apps/aftertouch`, following the same structure as `apps/lobehub`.

## Key Decisions
- **Image**: `ghcr.io/gesellix/bose-soundtouch:v0.76.0` (pinned)
- **Hostname**: `aftertouch.flinker.fritz.box` (local network only, no Cloudflare tunnel)
- **Auth**: `AuthType.NONE` — no OAuth proxy, local network access only
- **Ports**: 8000 (HTTP web UI + speaker API) and 8443 (HTTPS for speaker TLS redirect)
  - The web UI and speaker communication both use port 8000; HTTPS on 8443 is required for DNS redirect method
  - `createExposedWebApp` exposes the primary port (8000); port 8443 needs an additional K8s Service
- **Storage**: 500Mi PVC at `/app/data`, `longhorn-uncritical` storage class
- **No database** needed — AfterTouch is stateless except for the data volume
- **Root**: use `securityContext.allowRoot: true` (upstream image likely runs as root)
- **Domain**: hardcoded `aftertouch.flinker.fritz.box` — NOT derived from homelab stack (no Cloudflare tunnel for local-only apps)
- **MGMT credentials**: `MGMT_USERNAME` / `MGMT_PASSWORD` stored as a K8s Secret, mounted via `envFrom`
- **CI secret name**: `KUBECONFIG_AFTERTOUCH` (per-app pattern from the skill)
- **Homelab SHA** (for workflow pin): `01381eee8c089d1e860050bf46a4da0258b8732a` (latest as of 2026-05-11, commit: feat(scripts): add generate-ci-kubeconfig.sh)
- **homelabStack reference still needed**: even with `AuthType.NONE` and no Cloudflare, `createHomelabContextFromStack` is still called — the stack ref is used internally by `createExposedWebApp` for Traefik IngressRoute config. Config key `aftertouch:homelabStack` defaults to `mrsimpson/homelab/dev`.
- **ExposedWebApp not used**: dropped in favour of raw K8s resources (Namespace, Secret, PVC, Deployment, 2×NodePort Service). The cluster uses Cloudflare Tunnel for internet traffic; Traefik has no hostPort/LoadBalancer on the LAN. `ExposedWebApp` creates an HTTPRoute/IngressRoute that is only reachable via cloudflared, making it useless for local-only apps.
- **LAN access pattern**: Two NodePort services expose the app directly on the node (`192.168.13.5`): port 30800 (HTTP) and 30843 (HTTPS). Fritz.Box DNS entry required: `aftertouch.flinker.fritz.box → 192.168.13.5`. No Traefik IngressRoute needed.
- **hostNetwork: true**: Required for SSDP/UPnP multicast discovery on the LAN. Without it, the pod cannot send/receive `239.255.255.250:1900` multicast and speakers are not discovered. Pod is pinned to `flinker` node via `nodeSelector`. Pod IP becomes `192.168.13.5` (node IP).
- **Services**: Three services deployed — ClusterIP (`aftertouch`) for in-cluster DNS (`aftertouch.aftertouch.svc.cluster.local`), NodePort HTTP (30800), NodePort HTTPS (30843).
- **soundtouch-web UI sidecar**: `ghcr.io/gesellix/bose-soundtouch-web:0.77` added as second container. Image: `ghcr.io/gesellix/bose-soundtouch-web`. Both containers share network namespace via `hostNetwork`, so web UI reaches backend at `localhost:8000`.
- **hostNetwork port conflicts**: Port 8000+8443 taken by Traefik (declared but hostPort=0, not actually bound). Port 8080 taken by llama.cpp running directly on the host. Web UI therefore runs on port 9090 via `-port 9090` arg.
- **Deployment strategy**: Must be `Recreate` (not `RollingUpdate`) because hostNetwork pods bind real host ports — two pods cannot run simultaneously. Required patching the live deployment to remove `rollingUpdate` before Pulumi could apply the change.
- **NodePort for web UI**: 30909 → container port 9090. Web UI confirmed serving HTML at `http://192.168.13.5:30909/`, speakers discovered automatically.

## Notes
### What is needed end-to-end to deploy a new app
Based on reading `skills/deploy-homelab-app/SKILL.md`, `README.md`, and the lobehub reference:

1. **Pulumi stack files** (`apps/aftertouch/`):
   - `package.json` — npm workspace member
   - `tsconfig.json` — extends root tsconfig
   - `Pulumi.yaml` — project definition
   - `Pulumi.dev.yaml` — stack config (image, hostname, secrets)
   - `src/index.ts` — Pulumi program

2. **GitHub Actions workflow** (`.github/workflows/deploy-aftertouch.yml`):
   - Calls `mrsimpson/homelab/.github/workflows/deploy-to-cluster.yml` (pinned SHA)
   - Uses `KUBECONFIG_AFTERTOUCH` secret (per-app, namespace-scoped)

3. **Pulumi stack created** (CLI step, then pushed state to Pulumi Cloud):
   - `pulumi stack select --create mrsimpson/aftertouch/dev`

4. **CI RBAC + kubeconfig** (**manual step by user** — cannot be automated):
   - Run `create-kubeconfig.sh aftertouch` from the homelab repo
   - Upload result as `KUBECONFIG_AFTERTOUCH` GitHub secret

5. **K8s Secret** for MGMT credentials:
   - `MGMT_USERNAME` and `MGMT_PASSWORD` — set via `pulumi config set --secret`

### Additional port (8443)
`createExposedWebApp` creates one Service + IngressRoute for the primary port (8000).
Port 8443 (HTTPS) needs a separate raw `k8s.core.v1.Service` with a NodePort or LoadBalancer,
OR we expose it via an additional Traefik IngressRoute TCP route.
The simplest approach: add a second `k8s.core.v1.Service` of type `NodePort` for 8443,
pointing to the same pod — speakers on the LAN can reach it directly via `flinker.fritz.box:8443`.

## Explore
<!-- beads-phase-id: homelab-apps-1.1 -->
### Tasks
<!-- beads-synced: 2026-05-12 -->
*Auto-synced — do not edit here, use `bd` CLI instead.*

- [x] `homelab-apps-1.1.1` Scaffold apps/aftertouch directory with Pulumi.yaml and package.json
- [x] `homelab-apps-1.1.2` Write src/index.ts — Pulumi stack deploying AfterTouch
- [x] `homelab-apps-1.1.3` Write Pulumi.dev.yaml — stack config for dev environment
- [x] `homelab-apps-1.1.4` Write tsconfig.json

## Plan
<!-- beads-phase-id: homelab-apps-1.2 -->
### Tasks
<!-- beads-synced: 2026-05-12 -->
*Auto-synced — do not edit here, use `bd` CLI instead.*


## Code
<!-- beads-phase-id: homelab-apps-1.3 -->
### Tasks
<!-- beads-synced: 2026-05-12 -->
*Auto-synced — do not edit here, use `bd` CLI instead.*

- [x] `homelab-apps-1.3.1` Scaffold apps/aftertouch directory structure
- [x] `homelab-apps-1.3.10` Add NodePort service for port 8000 (HTTP web UI) for LAN access
- [x] `homelab-apps-1.3.11` Rewrite src/index.ts — raw K8s resources, no ExposedWebApp, NodePort for LAN
- [x] `homelab-apps-1.3.12` Add hostNetwork + ClusterIP service with DNS name
- [x] `homelab-apps-1.3.13` Add soundtouch-web UI sidecar container (port 8080)
- [x] `homelab-apps-1.3.2` Write src/index.ts — Pulumi stack
- [x] `homelab-apps-1.3.3` Write Pulumi.yaml and Pulumi.dev.yaml
- [x] `homelab-apps-1.3.4` Write package.json and tsconfig.json
- [x] `homelab-apps-1.3.5` Write GitHub Actions deploy workflow
- [x] `homelab-apps-1.3.6` Run npm install and type-check
- [x] `homelab-apps-1.3.7` Set Pulumi secrets and run pulumi up locally
- [x] `homelab-apps-1.3.8` Set up CI credentials (KUBECONFIG_AFTERTOUCH) — human step
- [x] `homelab-apps-1.3.9` Get latest homelab SHA and update workflow

## Commit
<!-- beads-phase-id: homelab-apps-1.4 -->
### Tasks
<!-- beads-synced: 2026-05-12 -->
*Auto-synced — do not edit here, use `bd` CLI instead.*

