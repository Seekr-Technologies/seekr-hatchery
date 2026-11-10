# Run at container-exec time as `sh -c <this> sh <pi args>`. Scans the
# environment for every HATCHERY_PI_<UP>_ID marker (one per resolvable provider
# — see PiBackend.container_env) and builds ~/.pi/agent/models.json +
# ~/.pi/agent/auth.json from that provider's {ID,BASEURL,KEY,SHAPE} vars, then
# execs pi. A sanitized host models.json is used as the base when present, so
# custom model definitions and provider options survive while baseUrl/apiKey
# are replaced with the per-launch proxy route and token.
#
# The real credentials never touch the container: KEY is the per-launch proxy
# token and BASEURL a proxy-port URL; the host-side
# ProxyEndpoint.header_mutator injects the real key/token on the way out.
# SHAPE=oauth writes an oauth-shaped entry (access, refresh, expires) with
# expires pinned far in the future so pi never attempts its own (unroutable)
# refresh against the fake token.
set -e
mkdir -p "$HOME/.pi/agent"
node <<'HATCHERY_NODE'
const fs = require("fs");
const path = require("path");

const agentDir = path.join(process.env.HOME, ".pi", "agent");
const hostModelsPath = path.join(agentDir, "hatchery-host-models.json");
let models = {};
if (fs.existsSync(hostModelsPath)) {
  models = JSON.parse(fs.readFileSync(hostModelsPath, "utf8"));
}
if (!models || typeof models !== "object" || Array.isArray(models)) models = {};
if (!models.providers || typeof models.providers !== "object" || Array.isArray(models.providers)) {
  models.providers = {};
}

const auth = {};
for (const name of Object.keys(process.env).filter((key) => /^HATCHERY_PI_[A-Z0-9_]+_ID$/.test(key))) {
  const prefix = name.slice(0, -3);
  const id = process.env[name];
  const baseUrl = process.env[`${prefix}_BASEURL`];
  const key = process.env[`${prefix}_KEY`];
  const shape = process.env[`${prefix}_SHAPE`];
  if (!id || !baseUrl || !key) continue;

  const existing = models.providers[id];
  models.providers[id] = {
    ...(existing && typeof existing === "object" && !Array.isArray(existing) ? existing : {}),
    baseUrl,
    apiKey: key,
  };
  auth[id] = shape === "oauth"
    ? { type: "oauth", access: key, refresh: "", expires: 4102444800000 }
    : { type: "api_key", key };
}

fs.writeFileSync(path.join(agentDir, "models.json"), JSON.stringify(models));
fs.writeFileSync(path.join(agentDir, "auth.json"), JSON.stringify(auth));
HATCHERY_NODE
exec pi "$@"
