---
name: deploy-homelab-app
description: Add a new personal app to the homelab-apps monorepo and deploy it to the homelab k3s cluster via GitHub Actions CI/CD. Use when the user wants to self-host a new application (e.g. Vaultwarden, Gitea, Jellyfin) on their homelab cluster using the homelab-apps pattern.
compatibility: Requires kubectl (pointing at the homelab cluster), pulumi CLI, gh CLI (authenticated), and the homelab-apps repo cloned locally. The homelab cluster must be reachable via Tailscale.
metadata:
  author: mrsimpson
  homelab-repo: https://github.com/mrsimpson/homelab
  homelab-apps-repo: https://github.com/mrsimpson/homelab-apps
---

# Deploy a New App to homelab-apps

Deploy a new self-hosted app to the homelab k3s cluster using the `homelab-apps` monorepo pattern.

## Context

- **`homelab-apps`** is a monorepo at `~/projects/homelab-apps` (or wherever the user has it cloned)
- **`homelab`** is the framework repo at `~/projects/privat/homelab` — contains scripts used below
- Apps are Pulumi TypeScript stacks under `apps/<name>/`
- CI uses `mrsimpson/homelab/.github/workflows/deploy-to-cluster.yml` (reusable workflow)
- Internet exposure: Cloudflare Tunnel (outbound-only, no open ports)
- Auth: OAuth2-Proxy in front of every app (GitHub OAuth, `group: users`)
- Secrets: GitHub Actions secrets per repo; Pulumi Cloud for stack config

## Step 1 — Scaffold the workspace

In the `homelab-apps` repo:

```bash
APP=<app-name>   # e.g. vaultwarden — lowercase, hyphens only
mkdir -p apps/$APP/src
```

**`apps/$APP/package.json`** — replace `<app-name>`:
```json
{
  "name": "@homelab-apps/<app-name>",
  "version": "0.1.0",
  "private": true,
  "main": "src/index.ts",
  "scripts": { "type-check": "tsc --noEmit" },
  "dependencies": {
    "@mrsimpson/homelab-core-components": "^0.2.2",
    "@pulumi/kubernetes": "^4.0.0",
    "@pulumi/pulumi": "^3.0.0"
  }
}
```

**`apps/$APP/tsconfig.json`**:
```json
{
  "extends": "../../tsconfig.base.json",
  "compilerOptions": { "outDir": "dist" },
  "include": ["src/**/*.ts"]
}
```

**`apps/$APP/Pulumi.yaml`** — replace `<app-name>` and `<description>`:
```yaml
name: <app-name>
description: <description>
runtime:
  name: nodejs
  options:
    typescript: true
main: src/
config:
  <app-name>:homelabStack:
    value: mrsimpson/homelab/dev
  <app-name>:image:
    value: ""   # set in Pulumi.dev.yaml
```

Install from repo root:
```bash
npm install
```

## Step 2 — Write `src/index.ts`

Minimal stack for a stateless web app. Adjust `APP_PORT` and auth as needed.

```typescript
import * as pulumi from '@pulumi/pulumi';
import * as k8s from '@pulumi/kubernetes';
import { AuthType, createHomelabContextFromStack } from '@mrsimpson/homelab-core-components';

const APP_NAME = '<app-name>';
const APP_PORT = 8080;   // adjust to the container's listen port

const cfg = new pulumi.Config(APP_NAME);
const homelabStack = new pulumi.StackReference(
  cfg.get('homelabStack') ?? 'mrsimpson/homelab/dev'
);
const homelab = createHomelabContextFromStack(homelabStack);
const domain = homelabStack.getOutput('domain') as pulumi.Output<string>;

const image = cfg.require('image');

const ns = new k8s.core.v1.Namespace(`${APP_NAME}-ns`, {
  metadata: {
    name: APP_NAME,
    labels: {
      'pod-security.kubernetes.io/enforce': 'restricted',
      'pod-security.kubernetes.io/enforce-version': 'latest',
    },
  },
});

export const app = homelab.createExposedWebApp(APP_NAME, {
  namespace: ns,
  image: pulumi.output(image),
  domain: pulumi.interpolate`${APP_NAME}.${domain}`,
  port: APP_PORT,
  replicas: 1,
  auth: AuthType.OAUTH2_PROXY,
  oauth2Proxy: { group: 'users' },
});

export const url = pulumi.interpolate`https://${APP_NAME}.${domain}`;
```

Type-check:
```bash
npm run type-check --workspace=apps/$APP
```

## Step 3 — Create Pulumi stack and configure

```bash
cd apps/$APP
pulumi stack select --create mrsimpson/$APP/dev

# Set the container image (find the correct tag on Docker Hub or GHCR)
pulumi config set $APP:image <registry>/<image>:<tag>

# Set required secrets (examples — adjust to your app)
pulumi config set $APP:adminToken "$(openssl rand -hex 32)" --secret

cd ../..
git add apps/$APP/
git commit -m "feat($APP): add Pulumi stack"
```

## Step 4 — Deploy locally first

This creates the namespace before setting up CI (CI setup requires the namespace to exist).

```bash
cd apps/$APP
pulumi up --stack mrsimpson/$APP/dev
kubectl get pods -n $APP
```

Verify the pod is `Running` and the app is accessible at `https://<app>.${domain}`.

## Step 5 — Set up CI credentials ⚠️ Human action required

> **This step cannot be automated by an agent.** It requires direct access to the homelab
> cluster and must be performed by the user. Ask the user to complete it before continuing.

Tell the user:

---
Before the CI workflow can deploy, you need to create a namespace-scoped service account
on the cluster and upload its kubeconfig as a GitHub secret.

**From your `homelab` repo directory**, run:

```bash
APP=<app-name>
SERVER=https://<tailscale-ip>:6443 \
  ./scripts/create-kubeconfig.sh $APP
```

This script (idempotently) creates:
- `ServiceAccount ci` in namespace `$APP`
- `ClusterRole homelab-ci-deployer` (shared, created once — cluster-wide CRD/workload access)
- `Role homelab-ci-secrets` in namespace `$APP` (secrets scoped to this namespace only)
- A kubeconfig at `/tmp/$APP-ci.kubeconfig`

Then upload it as a **per-app** GitHub secret and shred the file:

```bash
base64 -i /tmp/$APP-ci.kubeconfig | tr -d '\n' | \
  gh secret set KUBECONFIG_$(echo $APP | tr '[:lower:]-' '[:upper:]_') \
  -R mrsimpson/homelab-apps

shred -u /tmp/$APP-ci.kubeconfig 2>/dev/null || rm -f /tmp/$APP-ci.kubeconfig
```

Per-app secrets (e.g. `KUBECONFIG_VAULTWARDEN`) limit blast radius: a leaked kubeconfig
only exposes that app's namespace. The secret name to use in Step 6 is
`KUBECONFIG_<APP_NAME_UPPER>` (e.g. `KUBECONFIG_VAULTWARDEN`).

**Confirm when done** so the agent can continue with the CI workflow in Step 6.

---

## Step 6 — Write the deploy workflow

Create `.github/workflows/deploy-$APP.yml` in the `homelab-apps` repo.

Get the current SHA of the homelab main branch:
```bash
gh api repos/mrsimpson/homelab/git/refs/heads/main --jq '.object.sha'
```

```yaml
name: Deploy <app-name>

on:
  push:
    branches: [main]
    paths: [apps/<app-name>/**]
  workflow_dispatch:

permissions:
  contents: read

env:
  FORCE_JAVASCRIPT_ACTIONS_TO_NODE24: true

jobs:
  deploy:
    name: Deploy
    # Pin to SHA — update intentionally when upgrading the reusable workflow
    uses: mrsimpson/homelab/.github/workflows/deploy-to-cluster.yml@<sha-from-above>
    with:
      pulumi-stack: mrsimpson/<app-name>/dev
      working-directory: apps/<app-name>
      pulumi-command: up
      npm-lock-file-path: package-lock.json
    secrets:
      # Explicit map — do NOT use 'secrets: inherit'
      TS_OAUTH_CLIENT_ID: ${{ secrets.TS_OAUTH_CLIENT_ID }}
      TS_OAUTH_CLIENT_SECRET: ${{ secrets.TS_OAUTH_CLIENT_SECRET }}
      # Use the per-app secret created in Step 5, e.g. KUBECONFIG_VAULTWARDEN
      KUBECONFIG: ${{ secrets.KUBECONFIG_<APP_NAME_UPPER> }}
      PULUMI_ACCESS_TOKEN: ${{ secrets.PULUMI_ACCESS_TOKEN }}
```

Commit and push:
```bash
git add .github/workflows/deploy-$APP.yml
git commit -m "ci($APP): add deploy workflow"
git push
```

## Step 7 — Verify

```bash
gh workflow run deploy-$APP.yml -R mrsimpson/homelab-apps
gh run list -R mrsimpson/homelab-apps --limit 3
```

A passing run shows:
```
✓ Deploy / pulumi up (mrsimpson/<app-name>/dev) in ~45s
```

## RBAC model

| Resource | Scope | Why |
|----------|-------|-----|
| Namespaces, CRDs (Traefik, CNPG, ExternalSecrets, Gateway), Deployments, Services, PVCs | Cluster-wide (`ClusterRole`) | Pulumi refresh reads these regardless of namespace |
| **Secrets** | **Namespace only (`Role`)** | Blast radius limited to one app's namespace |
| **RBAC (roles/rolebindings)** | **Namespace only (`Role`)** | Cannot escalate beyond one namespace |

`create-kubeconfig.sh` handles all of this. Run it once per new app namespace.

## Common issues

**Image not found** — check the registry and tag format:
- Docker Hub: `vaultwarden/server:latest` (no prefix)
- GHCR: `ghcr.io/owner/image:tag`
- Tags often differ from GitHub release names (e.g. no `v` prefix on Docker Hub)

**Pod fails DB migration** — if using CNPG, ensure the app user owns all schema objects:
```typescript
postInitApplicationSQL: [
  'ALTER DEFAULT PRIVILEGES IN SCHEMA <schema> GRANT ALL ON TABLES TO app',
  'ALTER DEFAULT PRIVILEGES IN SCHEMA <schema> GRANT ALL ON SEQUENCES TO app',
]
```

**CI `startup_failure`** — the reusable workflow SHA may be outdated. Update it:
```bash
gh api repos/mrsimpson/homelab/git/refs/heads/main --jq '.object.sha'
# update the SHA in .github/workflows/deploy-$APP.yml
```

**RBAC forbidden** — namespace not yet set up for CI. Run `create-kubeconfig.sh $APP`.

## See also

- `scripts/create-kubeconfig.sh` — creates SA + RBAC + kubeconfig (in the homelab repo)
- `scripts/setup-homelab-apps.sh` — one-time bootstrap for the entire homelab-apps repo
- `apps/lobehub/` — reference implementation
- `docs/howto/setup-tailscale-cicd.md` — Tailscale setup for CI access
