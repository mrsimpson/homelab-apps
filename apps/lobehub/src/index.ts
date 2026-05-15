import * as pulumi from '@pulumi/pulumi';
import * as k8s from '@pulumi/kubernetes';
import { AuthType, createHomelabContextFromStack, PostgresInstance } from '@mrsimpson/homelab-core-components';
import { fetchFreeModelList, fetchFlinkerModelList } from './models';

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const APP_NAME = 'lobehub';
const NAMESPACE = APP_NAME;
const APP_PORT = 3210;
const PG_DB = 'lobehub';

// ---------------------------------------------------------------------------
// Config
// ---------------------------------------------------------------------------

const cfg = new pulumi.Config('lobehub');

// StackReference to the homelab base stack — provides tunnelCname, cloudflareZoneId, domain
const homelabStackName = cfg.get('homelabStack') ?? 'mrsimpson/homelab/dev';
const homelabStack = new pulumi.StackReference(homelabStackName);

const domain = homelabStack.getOutput('domain') as pulumi.Output<string>;

const homelab = createHomelabContextFromStack(homelabStack);

// ---------------------------------------------------------------------------
// App config
// ---------------------------------------------------------------------------

const lobehubImage = cfg.require('lobehubImage');
const sandboxMcpImage = cfg.require('sandboxMcpImage');
const appStorageSize = cfg.get('storageSize') ?? '2Gi';
const dbStorageSize = cfg.get('databaseStorageSize') ?? '10Gi';

// Required secrets (app-side)
const authSecret = cfg.requireSecret('authSecret');
const keyVaultsSecret = cfg.requireSecret('keyVaultsSecret');
const jwksKey = cfg.requireSecret('jwksKey');

// S3 object storage (required for file uploads / knowledge base)
const s3AccessKeyId = cfg.requireSecret('s3AccessKeyId');
const s3SecretAccessKey = cfg.requireSecret('s3SecretAccessKey');
const s3Endpoint = cfg.require('s3Endpoint');
const s3Bucket = cfg.require('s3Bucket');

// Optional: external DATABASE_URL override — skips in-cluster Postgres when set.
const externalDatabaseUrl = cfg.getSecret('databaseUrl');

// Optional provider API keys — added to the env only when set
const openaiApiKey = cfg.getSecret('openaiApiKey');
const openrouterApiKey = cfg.requireSecret('openrouterApiKey');
const anthropicApiKey = cfg.getSecret('anthropicApiKey');

// Memory / embeddings — opt-in; requires bge-m3 on flinker:8080
const enableMemory = cfg.getBoolean('enableMemory') ?? false;

// GitHub OAuth — required for SSO login
const authGithubId = cfg.requireSecret('authGithubId');
const authGithubSecret = cfg.requireSecret('authGithubSecret');

// Optional OAuth provider credentials — added only when set
const authGoogleId = cfg.getSecret('authGoogleId');
const authGoogleSecret = cfg.getSecret('authGoogleSecret');

// ---------------------------------------------------------------------------
// 1. Namespace — pre-created with Pod Security Standards
// ---------------------------------------------------------------------------

const ns = new k8s.core.v1.Namespace(`${APP_NAME}-ns`, {
  metadata: {
    name: NAMESPACE,
    labels: {
      'app': APP_NAME,
      'pod-security.kubernetes.io/enforce': 'restricted',
      'pod-security.kubernetes.io/enforce-version': 'latest',
      'pod-security.kubernetes.io/warn': 'restricted',
      'pod-security.kubernetes.io/warn-version': 'latest',
    },
  },
});

// ---------------------------------------------------------------------------
// 2. In-cluster Postgres (paradedb via CNPG) — optional; skipped when externalDatabaseUrl is set
// ---------------------------------------------------------------------------

const db = externalDatabaseUrl === undefined
  ? new PostgresInstance(`${APP_NAME}-db`, {
      namespace: ns,
      databaseName: PG_DB,
      image: 'paradedb/paradedb:18-v0.23.4',
      storageSize: dbStorageSize,
      storageClass: 'longhorn-persistent',
      postgresUID: 999,
      postgresGID: 999,
      // pg_search and pg_cron must be preloaded; pg_stat_statements for observability.
      sharedPreloadLibraries: ['pg_search', 'pg_cron', 'pg_stat_statements'],
      // Pre-install as superuser so app migrations (CREATE EXTENSION IF NOT EXISTS)
      // are no-ops — the app user never needs superuser privileges.
      postInitApplicationSQL: [
        'CREATE EXTENSION IF NOT EXISTS vector',
        'CREATE EXTENSION IF NOT EXISTS pg_search',
        // Ensure the app user owns all objects it creates and has default privileges
        // on sequences in the drizzle schema. LobeHub runs Drizzle migrations as the
        // app user; without this, the migration tracking sequence (__drizzle_migrations_id_seq)
        // ends up owned by postgres (the bootstrap user) and the app user can't use it.
        'ALTER DEFAULT PRIVILEGES IN SCHEMA drizzle GRANT ALL ON TABLES TO app',
        'ALTER DEFAULT PRIVILEGES IN SCHEMA drizzle GRANT ALL ON SEQUENCES TO app',
        'ALTER DEFAULT PRIVILEGES FOR ROLE app IN SCHEMA drizzle GRANT ALL ON TABLES TO app',
        'ALTER DEFAULT PRIVILEGES FOR ROLE app IN SCHEMA drizzle GRANT ALL ON SEQUENCES TO app',
      ],
    })
  : undefined;

// DATABASE_URL — external override wins; otherwise read from CNPG adapter secret via connectionString
const databaseUrl: pulumi.Output<string> = externalDatabaseUrl ?? db!.connectionString;

// ---------------------------------------------------------------------------
// 3. Secret — app env values (DB, auth)
// ---------------------------------------------------------------------------

const appSecret = new k8s.core.v1.Secret(
  `${APP_NAME}-env`,
  {
    metadata: {
      name: `${APP_NAME}-env`,
      namespace: NAMESPACE,
      labels: { app: APP_NAME },
    },
    type: 'Opaque',
    stringData: {
      DATABASE_URL: databaseUrl,
      AUTH_SECRET: authSecret,
      KEY_VAULTS_SECRET: keyVaultsSecret,
      JWKS_KEY: jwksKey,
      S3_ACCESS_KEY_ID: s3AccessKeyId,
      S3_SECRET_ACCESS_KEY: s3SecretAccessKey,
      AUTH_GITHUB_ID: authGithubId,
      AUTH_GITHUB_SECRET: authGithubSecret,
      OPENROUTER_API_KEY: openrouterApiKey,
    },
  },
  { dependsOn: [ns] },
);

// ---------------------------------------------------------------------------
// 4. Env wiring — non-secret inline values + optional provider keys
// ---------------------------------------------------------------------------

const appDomain = pulumi.interpolate`${APP_NAME}.${domain}`;

// Fetch model lists at deploy time
const freeModelList = pulumi.output(fetchFreeModelList());
const flinkerModelList = pulumi.output(fetchFlinkerModelList());

const baseEnv: { name: string; value: string | pulumi.Output<string> }[] = [
  { name: 'APP_URL', value: pulumi.interpolate`https://${appDomain}` },
  { name: 'AUTH_TRUSTED_ORIGINS', value: pulumi.interpolate`https://${appDomain}` },
  { name: 'DATABASE_DRIVER', value: 'node' },
  { name: 'S3_ENDPOINT', value: s3Endpoint },
  { name: 'S3_BUCKET', value: s3Bucket },
  // Auth: GitHub SSO only, email/password disabled
  { name: 'AUTH_SSO_PROVIDERS', value: 'github' },
  { name: 'AUTH_DISABLE_EMAIL_PASSWORD', value: '1' },
  // OpenRouter — free models only (list built at deploy time)
  { name: 'OPENROUTER_MODEL_LIST', value: freeModelList },
  // Flinker (llama.cpp) — OpenAI-compatible in-cluster endpoint via lmstudio provider.
  // lmstudio is purpose-built for local inference servers: it never defaults to the
  // Responses API (unlike the openai provider), uses plain Chat Completions, and
  // handles reasoning_content + <think> tags via the shared OpenAIStream transformer.
  // A non-empty key is required to activate the provider; llama.cpp ignores its value.
  { name: 'LMSTUDIO_API_KEY', value: 'not-needed' },
  { name: 'LMSTUDIO_PROXY_URL', value: 'http://flinker:8080/v1' },
  { name: 'LMSTUDIO_MODEL_LIST', value: flinkerModelList },
  // Memory / embeddings — controlled by lobehub:enableMemory config flag
  ...(enableMemory
    ? [
        // bge-m3 served via lmstudio provider — no API key required.
        // lmstudio avoids the InvalidProviderAPIKey error that openai raises on dummy keys.
        { name: 'MEMORY_USER_MEMORY_EMBEDDING_BASE_URL', value: 'http://flinker:8080/v1' },
        { name: 'MEMORY_USER_MEMORY_EMBEDDING_MODEL', value: 'bge-m3' },
        { name: 'MEMORY_USER_MEMORY_EMBEDDING_PROVIDER', value: 'lmstudio' },
        {
          name: 'DEFAULT_FILES_CONFIG',
          value: 'embedding_model=lmstudio/bge-m3',
        },
      ]
    : [{ name: 'ENABLED_KNOWLEDGE_BASE', value: '0' }]),
];

const optionalSecretEnv = (
  name: string,
  value: pulumi.Output<string> | undefined,
): { name: string; value: string | pulumi.Output<string> }[] => (value ? [{ name, value }] : []);

const providerEnv = [
  ...optionalSecretEnv('OPENAI_API_KEY', openaiApiKey),
  ...optionalSecretEnv('ANTHROPIC_API_KEY', anthropicApiKey),
  ...optionalSecretEnv('AUTH_GOOGLE_ID', authGoogleId),
  ...optionalSecretEnv('AUTH_GOOGLE_SECRET', authGoogleSecret),
];

// ---------------------------------------------------------------------------
// 5. ExposedWebApp — Deployment, Service, OAuth2-Proxy auth, IngressRoute
// ---------------------------------------------------------------------------

// Dedicated ServiceAccount with token automount disabled.
// Neither lobehub nor sandbox-mcp need k8s API access; suppressing the token
// removes it as a post-exploit pivot point.
const sa = new k8s.core.v1.ServiceAccount(
  `${APP_NAME}-sa`,
  {
    metadata: { name: APP_NAME, namespace: NAMESPACE },
    automountServiceAccountToken: false,
  },
  { dependsOn: [ns] },
);

const appDependsOn: pulumi.Resource[] = [appSecret, sa];
if (db) appDependsOn.push(db);

export const app = homelab.createExposedWebApp(
  APP_NAME,
  {
    namespace: ns,
    image: pulumi.output(lobehubImage),
    domain: appDomain,
    port: APP_PORT,
    replicas: 1,
    auth: AuthType.OAUTH2_PROXY,
    oauth2Proxy: { group: 'users' }, // LobeHub Better Auth uses same GitHub App => this restricts the user group
    imagePullSecrets: [{ name: 'ghcr-pull-secret' }],
    serviceAccountName: APP_NAME,
    securityContext: {
      runAsUser: 1001,
      runAsGroup: 1001,
      fsGroup: 1001,
    },
    resources: {
      requests: { cpu: '100m', memory: '256Mi' },
      limits: { cpu: '1000m', memory: '1Gi' },
    },
    env: [...baseEnv, ...providerEnv],
    envFrom: [{ secretRef: { name: `${APP_NAME}-env` } }],
    probes: {
      readinessProbe: {
        httpGet: { path: '/', port: APP_PORT },
        initialDelaySeconds: 10,
        periodSeconds: 10,
        failureThreshold: 3,
      },
      livenessProbe: {
        httpGet: { path: '/', port: APP_PORT },
        initialDelaySeconds: 30,
        periodSeconds: 30,
        failureThreshold: 3,
      },
    },
    storage: {
      size: appStorageSize,
      storageClass: 'longhorn-uncritical',
      mountPath: '/app/data',
    },
    tags: ['lobehub', 'chat', 'ai'],
    // -----------------------------------------------------------------------
    // sandbox-mcp sidecar — provides sandboxed code-execution MCP tools.
    // Accessible at http://localhost:8888/mcp from within the pod (server-side).
    // Register in LobeHub: Settings → Plugins → Custom Plugin → MCP → URL above.
    // -----------------------------------------------------------------------
    extraVolumes: [
      // Session workspaces live in an emptyDir so the sidecar can use
      // readOnlyRootFilesystem: true and still write to /tmp/sessions.
      { name: 'sandbox-sessions', emptyDir: {} },
    ],
    extraContainers: [
      {
        name: 'sandbox-mcp',
        image: sandboxMcpImage,
        imagePullPolicy: 'Always',
        ports: [{ containerPort: 8888, protocol: 'TCP' }],
        resources: {
          requests: { cpu: '50m', memory: '128Mi' },
          limits: { cpu: '500m', memory: '512Mi' },
        },
        securityContext: {
          runAsNonRoot: true,
          runAsUser: 1000,
          allowPrivilegeEscalation: false,
          readOnlyRootFilesystem: true,
          seccompProfile: { type: 'RuntimeDefault' },
          capabilities: { drop: ['ALL'] },
        },
        volumeMounts: [
          { name: 'sandbox-sessions', mountPath: '/tmp/sessions' },
        ],
        // TCP probe — FastMCP does not expose a dedicated health endpoint.
        readinessProbe: {
          tcpSocket: { port: 8888 },
          initialDelaySeconds: 15,
          periodSeconds: 10,
          failureThreshold: 3,
        },
        livenessProbe: {
          tcpSocket: { port: 8888 },
          initialDelaySeconds: 30,
          periodSeconds: 30,
          failureThreshold: 3,
        },
      },
    ],
  },
  { dependsOn: appDependsOn },
);

// ---------------------------------------------------------------------------
// Stack outputs
// ---------------------------------------------------------------------------

export const url = pulumi.interpolate`https://${appDomain}`;
export const namespace = app.namespace.metadata.name;
export const databaseHost = db ? db.host : pulumi.output('external');
