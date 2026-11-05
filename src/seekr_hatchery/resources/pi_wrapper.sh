# Run at container-exec time as `sh -c <this> sh <pi args>`. Scans the
# environment for every HATCHERY_PI_<UP>_ID marker (one per resolvable provider
# — see PiBackend.container_env) and builds ~/.pi/agent/models.json +
# ~/.pi/agent/auth.json. It also copies the host catalog into the container's
# models-store.json, replacing each configured provider model's URL origin with
# the proxy origin while retaining its base path, then execs pi. A sanitized
# host models.json is used as the base when present, so custom model definitions
# and provider options survive while routing and authentication use the proxy.
#
# The real credentials never touch the container: KEY is the per-launch proxy
# token and PROXY_ORIGIN is a proxy-port URL; the host-side
# ProxyEndpoint.header_mutator injects the real key/token on the way out.
# SHAPE=oauth writes an oauth-shaped entry (access, refresh, expires) with
# expires pinned far in the future so pi never attempts its own (unroutable)
# refresh against the fake token.
# Keep Pi and its children from replacing the proxy-routed catalog from the network.
set -e
export PI_OFFLINE=1
mkdir -p "$HOME/.pi/agent"
node <<'HATCHERY_NODE'
const fs = require("fs");
const path = require("path");

const agentDir = path.join(process.env.HOME, ".pi", "agent");
const hostModelsPath = path.join(agentDir, "hatchery-host-models.json");
const hostStorePath = path.join(agentDir, "hatchery-host-models-store.json");
let models = {};
if (fs.existsSync(hostModelsPath)) {
  models = JSON.parse(fs.readFileSync(hostModelsPath, "utf8"));
}
if (!models || typeof models !== "object" || Array.isArray(models)) models = {};
if (!models.providers || typeof models.providers !== "object" || Array.isArray(models.providers)) {
  models.providers = {};
}

const hasHostStore = fs.existsSync(hostStorePath);
let store = {};
if (hasHostStore) {
  store = JSON.parse(fs.readFileSync(hostStorePath, "utf8"));
}
if (!store || typeof store !== "object" || Array.isArray(store)) store = {};

function proxyUrl(baseUrl, proxyOrigin) {
  try {
    const url = new URL(baseUrl);
    return `${proxyOrigin}${url.pathname === "/" ? "" : url.pathname}`;
  } catch {
    return baseUrl;
  }
}

function proxyModelUrls(entries, proxyOrigin) {
  if (!Array.isArray(entries)) return entries;
  return entries.map((entry) => {
    if (!entry || typeof entry !== "object" || Array.isArray(entry) || typeof entry.baseUrl !== "string") return entry;
    return { ...entry, baseUrl: proxyUrl(entry.baseUrl, proxyOrigin) };
  });
}

function uniformEffectiveBaseUrl(catalogModels, configuredModels) {
  const catalog = Array.isArray(catalogModels) ? catalogModels : [];
  const configured = Array.isArray(configuredModels) ? configuredModels : [];
  const configuredById = new Map(configured.filter((model) => model?.id).map((model) => [model.id, model]));
  const urls = new Set([
    ...catalog.map((model) => configuredById.get(model?.id)?.baseUrl || model?.baseUrl),
    ...configured.map((model) => model?.baseUrl || catalog.find((entry) => entry?.id === model?.id)?.baseUrl),
  ].filter((url) => typeof url === "string" && url));
  return urls.size === 1 ? urls.values().next().value : undefined;
}

const auth = {};
for (const name of Object.keys(process.env).filter((key) => /^HATCHERY_PI_[A-Z0-9_]+_ID$/.test(key))) {
  const prefix = name.slice(0, -3);
  const id = process.env[name];
  const proxyOrigin = process.env[`${prefix}_PROXY_ORIGIN`];
  const key = process.env[`${prefix}_KEY`];
  const shape = process.env[`${prefix}_SHAPE`];
  if (!id || !proxyOrigin || !key) continue;

  const catalogProvider = store[id];
  if (catalogProvider && typeof catalogProvider === "object" && !Array.isArray(catalogProvider)) {
    store[id] = { ...catalogProvider, models: proxyModelUrls(catalogProvider.models, proxyOrigin) };
  }

  const existing = models.providers[id];
  const provider = {
    ...(existing && typeof existing === "object" && !Array.isArray(existing) ? existing : {}),
    apiKey: key,
  };
  if (typeof provider.baseUrl === "string") provider.baseUrl = proxyUrl(provider.baseUrl, proxyOrigin);
  provider.models = proxyModelUrls(provider.models, proxyOrigin);
  // Static providers (e.g. Codex) ignore the stored catalog. Override their
  // built-in URL too, but never collapse heterogeneous model paths.
  if (!provider.baseUrl) {
    const baseUrl = uniformEffectiveBaseUrl(store[id]?.models, provider.models);
    if (baseUrl) provider.baseUrl = baseUrl;
  }
  models.providers[id] = provider;
  auth[id] = shape === "oauth"
    ? { type: "oauth", access: key, refresh: "", expires: 4102444800000 }
    : { type: "api_key", key };
}

if (hasHostStore) {
  // Old read-only bind mounts can leave a non-writable file in the task volume.
  const temporaryDir = fs.mkdtempSync(path.join(agentDir, ".models-store-"));
  try {
    const temporaryStore = path.join(temporaryDir, "models-store.json");
    fs.writeFileSync(temporaryStore, JSON.stringify(store));
    fs.renameSync(temporaryStore, path.join(agentDir, "models-store.json"));
  } finally {
    fs.rmSync(temporaryDir, { recursive: true, force: true });
  }
}
fs.writeFileSync(path.join(agentDir, "models.json"), JSON.stringify(models));
fs.writeFileSync(path.join(agentDir, "auth.json"), JSON.stringify(auth));
HATCHERY_NODE
exec pi "$@"
