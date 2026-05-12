import * as pulumi from '@pulumi/pulumi';
import * as k8s from '@pulumi/kubernetes';

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const APP_NAME = 'aftertouch';
const NAMESPACE = APP_NAME;
const APP_PORT = 8000;   // HTTP web UI + speaker API (AfterTouch backend)
const HTTPS_PORT = 8443; // HTTPS for speaker DNS redirect method
const WEB_PORT = 9090;   // soundtouch-web UI (sidecar) — 8080 taken by llama.cpp on host

// LAN access: Fritz.Box DNS entry required → aftertouch.flinker.fritz.box → 192.168.13.5
// NodePort services expose the app directly on the node for local network access.
// No Cloudflare Tunnel or Traefik IngressRoute is used — this is a local-only app.
const APP_HOSTNAME = 'aftertouch.flinker.fritz.box';
const NODE_PORT_HTTP  = 30800;  // http://aftertouch.flinker.fritz.box:30800 (or via Fritz.Box port redirect)
const NODE_PORT_HTTPS = 30843;  // https://aftertouch.flinker.fritz.box:8443  (speakers, TLS redirect method)
const NODE_PORT_WEB   = 30909;  // http://aftertouch.flinker.fritz.box:30909  (soundtouch-web UI)

// ---------------------------------------------------------------------------
// Config
// ---------------------------------------------------------------------------

const cfg = new pulumi.Config('aftertouch');
const appImage = cfg.require('image');
const webImage = cfg.get('webImage') ?? 'ghcr.io/gesellix/bose-soundtouch-web:0.77';
const mgmtUsername = cfg.requireSecret('mgmtUsername');
const mgmtPassword = cfg.requireSecret('mgmtPassword');
const preferredDevices = cfg.get('preferredDevices');

// ---------------------------------------------------------------------------
// 1. Namespace
// ---------------------------------------------------------------------------

const ns = new k8s.core.v1.Namespace(`${APP_NAME}-ns`, {
  metadata: {
    name: NAMESPACE,
    labels: { app: APP_NAME },
  },
});

// ---------------------------------------------------------------------------
// 2. Secret — management credentials injected via envFrom
// ---------------------------------------------------------------------------

const mgmtSecret = new k8s.core.v1.Secret(
  `${APP_NAME}-mgmt`,
  {
    metadata: {
      name: `${APP_NAME}-mgmt`,
      namespace: NAMESPACE,
      labels: { app: APP_NAME },
    },
    type: 'Opaque',
    stringData: {
      MGMT_USERNAME: mgmtUsername,
      MGMT_PASSWORD: mgmtPassword,
    },
  },
  { dependsOn: [ns] },
);

// ---------------------------------------------------------------------------
// 3. PersistentVolumeClaim — app data at /app/data
// ---------------------------------------------------------------------------

const pvc = new k8s.core.v1.PersistentVolumeClaim(
  `${APP_NAME}-pvc`,
  {
    metadata: {
      name: `${APP_NAME}-data`,
      namespace: NAMESPACE,
      labels: { app: APP_NAME },
    },
    spec: {
      accessModes: ['ReadWriteOnce'],
      storageClassName: 'longhorn-uncritical',
      resources: { requests: { storage: '500Mi' } },
    },
  },
  { dependsOn: [ns] },
);

// ---------------------------------------------------------------------------
// 4. Deployment
//
// hostNetwork: true — required so AfterTouch can:
//   - Send/receive SSDP multicast (239.255.255.250:1900) for UPnP speaker discovery
//   - Reach speakers on the LAN subnet directly (mDNS, Bose SoundTouch API)
// With hostNetwork the pod shares the node's network namespace, so it sees
// all LAN interfaces and multicast groups without a NAT boundary.
// The pod is pinned to the single k3s node (flinker) via nodeSelector.
// ---------------------------------------------------------------------------

const env: k8s.types.input.core.v1.EnvVar[] = [
  { name: 'BASE_URL', value: `http://${APP_HOSTNAME}` },
  ...(preferredDevices ? [{ name: 'PREFERRED_DEVICES', value: preferredDevices }] : []),
];

const deployment = new k8s.apps.v1.Deployment(
  `${APP_NAME}-deployment`,
  {
    metadata: {
      name: APP_NAME,
      namespace: NAMESPACE,
      labels: { app: APP_NAME },
    },
    spec: {
      replicas: 1,
      // Recreate (not RollingUpdate) is required because hostNetwork: true means the pod
      // binds to real host ports (8000, 8443, 8080). Two pods cannot run simultaneously
      // on the same node with the same host ports — the old pod must terminate before
      // the new one can start.
      strategy: { type: 'Recreate', rollingUpdate: undefined },
      selector: { matchLabels: { app: APP_NAME } },
      template: {
        metadata: { labels: { app: APP_NAME } },
        spec: {
          // Required for SSDP/mDNS multicast discovery on the LAN
          hostNetwork: true,
          // Pin to the single node; hostNetwork pods are node-local by definition
          nodeSelector: { 'kubernetes.io/hostname': 'flinker' },
          // AfterTouch upstream image runs as root
          securityContext: {},
          containers: [
            {
              name: 'app',
              image: appImage,
              ports: [
                { name: 'http',  containerPort: APP_PORT   },
                { name: 'https', containerPort: HTTPS_PORT },
              ],
              env,
              envFrom: [{ secretRef: { name: `${APP_NAME}-mgmt` } }],
              resources: {
                requests: { cpu: '50m',  memory: '128Mi' },
                limits:   { cpu: '500m', memory: '512Mi' },
              },
              volumeMounts: [{ name: 'data', mountPath: '/app/data' }],
              readinessProbe: {
                httpGet: { path: '/', port: APP_PORT },
                initialDelaySeconds: 15,
                periodSeconds: 15,
                failureThreshold: 3,
              },
              livenessProbe: {
                httpGet: { path: '/', port: APP_PORT },
                initialDelaySeconds: 30,
                periodSeconds: 30,
                failureThreshold: 3,
              },
            },
            // soundtouch-web UI sidecar — SPA frontend that talks to the AfterTouch
            // backend on localhost:8000 (shared network namespace via hostNetwork).
            {
              name: 'web',
              image: webImage,
              // Default port is 8080 which is taken by llama.cpp on this host.
              // Override via the -port flag to use 9090 instead.
              args: ['-port', `${WEB_PORT}`],
              ports: [{ name: 'web', containerPort: WEB_PORT }],
              resources: {
                requests: { cpu: '20m',  memory: '32Mi' },
                limits:   { cpu: '200m', memory: '128Mi' },
              },
              readinessProbe: {
                httpGet: { path: '/', port: WEB_PORT },
                initialDelaySeconds: 5,
                periodSeconds: 10,
                failureThreshold: 3,
              },
            },
          ],
          volumes: [
            {
              name: 'data',
              persistentVolumeClaim: { claimName: `${APP_NAME}-data` },
            },
          ],
        },
      },
    },
  },
  { dependsOn: [ns, pvc, mgmtSecret] },
);

// ---------------------------------------------------------------------------
// 5. Services
//
// ClusterIP — stable in-cluster DNS name: aftertouch.aftertouch.svc.cluster.local
//   Used by other pods that want to reach the AfterTouch API.
//
// NodePort (HTTP + HTTPS) — direct LAN access on the node's IP (192.168.13.5).
//   Fritz.Box DNS: aftertouch.flinker.fritz.box → 192.168.13.5
//   Web UI:  http://aftertouch.flinker.fritz.box:30800  (or port-redirect 80→30800)
//   Speakers: https://aftertouch.flinker.fritz.box:30843 (TLS redirect method)
// ---------------------------------------------------------------------------

// ClusterIP — in-cluster service discovery
const clusterService = new k8s.core.v1.Service(
  `${APP_NAME}-svc`,
  {
    metadata: {
      name: APP_NAME,
      namespace: NAMESPACE,
      labels: { app: APP_NAME },
    },
    spec: {
      type: 'ClusterIP',
      selector: { app: APP_NAME },
      ports: [
        { name: 'http',  port: APP_PORT,   targetPort: APP_PORT,   protocol: 'TCP' },
        { name: 'https', port: HTTPS_PORT, targetPort: HTTPS_PORT, protocol: 'TCP' },
        { name: 'web',   port: WEB_PORT,   targetPort: WEB_PORT,   protocol: 'TCP' },
      ],
    },
  },
  { dependsOn: [deployment] },
);

// NodePort — HTTP web UI, LAN access
const httpService = new k8s.core.v1.Service(
  `${APP_NAME}-http`,
  {
    metadata: {
      name: `${APP_NAME}-http`,
      namespace: NAMESPACE,
      labels: { app: APP_NAME },
    },
    spec: {
      type: 'NodePort',
      selector: { app: APP_NAME },
      ports: [
        {
          name: 'http',
          port: APP_PORT,
          targetPort: APP_PORT,
          protocol: 'TCP',
          nodePort: NODE_PORT_HTTP,
        },
      ],
    },
  },
  { dependsOn: [deployment] },
);

// NodePort — HTTPS, for speaker DNS redirect method
const httpsService = new k8s.core.v1.Service(
  `${APP_NAME}-https`,
  {
    metadata: {
      name: `${APP_NAME}-https`,
      namespace: NAMESPACE,
      labels: { app: APP_NAME },
    },
    spec: {
      type: 'NodePort',
      selector: { app: APP_NAME },
      ports: [
        {
          name: 'https',
          port: HTTPS_PORT,
          targetPort: HTTPS_PORT,
          protocol: 'TCP',
          nodePort: NODE_PORT_HTTPS,
        },
      ],
    },
  },
  { dependsOn: [deployment] },
);

// NodePort — soundtouch-web UI
const webService = new k8s.core.v1.Service(
  `${APP_NAME}-web`,
  {
    metadata: {
      name: `${APP_NAME}-web`,
      namespace: NAMESPACE,
      labels: { app: APP_NAME },
    },
    spec: {
      type: 'NodePort',
      selector: { app: APP_NAME },
      ports: [
        {
          name: 'web',
          port: WEB_PORT,
          targetPort: WEB_PORT,
          protocol: 'TCP',
          nodePort: NODE_PORT_WEB,
        },
      ],
    },
  },
  { dependsOn: [deployment] },
);

// ---------------------------------------------------------------------------
// Stack outputs
// ---------------------------------------------------------------------------

export const url            = `http://${APP_HOSTNAME}:${NODE_PORT_HTTP}`;
export const webUiUrl       = `http://192.168.13.5:${NODE_PORT_WEB}`;
export const directHttpUrl  = `http://192.168.13.5:${NODE_PORT_HTTP}`;
export const directHttpsUrl = `https://192.168.13.5:${NODE_PORT_HTTPS}`;
export const inClusterUrl   = `http://${APP_NAME}.${NAMESPACE}.svc.cluster.local:${APP_PORT}`;
export const lanNote        = `Fritz.Box DNS required: ${APP_HOSTNAME} → 192.168.13.5`;
export const namespace      = ns.metadata.name;
