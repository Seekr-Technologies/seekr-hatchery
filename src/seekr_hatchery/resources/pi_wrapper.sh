# Run at container-exec time as `sh -c <this> sh <pi args>`. Scans the
# environment for every HATCHERY_PI_<UP>_ID marker (one per resolvable provider
# — see PiBackend.container_env) and builds ~/.pi/agent/models.json +
# ~/.pi/agent/auth.json from that provider's {ID,BASEURL,KEY,SHAPE} vars, then
# execs pi. The real credentials never touch the container: KEY is the
# per-launch proxy token and BASEURL a proxy-port URL; the host-side
# ProxyEndpoint.header_mutator injects the real key/token on the way out.
# SHAPE=oauth writes an oauth-shaped entry (access, refresh, expires) with
# expires pinned far in the future so pi never attempts its own (unroutable)
# refresh against the fake token.
set -e; mkdir -p "$HOME/.pi/agent"; m=""; a=""
for idvar in $(env | sed -n 's/^\(HATCHERY_PI_[A-Za-z0-9_]*\)_ID=.*/\1/p'); do
  eval "id=\$${idvar}_ID"; eval "base=\$${idvar}_BASEURL"; eval "key=\$${idvar}_KEY"; eval "shape=\$${idvar}_SHAPE"
  [ -z "$base" ] && continue
  sm=""; [ -n "$m" ] && sm=","
  m="$m$sm\"$id\":{\"baseUrl\":\"$base\",\"apiKey\":\"$key\"}"
  sa=""; [ -n "$a" ] && sa=","
  if [ "$shape" = "oauth" ]; then
    a="$a$sa\"$id\":{\"type\":\"oauth\",\"access\":\"$key\",\"refresh\":\"\",\"expires\":4102444800000}"
  else
    a="$a$sa\"$id\":{\"type\":\"api_key\",\"key\":\"$key\"}"
  fi
done
printf '{"providers":{%s}}' "$m" > "$HOME/.pi/agent/models.json"
printf '{%s}' "$a" > "$HOME/.pi/agent/auth.json"
exec pi "$@"
