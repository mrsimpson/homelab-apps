# homelab-apps

Personal homelab app deployments — each app is a [Pulumi](https://www.pulumi.com/) project
that consumes [`@mrsimpson/homelab-core-components`](https://www.npmjs.com/package/@mrsimpson/homelab-core-components)
and deploys onto the homelab k3s cluster.

## Repository layout

```
homelab-apps/
├── package.json               ← npm workspaces root; shared devDeps (typescript, @types/node)
├── tsconfig.base.json         ← shared TypeScript compiler options
├── apps/
│   └── lobehub/               ← self-hosted LobeHub (AI chat)
│       ├── Pulumi.yaml
│       ├── Pulumi.dev.yaml
│       ├── src/
│       │   ├── index.ts
│       │   └── models.ts
│       ├── package.json
│       └── tsconfig.json
└── .github/
    └── workflows/
        └── deploy-lobehub.yml ← calls mrsimpson/homelab/.github/workflows/deploy-to-cluster.yml
```

## How it works

Each app in `apps/<name>/` is a standalone Pulumi project.  It reads shared
infrastructure outputs (domain, tunnel CNAME, Cloudflare zone) via
`StackReference("mrsimpson/homelab/dev")` and manages its own Kubernetes resources
in an isolated Pulumi state.

GitHub Actions deploys to the cluster by calling the reusable
[`deploy-to-cluster.yml`](https://github.com/mrsimpson/homelab/blob/main/.github/workflows/deploy-to-cluster.yml)
workflow from the `mrsimpson/homelab` repo.

## Required repository secrets

| Secret | Description |
|--------|-------------|
| `PULUMI_ACCESS_TOKEN` | Pulumi Cloud access token |
| `KUBECONFIG` | base64-encoded namespace-scoped kubeconfig (Tailscale address) |
| `TS_OAUTH_CLIENT_ID` | Tailscale OAuth client ID (`tag:ci`) |
| `TS_OAUTH_CLIENT_SECRET` | Tailscale OAuth client secret |

Bootstrap all secrets in one step from the `mrsimpson/homelab` repo:

```bash
./scripts/setup-homelab-apps.sh
```

## Agent skills

This repo ships [agentskills.io](https://agentskills.io)-compatible skills under `skills/`.
Load them in your AI agent to deploy and configure apps without reading documentation manually.

| Skill | When to use |
|-------|-------------|
| [`deploy-homelab-app`](./skills/deploy-homelab-app/SKILL.md) | Add a new app end-to-end (scaffold → RBAC → CI) |
| [`add-app-with-database`](./skills/add-app-with-database/SKILL.md) | App needs a PostgreSQL database (CNPG) |
| [`add-app-with-oauth`](./skills/add-app-with-oauth/SKILL.md) | Protect an app with GitHub OAuth (oauth2-proxy) |
| [`add-app-with-secrets`](./skills/add-app-with-secrets/SKILL.md) | Wire secrets from Pulumi ESC into an app |

## Adding a new app

Load the [`deploy-homelab-app`](./skills/deploy-homelab-app/SKILL.md) skill in your AI agent and say *"add a new app to homelab-apps"*.

Or manually:
1. Create `apps/<name>/` following the structure of `apps/lobehub/`
2. Add a deploy workflow `.github/workflows/deploy-<name>.yml`
3. Run `pulumi up` locally once to create the Kubernetes namespace
4. Generate a namespace-scoped KUBECONFIG:
   ```bash
   # from the mrsimpson/homelab repo
   bash scripts/create-kubeconfig.sh <name>
   base64 -w0 /tmp/<name>-ci.kubeconfig | gh secret set KUBECONFIG --repo mrsimpson/homelab-apps
   ```
