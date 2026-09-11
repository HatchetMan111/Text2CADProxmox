#!/usr/bin/env bash
###############################################################################
# text2CAD — Proxmox LXC Einzeiler-Installer (Community-Scripts-Stil)
#
#   bash -c "$(wget -qLO - https://raw.githubusercontent.com/HatchetMan111/Text2CADProxmox/main/install/text2cad.sh)"
#
# Was es tut (Host-seitig):
#   1. waehlt freie CT-ID (wenn vergeben -> naechste), Hostname text2cad
#   2. erstellt + startet Debian-12 LXC (onboot=1), installiert alles im CT
#   3. App: FastAPI + cadgen/build123d (earthtojake/text-to-cad) + STL/STEP/3MF
#      Web-UI auf http://[LXC-IP]:8080 mit Provider-Dropdown (OpenRouter/OmniRouter)
#   4. systemd-Service text2cad (Restart=always), Verifikation + URL-Ausgabe
#
# Idempotent: erneut laufen lassen repariert/aktualisiert statt zu crashen.
# Debugging: DEBUG=1 bash -x install/text2cad.sh  (oder var_verbose=yes)
# Fehler: IMMER komplette Kette (Befehl, Exit-Code, stdout/stderr, Journal).
###############################################################################
set -euo pipefail

# ---------------- Variablen (oben, Community-Scripts-konform) ----------------
APP="text2CAD"
HOSTNAME_BASE="${var_hostname:-${HOSTNAME_BASE:-text2cad}}"  # LXC-Name: text2cad
CTID_REQ="${var_ctid:-${CTID:-0}}"          # 0 = auto (naechste freie ab 200)
CTID_START="${CTID_START:-200}"
VAR_CPU="${var_cpu:-2}"
VAR_RAM="${var_ram:-2048}"                  # MiB
VAR_DISK="${var_disk:-8}"                   # GiB
VAR_STORAGE="${var_storage:-local-lvm}"
VAR_TEMPLATE_STORAGE="${var_template_storage:-local}"
VAR_BRIDGE="${var_bridge:-vmbr0}"
VAR_IP="${var_ip:-dhcp}"                    # z.B. dhcp oder 192.168.1.50/24
VAR_GW="${var_gateway:-}"                   # nur bei statischer IP noetig
VAR_UNPRIVILEGED="${var_unprivileged:-1}"
VAR_TAGS="${var_tags:-ai;cad;3d-print}"
VAR_PORT="${var_port:-8080}"
GITHUB_REPO="${GITHUB_REPO:-https://github.com/HatchetMan111/Text2CADProxmox}"
GITHUB_BRANCH="${GITHUB_BRANCH:-main}"
VERBOSE="${var_verbose:-${DEBUG:-0}}"

# ---------------- Farben / Logging ----------------
if [[ -t 1 ]]; then GN='\e[32m'; YW='\e[33m'; RD='\e[31m'; CL='\e[0m'; BLD='\e[1m'; else GN=''; YW=''; RD=''; CL=''; BLD=''; fi
msg_info(){ echo -e "${BLD}[INFO]${CL} $*"; }
msg_ok(){ echo -e "${GN}[OK]${CL} $*"; }
msg_warn(){ echo -e "${YW}[WARN]${CL} $*"; }
msg_error(){ echo -e "${RD}[FEHLER]${CL} $*" >&2; }
header(){ echo -e "${GN}=== ${APP} Proxmox-Installer ===${CL}\nRepo: ${GITHUB_REPO} (${GITHUB_BRANCH}) | Port: ${VAR_PORT}"; }

# Komplette Fehlerkette bei jedem Fehler
FAIL_CMD=""; FAIL_LINE=0
trap 'FAIL_LINE=${LINENO}; FAIL_CMD=${BASH_COMMAND}' DEBUG
on_err(){
  local rc=$?
  msg_error "Abbruch (Exit ${rc}) in Zeile ${FAIL_LINE}: ${FAIL_CMD}"
  msg_error "Hinweis: erneut mit DEBUG=1 starten fuer bash -x Log:"
  msg_error "  DEBUG=1 bash -x ./install/text2cad.sh"
  msg_error "Host-Logs: journalctl -xe ; pct logs <CTID> ; pveversion -v"
  exit "${rc}"
}
trap on_err ERR
[[ "${VERBOSE}" == "1" || "${VERBOSE}" == "yes" ]] && set -x

need_root(){ [[ ${EUID} -eq 0 ]] || { msg_error "Bitte als root auf dem Proxmox-Host ausfuehren."; exit 1; }; }
need_pve(){
  command -v pct >/dev/null || { msg_error "pct nicht gefunden — kein Proxmox-Host?"; exit 1; }
  command -v pveam >/dev/null || { msg_error "pveam nicht gefunden."; exit 1; }
  msg_ok "Proxmox-Host erkannt: $(pveversion 2>/dev/null | head -n1 || echo unknown)"
}

# VMIDs teilen sich EINEN Namensraum fuer LXC *und* QEMU-VMs — darum immer
# beides pruefen (pct status sieht keine VMs, qm status keine Container).
guest_exists(){
  local id="$1"
  pct status "${id}" >/dev/null 2>&1 && return 0
  if command -v qm >/dev/null 2>&1; then qm status "${id}" >/dev/null 2>&1 && return 0; fi
  [[ -e "/etc/pve/lxc/${id}.conf" || -e "/etc/pve/qemu-server/${id}.conf" ]] && return 0
  return 1
}
ct_exists(){ guest_exists "$1"; }  # Alias (Rueckwaertskompatibilitaet)
hostname_taken(){
  {
    pct list 2>/dev/null | awk 'NR>1{print $3}';
    if command -v qm >/dev/null 2>&1; then qm list 2>/dev/null | awk 'NR>1{print $2}'; fi
  } | grep -qx "$1"
}

next_free_id(){
  local id="$1"
  if [[ "${id}" -eq 0 ]]; then id="${CTID_START}"; fi
  while guest_exists "${id}"; do id=$((id+1)); done
  echo "${id}"
}
free_hostname(){
  local base="$1"
  [[ -z "${base}" ]] && base="text2cad"
  local h="${base}"; local n=2
  while hostname_taken "${h}"; do h="${base}-${n}"; n=$((n+1)); done
  echo "${h}"
}

ensure_template(){
  # WICHTIG: Alles ausser der letzten echo-Zeile MUSS nach stderr — der Aufrufer
  # macht TPL=$(ensure_template) und wuerde Logzeilen sonst in den Pfad einbauen.
  local stor="${VAR_TEMPLATE_STORAGE}"
  msg_info "Suche Debian-12 LXC-Template auf Storage '${stor}' …" >&2
  local avail; avail=$(pveam available --section system 2>/dev/null | grep -o 'debian-12-standard_[^ ]*amd64.tar.[a-z0-9]*' | sort -V | tail -n1 || true)
  local local_tpl; local_tpl=$(pveam list "${stor}" 2>/dev/null | grep -o 'debian-12-standard_[^ ]*amd64.tar.[a-z0-9]*' | sort -V | tail -n1 || true)
  if [[ -n "${local_tpl}" ]]; then msg_ok "Template vorhanden: ${stor}:vztmpl/${local_tpl}" >&2; echo "${stor}:vztmpl/${local_tpl}"; return 0; fi
  [[ -z "${avail}" ]] && { msg_error "Kein debian-12 Template gefunden (pveam available leer). Netzwerk/DNS pruefen."; pveam update >&2; avail=$(pveam available --section system 2>/dev/null | grep -o 'debian-12-standard_[^ ]*amd64.tar.[a-z0-9]*' | sort -V | tail -n1 || true); }
  [[ -z "${avail}" ]] && { msg_error "Weiterhin kein Template. Ausgabe von 'pveam available':"; pveam available --section system 2>&1 | head -n30 >&2; exit 1; }
  msg_info "Lade Template ${avail} … (dauert beim ersten Mal)" >&2
  pveam download "${stor}" "${avail}" >&2
  echo "${stor}:vztmpl/${avail}"
}

wait_ct_network(){
  local ctid="$1"; local tries=30
  msg_info "Warte auf Netzwerk in CT ${ctid} …"
  for ((i=1;i<=tries;i++)); do
    if pct exec "${ctid}" -- bash -c "getent hosts deb.debian.org >/dev/null 2>&1 || ping -c1 -W2 1.1.1.1 >/dev/null 2>&1"; then msg_ok "CT-Netzwerk OK."; return 0; fi
    sleep 4
  done
  msg_error "CT hat kein Netzwerk nach $((tries*4))s. Bridge '${VAR_BRIDGE}' / DHCP / Firewall pruefen. 'pct config ${ctid}' ausgeben:"
  pct config "${ctid}" || true
  exit 1
}

# ---------------- Hauptablauf ----------------
main(){
  header
  need_root; need_pve

  local CTID; CTID=$(next_free_id "${CTID_REQ}")
  if [[ "${CTID_REQ}" != "0" ]] && guest_exists "${CTID_REQ}"; then
    msg_warn "Gast-ID ${CTID_REQ} vergeben (LXC oder VM) — nehme naechste freie: ${CTID}"
  else
    msg_info "CT-ID: ${CTID}"
  fi
  local HN; HN=$(free_hostname "${HOSTNAME_BASE}")
  [[ "${HN}" != "${HOSTNAME_BASE}" ]] && msg_warn "Hostname '${HOSTNAME_BASE}' vergeben — nutze '${HN}'."
  msg_info "Hostname: ${HN} | CPU: ${VAR_CPU} | RAM: ${VAR_RAM}MiB | Disk: ${VAR_DISK}G | Storage: ${VAR_STORAGE}"

  local TPL; TPL=$(ensure_template)

  # Storage pruefen/fallbacken
  if ! pvesm status --storage "${VAR_STORAGE}" >/dev/null 2>&1; then
    msg_warn "Storage '${VAR_STORAGE}' fehlt — fallback auf 'local'."
    VAR_STORAGE="local"
  fi

  # Netz-Args
  local NET="name=eth0,bridge=${VAR_BRIDGE},ip=${VAR_IP}"
  if [[ "${VAR_IP}" != "dhcp" ]]; then
    [[ -z "${VAR_GW}" ]] && { msg_error "Statische IP '${VAR_IP}' braucht VAR_GW (var_gateway). Oder VAR_IP=dhcp nutzen."; exit 1; }
    NET="${NET},gw=${VAR_GW}"
  fi

  if ! pct status "${CTID}" >/dev/null 2>&1; then
    # Kein Container mit dieser ID — aber evtl. eine VM (gleicher ID-Raum)?
    while guest_exists "${CTID}"; do
      msg_warn "ID ${CTID} ist durch eine VM belegt — weiche auf naechste freie ID aus."
      CTID=$(next_free_id "$((CTID+1))")
    done
    msg_info "Erstelle LXC ${CTID} (${HN}) …"
    local attempt=0 errf; errf=$(mktemp)
    while true; do
      if pct create "${CTID}" "${TPL}" \
        --hostname "${HN}" \
        --cores "${VAR_CPU}" --memory "${VAR_RAM}" --swap 512 \
        --rootfs "${VAR_STORAGE}:${VAR_DISK}" \
        --net0 "${NET}" \
        --ostype debian --arch amd64 \
        --unprivileged "${VAR_UNPRIVILEGED}" --features "nesting=1" \
        --onboot 1 --start 0 \
        --tags "${VAR_TAGS}" \
        --password "$(openssl rand -base64 12 | tr -d '/+=' | cut -c1-16)" 2>"${errf}"; then
        rm -f "${errf}"
        break
      fi
      if grep -qi "already exists" "${errf}" && [[ ${attempt} -lt 10 ]]; then
        attempt=$((attempt+1))
        CTID=$(next_free_id "$((CTID+1))")
        msg_warn "ID kollidiert (Versuch ${attempt}/10) — versuche ${CTID} …"
        continue
      fi
      msg_error "pct create schlug fehl (CTID ${CTID}). Vollausgabe:"
      cat "${errf}" >&2; rm -f "${errf}"
      msg_error "Diagnose (vollstaendig, nicht nur letzte Zeile):"
      pct status "${CTID}" 2>&1 || true
      if command -v qm >/dev/null 2>&1; then qm status "${CTID}" 2>&1 || true; fi
      ls -la "/etc/pve/lxc/${CTID}.conf" "/etc/pve/qemu-server/${CTID}.conf" 2>&1 || true
      pct config "${CTID}" 2>&1 || true
      exit 1
    done
    msg_ok "Container erstellt (CTID ${CTID})."
  else
    msg_warn "CT ${CTID} existiert bereits — idempotenter Re-Run (kein Neu-Erstellen)."
  fi

  # onboot sicherstellen (reboot-sicher)
  pct set "${CTID}" --onboot 1 || true

  msg_info "Starte CT ${CTID} …"
  pct start "${CTID}" || msg_warn "pct start meldete Fehler (evtl. schon laufend)."
  sleep 5
  wait_ct_network "${CTID}"

  msg_info "Installiere ${APP} im Container (Debian + Python + cadgen + systemd) …"
  pct push "${CTID}" /dev/stdin /tmp/text2cad-provision.sh <<PROVISION_EOF
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
export APP_PORT="${VAR_PORT}"
export GITHUB_REPO="${GITHUB_REPO}"
export GITHUB_BRANCH="${GITHUB_BRANCH}"
echo "[CT] Provisioniere text2CAD (Port \${APP_PORT}, Repo \${GITHUB_REPO}) …"
apt-get update
apt-get install -y --no-install-recommends python3 python3-venv python3-pip git curl ca-certificates iproute2 \
  libgl1 libglib2.0-0 libsm6 libxext6 libxrender1 libocct-foundation-7.6 libocct-data-exchange-7.6 || {
  echo "[CT] WARN: OCCT-Libs z.T. fehlend — versuche ohne Versions-Pin"
  apt-get install -y --no-install-recommends python3 python3-venv python3-pip git curl ca-certificates iproute2 libgl1 libglib2.0-0 || exit 1
}
mkdir -p /opt/text2cad/app /opt/text2cad/data
if [[ -d /opt/text2cad-src/.git ]]; then
  echo "[CT] Update bestehendes Repo …"
  git -C /opt/text2cad-src pull --ff-only || git -C /opt/text2cad-src fetch origin "\${GITHUB_BRANCH}" && git -C /opt/text2cad-src reset --hard "origin/\${GITHUB_BRANCH}"
else
  rm -rf /opt/text2cad-src
  if [[ "\${GITHUB_REPO}" == *"USER/REPO"* ]]; then
    echo "[CT] FEHLER: GITHUB_REPO enthaelt noch USER/REPO-Platzhalter."
    echo "[CT] Entweder Repo forken + Variable setzen, oder Installer mit GITHUB_REPO=https://github.com/DEINUSER/DEINREPO aufrufen."
    exit 1
  fi
  git clone --depth 1 --branch "\${GITHUB_BRANCH}" "\${GITHUB_REPO}" /opt/text2cad-src
fi
cp -r /opt/text2cad-src/app/* /opt/text2cad/app/
cp /opt/text2cad-src/systemd/text2cad.service /etc/systemd/system/text2cad.service
sed -i "s/--port 8080/--port \${APP_PORT}/; s/:8080/:\${APP_PORT}/" /etc/systemd/system/text2cad.service || true
REQ_HASH=$(sha256sum /opt/text2cad/app/requirements.txt | awk '{print $1}')
if [[ ! -d /opt/text2cad/.venv || ! -f /opt/text2cad/.venv.reqhash || "$(cat /opt/text2cad/.venv.reqhash 2>/dev/null)" != "${REQ_HASH}" ]]; then
  echo "[CT] Baue Python-venv (neu/frisch, Hash ${REQ_HASH:0:12}…) — alte OCP-Mischungen werden restlos entfernt."
  rm -rf /opt/text2cad/.venv
  python3 -m venv /opt/text2cad/.venv
  /opt/text2cad/.venv/bin/pip install --upgrade pip wheel
  /opt/text2cad/.venv/bin/pip install -r /opt/text2cad/app/requirements.txt
  echo "${REQ_HASH}" > /opt/text2cad/.venv.reqhash
else
  echo "[CT] venv aktuell (Requirements unverändert) — kein Neuaufbau."
fi
# CAD-Smoke-Test: faengt kaputte OCP-Umgebungen SOFORT mit voller Kette ab (statt erst beim ersten Modell)
echo "[CT] CAD-Smoke-Test (OCP-Kernel + build123d) …"
/opt/text2cad/.venv/bin/python -c "from cadgen import build123d as bd; v=bd.Box(10,10,10).volume; assert abs(v-1000)<1e-6, v; print('CAD-SMOKE OK, Box-Volumen:', v)" || {
  echo "[CT] FEHLER: CAD-Smoke-Test fehlgeschlagen. Installierte OCP-Pakete:"; /opt/text2cad/.venv/bin/pip list 2>/dev/null | grep -iE 'ocp|gordon|build123d|cadgen|vtk' || true; exit 1
}
# Playwright-Chromium fuer PNG-Snapshots (optional, kein Hard-Fail)
/opt/text2cad/.venv/bin/python -m playwright install --with-deps chromium || echo "[CT] WARN: Playwright/Chromium fehlt — PNG-Snapshot deaktiviert, STL-Viewer geht trotzdem."
touch /opt/text2cad/.env
systemctl daemon-reload
systemctl enable text2cad
systemctl restart text2cad
sleep 6
echo "[CT] Service-Status:"
systemctl is-active text2cad || { echo "[CT] FEHLER: Service nicht aktiv. Journal:"; journalctl -u text2cad -n 100 --no-pager; exit 1; }
echo "[CT] HTTP-Check auf localhost:\${APP_PORT} …"
for i in 1 2 3 4 5 6; do
  if curl -fsS "http://127.0.0.1:\${APP_PORT}/api/health" | grep -q '"ok"'; then echo "[CT] Web UI antwortet."; break; fi
  if [[ \$i -eq 6 ]]; then echo "[CT] FEHLER: Web UI antwortet nicht. Journal:"; journalctl -u text2cad -n 120 --no-pager; ss -ltnp | head -n20 || true; exit 1; fi
  sleep 5
done
echo "[CT] Provisionierung OK."
PROVISION_EOF
  pct exec "${CTID}" -- bash /tmp/text2cad-provision.sh

  # IP + finale Verifikation (Host-seitig)
  local IP; IP=$(pct exec "${CTID}" -- bash -c "hostname -I | awk '{print \$1}'" | tr -d '\r\n' || true)
  [[ -z "${IP}" ]] && IP="<CT-IP via 'pct exec ${CTID} -- hostname -I'>"
  msg_info "Host-seitige Verifikation …"
  pct exec "${CTID}" -- systemctl is-active text2cad || { msg_error "Service im CT nicht aktiv. Logs: pct exec ${CTID} -- journalctl -u text2cad -n 100 --no-pager"; exit 1; }
  if pct exec "${CTID}" -- curl -fsS "http://127.0.0.1:${VAR_PORT}/api/health" | grep -q '"ok"'; then
    msg_ok "Web UI antwortet (localhost:${VAR_PORT} im CT)."
  else
    msg_error "Web UI antwortet NICHT. Diagnose:"
    pct exec "${CTID}" -- bash -c "journalctl -u text2cad -n 120 --no-pager; ss -ltnp | head -n30" || true
    exit 1
  fi

  echo ""
  msg_ok "Fertig! ${APP} läuft in CT ${CTID} (${HN}, onboot=1, service=text2cad)."
  echo -e "  ${GN}Web UI:${CL}  http://${IP}:${VAR_PORT}"
  echo -e "  Container: pct enter ${CTID} | Logs: pct exec ${CTID} -- journalctl -u text2cad -f"
  echo -e "  Update:  pct exec ${CTID} -- bash -c 'cd /opt/text2cad-src && git pull && systemctl restart text2cad'"
  echo -e "  Deinstall: pct stop ${CTID} && pct destroy ${CTID}"
  echo -e "  Hinweis: OpenRouter-Key bzw. OmniRouter-URL in der Web-UI unter Einstellungen hinterlegen."
  echo -e "  Modell bequem per Dropdown wechseln. Reboot-Test: pct reboot ${CTID} && nach 60s URL erneut oeffnen."
}

main "$@"
