#!/usr/bin/env bash
# KubeCanvas host agent installer (read-only agent, ADR-0008).
#
# Download this file, verify it against install.sh.sha256, read it, then run it as root:
#
#   sudo bash install.sh --hub hub.example.com:443 --token <one-time token from the Add host screen> #        --command-key <public key from the Add host screen> --host-id <host id from the Add host screen>
#
# What it does: installs the agent binary, creates the system user kubecanvas-hostagent, writes
# /etc/kubecanvas-hostagent/hostagent.env, installs the hardened systemd unit and starts it.
# It never opens a listening port. Remove everything with: sudo bash install.sh --uninstall
set -euo pipefail

SERVICE=kubecanvas-hostagent
BIN=/usr/local/bin/${SERVICE}
UNIT=/etc/systemd/system/${SERVICE}.service
ETC=/etc/${SERVICE}
STATE=/var/lib/${SERVICE}
DOWNLOAD_URL_DEFAULT="https://raw.githubusercontent.com/HabibQayoom/Scripts/main/kubecanvas/hostagent"

hub=""
token=""
binary=""
ca_file=""
command_key=""
host_id=""
insecure_dev=0
signing_key=""
skip_signature_check=0
allow_downgrade=0
uninstall=0
mode=readonly
download_url="${KC_HOSTAGENT_DOWNLOAD_URL:-$DOWNLOAD_URL_DEFAULT}"

usage() {
  cat <<'USAGE'
usage: install.sh --hub HOST:PORT --token TOKEN --command-key KEY[,KEY2] --host-id UUID [options]
       install.sh --uninstall

  --hub HOST:PORT     the KubeCanvas hub address (required)
  --token TOKEN       one-time enrollment token from the Add host screen (required on first install)
  --command-key KEYS  base64 Ed25519 public key(s) that sign commands to this host, at most 4 comma separated
                      (required on first install; an upgrade keeps the old value when omitted)
  --host-id UUID      this host's id from the Add host screen (required on first install; kept on upgrade)
  --binary PATH       install this agent binary instead of downloading one
  --ca-file PATH      private CA bundle for the hub certificate
  --mode MODE         readonly (default) or managed; must match the mode chosen when the host was added
  --signing-key PATH  the pinned public key (cosign, PEM file) that signed the release manifest (version and
                      checksum); the manifest is verified with it using `cosign verify-blob` before the agent
                      download is trusted or installed
  --insecure-skip-signature-check
                      install a downloaded agent without verifying its signature (the checksum alone proves
                      nothing about who made it); never use this unless you checked the file yourself
  --allow-downgrade   install a downloaded agent even when its signed version is older than the installed one
                      (the default refuses: an old signed release replayed by the download origin)
  --insecure-dev      plaintext connection to the hub (local development only)
  --uninstall         stop the agent and remove everything it installed
USAGE
}

die() { echo "install.sh: $*" >&2; exit 1; }

while [ $# -gt 0 ]; do
  case "$1" in
    --hub) hub="${2:-}"; shift 2 ;;
    --token) token="${2:-}"; shift 2 ;;
    --binary) binary="${2:-}"; shift 2 ;;
    --ca-file) ca_file="${2:-}"; shift 2 ;;
    --command-key) command_key="${2:-}"; shift 2 ;;
    --host-id) host_id="${2:-}"; shift 2 ;;
    --signing-key) signing_key="${2:-}"; shift 2 ;;
    --insecure-skip-signature-check) skip_signature_check=1; shift ;;
    --allow-downgrade) allow_downgrade=1; shift ;;
    --insecure-dev) insecure_dev=1; shift ;;
    --mode) mode="${2:-}"; shift 2 ;;
    --uninstall) uninstall=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; die "unknown option: $1" ;;
  esac
done

case "$mode" in
  readonly|managed) ;;
  *) die "--mode must be readonly or managed" ;;
esac

# Values that end up in hostagent.env: a newline, other control character or space would inject a second
# setting (or a second line of the unit), so refuse them here, before anything needs root.
bad_value() { # name value
  case "$2" in
    *[[:space:][:cntrl:]]*|*\"*|*\'*|*\\*) die "$1 must not contain spaces, quotes, backslashes or control characters" ;;
  esac
}
bad_value --hub "$hub"
bad_value --token "$token"
bad_value --command-key "$command_key"
bad_value --host-id "$host_id"

# Command keys (ADR-0015): base64 (standard) of exactly 32 bytes each = 43 characters and one "=", at most 4.
valid_command_keys() { # comma separated list
  local list="$1" key count=0 IFS=,
  [ -n "$list" ] || return 1
  [[ "$list" =~ ^[^,]+(,[^,]+)*$ ]] || return 1
  for key in $list; do
    count=$((count + 1))
    [[ "$key" =~ ^[A-Za-z0-9+/]{43}=$ ]] || return 1
  done
  [ "$count" -ge 1 ] && [ "$count" -le 4 ]
}
valid_host_id() { [[ "$1" =~ ^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$ ]]; }
if [ -n "$command_key" ] && [ "$uninstall" -eq 0 ]; then
  valid_command_keys "$command_key" || die "--command-key must be 1 to 4 comma separated base64 Ed25519 public keys (32 bytes each)"
fi
if [ -n "$host_id" ] && [ "$uninstall" -eq 0 ]; then
  valid_host_id "$host_id" || die "--host-id must be a UUID"
fi
[ "$(id -u)" -eq 0 ] || die "run as root (for example: sudo bash install.sh ...)"
command -v systemctl >/dev/null 2>&1 || die "systemd is required (systemctl not found)"

if [ "$uninstall" -eq 1 ]; then
  systemctl disable --now "$SERVICE" 2>/dev/null || true
  rm -f "$UNIT" "$BIN"
  rm -rf "$ETC" "$STATE"
  systemctl daemon-reload
  userdel "$SERVICE" 2>/dev/null || true
  echo "KubeCanvas host agent removed."
  exit 0
fi

[ -n "$hub" ] || { usage >&2; die "--hub is required"; }
case "$hub" in
  *:[0-9]*) ;;
  *) die "--hub must look like host:port" ;;
esac
if [ -z "$token" ] && [ ! -s "${STATE}/credential.json" ]; then
  die "--token is required on first install (generate one in the Add host screen)"
fi

# A first install (no credential yet) must pin the key and the host id: without them the agent would
# refuse every command. An upgrade keeps the values already in hostagent.env.
old_env() { # name
  [ -r "${ETC}/hostagent.env" ] || return 0
  sed -n "s/^$1=//p" "${ETC}/hostagent.env" | tail -n 1 | tr -d '[:cntrl:]'
}
if [ -z "$command_key" ]; then
  old="$(old_env KC_HOST_COMMAND_KEY)"
  if [ -n "$old" ] && valid_command_keys "$old"; then command_key="$old"; fi
fi
if [ -z "$host_id" ]; then
  old="$(old_env KC_HOST_ID)"
  if [ -n "$old" ] && valid_host_id "$old"; then host_id="$old"; fi
fi
if [ ! -s "${STATE}/credential.json" ]; then
  [ -n "$command_key" ] || die "--command-key is required on first install (copy it from the Add host screen)"
  [ -n "$host_id" ] || die "--host-id is required on first install (copy it from the Add host screen)"
fi

arch() {
  case "$(uname -m)" in
    x86_64|amd64) echo amd64 ;;
    aarch64|arm64) echo arm64 ;;
    *) die "unsupported architecture: $(uname -m) (amd64 and arm64 are supported)" ;;
  esac
}

# The checksum file comes from the same origin as the binary, so it proves nothing about who built it.
# The release signs a manifest (version, file name, checksum; cosign sign-blob, key pair); the public key is pinned by the user
# through --signing-key (the Add host screen shows it). Without a key, or without cosign, a download is
# refused unless --insecure-skip-signature-check is given. A local --binary needs no download check.
# KC-SIG-BEGIN
verify_signature() { # file signature_file
  if [ "$skip_signature_check" -eq 1 ]; then
    echo "install.sh: WARNING: signature check skipped (--insecure-skip-signature-check)" >&2
    return 0
  fi
  [ -n "$signing_key" ] || die "no signing key: pass --signing-key PATH (the public key from the Add host screen), or --insecure-skip-signature-check, or --binary PATH"
  [ -r "$signing_key" ] || die "--signing-key: cannot read $signing_key"
  command -v cosign >/dev/null 2>&1 || die "cosign is required to verify the download (https://docs.sigstore.dev); install it, or pass --binary PATH, or --insecure-skip-signature-check"
  [ -s "$2" ] || die "the signature file is missing or empty: $2"
  cosign verify-blob --key "$signing_key" --signature "$2" --insecure-ignore-tlog=true "$1" >/dev/null 2>&1     || die "the signature of $1 does not verify against the pinned key; do not install this download"
}
# KC-SIG-END

# The signed manifest binds the version and the checksum to the file name, so an old signed agent cannot be
# replayed under a new name and a manifest of another file cannot be replayed under this one. Exact format
# (four lines, nothing else):
#   kubecanvas-hostagent-manifest 1 / version=<semver> / file=<asset name> / sha256=<64 lowercase hex>
# KC-MANIFEST-BEGIN
num_cmp() { # a b: numeric strings of any length -> -1, 0 or 1
  local a="$1" b="$2"
  a="${a#"${a%%[!0]*}"}"; b="${b#"${b%%[!0]*}"}"
  if [ "${#a}" -lt "${#b}" ]; then echo -1; return; fi
  if [ "${#a}" -gt "${#b}" ]; then echo 1; return; fi
  if [[ "$a" < "$b" ]]; then echo -1; elif [[ "$a" > "$b" ]]; then echo 1; else echo 0; fi
}

semver_cmp() { # a b: semantic versions -> -1 (a older), 0, 1 (a newer); build metadata is ignored
  local a="${1%%+*}" b="${2%%+*}" ca cb pa="" pb="" i x y r
  ca="${a%%-*}"; cb="${b%%-*}"
  [ "$ca" = "$a" ] || pa="${a#*-}"
  [ "$cb" = "$b" ] || pb="${b#*-}"
  local IFS=.
  local -a xa xb
  read -ra xa <<< "$ca"; read -ra xb <<< "$cb"
  for i in 0 1 2; do
    r="$(num_cmp "${xa[i]:-0}" "${xb[i]:-0}")"
    if [ "$r" != 0 ]; then echo "$r"; return; fi
  done
  if [ -z "$pa" ] && [ -z "$pb" ]; then echo 0; return; fi
  if [ -z "$pa" ]; then echo 1; return; fi   # a release is newer than its pre-release
  if [ -z "$pb" ]; then echo -1; return; fi
  read -ra xa <<< "$pa"; read -ra xb <<< "$pb"
  i=0
  while [ "$i" -lt "${#xa[@]}" ] && [ "$i" -lt "${#xb[@]}" ]; do
    x="${xa[i]}"; y="${xb[i]}"
    if [[ "$x" =~ ^[0-9]+$ ]] && [[ "$y" =~ ^[0-9]+$ ]]; then r="$(num_cmp "$x" "$y")"
    elif [[ "$x" =~ ^[0-9]+$ ]]; then r=-1      # numeric identifiers are lower than alphanumeric ones
    elif [[ "$y" =~ ^[0-9]+$ ]]; then r=1
    elif [[ "$x" < "$y" ]]; then r=-1
    elif [[ "$x" > "$y" ]]; then r=1
    else r=0; fi
    if [ "$r" != 0 ]; then echo "$r"; return; fi
    i=$((i + 1))
  done
  if [ "${#xa[@]}" -lt "${#xb[@]}" ]; then echo -1; elif [ "${#xa[@]}" -gt "${#xb[@]}" ]; then echo 1; else echo 0; fi
}

installed_version() { # agent binary: prints its semantic version, or nothing when absent or unknown
  local out=""
  [ -x "$1" ] || return 0
  if command -v timeout >/dev/null 2>&1; then
    out="$(timeout 10 "$1" --version 2>/dev/null | head -n 1)" || out=""
  else
    out="$("$1" --version 2>/dev/null | head -n 1)" || out=""
  fi
  if [[ "$out" =~ ^kubecanvas-hostagent\ ([0-9]+\.[0-9]+\.[0-9]+([-+][A-Za-z0-9.]+)?)$ ]]; then
    echo "${BASH_REMATCH[1]}"
  fi
}

read_manifest() { # manifest_file asset_name: sets manifest_version and manifest_sha or dies
  local -a l
  local n
  [ -f "$1" ] || die "the release manifest is missing: $1"
  n="$(wc -l < "$1")"
  mapfile -t l < "$1"
  [ "${#l[@]}" -eq 4 ] && [ "$((n + 0))" -eq 4 ] || die "the release manifest is malformed (it must be exactly four lines)"
  [ "${l[0]}" = "kubecanvas-hostagent-manifest 1" ] || die "the release manifest has an unknown format"
  [[ "${l[1]}" =~ ^version=([0-9]+\.[0-9]+\.[0-9]+([-+][A-Za-z0-9.]+)?)$ ]] || die "the release manifest has no valid version line"
  manifest_version="${BASH_REMATCH[1]}"
  [ "${l[2]}" = "file=$2" ] || die "the release manifest is for a different file than $2; do not install this download"
  [[ "${l[3]}" =~ ^sha256=([0-9a-f]{64})$ ]] || die "the release manifest has no valid sha256 line"
  manifest_sha="${BASH_REMATCH[1]}"
}

refuse_downgrade() { # new_version installed_version
  [ -n "$2" ] || return 0
  if [ "$(semver_cmp "$1" "$2")" = "-1" ]; then
    if [ "$allow_downgrade" -eq 1 ]; then
      echo "install.sh: WARNING: installing version $1 over the newer installed version $2 (--allow-downgrade)" >&2
      return 0
    fi
    die "refusing to install version $1: the installed agent is version $2 (a downgrade; pass --allow-downgrade if you really want it)"
  fi
}
# KC-MANIFEST-END

fetch_binary() {
  local target="$1" name base tmp have got
  name="${SERVICE}-linux-$(arch)"
  base="${download_url%/}"
  tmp="$(mktemp -d)"
  trap 'rm -rf "$tmp"' RETURN
  # HTTPS only, TLS 1.2 or newer, no redirect to another protocol; plain HTTP only with --insecure-dev.
  local curl_tls="--proto =https --tlsv1.2" wget_tls="--https-only"
  if [ "$insecure_dev" -eq 1 ]; then curl_tls=""; wget_tls=""; fi
  if command -v curl >/dev/null 2>&1; then
    # shellcheck disable=SC2086
    curl -fsSL $curl_tls -o "${tmp}/${name}" "${base}/${name}"
    # shellcheck disable=SC2086
    curl -fsSL $curl_tls -o "${tmp}/${name}.manifest" "${base}/${name}.manifest"
    # shellcheck disable=SC2086
    curl -fsSL $curl_tls -o "${tmp}/${name}.manifest.sig" "${base}/${name}.manifest.sig" || true
  elif command -v wget >/dev/null 2>&1; then
    # shellcheck disable=SC2086
    wget -q $wget_tls -O "${tmp}/${name}" "${base}/${name}"
    # shellcheck disable=SC2086
    wget -q $wget_tls -O "${tmp}/${name}.manifest" "${base}/${name}.manifest"
    # shellcheck disable=SC2086
    wget -q $wget_tls -O "${tmp}/${name}.manifest.sig" "${base}/${name}.manifest.sig" || true
  else
    die "curl or wget is required to download the agent (or pass --binary PATH)"
  fi
  verify_signature "${tmp}/${name}.manifest" "${tmp}/${name}.manifest.sig"
  read_manifest "${tmp}/${name}.manifest" "$name"
  # The downgrade rule also applies with --insecure-skip-signature-check (the version then comes from the unsigned manifest).
  have="$(installed_version "$BIN")"
  refuse_downgrade "$manifest_version" "$have"
  got="$(sha256sum "${tmp}/${name}" | cut -d' ' -f1)"
  [ "$got" = "$manifest_sha" ] || die "checksum of the downloaded agent does not match the signed manifest"
  install -m 0755 "${tmp}/${name}" "$target"
}

if [ -n "$binary" ]; then
  [ -f "$binary" ] || die "--binary: no such file: $binary"
  install -m 0755 "$binary" "$BIN"
else
  fetch_binary "$BIN"
fi

[ "$mode" = managed ] || id -u "$SERVICE" >/dev/null 2>&1 || useradd --system --no-create-home --home-dir "$STATE" --shell /usr/sbin/nologin "$SERVICE"

install -d -m 0755 "$ETC"
umask 077
{
  echo "KC_HOST_HUB=${hub}"
  [ -z "$token" ] || echo "KC_HOST_ENROLLMENT_TOKEN=${token}"
  [ -z "$ca_file" ] || echo "KC_HOST_CA_FILE=${ca_file}"
  [ -z "$command_key" ] || echo "KC_HOST_COMMAND_KEY=${command_key}"
  [ -z "$host_id" ] || echo "KC_HOST_ID=${host_id}"
} > "${ETC}/hostagent.env"
chmod 0600 "${ETC}/hostagent.env"  # read by systemd (root) before it drops privileges

# The unit below must equal deploy/hostagent/kubecanvas-hostagent.service (a unit test checks).
unit_extra=""
if [ "$insecure_dev" -eq 1 ]; then
  unit_extra=" --insecure-dev"
fi
# KC-UNIT-BEGIN
cat > "$UNIT" <<'UNITFILE'
[Unit]
Description=KubeCanvas host agent (read-only)
Documentation=https://github.com/HabibQayoom/infrasolutions
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=kubecanvas-hostagent
Group=kubecanvas-hostagent
EnvironmentFile=/etc/kubecanvas-hostagent/hostagent.env
ExecStart=/usr/local/bin/kubecanvas-hostagent --state-dir /var/lib/kubecanvas-hostagent --mode readonly
Restart=always
RestartSec=5
StateDirectory=kubecanvas-hostagent
StateDirectoryMode=0700
UMask=0077

# Read-only agent: no write capability of any kind (ADR-0008). The only writable path is its own
# state directory (the credential file); the only capability is reading files other users own.
NoNewPrivileges=yes
CapabilityBoundingSet=CAP_DAC_READ_SEARCH
AmbientCapabilities=CAP_DAC_READ_SEARCH
ProtectSystem=strict
ProtectHome=read-only
PrivateTmp=no
PrivateDevices=yes
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectControlGroups=yes
ProtectClock=yes
ProtectHostname=yes
RestrictNamespaces=yes
RestrictRealtime=yes
RestrictSUIDSGID=yes
LockPersonality=yes
MemoryDenyWriteExecute=yes
SystemCallArchitectures=native
SystemCallFilter=@system-service
SystemCallErrorNumber=EPERM
# Outbound only: no listening sockets (the metrics endpoint binds to 127.0.0.1 inside the process).
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX

# Never make a sick host sicker: the footprint budget is under 1.5 % of one core and 60 MB.
Nice=10
CPUQuota=25%
MemoryHigh=96M
MemoryMax=128M
TasksMax=256

[Install]
WantedBy=multi-user.target
UNITFILE
# KC-UNIT-END
if [ "$mode" = managed ]; then
# KC-MANAGED-UNIT-BEGIN
cat > "$UNIT" <<'UNITFILE'
[Unit]
Description=KubeCanvas host agent (managed)
Documentation=https://github.com/HabibQayoom/infrasolutions
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
Group=root
EnvironmentFile=/etc/kubecanvas-hostagent/hostagent.env
ExecStart=/usr/local/bin/kubecanvas-hostagent --state-dir /var/lib/kubecanvas-hostagent --mode managed
Restart=always
RestartSec=5
StateDirectory=kubecanvas-hostagent
StateDirectoryMode=0700
UMask=0077

# Managed agent (ADR-0012): the only unit where privileges grow, and they grow by name. It runs the closed
# command catalog with fixed tools (useradd, usermod, chage, gpasswd, visudo, kill) and nothing else.
NoNewPrivileges=yes
CapabilityBoundingSet=CAP_CHOWN CAP_DAC_OVERRIDE CAP_DAC_READ_SEARCH CAP_FOWNER CAP_KILL CAP_SETUID CAP_SETGID
AmbientCapabilities=CAP_CHOWN CAP_DAC_OVERRIDE CAP_DAC_READ_SEARCH CAP_FOWNER CAP_KILL CAP_SETUID CAP_SETGID
ProtectSystem=strict
# /etc is writable because the shadow tools write a temporary file next to /etc/passwd and rename it.
# The login databases are written by useradd (found by the container test: without them it fails with
# a read-only file system). TODO(verify): narrow /etc on every distribution in the container matrix.
ReadWritePaths=/etc -/home -/root -/var/spool/mail -/var/mail -/var/log/lastlog -/var/log/faillog -/var/log/tallylog
ProtectHome=no
PrivateTmp=no
PrivateDevices=yes
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectControlGroups=yes
ProtectClock=yes
ProtectHostname=yes
RestrictNamespaces=yes
RestrictRealtime=yes
RestrictSUIDSGID=no
LockPersonality=yes
MemoryDenyWriteExecute=yes
SystemCallArchitectures=native
SystemCallFilter=@system-service
SystemCallErrorNumber=EPERM
# Outbound only: no listening sockets (the metrics endpoint binds to 127.0.0.1 inside the process).
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX

# Never make a sick host sicker: the footprint budget is under 1.5 % of one core and 60 MB.
Nice=10
CPUQuota=25%
MemoryHigh=96M
MemoryMax=128M
TasksMax=256

[Install]
WantedBy=multi-user.target
UNITFILE
# KC-MANAGED-UNIT-END
fi
if [ -n "$unit_extra" ]; then
  sed -i "s|^ExecStart=.*|&${unit_extra}|" "$UNIT"
fi
chmod 0644 "$UNIT"

systemctl daemon-reload
# enable --now does nothing for a running unit: an upgrade must restart it to run the new binary.
systemctl enable "$SERVICE"
systemctl restart "$SERVICE"
echo "KubeCanvas host agent installed and started (${mode}). Watch it with: journalctl -u ${SERVICE} -f"
