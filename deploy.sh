#!/usr/bin/env bash
# =============================================================================
# deploy.sh - deploy and operate the VCSP external content library on Photon OS
#
# Serves VMware Content Subscription Protocol (VCSP) endpoints that VMware
# Cloud Director catalogs and vCenter content libraries subscribe to, plus
# upload portals for templates and ISOs: one provider library and any number
# of isolated tenant libraries. Supported: Photon OS 4.x and 5.x.
#
#   ./deploy.sh download-prereqs [--dest DIR] [--archive] [--refresh]
#                                 build or update an offline bundle (reused when complete)
#   ./deploy.sh install [--offline DIR] [--non-interactive]  install or upgrade
#   ./deploy.sh verify | status | reindex [--rebuild]
#   ./deploy.sh add-admin USER | remove-admin USER | set-library-password
#   ./deploy.sh cert-csr | cert-install CERT KEY [CHAIN]
#   ./deploy.sh uninstall [--purge]
#
# Tenants (each gets /tenants/NAME/lib/lib.json and /tenants/NAME/upload/):
#   ./deploy.sh tenant-add NAME [--display TEXT] [--admin USER] [--quota-gb N]
#                               [--lib-allow CIDRS] [--admin-allow CIDRS]
#   ./deploy.sh tenant-list | tenant-show NAME
#   ./deploy.sh tenant-update NAME [--display TEXT] [--quota-gb N] [--lib-allow CIDRS] [--admin-allow CIDRS]
#   ./deploy.sh tenant-add-admin NAME USER | tenant-remove-admin NAME USER
#   ./deploy.sh tenant-set-library-password NAME
#   ./deploy.sh tenant-suspend NAME | tenant-resume NAME | tenant-remove NAME [--keep-data]
#
# Non-interactive installs read VCSP_ADMIN_USER, VCSP_ADMIN_PASSWORD and
# VCSP_LIB_PASSWORD from the environment; missing passwords are generated
# and written to /root/vcsp-initial-credentials.txt (mode 0600).
# Non-interactive tenant-add reads VCSP_TENANT_ADMIN_USER (or --admin),
# VCSP_TENANT_ADMIN_PASSWORD (required) and VCSP_TENANT_LIB_PASSWORD
# (generated when empty); tenant-remove needs VCSP_CONFIRM_REMOVE=NAME.
# =============================================================================
set -Eeuo pipefail
umask 022

readonly VCSP_VERSION="2.0.1"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly SCRIPT_DIR
readonly VCSP_HOME=/opt/vcsp
readonly VCSP_ETC=/etc/vcsp
readonly CONF_FILE=$VCSP_ETC/vcsp.conf
readonly TLS_DIR=$VCSP_ETC/tls
readonly VCSP_USER=vcsp
readonly NGINX_CONF=/etc/nginx/nginx.conf
readonly NGINX_HEADERS=/etc/nginx/vcsp-headers.conf
readonly CRED_FILE=/root/vcsp-initial-credentials.txt
readonly BUNDLE_CACHE=/var/cache/vcsp/bundle
readonly TENANT_CONF_DIR=$VCSP_ETC/tenants                 # must match TENANT_CONF_DIR in the Python services
readonly NGINX_TENANT_DIR=/etc/nginx/vcsp-tenants
readonly PROXY_KEY_FILE=$VCSP_ETC/proxy.key                # must match PROXY_KEY_FILE in the Python services
readonly NGINX_PROXY_KEY_CONF=/etc/nginx/vcsp-proxy-key.conf
readonly TENANT_NAME_RE='^[a-z][a-z0-9-]{1,30}[a-z0-9]$'
readonly RESERVED_TENANTS="lib upload api tenants admin provider default static archive"

# Photon OS package names for everything this script and the services run.
# python3-xml carries xml.etree (OVF parsing); openssl creates certificates and
# password hashes; shadow provides useradd; iproute2 provides ip. Photon builds
# util-linux without PAM or libcap-ng, so it ships neither runuser nor setpriv;
# this script therefore drops privileges with python3 (see as_user).
readonly RUNTIME_PKGS=(nginx python3 python3-xml openssl iptables curl tar gzip shadow findutils logrotate
                       coreutils gawk sed grep iproute2)

NON_INTERACTIVE=${VCSP_NON_INTERACTIVE:-0}
GENERATED=()
ADMIN_PW_VERIFY=""
LIB_PW_VERIFY=""

# ----------------------------------------------------------------- logging
log()  { printf '%s %-5s %s\n' "$(date +%H:%M:%S)" "$1" "${*:2}" >&2; }
info() { log INFO "$@"; }
warn() { log WARN "$@"; }
die()  { log ERROR "$@"; exit 1; }
on_err() { log ERROR "command failed (line $1): $2"; }
trap 'on_err $LINENO "$BASH_COMMAND"' ERR

usage() { awk 'NR > 2 && /^# ====/ { exit } NR > 2 { sub(/^# ?/, ""); print }' "${BASH_SOURCE[0]}"; }

require_root() { [[ $EUID -eq 0 ]] || die "run as root"; }

# as_user USER COMMAND [ARGS...]: run COMMAND as USER with USER's groups.
# Photon OS has neither runuser nor setpriv, and su is optional (shadow-tools),
# so privileges are dropped with python3, which is always installed here.
as_user() {
  local user=$1
  shift
  python3 -c 'import os, pwd, sys
u = pwd.getpwnam(sys.argv[1])
os.initgroups(u.pw_name, u.pw_gid)
os.setgid(u.pw_gid)
os.setuid(u.pw_uid)
os.environ.update(HOME=u.pw_dir, USER=u.pw_name, LOGNAME=u.pw_name)
os.execvp(sys.argv[2], sys.argv[2:])' "$user" "$@"
}

# host names without the hostname command (a separate, optional package on Photon OS)
host_fqdn() { python3 -c 'import socket; print(socket.getfqdn())'; }
host_short() { python3 -c 'import socket; print(socket.gethostname())'; }

photon_major() {
  [[ -r /etc/os-release ]] || die "cannot read /etc/os-release"
  # shellcheck disable=SC1091
  local id version
  id=$(. /etc/os-release; echo "${ID:-}")
  version=$(. /etc/os-release; echo "${VERSION_ID:-}")
  [[ $id == photon ]] || die "this host is '$id', not Photon OS"
  echo "${version%%.*}"
}

# ----------------------------------------------------------------- config
load_config() {
  # defaults, overridden by the config file (LIB_NAME is consumed by the Python services)
  # shellcheck disable=SC2034
  LIB_NAME="Enterprise Content Library"; SERVER_FQDN="vcsp.example.local"; PUBLIC_BASE_URL=""
  DATA_ROOT=/srv/vcsp; STATE_DIR=/var/lib/vcsp; LIB_AUTH=basic; LIB_ALLOW_CIDRS=""; ADMIN_ALLOW_CIDRS=""
  UPLOAD_LISTEN=127.0.0.1; UPLOAD_PORT=8080; UPLOAD_CHUNK_MB=64; INDEX_INTERVAL=5min
  HTTP_REDIRECT=yes; LISTEN_IPV6=no; MANAGE_FIREWALL=yes; TLS_ORG="IT Infrastructure"; TENANT_DEFAULT_QUOTA_GB=0
  local src=$CONF_FILE
  [[ -f $src ]] || src=$SCRIPT_DIR/config/vcsp.conf
  [[ -f $src ]] || die "no configuration found ($CONF_FILE or $SCRIPT_DIR/config/vcsp.conf)"
  # shellcheck disable=SC1090
  source "$src"
  CONF_SOURCE=$src
  [[ $SERVER_FQDN =~ ^[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$ ]] || die "SERVER_FQDN '$SERVER_FQDN' is not a valid host name"
  [[ $LIB_AUTH == basic || $LIB_AUTH == none ]] || die "LIB_AUTH must be basic or none"
  [[ $DATA_ROOT == /* && $STATE_DIR == /* ]] || die "DATA_ROOT and STATE_DIR must be absolute paths"
  if ! [[ $UPLOAD_CHUNK_MB =~ ^[0-9]+$ ]] || (( UPLOAD_CHUNK_MB < 1 || UPLOAD_CHUNK_MB > 512 )); then
    die "UPLOAD_CHUNK_MB must be 1-512"
  fi
  [[ $UPLOAD_PORT =~ ^[0-9]+$ ]] || die "UPLOAD_PORT must be a number"
  [[ $TENANT_DEFAULT_QUOTA_GB =~ ^[0-9]+([.][0-9]+)?$ ]] || die "TENANT_DEFAULT_QUOTA_GB must be a number of GiB"
  local cidr
  for cidr in ${LIB_ALLOW_CIDRS//,/ } ${ADMIN_ALLOW_CIDRS//,/ }; do
    [[ $cidr =~ ^[0-9a-fA-F:.]+(/[0-9]{1,3})?$ ]] || die "'$cidr' is not an IP address or CIDR"
  done
  # Resolve symbolic links once. Photon OS ships /srv as a link to var/srv (filesystem
  # package), so the default DATA_ROOT=/srv/vcsp really lives in /var/srv/vcsp. nginx
  # (disable_symlinks) and the systemd sandbox are always given the real directories.
  DATA_ROOT_REAL=$(realpath -m -- "$DATA_ROOT")
  STATE_DIR_REAL=$(realpath -m -- "$STATE_DIR")
}

set_conf_value() {  # set_conf_value KEY VALUE (in the installed config)
  local key=$1 value=$2
  if grep -q "^${key}=" "$CONF_FILE"; then
    sed -i "s|^${key}=.*|${key}=\"${value}\"|" "$CONF_FILE"
  else
    printf '%s="%s"\n' "$key" "$value" >> "$CONF_FILE"
  fi
}

# ----------------------------------------------------------------- prerequisites
# The bundle holds every RPM needed to run all functions (web server, TLS, auth,
# upload portal, indexer, firewall) with its complete dependency closure, plus
# repository metadata and a copy of this project, so it can be carried to an
# air-gapped Photon OS host of the same major version.
#
# Nothing is downloaded when it is not needed:
#   * install skips download and installation when every package is installed,
#     and otherwise installs only the missing packages;
#   * an existing bundle is reused when it is complete and intact (same Photon
#     release and package list, every package present, every checksum correct);
#   * a download goes to a scratch folder and replaces the old bundle only on
#     success, so a failed or interrupted download never destroys a good bundle.
# Use 'download-prereqs --refresh' to fetch newer package versions on purpose.

VERIFIED_BUNDLE=""            # bundle whose checksums were verified or written in this run
BUNDLE_INCOMPLETE_REASON=""

missing_packages() {  # print the RUNTIME_PKGS that are not installed on this host
  local pkg
  for pkg in "${RUNTIME_PKGS[@]}"; do
    rpm -q "$pkg" >/dev/null 2>&1 || printf '%s\n' "$pkg"
  done
}

check_python_modules() {
  python3 -c 'import xml.etree.ElementTree, hashlib, http.server, tarfile' \
    || die "python3 is missing standard modules (is python3-xml installed?)"
}

bundle_rpms_complete() {  # bundle_rpms_complete DIR: 0 when DIR already holds a complete, intact RPM set
  local dir=$1 want have names pkg listed on_disk
  BUNDLE_INCOMPLETE_REASON=""
  if [[ ! -f $dir/BUNDLE.txt || ! -f $dir/SHA256SUMS || ! -f $dir/rpms/repodata/repomd.xml ]]; then
    BUNDLE_INCOMPLETE_REASON="no bundle yet"
    return 1
  fi
  if [[ $(sed -n 's/^photon_major=//p' "$dir/BUNDLE.txt") != "$(photon_major)" ]]; then
    BUNDLE_INCOMPLETE_REASON="the bundle was built for another Photon OS release"
    return 1
  fi
  want=$(printf '%s\n' "${RUNTIME_PKGS[@]}" | sort)
  have=$(sed -n 's/^packages=//p' "$dir/BUNDLE.txt" | tr ' ' '\n' | sed '/^$/d' | sort)
  if [[ $want != "$have" ]]; then
    BUNDLE_INCOMPLETE_REASON="the list of required packages changed"
    return 1
  fi
  names=$(rpm -qp --qf '%{NAME}\n' "$dir"/rpms/*.rpm 2>/dev/null | sort -u || true)
  for pkg in "${RUNTIME_PKGS[@]}"; do
    if ! grep -qxF "$pkg" <<< "$names"; then
      BUNDLE_INCOMPLETE_REASON="$pkg is missing from the bundle"
      return 1
    fi
  done
  listed=$(grep -c '  rpms/[^/]*\.rpm$' "$dir/SHA256SUMS" || true)
  on_disk=$(find "$dir/rpms" -maxdepth 1 -name '*.rpm' | wc -l)
  if [[ $listed != "$on_disk" ]]; then
    BUNDLE_INCOMPLETE_REASON="RPM files were added or removed after the download"
    return 1
  fi
  if ! (cd "$dir" && grep '  rpms/' SHA256SUMS | sha256sum --quiet -c - >/dev/null 2>&1); then
    BUNDLE_INCOMPLETE_REASON="an RPM or the repository metadata is damaged"
    return 1
  fi
  VERIFIED_BUNDLE=$(cd "$dir" && pwd)
}

bundle_age_note() {  # warn when reused RPMs may be missing recent Photon OS updates
  local dir=$1 created now downloaded age max=${VCSP_BUNDLE_MAX_AGE_DAYS:-30}
  created=$(sed -n 's/^created=//p' "$dir/BUNDLE.txt")
  now=$(date -u +%s)
  downloaded=$(date -u -d "$created" +%s 2>/dev/null || echo "$now")
  age=$(( (now - downloaded) / 86400 ))
  if (( age > max )); then
    warn "the RPMs in $dir were downloaded $age days ago; run 'deploy.sh download-prereqs --refresh' to pick up Photon OS updates"
  fi
}

refresh_payload() {  # copy this project into DEST/payload, replacing the old copy only when complete
  local dest=$1 real_dest
  real_dest=$(cd "$dest" && pwd)
  rm -rf "$dest/.payload.new"
  mkdir -p "$dest/.payload.new"
  (cd "$SCRIPT_DIR" && tar --exclude="./${real_dest#"$SCRIPT_DIR"/}" --exclude='./vcsp-offline-bundle*' \
      --exclude='*.pyc' --exclude='__pycache__' -cf - .) | (cd "$dest/.payload.new" && tar -xf -)
  rm -rf "$dest/.payload.old"
  if [[ -d $dest/payload ]]; then
    mv "$dest/payload" "$dest/.payload.old"
  fi
  mv "$dest/.payload.new" "$dest/payload"
  rm -rf "$dest/.payload.old"
}

write_bundle_manifest() {  # write_bundle_manifest DEST RPMS_DOWNLOADED_AT REUSE_RPM_CHECKSUMS(yes|no)
  local dest=$1 created=$2 reuse=$3
  {
    echo "vcsp_version=$VCSP_VERSION"
    echo "photon_major=$(photon_major)"
    echo "created=$created"
    echo "payload_updated=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    echo "host=$(host_short)"
    echo "packages=${RUNTIME_PKGS[*]}"
    echo "rpm_count=$(find "$dest/rpms" -maxdepth 1 -name '*.rpm' | wc -l)"
  } > "$dest/BUNDLE.txt"
  (
    cd "$dest"
    if [[ $reuse == yes ]]; then
      grep '  rpms/' SHA256SUMS            # verified moments ago by bundle_rpms_complete
    else
      find rpms -type f -print0 | sort -z | xargs -0 sha256sum
    fi
    find payload BUNDLE.txt -type f -print0 | sort -z | xargs -0 sha256sum
  ) > "$dest/SHA256SUMS.tmp"
  mv "$dest/SHA256SUMS.tmp" "$dest/SHA256SUMS"
}

download_prerequisites() {  # download_prerequisites DEST [ARCHIVE yes|no] [REFRESH yes|no]
  local dest=$1 archive=${2:-no} refresh=${3:-no} major created reuse=no tmp
  require_root
  major=$(photon_major)
  mkdir -p "$dest"
  if [[ $refresh != yes ]] && bundle_rpms_complete "$dest"; then
    created=$(sed -n 's/^created=//p' "$dest/BUNDLE.txt")
    info "All prerequisites are already in $dest ($(find "$dest/rpms" -maxdepth 1 -name '*.rpm' | wc -l) RPMs, downloaded $created); skipping the download"
    info "Use 'download-prereqs --refresh' to download them again"
    bundle_age_note "$dest"
    reuse=yes
  else
    if [[ $refresh == yes ]]; then
      info "Refreshing prerequisites for Photon OS $major in $dest"
    else
      info "Downloading prerequisites for Photon OS $major into $dest (${BUNDLE_INCOMPLETE_REASON:-no bundle yet})"
    fi
    command -v tdnf >/dev/null || die "tdnf is not available"
    tdnf makecache >/dev/null 2>&1 || die "cannot reach the Photon OS repositories (proxy settings live in /etc/tdnf/tdnf.conf)"
    rpm -q createrepo_c >/dev/null 2>&1 || tdnf install -y -q createrepo_c >/dev/null || die "cannot install createrepo_c"
    # Download into a scratch folder; an existing bundle is replaced only after success.
    # --alldeps ignores the local RPM database, so the bundle holds the full closure
    # even when this download host already has some packages installed.
    tmp="$dest/.rpms.new"
    rm -rf "$tmp"
    mkdir -p "$tmp"
    if ! tdnf install -y --downloadonly --alldeps --downloaddir="$tmp" "${RUNTIME_PKGS[@]}" >/dev/null; then
      rm -rf "$tmp"
      die "package download failed; the existing bundle in $dest (if any) was left unchanged"
    fi
    if ! createrepo_c -q "$tmp"; then
      rm -rf "$tmp"
      die "createrepo_c failed; the existing bundle in $dest (if any) was left unchanged"
    fi
    rm -rf "$dest/.rpms.old"
    if [[ -d $dest/rpms ]]; then
      mv "$dest/rpms" "$dest/.rpms.old"
    fi
    mv "$tmp" "$dest/rpms"
    rm -rf "$dest/.rpms.old"
    created=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  fi

  refresh_payload "$dest"
  write_bundle_manifest "$dest" "$created" "$reuse"
  VERIFIED_BUNDLE=$(cd "$dest" && pwd)
  info "Bundle ready: $(sed -n 's/^rpm_count=//p' "$dest/BUNDLE.txt") RPMs, $(du -sh "$dest" | cut -f1)"

  if [[ $archive == yes ]]; then
    local real_dest tarball
    real_dest=$(cd "$dest" && pwd)
    tarball="$(dirname "$real_dest")/$(basename "$real_dest").tar.gz"
    tar -C "$(dirname "$real_dest")" --exclude='.rpms.*' --exclude='.payload.*' -czf "$tarball" "$(basename "$real_dest")"
    sha256sum "$tarball" > "$tarball.sha256"
    info "Archive: $tarball (checksum in $tarball.sha256)"
    info "On the air-gapped host: tar -xzf $(basename "$tarball") && $(basename "$real_dest")/payload/deploy.sh install --offline \$PWD/$(basename "$real_dest")"
  fi
}

verify_bundle() {
  local dir=$1 major bundle_major
  [[ -f $dir/BUNDLE.txt && -f $dir/SHA256SUMS && -d $dir/rpms/repodata ]] || die "$dir is not a bundle made by 'deploy.sh download-prereqs'"
  major=$(photon_major)
  bundle_major=$(sed -n 's/^photon_major=//p' "$dir/BUNDLE.txt")
  [[ $bundle_major == "$major" ]] || die "bundle is for Photon OS $bundle_major, this host runs Photon OS $major"
  if [[ $(cd "$dir" && pwd) == "$VERIFIED_BUNDLE" ]]; then
    info "Bundle checksums already verified in this run"
  else
    info "Verifying bundle checksums"
    (cd "$dir" && sha256sum --quiet -c SHA256SUMS) || die "bundle checksum verification failed"
  fi
  info "Verifying RPM signatures against the Photon OS keys"
  local key bad
  for key in /etc/pki/rpm-gpg/VMWARE-RPM-GPG-KEY*; do
    [[ -f $key ]] && rpm --import "$key"
  done
  bad=$(rpm -K "$dir"/rpms/*.rpm 2>&1 | grep -v ' OK$' || true)
  if [[ -n $bad ]]; then
    [[ ${VCSP_ALLOW_UNSIGNED:-0} == 1 ]] || die "unsigned or tampered RPMs in bundle:"$'\n'"$bad"
    warn "continuing with unverified RPMs because VCSP_ALLOW_UNSIGNED=1"
  fi
}

install_prerequisites() {
  local bundle=${1:-} missing=()
  mapfile -t missing < <(missing_packages)
  if (( ${#missing[@]} == 0 )); then
    info "All prerequisites are already installed; skipping download and installation"
    check_python_modules
    return
  fi
  info "Missing prerequisites: ${missing[*]}"
  if [[ -z $bundle ]]; then
    # Online: fetch into the local cache (reused when already complete), then install
    # from it, so the offline procedure is exercised on every deployment.
    if (download_prerequisites "$BUNDLE_CACHE" no no); then
      bundle=$BUNDLE_CACHE
      VERIFIED_BUNDLE=$(cd "$BUNDLE_CACHE" && pwd)
    else
      warn "bundle download failed; installing the missing packages directly from the Photon OS repositories"
      tdnf install -y "${missing[@]}" || die "package installation failed"
      check_python_modules
      return
    fi
  fi
  verify_bundle "$bundle"
  info "Installing ${missing[*]} from $bundle"
  tdnf --repofrompath="vcsp-bundle,$bundle/rpms" --repo=vcsp-bundle --nogpgcheck install -y "${missing[@]}" \
    || die "package installation from the bundle failed"
  check_python_modules
}

# ----------------------------------------------------------------- host setup
create_user_and_dirs() {
  getent group "$VCSP_USER" >/dev/null || groupadd -r "$VCSP_USER"
  if ! id "$VCSP_USER" >/dev/null 2>&1; then
    useradd -r -g "$VCSP_USER" -d "$STATE_DIR" -s "$(command -v nologin || echo /bin/false)" \
      -c "VCSP content library" "$VCSP_USER"
  fi
  if [[ $DATA_ROOT_REAL != "$DATA_ROOT" ]]; then
    info "DATA_ROOT $DATA_ROOT resolves to $DATA_ROOT_REAL (on Photon OS /srv is a link to /var/srv); nginx and systemd use the resolved path"
  fi
  local sub
  for sub in lib staging; do
    if [[ -L $DATA_ROOT_REAL/$sub ]]; then
      die "$DATA_ROOT/$sub is a symbolic link to $(readlink -f -- "$DATA_ROOT_REAL/$sub"). nginx does not follow links inside the library. Set DATA_ROOT to the real folder that holds lib/ and staging/, or replace the link with a bind mount (README, Troubleshooting)."
    fi
  done
  install -d -m 0755 "$DATA_ROOT_REAL"
  install -d -m 0755 -o "$VCSP_USER" -g "$VCSP_USER" "$DATA_ROOT_REAL/lib"
  install -d -m 0750 -o "$VCSP_USER" -g "$VCSP_USER" "$DATA_ROOT_REAL/staging"
  install -d -m 0750 -o "$VCSP_USER" -g "$VCSP_USER" "$STATE_DIR_REAL"
  # 0751: nginx workers must traverse into /etc/vcsp to read the htpasswd files;
  # the files themselves stay group-restricted (vcsp.conf -> vcsp, htpasswd/tls -> nginx)
  install -d -m 0751 -o root -g "$VCSP_USER" "$VCSP_ETC"
  install -d -m 0751 -o root -g "$VCSP_USER" "$TENANT_CONF_DIR"
  install -d -m 0755 -o "$VCSP_USER" -g "$VCSP_USER" "$DATA_ROOT_REAL/tenants"
  install -d -m 0750 -o "$VCSP_USER" -g "$VCSP_USER" "$STATE_DIR_REAL/tenants"
  if [[ -n $(find "$DATA_ROOT_REAL/lib" -mindepth 1 ! -user "$VCSP_USER" -print -quit) ]]; then
    info "Taking ownership of existing library content in $DATA_ROOT_REAL/lib"
    chown -R "$VCSP_USER:$VCSP_USER" "$DATA_ROOT_REAL/lib"
  fi
  # nginx must be able to traverse every parent of the library
  local p=$DATA_ROOT_REAL
  while [[ $p != / ]]; do
    [[ $(stat -c %A "$p") == *x ]] || { warn "$p is not world-searchable; nginx cannot reach the library"; }
    p=$(dirname "$p")
  done
  [[ $(stat -c %d "$DATA_ROOT_REAL/lib") == "$(stat -c %d "$DATA_ROOT_REAL/staging")" ]] \
    || warn "$DATA_ROOT/lib and $DATA_ROOT/staging are on different filesystems; publishing will copy data"
  info "Library storage: $(df -h --output=avail "$DATA_ROOT_REAL" | tail -1 | tr -d ' ') free on $(df --output=target "$DATA_ROOT_REAL" | tail -1)"
}

install_payload() {
  # /opt/vcsp mirrors the project layout, so /opt/vcsp/deploy.sh can re-run any command later
  if [[ $SCRIPT_DIR != "$VCSP_HOME" ]]; then
    install -d -m 0755 "$VCSP_HOME" "$VCSP_HOME/bin" "$VCSP_HOME/app/static" "$VCSP_HOME/systemd" \
      "$VCSP_HOME/config" "$VCSP_HOME/tests"
    install -m 0755 "$SCRIPT_DIR/deploy.sh" "$VCSP_HOME/deploy.sh"
    install -m 0755 "$SCRIPT_DIR/bin/vcsp-index" "$VCSP_HOME/bin/vcsp-index"
    install -m 0644 "$SCRIPT_DIR/app/vcsp_upload.py" "$SCRIPT_DIR/app/vcsp_validate.py" "$VCSP_HOME/app/"
    install -m 0644 "$SCRIPT_DIR"/app/static/* "$VCSP_HOME/app/static/"
    install -m 0644 "$SCRIPT_DIR"/systemd/* "$VCSP_HOME/systemd/"
    install -m 0644 "$SCRIPT_DIR/config/vcsp.conf" "$VCSP_HOME/config/vcsp.conf"
    install -m 0644 "$SCRIPT_DIR"/tests/*.py "$VCSP_HOME/tests/"
    [[ -f $SCRIPT_DIR/README.md ]] && install -m 0644 "$SCRIPT_DIR/README.md" "$VCSP_HOME/README.md"
  fi
  ln -sf "$VCSP_HOME/bin/vcsp-index" /usr/local/bin/vcsp-index
  python3 -m py_compile "$VCSP_HOME/app/vcsp_upload.py" "$VCSP_HOME/app/vcsp_validate.py" \
    || die "python syntax check failed"

  if [[ ! -f $CONF_FILE ]]; then
    install -m 0640 -o root -g "$VCSP_USER" "$CONF_SOURCE" "$CONF_FILE"
    info "Configuration installed at $CONF_FILE"
  fi
  if [[ $SERVER_FQDN == vcsp.example.local ]]; then
    local detected
    detected=$(host_fqdn)
    if [[ $NON_INTERACTIVE != 1 && -t 0 ]]; then
      read -rp "DNS name subscribers will use [$detected]: " SERVER_FQDN
      SERVER_FQDN=${SERVER_FQDN:-$detected}
    else
      SERVER_FQDN=$detected
    fi
    set_conf_value SERVER_FQDN "$SERVER_FQDN"
    info "SERVER_FQDN set to $SERVER_FQDN"
  fi
}

# ----------------------------------------------------------------- TLS
build_san() {
  local san="DNS:$SERVER_FQDN" short=${SERVER_FQDN%%.*} ip
  [[ $short != "$SERVER_FQDN" ]] && san+=",DNS:$short"
  while read -r ip; do
    [[ -n $ip ]] && san+=",IP:$ip"
  done < <(if command -v ip >/dev/null; then ip -4 -o addr show scope global | awk '{split($4, a, "/"); print a[1]}'; fi)
  printf '%s' "$san"
}

configure_tls() {
  install -d -m 0750 -o root -g nginx "$TLS_DIR"
  if [[ -s $TLS_DIR/server.crt && -s $TLS_DIR/server.key ]]; then
    openssl x509 -checkend 2592000 -noout -in "$TLS_DIR/server.crt" >/dev/null \
      || warn "the TLS certificate expires within 30 days; renew it with cert-install"
    info "Keeping the existing TLS certificate"
    return
  fi
  local san
  san=$(build_san)
  info "Creating a self-signed certificate for $san"
  openssl req -x509 -newkey rsa:3072 -sha256 -days 825 -nodes \
    -keyout "$TLS_DIR/server.key" -out "$TLS_DIR/server.crt" \
    -subj "/CN=$SERVER_FQDN/O=$TLS_ORG" \
    -addext "subjectAltName=$san" -addext "basicConstraints=critical,CA:FALSE" \
    -addext "keyUsage=critical,digitalSignature,keyEncipherment" -addext "extendedKeyUsage=serverAuth" \
    >/dev/null 2>&1 || die "certificate generation failed"
  chown root:nginx "$TLS_DIR/server.key" "$TLS_DIR/server.crt"
  chmod 0640 "$TLS_DIR/server.key"
  chmod 0644 "$TLS_DIR/server.crt"
  cert_csr quiet
}

cert_csr() {
  [[ -s $TLS_DIR/server.key ]] || die "no private key in $TLS_DIR; run install first"
  openssl req -new -key "$TLS_DIR/server.key" -out "$TLS_DIR/server.csr" -subj "/CN=$SERVER_FQDN/O=$TLS_ORG" \
    -addext "subjectAltName=$(build_san)" >/dev/null 2>&1 || die "CSR generation failed"
  [[ ${1:-} == quiet ]] || info "CSR for your certificate authority: $TLS_DIR/server.csr"
}

cert_install() {
  local cert=${1:-} key=${2:-} chain=${3:-}
  [[ -r $cert && -r $key ]] || die "usage: deploy.sh cert-install CERT KEY [CHAIN]"
  openssl x509 -noout -in "$cert" || die "$cert is not a PEM certificate"
  [[ $(openssl x509 -noout -pubkey -in "$cert") == "$(openssl pkey -pubout -in "$key")" ]] \
    || die "the private key does not match the certificate"
  openssl x509 -noout -ext subjectAltName -in "$cert" 2>/dev/null | grep -q "DNS:$SERVER_FQDN" \
    || warn "the certificate has no subjectAltName for $SERVER_FQDN; subscribers will reject it"
  cp -p "$TLS_DIR/server.crt" "$TLS_DIR/server.crt.bak" 2>/dev/null || true
  cp -p "$TLS_DIR/server.key" "$TLS_DIR/server.key.bak" 2>/dev/null || true
  cat "$cert" ${chain:+"$chain"} > "$TLS_DIR/server.crt"
  install -m 0640 -o root -g nginx "$key" "$TLS_DIR/server.key"
  chown root:nginx "$TLS_DIR/server.crt"
  chmod 0644 "$TLS_DIR/server.crt"
  if nginx -t -q; then
    systemctl reload nginx
    info "Certificate installed. SHA-256 fingerprint: $(fingerprint)"
    info "Subscribers that trusted the old certificate must trust the new one (VCD: Trusted Certificates)."
  else
    mv "$TLS_DIR/server.crt.bak" "$TLS_DIR/server.crt"
    mv "$TLS_DIR/server.key.bak" "$TLS_DIR/server.key"
    die "nginx rejected the certificate; the previous one was restored"
  fi
}

fingerprint() { openssl x509 -noout -fingerprint -sha256 -in "$TLS_DIR/server.crt" | cut -d= -f2; }

# ----------------------------------------------------------------- authentication
read_password() {  # read_password PROMPT ENV_VAR LABEL -> REPLY_PW
  local prompt=$1 envvar=$2 label=$3 v v2
  v=${!envvar:-}
  if [[ -z $v && $NON_INTERACTIVE != 1 && -t 0 ]]; then
    while :; do
      read -rsp "$prompt (12+ characters, Enter to generate one): " v; echo >&2
      [[ -z $v ]] && break
      (( ${#v} >= 12 )) || { warn "use at least 12 characters"; continue; }
      read -rsp "Confirm: " v2; echo >&2
      [[ $v == "$v2" ]] && break
      warn "the passwords do not match"
    done
  fi
  if [[ -z $v ]]; then
    v=$(openssl rand -base64 36)
    v=${v//[^A-Za-z0-9]/}
    v=${v:0:24}
    GENERATED+=("$label: $v")
  fi
  (( ${#v} >= 12 )) || die "$label must be at least 12 characters"
  REPLY_PW=$v
}

set_htpasswd() {  # set_htpasswd FILE USER PASSWORD
  local file=$1 user=$2 pw=$3 hash tmp
  [[ $user =~ ^[A-Za-z0-9._@-]{1,64}$ ]] || die "user names use letters, digits and . _ @ -"
  hash=$(printf '%s' "$pw" | openssl passwd -6 -stdin) || die "password hashing failed"
  tmp=$(mktemp)
  [[ -f $file ]] && { grep -v "^${user}:" "$file" > "$tmp" || true; }
  printf '%s:%s\n' "$user" "$hash" >> "$tmp"
  install -m 0640 -o root -g nginx "$tmp" "$file"
  rm -f "$tmp"
}

configure_auth() {
  local admin=${VCSP_ADMIN_USER:-libadmin}
  if [[ ! -s $VCSP_ETC/htpasswd-admin ]]; then
    read_password "Password for portal administrator '$admin'" VCSP_ADMIN_PASSWORD "Upload portal user $admin"
    set_htpasswd "$VCSP_ETC/htpasswd-admin" "$admin" "$REPLY_PW"
    ADMIN_PW_VERIFY="$admin:$REPLY_PW"
    info "Portal administrator '$admin' created"
  fi
  if [[ $LIB_AUTH == basic && ! -s $VCSP_ETC/htpasswd-library ]]; then
    read_password "Library password for subscribers (user name is always 'vcsp')" VCSP_LIB_PASSWORD \
      "Library subscription password (user vcsp)"
    set_htpasswd "$VCSP_ETC/htpasswd-library" vcsp "$REPLY_PW"
    LIB_PW_VERIFY=$REPLY_PW
    info "Library password set for user 'vcsp'"
  fi
}

add_admin() {
  local user=${1:-}
  [[ -n $user ]] || die "usage: deploy.sh add-admin USER"
  read_password "Password for '$user'" VCSP_ADMIN_PASSWORD "Upload portal user $user"
  set_htpasswd "$VCSP_ETC/htpasswd-admin" "$user" "$REPLY_PW"
  info "Portal user '$user' set"
}

remove_admin() {
  local user=${1:-} tmp
  [[ -n $user ]] || die "usage: deploy.sh remove-admin USER"
  grep -q "^${user}:" "$VCSP_ETC/htpasswd-admin" || die "no portal user '$user'"
  (( $(grep -c . "$VCSP_ETC/htpasswd-admin") > 1 )) || die "refusing to remove the last portal user"
  tmp=$(mktemp)
  grep -v "^${user}:" "$VCSP_ETC/htpasswd-admin" > "$tmp"
  install -m 0640 -o root -g nginx "$tmp" "$VCSP_ETC/htpasswd-admin"
  rm -f "$tmp"
  info "Portal user '$user' removed"
}

set_library_password() {
  [[ $LIB_AUTH == basic ]] || warn "LIB_AUTH is '$LIB_AUTH'; the password takes effect only with LIB_AUTH=basic"
  read_password "New library password (user 'vcsp')" VCSP_LIB_PASSWORD "Library subscription password (user vcsp)"
  set_htpasswd "$VCSP_ETC/htpasswd-library" vcsp "$REPLY_PW"
  info "Library password changed. Update it in every subscriber (VCD: catalog, Subscribe settings)."
}

# ----------------------------------------------------------------- nginx
acl_lines() {  # acl_lines "cidr cidr" -> allow/deny directives
  local cidr out=""
  for cidr in ${1//,/ }; do out+="            allow $cidr;"$'\n'; done
  [[ -n $out ]] && out+="            deny all;"$'\n'
  printf '%s' "$out"
}

render_nginx_conf() {
  local lib_acl admin_acl lib_auth="" listen6="" redirect="" body_mb=$((UPLOAD_CHUNK_MB + 1))
  lib_acl=$(acl_lines "$LIB_ALLOW_CIDRS"; printf x); lib_acl=${lib_acl%x}
  admin_acl=$(acl_lines "$ADMIN_ALLOW_CIDRS"; printf x); admin_acl=${admin_acl%x}
  if [[ $LIB_AUTH == basic ]]; then
    lib_auth=$'            auth_basic "VCSP content library";\n'"            auth_basic_user_file $VCSP_ETC/htpasswd-library;"$'\n'
  fi
  local listen80_6=""
  if [[ $LISTEN_IPV6 == yes ]]; then
    listen6="        listen [::]:443 ssl;"
    listen80_6=$'\n        listen [::]:80;'
  fi
  if [[ $HTTP_REDIRECT == yes ]]; then
    redirect="    server {
        listen 80;$listen80_6
        server_name _;
        location / {
            return 301 https://\$host\$request_uri;
        }
    }"
  fi
  cat <<EOF
# Generated by $VCSP_HOME/deploy.sh ($VCSP_VERSION) - re-run 'deploy.sh install' after editing
# $CONF_FILE; manual edits to this file are overwritten.
user nginx;
worker_processes auto;
worker_rlimit_nofile 8192;
pid /var/run/nginx.pid;
error_log /var/log/nginx/error.log warn;

events {
    worker_connections 2048;
}

http {
    include       /etc/nginx/mime.types;
    default_type  application/octet-stream;
    server_tokens off;

    log_format vcsp '\$remote_addr - \$remote_user [\$time_local] "\$request" \$status '
                    '\$body_bytes_sent "\$http_user_agent" rt=\$request_time';
    access_log /var/log/nginx/access.log vcsp;

    # large template and ISO downloads straight from the page cache
    sendfile           on;
    sendfile_max_chunk 2m;
    tcp_nopush         on;
    tcp_nodelay        on;
    keepalive_timeout  65;
    client_body_buffer_size 1m;
    client_max_body_size    1m;

    gzip            on;
    gzip_types      application/json application/javascript text/css;
    gzip_min_length 1024;

    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_ciphers ECDHE-ECDSA-AES128-GCM-SHA256:ECDHE-RSA-AES128-GCM-SHA256:ECDHE-ECDSA-AES256-GCM-SHA384:ECDHE-RSA-AES256-GCM-SHA384:ECDHE-ECDSA-CHACHA20-POLY1305:ECDHE-RSA-CHACHA20-POLY1305;
    ssl_prefer_server_ciphers off;
    ssl_session_cache   shared:vcsp_tls:10m;
    ssl_session_timeout 1d;
    ssl_session_tickets off;

    # index files must always be revalidated; content files may be cached
    map \$uri \$vcsp_cache_control {
        ~\.json\$  "no-cache";
        default   "";
    }

    limit_req_zone \$binary_remote_addr zone=vcsp_admin:10m rate=20r/s;

    upstream vcsp_upload {
        server $UPLOAD_LISTEN:$UPLOAD_PORT;
    }

$redirect

    server {
        listen 443 ssl;
$listen6
        server_name $SERVER_FQDN;

        ssl_certificate     $TLS_DIR/server.crt;
        ssl_certificate_key $TLS_DIR/server.key;

        # temp files, .vcsp-meta.json and any other dot-file are never served
        location ~ /\. {
            return 404;
        }

        location = / {
            return 302 /upload/;
        }

        # VCSP endpoint - subscription URL: https://$SERVER_FQDN/lib/lib.json
        location /lib/ {
            root $DATA_ROOT_REAL;             # DATA_ROOT=$DATA_ROOT, links resolved
$lib_acl$lib_auth            limit_except GET {
                deny all;
            }
            autoindex off;
            # links above the document root are allowed (Photon OS: /srv -> var/srv);
            # links inside the library are refused so nothing outside it can be exposed
            disable_symlinks on from=\$document_root;
            types {
                application/json          json;
                application/xml           ovf;
                text/plain                mf cert;
                application/octet-stream  vmdk iso nvram ova;
            }
            default_type application/octet-stream;
            add_header Cache-Control \$vcsp_cache_control always;
            include $NGINX_HEADERS;
        }

        # upload portal (static files)
        location /upload/ {
            alias $VCSP_HOME/app/static/;
            index index.html;
$admin_acl            auth_basic "VCSP upload portal";
            auth_basic_user_file $VCSP_ETC/htpasswd-admin;
            limit_req zone=vcsp_admin burst=60 nodelay;
            add_header Cache-Control "no-cache" always;
            include $NGINX_HEADERS;
        }

        # upload portal API - chunks stream to the backend without buffering
        location /upload/api/ {
$admin_acl            auth_basic "VCSP upload portal";
            auth_basic_user_file $VCSP_ETC/htpasswd-admin;
            limit_req zone=vcsp_admin burst=60 nodelay;
            client_max_body_size ${body_mb}m;
            proxy_pass http://vcsp_upload/api/;
            proxy_request_buffering off;
            proxy_buffering off;
            proxy_read_timeout 900s;
            proxy_send_timeout 900s;
            proxy_set_header Host \$host;
            proxy_set_header X-Forwarded-Host \$http_host;
            proxy_set_header X-Remote-User \$remote_user;
            proxy_set_header X-Real-IP \$remote_addr;
            proxy_set_header Authorization "";
            proxy_set_header X-VCSP-Tenant "_provider";
            include $NGINX_PROXY_KEY_CONF;
            include $NGINX_HEADERS;
        }

        # tenant libraries and portals: one generated file per active tenant
        # (deploy.sh tenant-add); unknown or suspended tenants fall through to 404
        location /tenants/ {
            return 404;
        }
        include $NGINX_TENANT_DIR/*.conf;

        location / {
            return 404;
        }
    }
}
EOF
}

render_nginx_headers() {
  cat <<'EOF'
# Security headers for the VCSP content library (included per location).
add_header X-Content-Type-Options "nosniff" always;
add_header X-Frame-Options "DENY" always;
add_header Referrer-Policy "no-referrer" always;
add_header Strict-Transport-Security "max-age=31536000" always;
add_header Content-Security-Policy "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'" always;
EOF
}

configure_proxy_key() {  # shared secret proving to vcsp-upload that a request came through nginx
  if [[ ! -s $PROXY_KEY_FILE ]]; then
    (umask 077; openssl rand -hex 32 > "$PROXY_KEY_FILE")
    info "Generated the nginx-to-backend proxy key"
  fi
  chown root:"$VCSP_USER" "$PROXY_KEY_FILE"
  chmod 0640 "$PROXY_KEY_FILE"
  # nginx reads its configuration as root, so this include can stay 0600
  (umask 077; printf '# Generated by deploy.sh - proves to vcsp-upload that a request came through nginx.\nproxy_set_header X-VCSP-Proxy-Key "%s";\n' \
    "$(cat "$PROXY_KEY_FILE")" > "$NGINX_PROXY_KEY_CONF")
  chmod 0600 "$NGINX_PROXY_KEY_CONF"
}

configure_nginx() {
  [[ -f $NGINX_CONF && ! -f $NGINX_CONF.photon-default ]] && cp -p "$NGINX_CONF" "$NGINX_CONF.photon-default"
  [[ -f $NGINX_CONF ]] && cp -p "$NGINX_CONF" "$NGINX_CONF.vcsp-previous"
  render_nginx_headers > "$NGINX_HEADERS"
  configure_proxy_key
  render_all_tenants
  render_nginx_conf > "$NGINX_CONF"
  if ! nginx -t -q; then
    [[ -f $NGINX_CONF.vcsp-previous ]] && cp -p "$NGINX_CONF.vcsp-previous" "$NGINX_CONF"
    die "nginx rejected the generated configuration; the previous one was restored"
  fi
  if [[ -d /etc/logrotate.d && ! -e /etc/logrotate.d/nginx ]]; then
    cat > /etc/logrotate.d/vcsp-nginx <<'EOF'
/var/log/nginx/*.log {
    daily
    rotate 30
    missingok
    notifempty
    compress
    delaycompress
    sharedscripts
    postrotate
        [ -f /var/run/nginx.pid ] && kill -USR1 "$(cat /var/run/nginx.pid)"
    endscript
}
EOF
  fi
  systemctl enable nginx >/dev/null 2>&1
  systemctl restart nginx
  info "nginx configured for https://$SERVER_FQDN"
}

# ----------------------------------------------------------------- systemd
configure_systemd() {
  install -m 0644 "$VCSP_HOME"/systemd/vcsp-upload.service "$VCSP_HOME"/systemd/vcsp-index.service \
    "$VCSP_HOME"/systemd/vcsp-index.timer /etc/systemd/system/
  local unit
  for unit in vcsp-upload vcsp-index; do
    install -d -m 0755 "/etc/systemd/system/$unit.service.d"
    printf '[Service]\nReadWritePaths=%s %s\n' "$DATA_ROOT_REAL" "$STATE_DIR_REAL" > "/etc/systemd/system/$unit.service.d/10-paths.conf"
  done
  install -d -m 0755 /etc/systemd/system/vcsp-index.timer.d
  printf '[Timer]\nOnUnitActiveSec=\nOnUnitActiveSec=%s\n' "$INDEX_INTERVAL" > /etc/systemd/system/vcsp-index.timer.d/10-interval.conf
  systemctl daemon-reload
  systemctl enable vcsp-upload.service vcsp-index.timer >/dev/null 2>&1
  systemctl restart vcsp-upload.service
  systemctl restart vcsp-index.timer
  systemctl enable --now logrotate.timer >/dev/null 2>&1 || true
  info "Services enabled: vcsp-upload.service, vcsp-index.timer (every $INDEX_INTERVAL)"
}

initial_index() {
  info "Building the library index"
  systemctl start vcsp-index.service || warn "the first index run reported a problem: journalctl -u vcsp-index"
  as_user "$VCSP_USER" python3 "$VCSP_HOME/bin/vcsp-index" --config "$CONF_FILE" --status | head -20 || true
}

# ----------------------------------------------------------------- firewall
configure_firewall() {
  [[ $MANAGE_FIREWALL == yes ]] || { info "Firewall left unchanged (MANAGE_FIREWALL=no)"; return; }
  command -v iptables >/dev/null || { warn "iptables not found; open TCP 443 manually"; return; }
  local ports=(443) port
  [[ $HTTP_REDIRECT == yes ]] && ports+=(80)
  for port in "${ports[@]}"; do
    iptables -C INPUT -p tcp -m tcp --dport "$port" -j ACCEPT 2>/dev/null \
      || iptables -I INPUT -p tcp -m tcp --dport "$port" -j ACCEPT
    if [[ $LISTEN_IPV6 == yes ]] && command -v ip6tables >/dev/null; then
      ip6tables -C INPUT -p tcp -m tcp --dport "$port" -j ACCEPT 2>/dev/null \
        || ip6tables -I INPUT -p tcp -m tcp --dport "$port" -j ACCEPT
    fi
  done
  # Photon OS restores the rules in these files at boot (iptables.service)
  if [[ -d /etc/systemd/scripts ]]; then
    [[ -f /etc/systemd/scripts/ip4save && ! -f /etc/systemd/scripts/ip4save.vcsp-orig ]] \
      && cp -p /etc/systemd/scripts/ip4save /etc/systemd/scripts/ip4save.vcsp-orig
    iptables-save > /etc/systemd/scripts/ip4save
    if [[ $LISTEN_IPV6 == yes ]] && command -v ip6tables-save >/dev/null; then
      ip6tables-save > /etc/systemd/scripts/ip6save
    fi
  fi
  info "Firewall: TCP ${ports[*]} open and persisted"
}

# ----------------------------------------------------------------- tenants
# Each tenant has its own library (DATA_ROOT/tenants/NAME/lib), staging area,
# index state (STATE_DIR/tenants/NAME), portal administrators, subscription
# password and optional quota, described by TENANT_CONF_DIR/NAME/tenant.conf.
# nginx gets one generated file per active tenant in NGINX_TENANT_DIR; it
# authenticates against that tenant's own password files and stamps the tenant
# name on every API request, so tenants cannot reach each other's content.

TENANT_CREATING=""            # set while tenant-add runs; triggers rollback on failure
REPLY_USER=""

tenant_names() {  # tenant_names [active|all]: registered tenants, sorted
  local filter=${1:-all} dir t
  for dir in "$TENANT_CONF_DIR"/*/; do
    [[ -f $dir/tenant.conf ]] || continue
    t=$(basename "$dir")
    [[ $t =~ $TENANT_NAME_RE ]] || continue
    if [[ $filter == active && $(tenant_get "$t" TENANT_STATE) != active ]]; then
      continue
    fi
    printf '%s\n' "$t"
  done
}

tenant_get() {  # tenant_get NAME KEY: value from tenant.conf (parsed, never sourced)
  local file="$TENANT_CONF_DIR/$1/tenant.conf"
  [[ -f $file ]] || return 0
  sed -n "s/^$2=\"\(.*\)\"\$/\1/p" "$file" | tail -n 1
}

tenant_set() {  # tenant_set NAME KEY VALUE (value already validated)
  local file="$TENANT_CONF_DIR/$1/tenant.conf" tmp
  tmp=$(mktemp)
  grep -v "^$2=" "$file" > "$tmp" || true
  printf '%s="%s"\n' "$2" "$3" >> "$tmp"
  install -m 0640 -o root -g "$VCSP_USER" "$tmp" "$file"
  rm -f "$tmp"
}

validate_tenant_name() {  # validate_tenant_name NAME [new|existing]
  local t=$1 mode=${2:-existing}
  [[ -n $t ]] || die "a tenant name is required"
  [[ $t =~ $TENANT_NAME_RE ]] || die "tenant names use 3-32 lowercase letters, digits and hyphens, start with a letter and end with a letter or digit (for example acme-bank)"
  [[ " $RESERVED_TENANTS " != *" $t "* ]] || die "'$t' is reserved; choose another tenant name"
  if [[ $mode == new ]]; then
    [[ ! -e $TENANT_CONF_DIR/$t ]] || die "tenant $t already exists"
    [[ ! -e $DATA_ROOT_REAL/tenants/$t && ! -e $STATE_DIR_REAL/tenants/$t ]] \
      || die "leftover data for '$t' exists under $DATA_ROOT_REAL/tenants or $STATE_DIR_REAL/tenants; remove it or choose another name"
  else
    [[ -f $TENANT_CONF_DIR/$t/tenant.conf ]] || die "tenant $t does not exist (see 'deploy.sh tenant-list')"
  fi
}

validate_display() {
  local re='^[A-Za-z0-9][A-Za-z0-9 ._(),&-]{0,63}$'
  [[ $1 =~ $re ]] || die "display names use up to 64 letters, digits, spaces and . _ ( ) , & -"
}

validate_quota() {
  [[ $1 =~ ^[0-9]+([.][0-9]+)?$ ]] || die "quota must be a number of GiB (0 = unlimited)"
}

validate_cidrs() {
  local cidr
  for cidr in ${1//,/ }; do
    [[ $cidr =~ ^[0-9a-fA-F:.]+(/[0-9]{1,3})?$ ]] || die "'$cidr' is not an IP address or CIDR"
  done
}

read_username() {  # read_username PROMPT ENV_VAR -> REPLY_USER (always required)
  local v=${!2:-}
  if [[ -z $v && $NON_INTERACTIVE != 1 && -t 0 ]]; then
    read -rp "$1: " v
  fi
  [[ -n $v ]] || die "an administrator user name is required (--admin USER or $2)"
  [[ $v =~ ^[A-Za-z0-9._@-]{1,64}$ ]] || die "user names use 1-64 letters, digits and . _ @ -"
  [[ $v != vcsp-verify-* ]] || die "user names starting with vcsp-verify- are reserved"
  REPLY_USER=$v
}

read_required_password() {  # read_required_password PROMPT ENV_VAR LABEL -> REPLY_PW (never generated)
  local prompt=$1 envvar=$2 label=$3 v v2
  v=${!envvar:-}
  if [[ -z $v ]]; then
    [[ $NON_INTERACTIVE != 1 && -t 0 ]] || die "$label is required; set $envvar for non-interactive runs"
    while :; do
      read -rsp "$prompt (12+ characters): " v; echo >&2
      (( ${#v} >= 12 )) || { warn "use at least 12 characters"; continue; }
      read -rsp "Confirm: " v2; echo >&2
      [[ $v == "$v2" ]] && break
      warn "the passwords do not match"
    done
  fi
  (( ${#v} >= 12 )) || die "$label must be at least 12 characters"
  REPLY_PW=$v
}

render_tenant_conf() {  # render_tenant_conf NAME: nginx locations for one tenant
  local t=$1 tdir="$TENANT_CONF_DIR/$1" body_mb=$((UPLOAD_CHUNK_MB + 1)) lib_acl admin_acl
  lib_acl=$(acl_lines "$(tenant_get "$t" TENANT_LIB_ALLOW_CIDRS)"; printf x); lib_acl=${lib_acl%x}
  admin_acl=$(acl_lines "$(tenant_get "$t" TENANT_ADMIN_ALLOW_CIDRS)"; printf x); admin_acl=${admin_acl%x}
  cat <<EOF
# Tenant $t - generated by deploy.sh $VCSP_VERSION; change it with 'deploy.sh tenant-update $t'.
# Subscription URL: https://$SERVER_FQDN/tenants/$t/lib/lib.json   Portal: https://$SERVER_FQDN/tenants/$t/upload/

        location /tenants/$t/lib/ {
            root $DATA_ROOT_REAL;
$lib_acl            auth_basic "VCSP library: $t";
            auth_basic_user_file $tdir/htpasswd-library;
            limit_except GET {
                deny all;
            }
            autoindex off;
            disable_symlinks on from=\$document_root;
            types {
                application/json          json;
                application/xml           ovf;
                text/plain                mf cert;
                application/octet-stream  vmdk iso nvram ova;
            }
            default_type application/octet-stream;
            add_header Cache-Control \$vcsp_cache_control always;
            include $NGINX_HEADERS;
        }

        location /tenants/$t/upload/ {
            alias $VCSP_HOME/app/static/;
            index index.html;
$admin_acl            auth_basic "VCSP portal: $t";
            auth_basic_user_file $tdir/htpasswd-admin;
            limit_req zone=vcsp_admin burst=60 nodelay;
            add_header Cache-Control "no-cache" always;
            include $NGINX_HEADERS;
        }

        location /tenants/$t/upload/api/ {
$admin_acl            auth_basic "VCSP portal: $t";
            auth_basic_user_file $tdir/htpasswd-admin;
            limit_req zone=vcsp_admin burst=60 nodelay;
            client_max_body_size ${body_mb}m;
            proxy_pass http://vcsp_upload/api/;
            proxy_request_buffering off;
            proxy_buffering off;
            proxy_read_timeout 900s;
            proxy_send_timeout 900s;
            proxy_set_header Host \$host;
            proxy_set_header X-Forwarded-Host \$http_host;
            proxy_set_header X-Remote-User \$remote_user;
            proxy_set_header X-Real-IP \$remote_addr;
            proxy_set_header Authorization "";
            proxy_set_header X-VCSP-Tenant "$t";
            include $NGINX_PROXY_KEY_CONF;
            include $NGINX_HEADERS;
        }
EOF
}

render_all_tenants() {  # one nginx file per active tenant; suspended tenants get none (404)
  install -d -m 0755 "$NGINX_TENANT_DIR"
  rm -f "$NGINX_TENANT_DIR"/*.conf
  local t
  while read -r t; do
    [[ -n $t ]] && render_tenant_conf "$t" > "$NGINX_TENANT_DIR/$t.conf"
  done < <(tenant_names active)
}

apply_tenant_nginx() {  # apply_tenant_nginx NAME: (re)write or remove the tenant's nginx file, test, reload
  local t=$1 file="$NGINX_TENANT_DIR/$1.conf" backup=""
  install -d -m 0755 "$NGINX_TENANT_DIR"
  if [[ -f $file ]]; then
    backup=$(mktemp)
    cp -p "$file" "$backup"
  fi
  if [[ $(tenant_get "$t" TENANT_STATE) == active ]]; then
    render_tenant_conf "$t" > "$file"
  else
    rm -f "$file"
  fi
  if ! nginx -t -q; then
    if [[ -n $backup ]]; then mv "$backup" "$file"; else rm -f "$file"; fi
    die "nginx rejected the configuration for tenant $t; the previous one was restored"
  fi
  [[ -n $backup ]] && rm -f "$backup"
  systemctl reload nginx
}

tenant_rollback() {  # EXIT trap during tenant-add: remove whatever a failed onboarding created
  local t=$TENANT_CREATING
  [[ -n $t ]] || return 0
  TENANT_CREATING=""
  warn "onboarding of tenant $t failed; removing what was created"
  rm -f "$NGINX_TENANT_DIR/$t.conf"
  if nginx -t -q 2>/dev/null; then systemctl reload nginx 2>/dev/null || true; fi
  rm -rf "${TENANT_CONF_DIR:?}/$t" "${DATA_ROOT_REAL:?}/tenants/$t" "${STATE_DIR_REAL:?}/tenants/$t"
}

tenant_add() {
  local t=${1:-} display="" quota="" lib_allow="" admin_allow="" admin_pw lib_pw
  shift || true
  while (( $# )); do
    case $1 in
      --display) display=${2:?--display needs a value}; shift 2 ;;
      --admin) export VCSP_TENANT_ADMIN_USER=${2:?--admin needs a user name}; shift 2 ;;
      --quota-gb) quota=${2:?--quota-gb needs a number}; shift 2 ;;
      --lib-allow) lib_allow=${2:?--lib-allow needs CIDRs}; shift 2 ;;
      --admin-allow) admin_allow=${2:?--admin-allow needs CIDRs}; shift 2 ;;
      --non-interactive) NON_INTERACTIVE=1; shift ;;
      *) die "unknown option $1" ;;
    esac
  done
  require_root
  load_config
  [[ -f $CONF_FILE && -x $VCSP_HOME/bin/vcsp-index ]] || die "install the library first: deploy.sh install"
  validate_tenant_name "$t" new
  display=${display:-$t}
  validate_display "$display"
  quota=${quota:-${TENANT_DEFAULT_QUOTA_GB:-0}}
  validate_quota "$quota"
  validate_cidrs "$lib_allow $admin_allow"

  read_username "Administrator user name for tenant $t" VCSP_TENANT_ADMIN_USER
  read_required_password "Password for $REPLY_USER" VCSP_TENANT_ADMIN_PASSWORD "the tenant administrator password"
  admin_pw=$REPLY_PW
  read_password "Library password for subscribers of $t (user name is always 'vcsp')" VCSP_TENANT_LIB_PASSWORD \
    "Library subscription password for tenant $t (user vcsp)"
  lib_pw=$REPLY_PW

  info "Onboarding tenant $t ($display)"
  TENANT_CREATING=$t
  trap tenant_rollback EXIT
  install -d -m 0751 -o root -g "$VCSP_USER" "$TENANT_CONF_DIR" "$TENANT_CONF_DIR/$t"
  local tmp
  tmp=$(mktemp)
  {
    echo "# Tenant $t - managed by deploy.sh (tenant-update, tenant-suspend, tenant-resume)."
    printf 'TENANT_NAME="%s"\nTENANT_DISPLAY_NAME="%s"\nTENANT_CREATED="%s"\nTENANT_STATE="active"\n' \
      "$t" "$display" "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    printf 'TENANT_QUOTA_GB="%s"\nTENANT_LIB_ALLOW_CIDRS="%s"\nTENANT_ADMIN_ALLOW_CIDRS="%s"\n' \
      "$quota" "$lib_allow" "$admin_allow"
  } > "$tmp"
  install -m 0640 -o root -g "$VCSP_USER" "$tmp" "$TENANT_CONF_DIR/$t/tenant.conf"
  rm -f "$tmp"
  set_htpasswd "$TENANT_CONF_DIR/$t/htpasswd-admin" "$REPLY_USER" "$admin_pw"
  set_htpasswd "$TENANT_CONF_DIR/$t/htpasswd-library" vcsp "$lib_pw"
  install -d -m 0755 -o "$VCSP_USER" -g "$VCSP_USER" "$DATA_ROOT_REAL/tenants" "$DATA_ROOT_REAL/tenants/$t" "$DATA_ROOT_REAL/tenants/$t/lib"
  install -d -m 0750 -o "$VCSP_USER" -g "$VCSP_USER" "$DATA_ROOT_REAL/tenants/$t/staging" "$STATE_DIR_REAL/tenants" "$STATE_DIR_REAL/tenants/$t"
  apply_tenant_nginx "$t"
  as_user "$VCSP_USER" python3 "$VCSP_HOME/bin/vcsp-index" --config "$CONF_FILE" --tenant "$t" --wait 60 >/dev/null \
    || die "the first index run for tenant $t failed"

  local base="https://$SERVER_FQDN"
  CHECK_FAILS=0
  check "tenant $t: lib.json without credentials returns 401" test "$(http_code "$base/tenants/$t/lib/lib.json")" = 401
  check "tenant $t: lib.json as user vcsp returns 200" test "$(http_code "$base/tenants/$t/lib/lib.json" "vcsp:$lib_pw")" = 200
  check "tenant $t: portal API answers for $REPLY_USER" test "$(http_code "$base/tenants/$t/upload/api/config" "$REPLY_USER:$admin_pw")" = 200
  check "tenant $t: provider administrators are refused" \
    test "$(probe_code "$VCSP_ETC/htpasswd-admin" "$base/tenants/$t/upload/api/config")" = 401
  (( CHECK_FAILS == 0 )) || die "tenant $t did not pass its checks"
  TENANT_CREATING=""
  trap - EXIT

  if (( ${#GENERATED[@]} )); then
    local cred="/root/vcsp-tenant-$t-credentials.txt"
    {
      echo "# VCSP tenant $t - generated credentials ($(date -u +%Y-%m-%dT%H:%MZ))"
      echo "# Hand the library password to the tenant's Cloud Director administrators, then delete this file."
      printf '%s\n' "${GENERATED[@]}"
    } > "$cred"
    chmod 0600 "$cred"
    warn "The generated library password is in $cred (mode 0600)."
  fi
  echo >&2
  info "Tenant $t is ready"
  info "  Subscription URL : ${PUBLIC_BASE_URL:-$base}/tenants/$t/lib/lib.json   (user: vcsp)"
  info "  Upload portal    : ${PUBLIC_BASE_URL:-$base}/tenants/$t/upload/   (user: $REPLY_USER)"
  info "  Storage quota    : $([[ $quota == 0 ]] && echo unlimited || echo "$quota GiB")"
}

tenant_list() {
  local t n=0 items size quota state
  printf '%-20s %-26s %-10s %6s %6s %9s %9s\n' TENANT "DISPLAY NAME" STATE ADMINS ITEMS SIZE QUOTA
  while read -r t; do
    [[ -n $t ]] || continue
    n=$((n + 1))
    items=$(python3 -c 'import json,sys
try: print(sum(1 for r in json.load(open(sys.argv[1]))["items"].values() if r.get("entry")))
except Exception: print(0)' "$STATE_DIR_REAL/tenants/$t/state.json")
    size=$(du -sh "$DATA_ROOT_REAL/tenants/$t" 2>/dev/null | cut -f1)
    quota=$(tenant_get "$t" TENANT_QUOTA_GB)
    state=$(tenant_get "$t" TENANT_STATE)
    printf '%-20s %-26.26s %-10s %6s %6s %9s %9s\n' "$t" "$(tenant_get "$t" TENANT_DISPLAY_NAME)" "$state" \
      "$(grep -c . "$TENANT_CONF_DIR/$t/htpasswd-admin" 2>/dev/null || echo 0)" "$items" "${size:-0}" \
      "$([[ ${quota:-0} == 0 ]] && echo unlimited || echo "${quota}G")"
  done < <(tenant_names all)
  (( n )) || echo "(no tenants yet; add one with: deploy.sh tenant-add NAME)"
}

tenant_show() {
  local t=$1 base="${PUBLIC_BASE_URL:-https://$SERVER_FQDN}" key
  validate_tenant_name "$t"
  for key in TENANT_DISPLAY_NAME TENANT_STATE TENANT_CREATED TENANT_QUOTA_GB TENANT_LIB_ALLOW_CIDRS TENANT_ADMIN_ALLOW_CIDRS; do
    printf '%-26s %s\n' "$key" "$(tenant_get "$t" "$key")"
  done
  printf '%-26s %s\n' "Subscription URL" "$base/tenants/$t/lib/lib.json (user vcsp)"
  printf '%-26s %s\n' "Upload portal" "$base/tenants/$t/upload/"
  printf '%-26s %s\n' "Administrators" "$(cut -d: -f1 "$TENANT_CONF_DIR/$t/htpasswd-admin" | paste -sd ' ' -)"
  printf '%-26s %s\n' "Library folder" "$DATA_ROOT_REAL/tenants/$t/lib ($(du -sh "$DATA_ROOT_REAL/tenants/$t" 2>/dev/null | cut -f1))"
  echo
  as_user "$VCSP_USER" python3 "$VCSP_HOME/bin/vcsp-index" --config "$CONF_FILE" --tenant "$t" --status || true
}

tenant_update() {
  local t=${1:-} changed=0
  shift || true
  validate_tenant_name "$t"
  while (( $# )); do
    case $1 in
      --display) validate_display "${2:?}"; tenant_set "$t" TENANT_DISPLAY_NAME "$2"; changed=1; shift 2 ;;
      --quota-gb) validate_quota "${2:?}"; tenant_set "$t" TENANT_QUOTA_GB "$2"; changed=1; shift 2 ;;
      --lib-allow) validate_cidrs "${2?}"; tenant_set "$t" TENANT_LIB_ALLOW_CIDRS "$2"; changed=1; shift 2 ;;
      --admin-allow) validate_cidrs "${2?}"; tenant_set "$t" TENANT_ADMIN_ALLOW_CIDRS "$2"; changed=1; shift 2 ;;
      *) die "unknown option $1" ;;
    esac
  done
  (( changed )) || die "nothing to change; use --display, --quota-gb, --lib-allow or --admin-allow"
  apply_tenant_nginx "$t"
  info "Tenant $t updated (a changed display name reaches subscribers on the next index run)"
}

tenant_add_admin() {
  local t=${1:-} user=${2:-} action=added
  validate_tenant_name "$t"
  VCSP_TENANT_ADMIN_USER=$user read_username "Administrator user name for tenant $t" VCSP_TENANT_ADMIN_USER
  grep -q "^${REPLY_USER}:" "$TENANT_CONF_DIR/$t/htpasswd-admin" && action="password reset"
  read_required_password "Password for $REPLY_USER" VCSP_TENANT_ADMIN_PASSWORD "the administrator password"
  set_htpasswd "$TENANT_CONF_DIR/$t/htpasswd-admin" "$REPLY_USER" "$REPLY_PW"
  info "Tenant $t: administrator $REPLY_USER $action"
}

tenant_remove_admin() {
  local t=${1:-} user=${2:-}
  validate_tenant_name "$t"
  [[ -n $user ]] || die "usage: deploy.sh tenant-remove-admin NAME USER"
  grep -q "^${user}:" "$TENANT_CONF_DIR/$t/htpasswd-admin" || die "tenant $t has no administrator '$user'"
  (( $(grep -c . "$TENANT_CONF_DIR/$t/htpasswd-admin") > 1 )) || die "refusing to remove the last administrator of tenant $t"
  remove_htpasswd_users "$TENANT_CONF_DIR/$t/htpasswd-admin" "^${user}:"
  info "Tenant $t: administrator $user removed"
}

tenant_set_library_password() {
  local t=${1:-}
  validate_tenant_name "$t"
  read_password "New library password for tenant $t (user 'vcsp')" VCSP_TENANT_LIB_PASSWORD \
    "Library subscription password for tenant $t (user vcsp)"
  set_htpasswd "$TENANT_CONF_DIR/$t/htpasswd-library" vcsp "$REPLY_PW"
  if (( ${#GENERATED[@]} )); then
    local cred="/root/vcsp-tenant-$t-credentials.txt"
    printf '%s\n' "${GENERATED[@]}" > "$cred"
    chmod 0600 "$cred"
    warn "The generated password is in $cred (mode 0600)."
  fi
  info "Tenant $t: library password changed; update it in the tenant's subscribed catalogs"
}

tenant_suspend() {
  local t=${1:-}
  validate_tenant_name "$t"
  tenant_set "$t" TENANT_STATE suspended
  apply_tenant_nginx "$t"
  info "Tenant $t suspended: its library and portal now return 404; content is kept. Resume with: deploy.sh tenant-resume $t"
}

tenant_resume() {
  local t=${1:-}
  validate_tenant_name "$t"
  tenant_set "$t" TENANT_STATE active
  apply_tenant_nginx "$t"
  info "Tenant $t resumed"
}

tenant_remove() {
  local t=${1:-} keep=${2:-no} answer archive
  validate_tenant_name "$t"
  if [[ ${VCSP_CONFIRM_REMOVE:-} != "$t" ]]; then
    [[ $NON_INTERACTIVE != 1 && -t 0 ]] || die "set VCSP_CONFIRM_REMOVE=$t to remove the tenant non-interactively"
    read -rp "Type the tenant name ($t) to remove it$([[ $keep == yes ]] && echo ', keeping its library in the archive' || echo ' and delete its library'): " answer
    [[ $answer == "$t" ]] || die "removal cancelled"
  fi
  tenant_set "$t" TENANT_STATE removing
  apply_tenant_nginx "$t"                      # access is revoked before any data is touched
  if [[ $keep == yes && -d $DATA_ROOT_REAL/tenants/$t/lib ]]; then
    archive="$DATA_ROOT_REAL/archive/$t-$(date -u +%Y%m%dT%H%M%SZ)"
    install -d -m 0750 -o root -g root "$DATA_ROOT_REAL/archive" "$archive"
    mv "$DATA_ROOT_REAL/tenants/$t/lib" "$archive/lib"
    cp -p "$TENANT_CONF_DIR/$t/tenant.conf" "$archive/tenant.conf"
    info "Library of $t archived in $archive"
  fi
  rm -rf "${DATA_ROOT_REAL:?}/tenants/$t" "${STATE_DIR_REAL:?}/tenants/$t" "${TENANT_CONF_DIR:?}/$t"
  info "Tenant $t removed; its subscription URL and portal no longer exist"
}

# ----------------------------------------------------------------- verification
CHECK_FAILS=0
check() {  # check "description" command...
  local what=$1; shift
  if "$@" >/dev/null 2>&1; then
    printf '  PASS  %s\n' "$what" >&2
  else
    printf '  FAIL  %s\n' "$what" >&2
    CHECK_FAILS=$((CHECK_FAILS + 1))
  fi
}

http_code() {  # http_code URL [user:password]
  local url=$1 cred=${2:-}
  if [[ -n $cred ]]; then
    cred=${cred//\\/\\\\}
    cred=${cred//\"/\\\"}
    printf 'user = "%s"\n' "$cred" | curl -sk --resolve "$SERVER_FQDN:443:127.0.0.1" -K - -o /dev/null \
      -w '%{http_code}' "$url" || true
  else
    curl -sk --resolve "$SERVER_FQDN:443:127.0.0.1" -o /dev/null -w '%{http_code}' "$url" || true
  fi
}

remove_htpasswd_users() {  # remove_htpasswd_users FILE REGEX
  local file=$1 regex=$2 tmp
  [[ -f $file ]] && grep -qE "$regex" "$file" || return 0
  tmp=$(mktemp)
  grep -vE "$regex" "$file" > "$tmp" || true
  install -m 0640 -o root -g nginx "$tmp" "$file"
  rm -f "$tmp"
}

probe_code() {  # probe_code HTPASSWD_FILE URL: HTTP status for a signed-in request
  # Uses a throw-away account so verify never needs the real passwords; nginx
  # reads the password file per request, so no reload is involved.
  local file=$1 url=$2 user pw code
  user="vcsp-verify-$$"
  pw=$(openssl rand -hex 16)
  set_htpasswd "$file" "$user" "$pw"
  code=$(http_code "$url" "$user:$pw")
  remove_htpasswd_users "$file" "^${user}:"
  printf '%s' "$code"
}

library_path_ok() {  # nginx refuses links below the document root
  local p
  for p in "$DATA_ROOT_REAL/lib" "$DATA_ROOT_REAL/lib/lib.json"; do
    [[ -e $p && ! -L $p ]] || return 1
  done
  as_user nginx test -r "$DATA_ROOT_REAL/lib/lib.json"
}

verify() {
  local base="https://$SERVER_FQDN" expected
  CHECK_FAILS=0
  info "Verifying the deployment"
  remove_htpasswd_users "$VCSP_ETC/htpasswd-library" '^vcsp-verify-'   # leftovers of an interrupted run
  remove_htpasswd_users "$VCSP_ETC/htpasswd-admin" '^vcsp-verify-'
  check "nginx configuration is valid" nginx -t -q
  check "nginx is running" systemctl is-active --quiet nginx
  check "upload service is running" systemctl is-active --quiet vcsp-upload
  check "index timer is active" systemctl is-active --quiet vcsp-index.timer
  check "upload backend answers on $UPLOAD_LISTEN:$UPLOAD_PORT" curl -fsS "http://$UPLOAD_LISTEN:$UPLOAD_PORT/api/health"
  check "lib.json on disk is valid JSON" python3 -m json.tool "$DATA_ROOT_REAL/lib/lib.json"
  check "library path is readable by nginx without links ($DATA_ROOT_REAL/lib)" library_path_ok
  check "TLS certificate covers $SERVER_FQDN" sh -c "openssl x509 -noout -ext subjectAltName -in '$TLS_DIR/server.crt' | grep -q 'DNS:$SERVER_FQDN'"
  expected=200
  [[ $LIB_AUTH == basic ]] && expected=401
  check "lib.json without credentials returns $expected" test "$(http_code "$base/lib/lib.json")" = "$expected"
  if [[ $LIB_AUTH == none ]]; then
    :   # the anonymous request above already fetched the file
  elif [[ -n $LIB_PW_VERIFY ]]; then
    check "lib.json as user vcsp returns 200" test "$(http_code "$base/lib/lib.json" "vcsp:$LIB_PW_VERIFY")" = 200
  else
    check "lib.json with valid credentials returns 200" \
      test "$(probe_code "$VCSP_ETC/htpasswd-library" "$base/lib/lib.json")" = 200
  fi
  check "hidden files are not served" test "$(http_code "$base/lib/.vcsp-probe")" = 404
  check "portal requires sign-in" test "$(http_code "$base/upload/")" = 401
  check "backend refuses requests that bypass nginx" \
    test "$(curl -s -o /dev/null -w '%{http_code}' -H 'X-VCSP-Tenant: _provider' "http://$UPLOAD_LISTEN:$UPLOAD_PORT/api/config" || true)" = 403
  if [[ -n $ADMIN_PW_VERIFY ]]; then
    check "portal API answers for the administrator" test "$(http_code "$base/upload/api/config" "$ADMIN_PW_VERIFY")" = 200
  else
    check "portal API answers for a signed-in user" \
      test "$(probe_code "$VCSP_ETC/htpasswd-admin" "$base/upload/api/config")" = 200
  fi
  local t tdir
  while read -r t; do
    [[ -n $t ]] || continue
    tdir=$TENANT_CONF_DIR/$t
    remove_htpasswd_users "$tdir/htpasswd-library" '^vcsp-verify-'
    remove_htpasswd_users "$tdir/htpasswd-admin" '^vcsp-verify-'
    check "tenant $t: lib.json without credentials returns 401" test "$(http_code "$base/tenants/$t/lib/lib.json")" = 401
    check "tenant $t: lib.json with valid credentials returns 200" \
      test "$(probe_code "$tdir/htpasswd-library" "$base/tenants/$t/lib/lib.json")" = 200
    check "tenant $t: portal API answers for a signed-in user" \
      test "$(probe_code "$tdir/htpasswd-admin" "$base/tenants/$t/upload/api/config")" = 200
    check "tenant $t: provider administrators are refused" \
      test "$(probe_code "$VCSP_ETC/htpasswd-admin" "$base/tenants/$t/upload/api/config")" = 401
  done < <(tenant_names active)
  if (( CHECK_FAILS )); then
    warn "$CHECK_FAILS check(s) failed; see /var/log/nginx/error.log and journalctl -u vcsp-upload"
    warn "'(20: Not a directory)' in the nginx log means a folder in the library path is a symbolic link"
    return 1
  fi
  info "All checks passed"
}

status() {
  systemctl --no-pager --lines=0 status nginx vcsp-upload vcsp-index.timer 2>/dev/null | grep -E '^(●|○|\S+ )|Active:' || true
  echo
  as_user "$VCSP_USER" python3 "$VCSP_HOME/bin/vcsp-index" --config "$CONF_FILE" --status || true
  echo
  df -h "$DATA_ROOT_REAL" | tail -1 | awk '{printf "Library storage: %s used of %s (%s free)\n", $3, $2, $4}'
  if [[ $DATA_ROOT_REAL != "$DATA_ROOT" ]]; then
    echo "Library folder: $DATA_ROOT_REAL/lib (DATA_ROOT $DATA_ROOT resolves here)"
  else
    echo "Library folder: $DATA_ROOT_REAL/lib"
  fi
  local missing
  missing=$(missing_packages | tr '\n' ' ')
  echo "Prerequisites: ${missing:+missing: }${missing:-all installed}"
  echo "Subscription URL: ${PUBLIC_BASE_URL:-https://$SERVER_FQDN}/lib/lib.json"
  echo "Certificate SHA-256: $(fingerprint 2>/dev/null || echo unavailable)"
  echo
  tenant_list
}

reindex() {
  as_user "$VCSP_USER" python3 "$VCSP_HOME/bin/vcsp-index" --config "$CONF_FILE" --wait 300 "$@"
}

# ----------------------------------------------------------------- install / uninstall
install_all() {
  local offline=$1 major
  require_root
  major=$(photon_major)
  [[ $major == 4 || $major == 5 ]] || warn "tested on Photon OS 4 and 5; this host runs $major"
  load_config
  info "Installing VCSP content library $VCSP_VERSION (config: $CONF_SOURCE)"
  install_prerequisites "$offline"
  create_user_and_dirs
  install_payload
  load_config                # re-read the installed config (SERVER_FQDN may have been set)
  configure_tls
  configure_auth
  configure_nginx
  configure_systemd
  configure_firewall
  initial_index
  sleep 1
  verify || true

  local url=${PUBLIC_BASE_URL:-https://$SERVER_FQDN}
  echo >&2
  info "Subscription URL : $url/lib/lib.json$([[ $LIB_AUTH == basic ]] && echo '   (user: vcsp)')"
  info "Upload portal    : $url/upload/"
  info "Certificate SHA-256 fingerprint (compare when Cloud Director asks you to trust it):"
  info "  $(fingerprint)"
  if (( ${#GENERATED[@]} )); then
    {
      echo "# VCSP content library - generated credentials ($(date -u +%Y-%m-%dT%H:%MZ))"
      echo "# Move these to your password vault, then delete this file."
      printf '%s\n' "${GENERATED[@]}"
    } > "$CRED_FILE"
    chmod 0600 "$CRED_FILE"
    warn "Generated passwords were written to $CRED_FILE (mode 0600). Vault them and delete the file."
  fi
}

uninstall() {
  local purge=${1:-no} answer
  require_root
  load_config
  systemctl disable --now vcsp-upload.service vcsp-index.timer >/dev/null 2>&1 || true
  rm -rf /etc/systemd/system/vcsp-upload.service* /etc/systemd/system/vcsp-index.service* /etc/systemd/system/vcsp-index.timer*
  systemctl daemon-reload
  if [[ -f $NGINX_CONF.photon-default ]]; then
    cp -p "$NGINX_CONF.photon-default" "$NGINX_CONF"
    rm -f "$NGINX_HEADERS" "$NGINX_PROXY_KEY_CONF"
    rm -rf "$NGINX_TENANT_DIR"
    systemctl restart nginx || true
  fi
  rm -f /usr/local/bin/vcsp-index /etc/logrotate.d/vcsp-nginx
  rm -rf "$VCSP_HOME"
  info "Services and program files removed; firewall rules for 443/80 were left in place"
  if [[ $purge == yes ]]; then
    if [[ ${VCSP_CONFIRM_PURGE:-} != yes ]]; then
      read -rp "Type 'purge' to delete $DATA_ROOT, $STATE_DIR and $VCSP_ETC permanently: " answer
      [[ $answer == purge ]] || die "purge cancelled; data kept"
    fi
    rm -rf "$DATA_ROOT_REAL" "$STATE_DIR_REAL" "$VCSP_ETC"
    info "Library data, index state and configuration deleted"
  else
    info "Kept: library $DATA_ROOT, index state $STATE_DIR, configuration $VCSP_ETC"
  fi
}

# ----------------------------------------------------------------- entry point
main() {
  local cmd=${1:-help}
  shift || true
  case $cmd in
    download-prereqs)
      local dest="" archive=no refresh=no
      while (( $# )); do
        case $1 in
          --dest) dest=${2:?--dest needs a folder}; shift 2 ;;
          --archive) archive=yes; shift ;;
          --refresh) refresh=yes; shift ;;
          *) die "unknown option $1" ;;
        esac
      done
      [[ -n $dest ]] || dest="$PWD/vcsp-offline-bundle-photon$(photon_major)"
      download_prerequisites "$dest" "$archive" "$refresh"
      ;;
    install)
      local offline=""
      while (( $# )); do
        case $1 in
          --offline) offline=$(cd "${2:?--offline needs the bundle folder}" && pwd); shift 2 ;;
          --non-interactive) NON_INTERACTIVE=1; shift ;;
          *) die "unknown option $1" ;;
        esac
      done
      if [[ -z $offline && -d $SCRIPT_DIR/../rpms/repodata && -f $SCRIPT_DIR/../BUNDLE.txt ]]; then
        offline=$(cd "$SCRIPT_DIR/.." && pwd)
        info "Running from an offline bundle: $offline"
      fi
      install_all "$offline"
      ;;
    verify) require_root; load_config; verify ;;
    status) require_root; load_config; status ;;
    reindex) require_root; load_config; reindex "$@" ;;
    add-admin) require_root; load_config; add_admin "${1:-}" ;;
    remove-admin) require_root; load_config; remove_admin "${1:-}" ;;
    set-library-password) require_root; load_config; set_library_password ;;
    cert-csr) require_root; load_config; cert_csr ;;
    cert-install) require_root; load_config; cert_install "${1:-}" "${2:-}" "${3:-}" ;;
    uninstall)
      if [[ ${1:-} == --purge ]]; then uninstall yes; else uninstall no; fi
      ;;
    tenant-add) tenant_add "$@" ;;
    tenant-list) require_root; load_config; tenant_list ;;
    tenant-show) require_root; load_config; tenant_show "${1:-}" ;;
    tenant-update) require_root; load_config; tenant_update "$@" ;;
    tenant-add-admin) require_root; load_config; tenant_add_admin "${1:-}" "${2:-}" ;;
    tenant-remove-admin) require_root; load_config; tenant_remove_admin "${1:-}" "${2:-}" ;;
    tenant-set-library-password) require_root; load_config; tenant_set_library_password "${1:-}" ;;
    tenant-suspend) require_root; load_config; tenant_suspend "${1:-}" ;;
    tenant-resume) require_root; load_config; tenant_resume "${1:-}" ;;
    tenant-remove)
      require_root; load_config
      if [[ ${2:-} == --keep-data ]]; then tenant_remove "${1:-}" yes; else tenant_remove "${1:-}" no; fi
      ;;
    help|-h|--help) usage ;;
    *) usage; die "unknown command '$cmd'" ;;
  esac
}

if [[ ${BASH_SOURCE[0]} == "$0" ]]; then
  main "$@"
fi
