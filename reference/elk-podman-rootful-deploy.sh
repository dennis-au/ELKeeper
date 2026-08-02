#!/usr/bin/env bash
# Rootful Podman installer for a multi-host Elastic Stack cluster.
set -Eeuo pipefail

ES_VERSION="8.19.0"
ES_IMAGE="docker.elastic.co/elasticsearch/elasticsearch:${ES_VERSION}"
KIBANA_IMAGE="docker.elastic.co/kibana/kibana:${ES_VERSION}"
AGENT_IMAGE="docker.elastic.co/beats/elastic-agent:${ES_VERSION}"
CONFIG_DIR=/etc/elastic-stack
CERT_DIR="${CONFIG_DIR}/certs"
UNIT_DIR=/etc/containers/systemd
DATA_DIR=/var/lib/elastic-stack
TOPOLOGY_DIR="${CONFIG_DIR}/topology"
MANIFEST_FILE="${CONFIG_DIR}/managed-paths"
TRANSPORT_CA_DIR="${CERT_DIR}/transport-ca"
TRANSPORT_CERT_DIR="${CERT_DIR}/transport"
HTTP_CA_DIR="${CERT_DIR}/http-ca"
HTTP_CERT_DIR="${CERT_DIR}/http"

die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
info() { printf '\n==> %s\n' "$*"; }
ask() {
  local value prompt=$1 default=$2
  if [[ -n $default ]]; then
    prompt+=" [$default]"
  fi
  read -r -p "$prompt: " value || die "Input closed."
  printf '%s' "${value:-$default}"
}
secret() { local value; read -r -s -p "$1: " value; printf '\n' >&2; printf '%s' "$value"; }
cancel() { printf '\nNo changes were made.\n'; exit 0; }

confirm() {
  local answer
  while true; do
    read -r -p "$1 [y/N/q]: " answer || cancel
    case "$answer" in
      [Yy]|[Yy][Ee][Ss]) return 0 ;;
      [Qq]|[Qq][Uu][Ii][Tt]) printf '\nReturning to the main menu.\n'; exit 0 ;;
      ''|[Nn]|[Nn][Oo]) printf 'Not confirmed. Enter y to continue or q to quit.\n' >&2 ;;
      *) printf 'Please answer y, n, or q.\n' >&2 ;;
    esac
  done
}

confirm_optional() {
  local answer
  while true; do
    read -r -p "$1 [y/n/q]: " answer || cancel
    case "$answer" in
      [Yy]|[Yy][Ee][Ss]) return 0 ;;
      ''|[Nn]|[Nn][Oo]) return 1 ;;
      [Qq]|[Qq][Uu][Ii][Tt]) printf '\nReturning to the main menu.\n'; exit 0 ;;
      *) printf 'Please answer y, n, or q.\n' >&2 ;;
    esac
  done
}

valid_path() {
  local value=$1
  [[ $value == /* && $value != / && $value != *:* && $value != *[[:space:]]* ]] || return 1
  case "$value" in /etc|/usr|/var|/home|/root|/opt|/bin|/sbin|/lib|/lib64|/boot|/proc|/sys|/dev|/run|/tmp) return 1 ;; esac
}

valid_node_name() {
  [[ $1 =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$ && $1 != *..* ]]
}

valid_cluster_name() {
  [[ $1 =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$ && $1 != *..* ]]
}

valid_ipv4() {
  local value=$1 octet
  [[ $value =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]] || return 1
  local IFS=.
  for octet in $value; do
    (( 10#$octet <= 255 )) || return 1
  done
}

ask_node_name() {
  local label=$1 default=$2 value
  while true; do
    value=$(ask "$label" "$default")
    valid_node_name "$value" && { printf '%s' "$value"; return; }
    printf 'Use 1-128 letters, numbers, dots, underscores, or hyphens; do not use "..".\n' >&2
  done
}

ask_cluster_name() {
  local default=$1 value
  while true; do
    value=$(ask "Cluster name" "$default")
    valid_cluster_name "$value" && { printf '%s' "$value"; return; }
    printf 'Use 1-128 letters, numbers, dots, underscores, or hyphens; do not use "..".\n' >&2
  done
}

ask_ipv4() {
  local label=$1 default=$2 value
  while true; do
    value=$(ask "$label" "$default")
    valid_ipv4 "$value" && { printf '%s' "$value"; return; }
    printf 'Enter a valid IPv4 address, such as 192.0.2.102.\n' >&2
  done
}

valid_seed_address() {
  local value=$1 host port
  [[ $value == *:* ]] || return 1
  host=${value%:*}
  port=${value##*:}
  [[ $host =~ ^[A-Za-z0-9][A-Za-z0-9.-]*$ && $host != *..* ]] || return 1
  valid_integer_range "$port" 1 65535
}

ask_seed_address() {
  local default=$1 value
  while true; do
    value=$(ask "Master transport seed (IP-or-DNS:9300)" "$default")
    valid_seed_address "$value" && { printf '%s' "$value"; return; }
    printf 'Enter a DNS name or IPv4 address followed by a port, such as 192.0.2.102:9300.\n' >&2
  done
}

valid_cpu_limit() {
  local value=$1
  [[ $value =~ ^[0-9]+([.][0-9]+)?$ ]] && awk "BEGIN { exit !($value > 0) }"
}

valid_memory_limit() {
  local value=$1
  [[ $value =~ ^[1-9][0-9]*([.][0-9]+)?[bBkKmMgG]$ ]]
}

valid_https_url() {
  local host_port port
  [[ $1 =~ ^https://[A-Za-z0-9.-]+(:[0-9]{1,5})?(/.*)?$ ]] || return 1
  host_port=${1#https://}
  host_port=${host_port%%/*}
  [[ $host_port == *:* ]] || return 0
  port=${host_port##*:}
  valid_integer_range "$port" 1 65535
}

ask_https_url() {
  local label=$1 default=$2 value
  while true; do
    value=$(ask "$label" "$default")
    valid_https_url "$value" && { printf '%s' "$value"; return; }
    printf 'Enter an HTTPS URL such as https://192.0.2.102:9200.\n' >&2
  done
}

ask_path() {
  local label=$1 default=$2 value
  while true; do
    value=$(ask "$label" "$default")
    valid_path "$value" && { printf '%s' "$value"; return; }
    printf 'Use an absolute path without whitespace or a colon.\n' >&2
  done
}

ask_cpu_limit() {
  local label=$1 default=$2 value
  while true; do
    value=$(ask "$label (CPU cores, for example 2 or 1.5)" "$default")
    valid_cpu_limit "$value" && { printf '%s' "$value"; return; }
    printf 'Enter a CPU value greater than zero, such as 2 or 1.5.\n' >&2
  done
}

ask_memory_limit() {
  local label=$1 default=$2 minimum=${3:-} value
  while true; do
    value=$(ask "$label (for example 512m or 4g)" "$default")
    if valid_memory_limit "$value" && { [[ -z $minimum ]] || memory_limit_at_least "$value" "$minimum"; }; then
      printf '%s' "$value"
      return
    fi
    if [[ -n $minimum ]]; then
      printf 'Enter a memory limit of at least %s.\n' "$minimum" >&2
    else
      printf 'Enter a positive memory size, such as 512m or 4g.\n' >&2
    fi
  done
}

memory_limit_at_least() {
  local limit=$1 required=$2
  awk -v limit="$limit" -v required="$required" '
    function bytes(value, unit) {
      unit = toupper(substr(value, length(value), 1))
      value = substr(value, 1, length(value) - 1)
      if (unit == "T") return value * 1024 * 1024 * 1024 * 1024
      if (unit == "G") return value * 1024 * 1024 * 1024
      if (unit == "M") return value * 1024 * 1024
      if (unit == "K") return value * 1024
      return value
    }
    BEGIN { exit !(bytes(limit) >= bytes(required)) }
  '
}

valid_integer_range() {
  local value=$1 minimum=$2 maximum=$3
  [[ $value =~ ^[0-9]+$ ]] && (( 10#$value >= minimum && 10#$value <= maximum ))
}

valid_cpuset() {
  [[ $1 =~ ^[0-9]+(-[0-9]+)?(,[0-9]+(-[0-9]+)?)*$ ]]
}

valid_cpuset_for_host() {
  local value=$1 part start end maximum
  valid_cpuset "$value" || return 1
  maximum=$(getconf _NPROCESSORS_ONLN)
  local IFS=,
  for part in $value; do
    if [[ $part == *-* ]]; then
      start=${part%-*}
      end=${part#*-}
    else
      start=$part
      end=$part
    fi
    (( 10#$start <= 10#$end && 10#$end < maximum )) || return 1
  done
}

ask_optional_integer() {
  local label=$1 current=$2 minimum=$3 maximum=$4 value default
  default=${current:-none}
  while true; do
    value=$(ask "$label ($minimum-$maximum, or none)" "$default")
    [[ ${value,,} == none ]] && { printf ''; return; }
    valid_integer_range "$value" "$minimum" "$maximum" && { printf '%s' "$value"; return; }
    printf 'Enter a whole number from %s to %s, or none.\n' "$minimum" "$maximum" >&2
  done
}

ask_optional_memory() {
  local label=$1 current=$2 allow_unlimited=${3:-false} value default
  default=${current:-none}
  while true; do
    value=$(ask "$label (memory size, or none)" "$default")
    [[ ${value,,} == none ]] && { printf ''; return; }
    [[ $allow_unlimited == true && $value == -1 ]] && { printf '%s' "$value"; return; }
    valid_memory_limit "$value" && { printf '%s' "$value"; return; }
    printf 'Enter a memory size such as 512m or 4g%s.\n' \
      "$([[ $allow_unlimited == true ]] && printf ', -1, or none' || printf ', or none')" >&2
  done
}

ask_optional_pids() {
  local current=$1 value default=${1:-none}
  while true; do
    value=$(ask "Process limit (-1 for unlimited, or none)" "$default")
    [[ ${value,,} == none ]] && { printf ''; return; }
    [[ $value == -1 || $value =~ ^[1-9][0-9]*$ ]] && { printf '%s' "$value"; return; }
    printf 'Enter a positive whole number, -1, or none.\n' >&2
  done
}

ask_optional_cpuset() {
  local current=$1 value default=${1:-none}
  while true; do
    value=$(ask "Allowed CPU cores (for example 0-3 or 0,2; or none)" "$default")
    [[ ${value,,} == none ]] && { printf ''; return; }
    valid_cpuset_for_host "$value" && { printf '%s' "$value"; return; }
    printf 'Enter CPU numbers/ranges available on this host, such as 0-3 or 0,2, or none.\n' >&2
  done
}

validate_resource_combination() {
  local memory=$1 reservation=$2 swap=$3 cpuset=$4
  valid_memory_limit "$memory" || return 1
  [[ -z $reservation ]] || {
    valid_memory_limit "$reservation" && memory_limit_at_least "$memory" "$reservation"
  } || return 1
  [[ -z $swap || $swap == -1 ]] || {
    valid_memory_limit "$swap" && memory_limit_at_least "$swap" "$memory"
  } || return 1
  [[ -z $cpuset ]] || valid_cpuset_for_host "$cpuset"
}

quadlet_arg_value() {
  local file=$1 name=$2 line token
  line=$(sed -n 's/^PodmanArgs=//p' "$file" | tail -n 1)
  for token in $line; do
    case "$token" in
      --"$name"=*) printf '%s' "${token#*=}"; return 0 ;;
    esac
  done
  return 1
}

is_managed_resource_arg() {
  case "$1" in
    --cpus=*|--memory=*|--cpu-shares=*|--memory-reservation=*|--memory-swap=*|\
    --pids-limit=*|--cpuset-cpus=*|--blkio-weight=*) return 0 ;;
    *) return 1 ;;
  esac
}

copy_file_metadata() {
  local source=$1 target=$2 mode owner
  mode=$(stat -c '%a' "$source" 2>/dev/null || stat -f '%Lp' "$source")
  owner=$(stat -c '%u:%g' "$source" 2>/dev/null || stat -f '%u:%g' "$source")
  chmod "$mode" "$target"
  chown "$owner" "$target"
}

existing_env_value() {
  local file=$1 key=$2
  [[ -f $file ]] || return 1
  sed -n "s/^${key}=//p" "$file" | tail -n 1
}

existing_or_new_secret() {
  local file=$1 key=$2 value
  value=$(existing_env_value "$file" "$key" || true)
  printf '%s' "${value:-$(openssl rand -hex 32)}"
}

managed_path_marker() { printf '%s/.elastic-stack-managed' "$1"; }

safe_managed_data_path() {
  local path=$1 marker
  valid_path "$path" || return 1
  marker=$(managed_path_marker "$path")
  if [[ -e $path && ! -f $marker ]] && [[ -n $(find "$path" -mindepth 1 -maxdepth 1 -print -quit 2>/dev/null) ]]; then
    return 1
  fi
  return 0
}

ask_managed_data_path() {
  local label=$1 default=$2 value
  while true; do
    value=$(ask "$label" "$default")
    safe_managed_data_path "$value" && { printf '%s' "$value"; return; }
    printf 'Use an empty dedicated directory that is not a system path, or a directory previously created by this installer.\n' >&2
  done
}

register_managed_path() {
  local key=$1 path=$2 marker
  marker=$(managed_path_marker "$path")
  touch "$marker"
  chmod 0600 "$marker"
  touch "$MANIFEST_FILE"
  chmod 0600 "$MANIFEST_FILE"
  sed -i "\\|^${key}[[:space:]]|d" "$MANIFEST_FILE"
  printf '%s\t%s\n' "$key" "$path" >> "$MANIFEST_FILE"
}

configure_container_resources() {
  local service=$1 default_data_host=$2 default_data_container=$3 default_cpu=$4 default_memory=$5 owner=$6 minimum_memory=${7:-}
  local extra_host extra_container

  info "$service storage and resource settings"
  CONTAINER_DATA_HOST=$(ask_managed_data_path "Persistent data directory on this host" "$default_data_host")
  CONTAINER_DATA_PATH=$(ask_path "Data directory inside the container (advanced)" "$default_data_container")
  [[ $CONTAINER_DATA_PATH != / ]] || die "The in-container data directory must not be '/'."

  CONTAINER_CPUS=$(ask_cpu_limit "$service limit" "$default_cpu")
  CONTAINER_MEMORY=$(ask_memory_limit "$service memory limit" "$default_memory" "$minimum_memory")

  extra_host=$(ask "Optional additional read-write host bind directory" "")
  CONTAINER_EXTRA_VOLUME=
  if [[ -n $extra_host ]]; then
    while ! valid_path "$extra_host"; do
      printf 'Use an absolute path without whitespace or a colon.\n' >&2
      extra_host=$(ask "Optional additional read-write host bind directory" "")
    done
    extra_container=$(ask_path "Additional directory inside the container" "")
    [[ $extra_container != / ]] || die "Additional $service directory inside container must not be '/'."
    CONTAINER_EXTRA_VOLUME="Volume=$extra_host:$extra_container:Z"
  fi

  printf '\n%s settings:\n  Storage: %s -> %s\n  CPU: %s\n  Memory: %s\n' \
    "$service" "$CONTAINER_DATA_HOST" "$CONTAINER_DATA_PATH" "$CONTAINER_CPUS" "$CONTAINER_MEMORY"
  [[ -z $CONTAINER_EXTRA_VOLUME ]] || printf '  Extra mount: %s\n' "${CONTAINER_EXTRA_VOLUME#Volume=}"
  confirm "Apply these settings" || cancel

  install -d -m 0750 "$CONTAINER_DATA_HOST"
  chown "$owner" "$CONTAINER_DATA_HOST"
  register_managed_path "${service,,}" "$CONTAINER_DATA_HOST"
  [[ -z $extra_host ]] || install -d -m 0750 "$extra_host"
}

install_host() {
  command -v dnf >/dev/null 2>&1 || die "This installer supports dnf-based RHEL/CentOS hosts."
  dnf install -y podman curl unzip openssl python3
  install -d -m 0750 "$CONFIG_DIR" "$CERT_DIR" "$UNIT_DIR" "$DATA_DIR"
  if systemctl is-active --quiet firewalld.service || systemctl is-enabled --quiet firewalld.service 2>/dev/null; then
    printf 'Disabling firewalld as configured for this lab deployment.\n'
    systemctl disable --now firewalld.service
  fi
  printf 'vm.max_map_count=1048576\n' > /etc/sysctl.d/99-elasticsearch.conf
  sysctl --system >/dev/null
}

ensure_firewall_disabled() {
  if systemctl is-active --quiet firewalld.service || systemctl is-enabled --quiet firewalld.service 2>/dev/null; then
    systemctl disable --now firewalld.service
  fi
}

generate_ca() {
  local temp_dir
  [[ -f "$TRANSPORT_CA_DIR/ca.crt" && -f "$TRANSPORT_CA_DIR/ca.key" && -f "$HTTP_CA_DIR/ca.crt" && -f "$HTTP_CA_DIR/ca.key" ]] && return
  info "Generating transport and HTTPS certificate authorities"
  podman pull "$ES_IMAGE"
  podman run --rm --entrypoint /usr/share/elasticsearch/bin/elasticsearch-certutil \
    -v "$CERT_DIR:/work:Z,U" "$ES_IMAGE" ca --silent --pem --out /work/transport-ca.zip
  podman run --rm --entrypoint /usr/share/elasticsearch/bin/elasticsearch-certutil \
    -v "$CERT_DIR:/work:Z,U" "$ES_IMAGE" ca --silent --pem --out /work/http-ca.zip
  install -d -m 0750 "$TRANSPORT_CA_DIR" "$HTTP_CA_DIR" "$TRANSPORT_CERT_DIR" "$HTTP_CERT_DIR"
  temp_dir=$(mktemp -d "${TMPDIR:-/tmp}/elastic-ca.XXXXXX")
  unzip -qo "$CERT_DIR/transport-ca.zip" -d "$temp_dir/transport"
  unzip -qo "$CERT_DIR/http-ca.zip" -d "$temp_dir/http"
  mv "$temp_dir/transport/ca/ca.crt" "$temp_dir/transport/ca/ca.key" "$TRANSPORT_CA_DIR/"
  mv "$temp_dir/http/ca/ca.crt" "$temp_dir/http/ca/ca.key" "$HTTP_CA_DIR/"
  rm -rf "$temp_dir"
  rm -f "$CERT_DIR/transport-ca.zip" "$CERT_DIR/http-ca.zip"
  chmod 0600 "$TRANSPORT_CA_DIR/ca.key" "$HTTP_CA_DIR/ca.key"
}

generate_certificate() {
  local ca_rel=$1 cert_rel=$2 name=$3 advertise=$4 archive
  archive="$CERT_DIR/${cert_rel//\//-}-${name}.zip"
  [[ -f "$CERT_DIR/$cert_rel/$name/$name.crt" && -f "$CERT_DIR/$cert_rel/$name/$name.key" ]] && return
  cat > "$CERT_DIR/${cert_rel//\//-}-${name}.yml" <<EOF
instances:
  - name: $name
    dns:
      - $name
      - localhost
    ip:
      - $advertise
      - 127.0.0.1
EOF
  podman run --rm --entrypoint /usr/share/elasticsearch/bin/elasticsearch-certutil \
    -v "$CERT_DIR:/work:Z,U" "$ES_IMAGE" cert --silent --pem \
    --in "/work/${cert_rel//\//-}-${name}.yml" --ca-cert "/work/$ca_rel/ca.crt" --ca-key "/work/$ca_rel/ca.key" \
    --out "/work/${cert_rel//\//-}-${name}.zip"
  install -d -m 0750 "$CERT_DIR/$cert_rel"
  unzip -qo "$archive" -d "$CERT_DIR/$cert_rel"
  rm -f "$archive" "$CERT_DIR/${cert_rel//\//-}-${name}.yml"
  chmod 0640 "$CERT_DIR/$cert_rel/$name/$name.key"
}

generate_node_certificate() {
  local node=$1 advertise=$2
  valid_node_name "$node" || die "Invalid node name: $node"
  valid_ipv4 "$advertise" || die "Invalid advertised IPv4 address: $advertise"
  generate_ca
  generate_certificate transport-ca transport "$node" "$advertise"
  generate_certificate http-ca http "$node" "$advertise"
  generate_certificate http-ca http "kibana-$node" "$advertise"
  generate_certificate http-ca http "fleet-$node" "$advertise"
}

create_remote_bundle() {
  local node advertise bundle_dir
  [[ -f "$TRANSPORT_CA_DIR/ca.crt" && -f "$TRANSPORT_CA_DIR/ca.key" && -f "$HTTP_CA_DIR/ca.crt" && -f "$HTTP_CA_DIR/ca.key" ]] || \
    die "No bootstrap cluster CA exists. Run action 1 on the first cluster host first."
  info "Run this only on the bootstrap host. The remote host name and LAN IP must match its deployment settings."
  node=$(ask_node_name "Remote host/node name" "es-warm-01")
  advertise=$(ask_ipv4 "Remote host LAN IP address" "192.0.2.103")
  generate_node_certificate "$node" "$advertise"
  bundle_dir="${CONFIG_DIR}/enrollment"
  install -d -m 0700 "$bundle_dir"
  tar -C "$CERT_DIR" -czf "$bundle_dir/${node}.tar.gz" \
    "transport-ca/ca.crt" "http-ca/ca.crt" \
    "transport/$node/$node.crt" "transport/$node/$node.key" \
    "http/$node/$node.crt" "http/$node/$node.key" \
    "http/kibana-$node/kibana-$node.crt" "http/kibana-$node/kibana-$node.key" \
    "http/fleet-$node/fleet-$node.crt" "http/fleet-$node/fleet-$node.key"
  chmod 0600 "$bundle_dir/${node}.tar.gz"
  printf '\nCopy this protected bundle to the remote host: %s\n' "$bundle_dir/${node}.tar.gz"
}

import_remote_bundle() {
  local bundle=$1 node=$2 expected actual
  [[ -f $bundle ]] || die "Bundle not found: $bundle"
  valid_node_name "$node" || die "Invalid node name: $node"
  tar -tzf "$bundle" >/dev/null || die "Unable to read certificate bundle: $bundle"
  expected=$(printf 'transport-ca/ca.crt\nhttp-ca/ca.crt\ntransport/%s/%s.crt\ntransport/%s/%s.key\nhttp/%s/%s.crt\nhttp/%s/%s.key\nhttp/kibana-%s/kibana-%s.crt\nhttp/kibana-%s/kibana-%s.key\nhttp/fleet-%s/fleet-%s.crt\nhttp/fleet-%s/fleet-%s.key\n' \
    "$node" "$node" "$node" "$node" "$node" "$node" "$node" "$node" "$node" "$node" "$node" "$node" "$node" "$node" "$node" "$node" | sort)
  actual=$(tar -tzf "$bundle" | sort)
  [[ $actual == "$expected" ]] || die "Certificate bundle contains unexpected files."
  tar --no-same-owner --no-same-permissions -xzf "$bundle" -C "$CERT_DIR"
  [[ -f "$TRANSPORT_CA_DIR/ca.crt" && -f "$HTTP_CA_DIR/ca.crt" && -f "$TRANSPORT_CERT_DIR/$node/$node.crt" && -f "$TRANSPORT_CERT_DIR/$node/$node.key" && -f "$HTTP_CERT_DIR/$node/$node.crt" && -f "$HTTP_CERT_DIR/$node/$node.key" ]] || \
    die "The bundle does not contain the expected certificate files."
  chmod 0640 "$TRANSPORT_CERT_DIR/$node/$node.key" "$HTTP_CERT_DIR/$node/$node.key" \
    "$HTTP_CERT_DIR/kibana-$node/kibana-$node.key" "$HTTP_CERT_DIR/fleet-$node/fleet-$node.key"
}

prepare_service_certificate() {
  local service=$1 node advertise bundle
  node=$(existing_env_value "$CONFIG_DIR/elasticsearch.env" ES_SETTING_NODE_NAME || true)
  advertise=$(existing_env_value "$CONFIG_DIR/elasticsearch.env" ES_SETTING_TRANSPORT_PUBLISH__HOST || true)
  if [[ -n $node && -n $advertise && -f "$HTTP_CERT_DIR/$service-$node/$service-$node.crt" ]]; then
    printf '%s\t%s' "$node" "$advertise"
    return 0
  fi

  printf '\n%s is running without a local Elasticsearch node. Import a bundle created by action 2 on the bootstrap host.\n' "$service" >&2
  node=$(ask_node_name "Certificate bundle node name" "$(hostname -s)")
  advertise=$(ask_ipv4 "This host LAN IP address (must match the certificate bundle)" "$(hostname -I | awk '{print $1}')")
  bundle=$(ask "Path to this host's certificate bundle" "/root/${node}.tar.gz")
  import_remote_bundle "$bundle" "$node"
  [[ -f "$HTTP_CERT_DIR/$service-$node/$service-$node.crt" ]] || die "$service TLS certificate is unavailable in the imported bundle."
  printf '%s\t%s' "$node" "$advertise"
}

write_es_unit() {
  local node=$1 roles=$2 cluster=$3 advertise=$4 seed=$5 heap=$6
  local data_host data_path cpus memory extra_volume
  configure_container_resources "Elasticsearch" "$DATA_DIR/elasticsearch" \
    "/usr/share/elasticsearch/data" 2 4g 1000:0 "$heap"
  data_host=$CONTAINER_DATA_HOST
  data_path=$CONTAINER_DATA_PATH
  cpus=$CONTAINER_CPUS
  memory=$CONTAINER_MEMORY
  extra_volume=$CONTAINER_EXTRA_VOLUME
  cat > "$CONFIG_DIR/elasticsearch.env" <<EOF
ES_SETTING_CLUSTER_NAME=$cluster
ES_SETTING_NODE_NAME=$node
ES_SETTING_NODE_ROLES=$roles
ES_SETTING_NETWORK_HOST=0.0.0.0
ES_SETTING_TRANSPORT_PUBLISH__HOST=$advertise
ES_SETTING_TRANSPORT_PUBLISH__PORT=9300
ES_SETTING_DISCOVERY_SEED__HOSTS=$seed
ES_SETTING_XPACK_SECURITY_ENABLED=true
ES_SETTING_XPACK_SECURITY_HTTP_SSL_ENABLED=true
ES_SETTING_XPACK_SECURITY_HTTP_SSL_KEY=/usr/share/elasticsearch/config/certs/http/$node/$node.key
ES_SETTING_XPACK_SECURITY_HTTP_SSL_CERTIFICATE=/usr/share/elasticsearch/config/certs/http/$node/$node.crt
ES_SETTING_XPACK_SECURITY_HTTP_SSL_CERTIFICATE__AUTHORITIES=/usr/share/elasticsearch/config/certs/http-ca/ca.crt
ES_SETTING_XPACK_SECURITY_TRANSPORT_SSL_ENABLED=true
ES_SETTING_XPACK_SECURITY_TRANSPORT_SSL_VERIFICATION__MODE=full
ES_SETTING_XPACK_SECURITY_TRANSPORT_SSL_KEY=/usr/share/elasticsearch/config/certs/transport/$node/$node.key
ES_SETTING_XPACK_SECURITY_TRANSPORT_SSL_CERTIFICATE=/usr/share/elasticsearch/config/certs/transport/$node/$node.crt
ES_SETTING_XPACK_SECURITY_TRANSPORT_SSL_CERTIFICATE__AUTHORITIES=/usr/share/elasticsearch/config/certs/transport-ca/ca.crt
ES_SETTING_PATH_DATA=$data_path
ES_JAVA_OPTS=-Xms$heap -Xmx$heap
EOF
  chmod 0600 "$CONFIG_DIR/elasticsearch.env"
  cat > "$UNIT_DIR/elasticsearch.container" <<EOF
[Unit]
Description=Elasticsearch $node
After=network-online.target
Wants=network-online.target

[Container]
Image=$ES_IMAGE
ContainerName=elasticsearch
Network=host
EnvironmentFile=$CONFIG_DIR/elasticsearch.env
Volume=$data_host:$data_path:Z
Volume=$CERT_DIR:/usr/share/elasticsearch/config/certs:ro,Z
$extra_volume
PodmanArgs=--cpus=$cpus --memory=$memory

[Service]
Restart=always
TimeoutStartSec=900

[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload
  ensure_firewall_disabled
}

curl_config_quote() {
  local value=$1
  value=${value//\\/\\\\}
  value=${value//\"/\\\"}
  printf '"%s"' "$value"
}

curl_authenticated() {
  local user=$1 password=$2 config status
  shift 2
  config=$(mktemp "${TMPDIR:-/tmp}/elastic-curl.XXXXXX")
  chmod 0600 "$config"
  { printf 'user = '; curl_config_quote "$user:$password"; printf '\n'; } > "$config"
  curl --config "$config" "$@"
  status=$?
  rm -f "$config"
  return "$status"
}

wait_for_es() {
  local password=$1 attempt
  printf 'Waiting for Elasticsearch to become ready'
  for attempt in $(seq 1 90); do
    if curl_authenticated elastic "$password" --fail --silent --show-error --cacert "$HTTP_CA_DIR/ca.crt" https://127.0.0.1:9200 >/dev/null 2>&1; then
      printf '\n'
      return 0
    fi
    (( attempt % 5 == 0 )) && printf '.'
    sleep 2
  done
  printf '\n' >&2
  die "Elasticsearch did not become ready. Check: journalctl -u elasticsearch.service"
}

kibana_system_password_valid() {
  local es_url=$1 password=$2 status
  status=$(curl_authenticated kibana_system "$password" --silent --show-error --max-time 5 --cacert "$HTTP_CA_DIR/ca.crt" -o /dev/null -w '%{http_code}' \
    "$es_url/_security/_authenticate" 2>/dev/null || true)
  [[ $status == 200 ]]
}

elasticsearch_url_is_local() {
  local es_url=$1 host local_ip
  host=${es_url#*://}
  host=${host%%/*}
  host=${host%%:*}
  case "$host" in
    localhost|127.0.0.1|::1) return 0 ;;
  esac
  for local_ip in $(hostname -I); do
    [[ $host == "$local_ip" ]] && return 0
  done
  return 1
}

reset_kibana_system_password() {
  local output password
  output=$(podman exec elasticsearch /usr/share/elasticsearch/bin/elasticsearch-reset-password \
    --username kibana_system --batch 2>&1) || return 1
  password=$(printf '%s\n' "$output" | sed -n 's/^New value: //p')
  [[ -n $password ]] || return 1
  printf '%s' "$password"
}

deploy_bootstrap_master() {
  local cluster node advertise password cluster_uuid
  if [[ -e "$UNIT_DIR/elasticsearch.container" || -d "$DATA_DIR/elasticsearch/nodes" ]]; then
    printf '\nAn Elasticsearch deployment already exists on this host.\n'
    printf 'Bootstrap is blocked to protect the existing cluster and credentials.\n'
    printf 'Use menu action 9 to inspect it, or action 12 to remove it before a fresh bootstrap.\n'
    return 0
  fi
  info "This creates the first master-only cluster node and a new transport certificate authority."
  printf 'Deploy a hot data node with action 3 before indexing production data or deploying Kibana.\n'
  cluster=$(ask_cluster_name "elastic-lab")
  node=$(ask_node_name "Master node name" "es-master-01")
  advertise=$(ask_ipv4 "This host LAN IP address" "$(hostname -I | awk '{print $1}')")
  password=$(openssl rand -hex 24)
  generate_node_certificate "$node" "$advertise"
  write_es_unit "$node" 'master,remote_cluster_client' \
    "$cluster" "$advertise" "$advertise:9300" 2g
  printf 'ELASTIC_PASSWORD=%s\nES_SETTING_CLUSTER_INITIAL__MASTER__NODES=%s\n' "$password" "$node" >> "$CONFIG_DIR/elasticsearch.env"
  systemctl restart elasticsearch.service
  wait_for_es "$password"
  sed -i '/^ES_SETTING_CLUSTER_INITIAL__MASTER__NODES=/d' "$CONFIG_DIR/elasticsearch.env"
  cluster_uuid=$(curl_authenticated elastic "$password" --fail --silent --show-error --cacert "$HTTP_CA_DIR/ca.crt" \
    'https://127.0.0.1:9200/?filter_path=cluster_uuid' | sed -n 's/.*"cluster_uuid"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p')
  [[ -n $cluster_uuid ]] || die "Could not determine the new cluster UUID. Check: journalctl -u elasticsearch.service"
  printf 'Elasticsearch URL: https://%s:9200\nCluster UUID: %s\nUsername: elastic\nPassword: %s\nCA certificate: %s\n' "$advertise" "$cluster_uuid" "$password" "$HTTP_CA_DIR/ca.crt" \
    > "$CONFIG_DIR/credentials.txt"
  chmod 0600 "$CONFIG_DIR/credentials.txt"
  printf '\nBootstrap complete. Credentials: %s\n' "$CONFIG_DIR/credentials.txt"
}

deploy_joining_node() {
  local kind roles heap cluster node advertise seed bundle admin_password expected_uuid
  kind=$1; roles=$2; heap=$3
  info "Join a $kind node to an existing cluster. Use a certificate bundle created by action 2 on the bootstrap host."
  cluster=$(ask_cluster_name "elastic-lab")
  node=$(ask_node_name "This node name" "es-${kind}-$(hostname -s)")
  advertise=$(ask_ipv4 "This host LAN IP address" "$(hostname -I | awk '{print $1}')")
  seed=$(ask_seed_address "192.0.2.102:9300")
  bundle=$(ask "Path to this node's certificate bundle" "/root/${node}.tar.gz")
  import_remote_bundle "$bundle" "$node"
  write_es_unit "$node" "$roles" "$cluster" "$advertise" "$seed" "$heap"
  systemctl restart elasticsearch.service
  wait_for_workload elasticsearch || die "Elasticsearch did not become reachable after joining. Check: journalctl -u elasticsearch.service"
  admin_password=$(secret "elastic administrator password (used once to verify cluster membership)")
  [[ -n $admin_password ]] || die "The elastic administrator password is required to verify cluster membership."
  expected_uuid=$(ask "Expected cluster UUID (from the bootstrap credentials file)" "")
  [[ -n $expected_uuid ]] || die "The expected cluster UUID is required to verify cluster membership."
  verify_joined_node "$seed" "$node" "$admin_password" "$expected_uuid" || die "This node did not join the expected cluster. Check the seed, cluster UUID, HTTP CA, and transport certificate."
  printf 'Elasticsearch joined the expected cluster.\n'
}

verify_joined_node() {
  local seed=$1 node=$2 password=$3 expected_uuid=$4 host nodes remote_uuid attempt
  host=${seed%:*}
  for attempt in $(seq 1 60); do
    nodes=$(curl_authenticated elastic "$password" --fail --silent --show-error --max-time 10 --cacert "$HTTP_CA_DIR/ca.crt" \
      "https://${host}:9200/_cat/nodes?h=name" 2>/dev/null || true)
    remote_uuid=$(curl_authenticated elastic "$password" --fail --silent --show-error --max-time 10 --cacert "$HTTP_CA_DIR/ca.crt" \
      "https://${host}:9200/?filter_path=cluster_uuid" 2>/dev/null | sed -n 's/.*"cluster_uuid"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' || true)
    if grep -Fxq "$node" <<< "$nodes" && [[ $remote_uuid == "$expected_uuid" ]]; then
      return 0
    fi
    sleep 2
  done
  return 1
}

deploy_kibana() {
  local es_url kibana_advertise password password_hint data_host data_path cpus memory extra_volume encryption_key saved_objects_key reporting_key host_node host_advertise certificate
  info "Deploy Kibana. Elasticsearch may be local or on another reachable host."
  printf 'Use the remote Elasticsearch HTTP address and its kibana_system password when Kibana is on a different host.\n'
  es_url=$(ask_https_url "Elasticsearch HTTPS URL" "https://192.0.2.102:9200")
  certificate=$(prepare_service_certificate kibana)
  host_node=${certificate%%$'\t'*}
  host_advertise=${certificate#*$'\t'}
  kibana_advertise=$(ask_ipv4 "This host LAN IP address (for the Kibana URL)" "$host_advertise")
  [[ $host_advertise == "$kibana_advertise" ]] || die "Kibana URL IP must match this host's certificate address ($host_advertise)."
  password_hint=
  if elasticsearch_url_is_local "$es_url" && podman container exists elasticsearch; then
    password_hint=$(existing_env_value "$CONFIG_DIR/kibana.env" ELASTICSEARCH_PASSWORD || true)
    if [[ -z $password_hint ]] || ! kibana_system_password_valid "$es_url" "$password_hint"; then
      password_hint=$(reset_kibana_system_password) || die "Could not generate a kibana_system password. Ensure a data-capable Elasticsearch node is online and the security index is allocated, then retry."
    fi
    printf '\nGenerated kibana_system password hint: %s\n' "$password_hint"
    password=$(secret "kibana_system password (paste the hint above)")
  else
    password=$(secret "kibana_system password")
  fi
  [[ -n $password ]] || die "kibana_system password is required."
  if ! kibana_system_password_valid "$es_url" "$password"; then
    printf 'The kibana_system password was rejected by %s. No Kibana files were changed.\n' "$es_url" >&2
    printf 'On the Elasticsearch host, reset it with:\n' >&2
    printf '  podman exec elasticsearch /usr/share/elasticsearch/bin/elasticsearch-reset-password --username kibana_system --batch\n' >&2
    return 0
  fi
  configure_container_resources "Kibana" "$DATA_DIR/kibana" "/usr/share/kibana/data" 1 2g 1000:0
  data_host=$CONTAINER_DATA_HOST
  data_path=$CONTAINER_DATA_PATH
  cpus=$CONTAINER_CPUS
  memory=$CONTAINER_MEMORY
  extra_volume=$CONTAINER_EXTRA_VOLUME
  encryption_key=$(existing_or_new_secret "$CONFIG_DIR/kibana.env" XPACK_SECURITY_ENCRYPTIONKEY)
  saved_objects_key=$(existing_or_new_secret "$CONFIG_DIR/kibana.env" XPACK_ENCRYPTEDSAVEDOBJECTS_ENCRYPTIONKEY)
  reporting_key=$(existing_or_new_secret "$CONFIG_DIR/kibana.env" XPACK_REPORTING_ENCRYPTIONKEY)
  cat > "$CONFIG_DIR/kibana.env" <<EOF
ELASTICSEARCH_HOSTS=$es_url
ELASTICSEARCH_USERNAME=kibana_system
ELASTICSEARCH_PASSWORD=$password
ELASTICSEARCH_SSL_CERTIFICATEAUTHORITIES=/usr/share/kibana/config/certs/http-ca/ca.crt
ELASTICSEARCH_SSL_VERIFICATIONMODE=full
SERVER_HOST=0.0.0.0
SERVER_SSL_ENABLED=true
SERVER_SSL_CERTIFICATE=/usr/share/kibana/config/certs/http/kibana-$host_node/kibana-$host_node.crt
SERVER_SSL_KEY=/usr/share/kibana/config/certs/http/kibana-$host_node/kibana-$host_node.key
XPACK_SECURITY_ENCRYPTIONKEY=$encryption_key
XPACK_ENCRYPTEDSAVEDOBJECTS_ENCRYPTIONKEY=$saved_objects_key
XPACK_REPORTING_ENCRYPTIONKEY=$reporting_key
PATH_DATA=$data_path
EOF
  chmod 0600 "$CONFIG_DIR/kibana.env"
  cat > "$UNIT_DIR/kibana.pod" <<'EOF'
[Pod]
PodName=kibana-pod
Network=host

[Install]
WantedBy=multi-user.target
EOF
  cat > "$UNIT_DIR/kibana.container" <<EOF
[Container]
Image=$KIBANA_IMAGE
ContainerName=kibana
Pod=kibana.pod
EnvironmentFile=$CONFIG_DIR/kibana.env
Volume=$data_host:$data_path:Z
Volume=$CERT_DIR:/usr/share/kibana/config/certs:ro,Z
$extra_volume
PodmanArgs=--cpus=$cpus --memory=$memory

[Service]
Restart=always
TimeoutStartSec=900

[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload
  systemctl start kibana-pod.service
  systemctl restart kibana.service
  ensure_firewall_disabled
  info "Waiting for Kibana to become ready"
  wait_for_workload kibana || die "Kibana did not become ready. Check: journalctl -u kibana.service"
  printf 'Kibana URL: https://%s:5601/login\nCA certificate: %s\n' "$kibana_advertise" "$HTTP_CA_DIR/ca.crt"
}

deploy_fleet() {
  local es_url fleet_advertise token policy data_host data_path cpus memory extra_volume host_node certificate
  info "Deploy Fleet Server. Create its service token and policy in Kibana before continuing."
  es_url=$(ask_https_url "Elasticsearch HTTPS URL" "https://192.0.2.102:9200")
  fleet_advertise=$(ask_ipv4 "This host LAN IP address (for Fleet Server enrollment)" "$(hostname -I | awk '{print $1}')")
  token=$(secret "Fleet Server service token")
  policy=$(ask "Fleet Server policy ID" "")
  [[ -n $token && -n $policy ]] || die "Fleet Server requires a service token and policy ID."
  certificate=$(prepare_service_certificate fleet)
  host_node=${certificate%%$'\t'*}
  configure_container_resources "Fleet Server" "$DATA_DIR/fleet" "/usr/share/elastic-agent/state" 1 1g 1000:0
  data_host=$CONTAINER_DATA_HOST
  data_path=$CONTAINER_DATA_PATH
  cpus=$CONTAINER_CPUS
  memory=$CONTAINER_MEMORY
  extra_volume=$CONTAINER_EXTRA_VOLUME
  cat > "$CONFIG_DIR/fleet.env" <<EOF
FLEET_SERVER_ENABLE=true
FLEET_SERVER_HOST=0.0.0.0
FLEET_SERVER_PORT=8220
FLEET_URL=https://$fleet_advertise:8220
FLEET_ENROLL=true
FLEET_CA=/usr/share/elastic-agent/certs/http-ca/ca.crt
FLEET_SERVER_ELASTICSEARCH_HOST=$es_url
FLEET_SERVER_SERVICE_TOKEN=$token
FLEET_SERVER_POLICY_ID=$policy
FLEET_SERVER_INSECURE_HTTP=false
FLEET_SERVER_ELASTICSEARCH_CA=/usr/share/elastic-agent/certs/http-ca/ca.crt
FLEET_SERVER_CERT=/usr/share/elastic-agent/certs/http/fleet-$host_node/fleet-$host_node.crt
FLEET_SERVER_CERT_KEY=/usr/share/elastic-agent/certs/http/fleet-$host_node/fleet-$host_node.key
STATE_PATH=$data_path
EOF
  chmod 0600 "$CONFIG_DIR/fleet.env"
  cat > "$UNIT_DIR/fleet.container" <<EOF
[Container]
Image=$AGENT_IMAGE
ContainerName=fleet-server
Network=host
EnvironmentFile=$CONFIG_DIR/fleet.env
Volume=$data_host:$data_path:Z
Volume=$CERT_DIR:/usr/share/elastic-agent/certs:ro,Z
$extra_volume
PodmanArgs=--cpus=$cpus --memory=$memory

[Service]
Restart=always
TimeoutStartSec=900

[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload
  systemctl restart fleet.service
  ensure_firewall_disabled
  info "Waiting for Fleet Server to become ready"
  wait_for_workload fleet || die "Fleet Server did not become ready. Check: journalctl -u fleet.service"
  wait_for_fleet_enrollment || die "Fleet Server did not report a healthy enrollment. Check: journalctl -u fleet.service"
}

add_managed_workload() {
  WORKLOAD_KEYS+=("$1")
  WORKLOAD_LABELS+=("$2")
  WORKLOAD_UNITS+=("$3")
  WORKLOAD_CONTAINERS+=("$4")
  WORKLOAD_QUADLETS+=("$5")
}

discover_managed_workloads() {
  WORKLOAD_KEYS=()
  WORKLOAD_LABELS=()
  WORKLOAD_UNITS=()
  WORKLOAD_CONTAINERS=()
  WORKLOAD_QUADLETS=()
  if [[ -f "$UNIT_DIR/elasticsearch.container" ]]; then
    add_managed_workload elasticsearch Elasticsearch elasticsearch.service elasticsearch "$UNIT_DIR/elasticsearch.container"
  fi
  if [[ -f "$UNIT_DIR/kibana.container" ]]; then
    add_managed_workload kibana Kibana kibana.service kibana "$UNIT_DIR/kibana.container"
  fi
  if [[ -f "$UNIT_DIR/fleet.container" ]]; then
    add_managed_workload fleet "Fleet Server" fleet.service fleet-server "$UNIT_DIR/fleet.container"
  fi
  return 0
}

default_resource_value() {
  local key=$1 resource=$2
  case "$key:$resource" in
    elasticsearch:cpus) printf '2' ;;
    elasticsearch:memory) printf '4g' ;;
    kibana:cpus|fleet:cpus) printf '1' ;;
    kibana:memory) printf '2g' ;;
    fleet:memory) printf '1g' ;;
  esac
}

show_effective_resources() {
  local container=$1
  if ! command -v podman >/dev/null 2>&1 || ! podman container exists "$container"; then
    printf '  Runtime: container not present\n'
    return 0
  fi
  podman inspect --format \
    '  Runtime: cpu quota={{.HostConfig.CpuQuota}}/{{.HostConfig.CpuPeriod}}, cpu shares={{.HostConfig.CpuShares}}, cpuset={{.HostConfig.CpusetCpus}}, memory={{.HostConfig.Memory}} bytes, reservation={{.HostConfig.MemoryReservation}} bytes, swap={{.HostConfig.MemorySwap}} bytes, pids={{.HostConfig.PidsLimit}}, blkio={{.HostConfig.BlkioWeight}}' \
    "$container"
}

rewrite_quadlet_resources() {
  local file=$1 replacement old_line token temp
  shift
  local -a preserved=() combined=()
  old_line=$(sed -n 's/^PodmanArgs=//p' "$file" | tail -n 1)
  [[ -n $old_line ]] || die "No PodmanArgs line found in $file."
  for token in $old_line; do
    is_managed_resource_arg "$token" || preserved+=("$token")
  done
  combined=("${preserved[@]}" "$@")
  replacement=${combined[*]}
  temp=$(mktemp "${file}.tmp.XXXXXX")
  sed "s|^PodmanArgs=.*|PodmanArgs=$replacement|" "$file" > "$temp"
  copy_file_metadata "$file" "$temp"
  mv -f "$temp" "$file"
}

restart_workload() {
  local key=$1 unit=$2
  if [[ $key == kibana ]]; then
    systemctl restart kibana-pod.service
    systemctl restart "$unit"
  else
    systemctl restart "$unit"
  fi
}

wait_for_workload() {
  local key=$1 attempts url status
  case "$key" in
    elasticsearch) attempts=90; url=https://127.0.0.1:9200 ;;
    kibana) attempts=120; url=https://127.0.0.1:5601/api/status ;;
    fleet) attempts=90; url=https://127.0.0.1:8220/api/status ;;
  esac
  while (( attempts-- > 0 )); do
    status=$(curl --silent --show-error --max-time 2 --cacert "$HTTP_CA_DIR/ca.crt" -o /dev/null -w '%{http_code}' "$url" 2>/dev/null || true)
    if [[ $key == elasticsearch && ( $status == 200 || $status == 401 ) ]]; then
      return 0
    fi
    if [[ $key != elasticsearch && $status == 200 ]]; then
      return 0
    fi
    sleep 2
  done
  return 1
}

wait_for_fleet_enrollment() {
  local attempts=60 status
  while (( attempts-- > 0 )); do
    status=$(podman exec fleet-server elastic-agent status --output json 2>/dev/null || true)
    if grep -Eq '"(status|state)"[[:space:]]*:[[:space:]]*"(HEALTHY|healthy)"' <<< "$status"; then
      return 0
    fi
    sleep 2
  done
  return 1
}

rollback_resource_change() {
  local backup=$1 quadlet=$2 key=$3 unit=$4 was_active=$5
  printf 'Restoring previous resource configuration from %s\n' "$backup" >&2
  cp -a "$backup" "$quadlet"
  systemctl daemon-reload
  if [[ $was_active == active ]]; then
    restart_workload "$key" "$unit" || true
  fi
}

apply_resource_change() {
  local key=$1 label=$2 unit=$3 container=$4 quadlet=$5
  shift 5
  local was_active backup_dir backup
  local -a update_args=("$@")
  was_active=$(systemctl is-active "$unit" 2>/dev/null || true)
  backup_dir="$CONFIG_DIR/backups/resources/$(date +%Y%m%d-%H%M%S)"
  install -d -m 0700 "$backup_dir"
  backup="$backup_dir/$(basename "$quadlet")"
  cp -a "$quadlet" "$backup"

  rewrite_quadlet_resources "$quadlet" "${update_args[@]}"
  if ! systemctl daemon-reload; then
    cp -a "$backup" "$quadlet"
    systemctl daemon-reload || true
    die "Unable to reload systemd after updating $label; the previous Quadlet was restored."
  fi

  if command -v podman >/dev/null 2>&1 && podman container exists "$container"; then
    if ! podman update "${update_args[@]}" "$container" >/dev/null; then
      printf 'Warning: live update was not accepted; the persistent settings will be applied by service recreation.\n' >&2
    fi
  fi

  if [[ $was_active == active ]]; then
    info "Restarting $label to recreate it from the updated Quadlet"
    if ! restart_workload "$key" "$unit" || ! wait_for_workload "$key"; then
      rollback_resource_change "$backup" "$quadlet" "$key" "$unit" "$was_active"
      die "$label failed verification; the previous Quadlet was restored."
    fi
    printf '%s restarted and passed its listener check.\n' "$label"
  else
    printf '%s was %s. Persistent limits were updated and will apply at its next service start.\n' \
      "$label" "${was_active:-inactive}"
  fi

  show_effective_resources "$container"
  printf 'Backup: %s\n' "$backup"
}

adjust_existing_resources() {
  local selection index key label unit container quadlet
  local current_cpu current_memory current_shares current_reservation current_swap current_pids current_cpuset current_blkio
  local new_cpu new_memory new_shares new_reservation new_swap new_pids new_cpuset new_blkio heap
  local -a resource_args=()

  discover_managed_workloads
  if (( ${#WORKLOAD_KEYS[@]} == 0 )); then
    printf '\nNo script-managed container Quadlets were found in %s.\n' "$UNIT_DIR"
    if command -v podman >/dev/null 2>&1; then
      printf 'Existing pods or containers may be orphaned and cannot receive persistent changes:\n'
      podman pod ps --format '  pod {{.Name}}: {{.Status}}' 2>/dev/null || true
      podman ps -a --format '  container {{.Names}}: {{.Status}}' 2>/dev/null || true
    fi
    printf 'Deploy or restore a managed workload before adjusting resources.\n'
    return 0
  fi

  printf '\nManaged workloads:\n'
  for index in "${!WORKLOAD_KEYS[@]}"; do
    printf '  %d) %s (%s)\n' "$((index + 1))" "${WORKLOAD_LABELS[$index]}" \
      "$(systemctl is-active "${WORKLOAD_UNITS[$index]}" 2>/dev/null || true)"
  done
  printf '  0) Cancel\n'
  while true; do
    selection=$(ask "Select a workload" "0")
    [[ $selection == 0 ]] && return 0
    if [[ $selection =~ ^[0-9]+$ ]] && (( selection >= 1 && selection <= ${#WORKLOAD_KEYS[@]} )); then
      index=$((selection - 1))
      break
    fi
    printf 'Choose a listed workload number or 0.\n' >&2
  done

  key=${WORKLOAD_KEYS[$index]}
  label=${WORKLOAD_LABELS[$index]}
  unit=${WORKLOAD_UNITS[$index]}
  container=${WORKLOAD_CONTAINERS[$index]}
  quadlet=${WORKLOAD_QUADLETS[$index]}

  current_cpu=$(quadlet_arg_value "$quadlet" cpus || true)
  current_memory=$(quadlet_arg_value "$quadlet" memory || true)
  current_cpu=${current_cpu:-$(default_resource_value "$key" cpus)}
  current_memory=${current_memory:-$(default_resource_value "$key" memory)}
  current_shares=$(quadlet_arg_value "$quadlet" cpu-shares || true)
  current_reservation=$(quadlet_arg_value "$quadlet" memory-reservation || true)
  current_swap=$(quadlet_arg_value "$quadlet" memory-swap || true)
  current_pids=$(quadlet_arg_value "$quadlet" pids-limit || true)
  current_cpuset=$(quadlet_arg_value "$quadlet" cpuset-cpus || true)
  current_blkio=$(quadlet_arg_value "$quadlet" blkio-weight || true)

  printf '\n%s persistent settings from %s:\n' "$label" "$quadlet"
  printf '  CPU: %s\n  Memory: %s\n  CPU shares: %s\n  Memory reservation: %s\n  Swap: %s\n  PIDs: %s\n  CPU set: %s\n  Block I/O weight: %s\n' \
    "$current_cpu" "$current_memory" "${current_shares:-none}" "${current_reservation:-none}" \
    "${current_swap:-none}" "${current_pids:-none}" "${current_cpuset:-none}" "${current_blkio:-none}"
  show_effective_resources "$container"

  heap=
  if [[ $key == elasticsearch && -f "$CONFIG_DIR/elasticsearch.env" ]]; then
    heap=$(sed -n 's/^ES_JAVA_OPTS=.*-Xmx\([^ ]*\).*$/\1/p' "$CONFIG_DIR/elasticsearch.env" | tail -n 1)
  fi
  new_cpu=$(ask_cpu_limit "$label CPU limit" "$current_cpu")
  new_memory=$(ask_memory_limit "$label memory limit" "$current_memory" "$heap")
  new_shares=$current_shares
  new_reservation=$current_reservation
  new_swap=$current_swap
  new_pids=$current_pids
  new_cpuset=$current_cpuset
  new_blkio=$current_blkio

  local configure_advanced=false
  if confirm_optional "Configure advanced resource controls"; then
    configure_advanced=true
  fi

  while true; do
    if [[ $configure_advanced == true ]]; then
    new_shares=$(ask_optional_integer "CPU shares" "$current_shares" 2 262144)
    while true; do
      new_reservation=$(ask_optional_memory "Memory reservation" "$current_reservation")
      if [[ -z $new_reservation ]] || memory_limit_at_least "$new_memory" "$new_reservation"; then
        break
      fi
      printf 'Memory reservation must not exceed the memory limit (%s).\n' "$new_memory" >&2
    done
    while true; do
      new_swap=$(ask_optional_memory "Memory plus swap limit (-1 for unlimited)" "$current_swap" true)
      if [[ -z $new_swap || $new_swap == -1 ]] || memory_limit_at_least "$new_swap" "$new_memory"; then
        break
      fi
      printf 'Swap limit must be -1 or at least the memory limit (%s).\n' "$new_memory" >&2
    done
    new_pids=$(ask_optional_pids "$current_pids")
    new_cpuset=$(ask_optional_cpuset "$current_cpuset")
    new_blkio=$(ask_optional_integer "Block I/O weight" "$current_blkio" 10 1000)
    fi

    if validate_resource_combination "$new_memory" "$new_reservation" "$new_swap" "$new_cpuset"; then
      break
    fi

    printf 'The saved advanced limits are incompatible with the selected memory or available CPUs.\n' >&2
    printf 'Set compatible advanced values before continuing.\n' >&2
    configure_advanced=true
  done

  resource_args=("--cpus=$new_cpu" "--memory=$new_memory")
  [[ -z $new_shares ]] || resource_args+=("--cpu-shares=$new_shares")
  [[ -z $new_reservation ]] || resource_args+=("--memory-reservation=$new_reservation")
  [[ -z $new_swap ]] || resource_args+=("--memory-swap=$new_swap")
  [[ -z $new_pids ]] || resource_args+=("--pids-limit=$new_pids")
  [[ -z $new_cpuset ]] || resource_args+=("--cpuset-cpus=$new_cpuset")
  [[ -z $new_blkio ]] || resource_args+=("--blkio-weight=$new_blkio")

  printf '\nNew %s resources:\n  %s\n' "$label" "${resource_args[*]}"
  printf 'The Quadlet will be backed up. An active service will be recreated and briefly unavailable.\n'
  confirm "Apply this resource change" || { printf 'Resource change cancelled.\n'; return 0; }
  apply_resource_change "$key" "$label" "$unit" "$container" "$quadlet" "${resource_args[@]}"
}

prepare_host() {
  info "Host preparation"
  printf 'This will install or update Podman, curl, unzip, OpenSSL, and Python, disable firewalld, set vm.max_map_count=1048576, and create Elastic Stack directories.\n'
  confirm "Continue with host preparation" || cancel
  install_host
}

show_status() {
  info "Local Elastic Stack status"
  printf '%-24s %-12s %s\n' "Service" "State" "Managed Quadlet"
  local service state quadlet index
  for service in elasticsearch.service kibana-pod.service kibana.service fleet.service; do
    state=$(systemctl is-active "$service" 2>/dev/null || true)
    case "$service" in
      elasticsearch.service) quadlet=$UNIT_DIR/elasticsearch.container ;;
      kibana-pod.service) quadlet=$UNIT_DIR/kibana.pod ;;
      kibana.service) quadlet=$UNIT_DIR/kibana.container ;;
      fleet.service) quadlet=$UNIT_DIR/fleet.container ;;
    esac
    printf '%-24s %-12s %s\n' "$service" "${state:-not installed}" "$([[ -f $quadlet ]] && printf yes || printf no)"
  done
  if command -v podman >/dev/null 2>&1; then
    printf '\nContainers:\n'
    podman ps -a --format '  {{.Names}}  {{.Status}}' 2>/dev/null || true
    printf '\nPods:\n'
    podman pod ps --format '  {{.Name}}  {{.Status}}' 2>/dev/null || true
    if podman pod exists kibana-pod 2>/dev/null && [[ ! -f "$UNIT_DIR/kibana.pod" ]]; then
      printf '  Warning: kibana-pod is orphaned because its Quadlet is absent.\n'
    fi
  fi
  discover_managed_workloads
  if (( ${#WORKLOAD_KEYS[@]} > 0 )); then
    printf '\nManaged resource limits:\n'
    for index in "${!WORKLOAD_KEYS[@]}"; do
      printf '  %s: %s\n' "${WORKLOAD_LABELS[$index]}" \
        "$(sed -n 's/^PodmanArgs=//p' "${WORKLOAD_QUADLETS[$index]}" | tail -n 1)"
      show_effective_resources "${WORKLOAD_CONTAINERS[$index]}"
    done
  fi
  [[ -f "$CONFIG_DIR/credentials.txt" ]] && printf '\nBootstrap credentials: %s\n' "$CONFIG_DIR/credentials.txt"
  return 0
}

topology_resource_limit() {
  local quadlet=$1 resource=$2 fallback=$3 value
  [[ -f $quadlet ]] || { printf '%s' "$fallback"; return 0; }
  value=$(quadlet_arg_value "$quadlet" "$resource" || true)
  printf '%s' "${value:-$fallback}"
}

show_cluster_topology() {
  local es_url user password cluster local_ip local_es_name
  local kibana_cpu kibana_memory fleet_cpu fleet_memory kibana_state fleet_state
  local tmp_dir stats_file info_file http_file mmd_file text_file

  info "Multi-host Elastic Stack topology"
  if ! command -v python3 >/dev/null 2>&1; then
    printf 'Python 3 is required to generate the topology data. Run a deployment action, then try again.\n' >&2
    return 0
  fi
  if [[ ! -f "$CONFIG_DIR/credentials.txt" ]]; then
    printf 'Topology requires the bootstrap credentials file. Run this action on the bootstrap host.\n' >&2
    return 0
  fi

  es_url=$(sed -n 's/^Elasticsearch URL: //p' "$CONFIG_DIR/credentials.txt" | tail -n 1)
  user=$(sed -n 's/^Username: //p' "$CONFIG_DIR/credentials.txt" | tail -n 1)
  password=$(sed -n 's/^Password: //p' "$CONFIG_DIR/credentials.txt" | tail -n 1)
  [[ -n $es_url && -n $user && -n $password ]] || {
    printf 'The bootstrap credentials file is incomplete.\n' >&2
    return 0
  }

  tmp_dir=$(mktemp -d "${TMPDIR:-/tmp}/elastic-stack-topology.XXXXXX")
  stats_file="$tmp_dir/stats.json"
  info_file="$tmp_dir/info.json"
  http_file="$tmp_dir/http.json"
  if ! curl_authenticated "$user" "$password" --fail --silent --show-error --cacert "$HTTP_CA_DIR/ca.crt" \
    "$es_url/_nodes/stats/os,jvm?filter_path=nodes.*.name,nodes.*.roles,nodes.*.os.mem.total_in_bytes,nodes.*.jvm.mem.heap_max_in_bytes" \
    > "$stats_file" || ! curl_authenticated "$user" "$password" --fail --silent --show-error --cacert "$HTTP_CA_DIR/ca.crt" \
    "$es_url/_nodes/os?filter_path=nodes.*.name,nodes.*.os.allocated_processors" \
    > "$info_file" || ! curl_authenticated "$user" "$password" --fail --silent --show-error --cacert "$HTTP_CA_DIR/ca.crt" \
    "$es_url/_nodes/http?filter_path=nodes.*.name,nodes.*.http.publish_address" \
    > "$http_file"; then
    rm -rf "$tmp_dir"
    printf 'Could not query the Elasticsearch cluster topology at %s.\n' "$es_url" >&2
    return 0
  fi

  cluster=$(existing_env_value "$CONFIG_DIR/elasticsearch.env" ES_SETTING_CLUSTER_NAME || true)
  cluster=${cluster:-Elastic Stack cluster}
  local_ip=$(existing_env_value "$CONFIG_DIR/elasticsearch.env" ES_SETTING_TRANSPORT_PUBLISH__HOST || true)
  local_ip=${local_ip:-$(hostname -I | awk '{print $1}')}
  local_es_name=$(existing_env_value "$CONFIG_DIR/elasticsearch.env" ES_SETTING_NODE_NAME || true)
  kibana_cpu=$(topology_resource_limit "$UNIT_DIR/kibana.container" cpus 1)
  kibana_memory=$(topology_resource_limit "$UNIT_DIR/kibana.container" memory 2g)
  fleet_cpu=$(topology_resource_limit "$UNIT_DIR/fleet.container" cpus 1)
  fleet_memory=$(topology_resource_limit "$UNIT_DIR/fleet.container" memory 1g)
  kibana_state=$(systemctl is-active kibana.service 2>/dev/null || true)
  fleet_state=$(systemctl is-active fleet.service 2>/dev/null || true)

  install -d -m 0750 "$TOPOLOGY_DIR"
  mmd_file="$TOPOLOGY_DIR/cluster-topology.mmd"
  text_file="$TOPOLOGY_DIR/cluster-topology.txt"
  if ! python3 - "$stats_file" "$info_file" "$http_file" "$mmd_file" "$text_file" "$cluster" "$local_ip" \
    "$local_es_name" "$UNIT_DIR/kibana.container" "$kibana_cpu" "$kibana_memory" "$kibana_state" \
    "$UNIT_DIR/fleet.container" "$fleet_cpu" "$fleet_memory" "$fleet_state" <<'PY'
import json
import os
import sys
import textwrap

stats_path, info_path, http_path, output_path, text_path, cluster, local_ip, local_es_name, kibana_unit, kibana_cpu, kibana_memory, kibana_state, fleet_unit, fleet_cpu, fleet_memory, fleet_state = sys.argv[1:]

with open(stats_path, encoding="utf-8") as source:
    stats = json.load(source).get("nodes", {})
with open(info_path, encoding="utf-8") as source:
    info = json.load(source).get("nodes", {})
with open(http_path, encoding="utf-8") as source:
    http = json.load(source).get("nodes", {})

def memory_label(value):
    value = int(value or 0)
    if value >= 1024 ** 3:
        return f"{value / (1024 ** 3):.1f} GiB"
    if value >= 1024 ** 2:
        return f"{value / (1024 ** 2):.0f} MiB"
    return f"{value} B"

def host_from_address(address):
    if not address:
        return "unknown"
    if address.startswith("["):
        return address.split("]", 1)[0].lstrip("[")
    return address.rsplit(":", 1)[0]

def mermaid_label(value):
    return value.replace("&", "&amp;").replace('"', "&quot;").replace("\n", "<br/>")

nodes = []
hosts = {}
for index, node_id in enumerate(sorted(stats)):
    node_stats = stats[node_id]
    node_info = info.get(node_id, {})
    node_http = http.get(node_id, {}).get("http", {})
    address = node_http.get("publish_address", "")
    host = host_from_address(address)
    roles = node_stats.get("roles", [])
    name = node_stats.get("name", node_id)
    details = [
        "Roles: " + (", ".join(roles) or "coordinating"),
        "HTTP: " + (address or "9200/tcp"),
        "Transport: " + host + ":9300/tcp TLS",
        "CPU: " + str(node_info.get("os", {}).get("allocated_processors", "unknown")) + " cores",
        "Memory: " + memory_label(node_stats.get("os", {}).get("mem", {}).get("total_in_bytes")),
        "JVM heap: " + memory_label(node_stats.get("jvm", {}).get("mem", {}).get("heap_max_in_bytes")),
    ]
    node = {"id": f"es_{index}", "host": host, "roles": roles, "name": name, "details": details, "label": name + "\n" + "\n".join(details)}
    nodes.append(node)
    hosts.setdefault(host, []).append(node)

local_services = []
if kibana_unit and os.path.isfile(kibana_unit):
    local_services.append(("kibana", "Kibana", "5601/tcp", kibana_cpu, kibana_memory, kibana_state))
if fleet_unit and os.path.isfile(fleet_unit):
    local_services.append(("fleet", "Fleet Server", "8220/tcp", fleet_cpu, fleet_memory, fleet_state))
if local_services:
    hosts.setdefault(local_ip or "local", [])

mermaid_lines = ["flowchart LR"]

for host_index, host in enumerate(sorted(hosts)):
    mermaid_lines.append(f'  subgraph host_{host_index}["Host {mermaid_label(host)}"]')
    for node in hosts[host]:
        mermaid_lines.append("    " + node["id"] + '["' + mermaid_label(node["label"]) + '"]')
    if host == (local_ip or "local"):
        for service_id, service_name, port, cpu, memory, state in local_services:
            label = "\n".join([service_name, "Port: " + port, "CPU limit: " + cpu, "Memory limit: " + memory, "State: " + (state or "unknown")])
            mermaid_lines.append("    " + service_id + '_local["' + mermaid_label(label) + '"]')
    mermaid_lines.append("  end")

masters = [node for node in nodes if "master" in node["roles"]]
if masters:
    master = masters[0]
    for node in nodes:
        if node["id"] != master["id"]:
            mermaid_lines.append("  " + master["id"] + " -->|transport 9300/tcp TLS| " + node["id"])
    if local_services:
        mermaid_lines.append("  kibana_local -->|HTTP 9200/tcp| " + master["id"])
        mermaid_lines.append("  fleet_local -->|HTTP 9200/tcp| " + master["id"])

mermaid_lines.extend([
    "  classDef elastic fill:#e8f3f5,stroke:#31576b,color:#102a35;",
    "  classDef service fill:#f5f0db,stroke:#806b22,color:#342a0a;",
    "  class " + ",".join(node["id"] for node in nodes) + " elastic;",
])
if local_services:
    mermaid_lines.append("  class " + ",".join(service[0] + "_local" for service in local_services) + " service;")
with open(output_path, "w", encoding="utf-8") as output:
    output.write("\n".join(mermaid_lines) + "\n")

outer_width = 80
content_width = outer_width - 2
inner_width = 74
inner_content_width = inner_width - 2

def outer(content=""):
    return "|" + content[:content_width].ljust(content_width) + "|"

def outer_border():
    return "+" + "=" * content_width + "+"

def inner_border():
    return outer("  +" + "-" * (inner_width - 2) + "+  ")

def inner_rows(value):
    for wrapped in textwrap.wrap(value, width=inner_content_width, subsequent_indent="  ") or [""]:
        yield outer("  |" + wrapped.ljust(inner_content_width) + "|  ")

def host_role_label(host_nodes):
    roles = {role for node in host_nodes for role in node["roles"]}
    for role, label in [
        ("master", "master node"),
        ("data_hot", "hot data node"),
        ("data_warm", "warm data node"),
        ("ml", "machine-learning node"),
        ("ingest", "ingest node"),
        ("data_content", "content data node"),
    ]:
        if role in roles:
            return label
    return "coordinating node"

terminal_lines = ["", ("Elastic Stack topology: " + cluster).center(outer_width)]
host_names = sorted(hosts)
for host_index, host in enumerate(host_names):
    host_badge = "[" + host_role_label(hosts[host]) + "]"
    host_header = ("  HOST: " + host).ljust(content_width - len(host_badge) - 2) + "  " + host_badge
    terminal_lines.extend([outer_border(), outer(host_header), outer_border(), outer()])
    for node_index, node in enumerate(hosts[host]):
        terminal_lines.append(outer("  Elasticsearch : " + node["name"]))
        terminal_lines.append(inner_border())
        for node_line in node["details"]:
            terminal_lines.extend(inner_rows(node_line))
        terminal_lines.append(inner_border())
        if node_index < len(hosts[host]) - 1:
            terminal_lines.append(outer())
    if host == (local_ip or "local"):
        for _, service_name, port, cpu, memory, state in local_services:
            terminal_lines.extend([outer(), outer("  " + service_name), inner_border()])
            terminal_lines.extend(inner_rows("Port      : " + port))
            terminal_lines.extend(inner_rows(f"CPU limit : {cpu}              Mem limit : {memory}         State : {state or 'unknown'}"))
            terminal_lines.append(inner_border())
    terminal_lines.extend([outer(), outer_border()])
    if host_index < len(host_names) - 1:
        connector_indent = (outer_width - len("|")) // 2
        terminal_lines.extend([
            " " * connector_indent + "|",
            " " * connector_indent + "|  Elasticsearch transport",
            " " * connector_indent + "|  9300/tcp (TLS)",
            " " * connector_indent + "v",
        ])

with open(text_path, "w", encoding="utf-8") as output:
    output.write("\n".join(terminal_lines) + "\n")
PY
  then
    rm -rf "$tmp_dir"
    printf 'Could not build the Mermaid topology source.\n' >&2
    return 0
  fi
  rm -rf "$tmp_dir"
  chmod 0640 "$mmd_file" "$text_file"
  cat "$text_file"
  printf 'Terminal view: %s\nMermaid source: %s\n' "$text_file" "$mmd_file"
}

show_configured_credentials() {
  local answer elastic_url elastic_user elastic_password kibana_url kibana_user kibana_password
  cat <<'EOF'

This displays stored credentials in plain text on this terminal and its scrollback.
EOF
  read -r -p "Type SHOW to reveal locally stored credentials: " answer || cancel
  [[ $answer == SHOW ]] || { printf 'Credential display cancelled.\n'; return 0; }

  info "Locally stored credentials"
  if [[ -f "$CONFIG_DIR/credentials.txt" ]]; then
    elastic_url=$(sed -n 's/^Elasticsearch URL: //p' "$CONFIG_DIR/credentials.txt" | tail -n 1)
    elastic_user=$(sed -n 's/^Username: //p' "$CONFIG_DIR/credentials.txt" | tail -n 1)
    elastic_password=$(sed -n 's/^Password: //p' "$CONFIG_DIR/credentials.txt" | tail -n 1)
    printf 'Browser administrator (elastic):\n  URL: %s\n  Username: %s\n  Password: %s\n' \
      "${elastic_url:-not stored}" "${elastic_user:-not stored}" "${elastic_password:-not stored}"
  else
    printf 'Browser administrator (elastic): not stored on this host.\n'
  fi

  if [[ -f "$CONFIG_DIR/kibana.env" ]]; then
    kibana_url=$(existing_env_value "$CONFIG_DIR/kibana.env" ELASTICSEARCH_HOSTS || true)
    kibana_user=$(existing_env_value "$CONFIG_DIR/kibana.env" ELASTICSEARCH_USERNAME || true)
    kibana_password=$(existing_env_value "$CONFIG_DIR/kibana.env" ELASTICSEARCH_PASSWORD || true)
    printf '\nKibana service account:\n  Elasticsearch URL: %s\n  Username: %s\n  Password: %s\n' \
      "${kibana_url:-not stored}" "${kibana_user:-not stored}" "${kibana_password:-not stored}"
  else
    printf '\nKibana service account: not stored on this host.\n'
  fi
}

show_help() {
  cat <<'EOF'

Workflow:
  1. Bootstrap the first master-only node on one host.
  2. On that host, create one certificate bundle for each remote Elasticsearch node.
  3. Copy the matching bundle securely to the remote host, then join a hot data node.
  4. Join warm, ML, or ingest nodes as needed.
  5. Deploy Kibana or Fleet Server after a hot data node is reachable.
     Kibana may run on another host: enter the Elasticsearch host's HTTP URL
     and that cluster's kibana_system password. Port 9200 must be reachable.

Changing defaults:
  Press Enter to accept a displayed default. Data paths must be absolute paths.
  CPU accepts values such as 2 or 1.5; memory accepts values such as 512m or 4g.
  Elasticsearch memory must be at least its JVM heap.
  At a confirmation prompt, y continues, n or Enter asks again, and q exits.

Existing resources:
  Use action 11 to update a managed application container. Podman cannot update
  a pod directly, so Kibana resources apply to the Kibana container, not its
  infra container. Active services are backed up, recreated, and verified.

Credentials:
  Use action 10 only when you need to reveal local elastic or kibana_system
  credentials. It requires typing SHOW and writes secrets to terminal scrollback.

Topology:
  Use t on the bootstrap host to print a live terminal diagram and write Mermaid source. It groups
  Elasticsearch nodes by published host address and shows roles, ports, CPU,
  memory, JVM heap, and local Kibana/Fleet resource limits.
EOF
}

remove_local_deployment() {
  local answer container kibana_pod key path marker
  cat <<'EOF'

This removes the rootful Elasticsearch, Kibana, and Fleet Server Quadlets,
containers, configuration, certificates, and installer-managed data on this host.
It does not remove images, optional user bind mounts, or unrelated services.
EOF
  read -r -p "Type REMOVE to continue: " answer || cancel
  [[ $answer == REMOVE ]] || { printf 'Removal cancelled.\n'; return; }

  # Remove generator inputs before Podman cleanup so Restart=always cannot recreate workloads.
  systemctl stop kibana.service kibana-pod.service elasticsearch.service fleet.service 2>/dev/null || true
  systemctl disable elasticsearch.service kibana.service kibana-pod.service fleet.service 2>/dev/null || true
  rm -f "$UNIT_DIR/elasticsearch.container" "$UNIT_DIR/kibana.container" "$UNIT_DIR/kibana.pod" "$UNIT_DIR/fleet.container"
  systemctl daemon-reload || die "Systemd reload failed; the Quadlet files were removed but data was kept."

  if command -v podman >/dev/null 2>&1; then
    if podman pod exists kibana-pod 2>/dev/null; then
      podman pod rm -f kibana-pod || die "Could not remove the Kibana pod. Resolve it before deleting its data."
    fi
    if podman container exists kibana-pod-infra 2>/dev/null; then
      kibana_pod=$(podman inspect --format '{{.Pod}}' kibana-pod-infra 2>/dev/null || true)
      [[ -n $kibana_pod ]] || die "Kibana infra container has no pod reference. Resolve it before deleting its data."
      podman pod rm -f "$kibana_pod" || die "Could not remove the Kibana pod. Resolve it before deleting its data."
    fi
    for container in elasticsearch kibana fleet-server; do
      if podman container exists "$container"; then
        podman rm -f "$container" || die "Could not remove container $container. Resolve it before deleting its data."
      fi
    done
    podman pod exists kibana-pod 2>/dev/null && die "Kibana pod still exists; local data was not removed."
    for container in elasticsearch kibana kibana-pod-infra fleet-server; do
      podman container exists "$container" && die "Container $container still exists; local data was not removed."
    done
  fi
  if [[ -f $MANIFEST_FILE ]]; then
    while IFS=$'\t' read -r key path; do
      [[ -n $path ]] || continue
      marker=$(managed_path_marker "$path")
      if valid_path "$path" && [[ -f $marker ]]; then
        rm -rf -- "$path"
      else
        printf 'Preserving unmarked managed-path entry: %s\n' "$path" >&2
      fi
    done < "$MANIFEST_FILE"
  fi
  rm -rf -- "$CONFIG_DIR"
  rmdir "$DATA_DIR" 2>/dev/null || true
  systemctl reset-failed elasticsearch.service kibana.service kibana-pod.service fleet.service 2>/dev/null || true
  printf 'Local rootful Elastic Stack deployment removed.\n'
}

elasticsearch_deployment_exists() {
  [[ -e "$UNIT_DIR/elasticsearch.container" || -d "$DATA_DIR/elasticsearch/nodes" ]]
}

bootstrap_ca_exists() {
  [[ -f "$TRANSPORT_CA_DIR/ca.crt" && -f "$TRANSPORT_CA_DIR/ca.key" && -f "$HTTP_CA_DIR/ca.crt" && -f "$HTTP_CA_DIR/ca.key" ]]
}

menu_container_state() {
  local container=$1 state
  if ! command -v podman >/dev/null 2>&1 || ! podman container exists "$container" 2>/dev/null; then
    printf 'not deployed'
    return 0
  fi
  state=$(podman inspect --format '{{.State.Status}}' "$container" 2>/dev/null || true)
  printf '%s' "${state:-unknown}"
}

print_menu() {
  local host
  host=$(hostname -s)
  cat <<EOF

Elastic Stack Rootful Podman Installer
Host: $host | Elastic Stack: $ES_VERSION
Live: Elasticsearch=$(menu_container_state elasticsearch) | Kibana=$(menu_container_state kibana) | Fleet=$(menu_container_state fleet-server)

  SETUP
  1) Bootstrap master-only node          Create the first TLS-protected cluster manager
  2) Create remote-host certificate bundle  Sign transport, Kibana, and Fleet certificates

  CLUSTER NODES
  3) Join hot data node                  Add hot/content storage to the cluster
  4) Join warm data node                 Add warm/content storage to the cluster
  5) Join machine-learning node          Add ML processing capacity
  6) Join ingest node                    Add ingest pipeline capacity

  SERVICES
  7) Deploy Kibana                       Serve the HTTPS browser interface
  8) Deploy Fleet Server                 Serve HTTPS Elastic Agent enrollment

  OPERATIONS
  9) View local deployment status        Inspect services, containers, and resources
 10) [!] View stored credentials         Reveals secrets after typing SHOW
 11) Adjust managed container resources  Update persistent CPU and memory controls
 12) [!] Remove local deployment         Deletes installer-managed Elastic data after typing REMOVE
  t) View multi-host topology            Print terminal diagram and write Mermaid source
  h) Help and workflow guide
  q) Quit
  0) Exit
EOF
}

MENU_EXIT=false

dispatch_menu_action() {
  local action=${1-}
  action=$(printf '%s' "$action" | tr '[:upper:]' '[:lower:]')
  MENU_EXIT=false
  case "${action}" in
    0|q|quit|exit)
      printf 'No changes made.\n'
      MENU_EXIT=true
      ;;
    h|help|\?) show_help ;;
    9|status) show_status ;;
    t|topology) (show_cluster_topology) ;;
    10|credentials) (show_configured_credentials) ;;
    11|resources) (adjust_existing_resources) ;;
    12|remove) (remove_local_deployment) ;;
    1)
      (
        if elasticsearch_deployment_exists; then
          deploy_bootstrap_master
        else
          prepare_host
          deploy_bootstrap_master
        fi
      )
      ;;
    2)
      (
        if bootstrap_ca_exists; then
          prepare_host
          create_remote_bundle
        else
          printf 'No bootstrap cluster CA exists. Run action 1 on the first cluster host first.\n' >&2
        fi
      )
      ;;
    3) (prepare_host; deploy_joining_node hot 'data_hot,data_content,remote_cluster_client' 2g) ;;
    4) (prepare_host; deploy_joining_node warm 'data_warm,data_content,remote_cluster_client' 2g) ;;
    5) (prepare_host; deploy_joining_node ml 'ml,remote_cluster_client' 4g) ;;
    6) (prepare_host; deploy_joining_node ingest 'ingest,remote_cluster_client' 1g) ;;
    7) (prepare_host; deploy_kibana) ;;
    8) (prepare_host; deploy_fleet) ;;
    *) printf 'Choose 0-12, t, h, or q.\n' >&2 ;;
  esac
}

main() {
  local action
  [[ ${EUID} -eq 0 ]] || die "Run as root."
  while true; do
    print_menu
    action=$(ask "Select an action" "")
    dispatch_menu_action "$action"
    [[ $MENU_EXIT == true ]] && return
  done
}

if [[ ${BASH_SOURCE[0]} == "$0" ]]; then
  main "$@"
fi
