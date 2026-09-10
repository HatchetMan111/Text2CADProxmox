# text2CAD — Text-zu-CAD für den 3D-Druck (Proxmox LXC, Einzeiler)

Lokale Web-App als Wrapper um [earthtojake/text-to-cad](https://github.com/earthtojake/text-to-cad)
(`cadgen`/`build123d`-Skills): Prompt eingeben → LLM erzeugt CAD-Python →
Build im LXC → **STL / STEP / 3MF-Download**, 3D-Druck-optimiert, mit
**Fortschrittsbalken** + **3D-Vorschau** (Three.js, nach Fertigstellung — nicht live).

- **Tech-Stack:** Python / FastAPI + `cadgen==0.5.1` + `build123d` + Three.js
- **LLM-Gateways:** **OpenRouter** + **OmniRouter** (beide OpenAI-kompatibel) + Custom
- **Modellwahl:** per **Dropdown** (live vom Gateway `/models`, mit kuratierter Fallback-Liste ohne Key)
- **Container:** LXC `text2cad` (Debian 12, 2 vCPU / 2 GB RAM / 8 GB, `onboot: 1`), systemd `text2cad.service`
- **Web-UI-Port:** `8080` → `http://[LXC-IP]:8080`

## Einzeiler (Proxmox-Host als root)

```bash
bash -c "$(wget -qLO - https://raw.githubusercontent.com/HatchetMan111/Text2CADProxmox/main/install/text2cad.sh)"
```

Mit Optionen:

```bash
GITHUB_REPO=https://github.com/HatchetMan111/Text2CADProxmox GITHUB_BRANCH=main \
  bash -c "$(wget -qLO - https://raw.githubusercontent.com/HatchetMan111/Text2CADProxmox/main/install/text2cad.sh)"
```

Ressourcen / Netz anpassen (Community-Scripts-`var_*`-Stil):

```bash
var_cpu=4 var_ram=4096 var_disk=12 var_ctid=201 var_ip=dhcp var_port=8080 \
  GITHUB_REPO=https://github.com/HatchetMan111/Text2CADProxmox \
  bash -c "$(wget -qLO - https://raw.githubusercontent.com/HatchetMan111/Text2CADProxmox/main/install/text2cad.sh)"
```

Debug-Log (`bash -x`):

```bash
DEBUG=1 bash -x ./install/text2cad.sh
```

### Erwartete Ausgabe (Erfolg)

```
=== text2CAD Proxmox-Installer ===
[INFO] CT-ID: 200
[INFO] Hostname: text2cad | CPU: 2 | RAM: 2048MiB | Disk: 8G | Storage: local-lvm
[OK] Template vorhanden …
[OK] Container erstellt.
[OK] CT-Netzwerk OK.
[OK] Web UI antwortet (localhost:8080 im CT).
[OK] Fertig! text2CAD läuft in CT 200 (text2cad, onboot=1, service=text2cad).
  Web UI:  http://192.168.1.123:8080
```

Falls die CT-ID vergeben ist, wählt das Script automatisch die nächste freie
(`CT-ID 201 vergeben — nehme naechste freie: 202`); ist der Hostname `text2cad`
belegt, wird `text2cad-2` genommen.

## Web-UI benutzen

1. `http://[LXC-IP]:8080` öffnen.
2. Provider wählen (**OpenRouter** / **OmniRouter** / Custom), Key bzw. OmniRouter-URL
   eintragen (z. B. `http://192.168.x.x:20128/v1`), **↻ Modelle** → **Modell per Dropdown** wählen.
3. Prompt eingeben (mm, FDM-gerecht, z. B. Wand ≥ 2 mm), Exporte + Snapshot wählen,
   **CAD berechnen**.
4. **Fortschrittsbalken** + Stage-Text + Voll-Logs verfolgen; nach Fertigstellung
   **STL im Viewer drehen/zoomen** und **STL / STEP / 3MF + model.py** herunterladen.
5. Ohne Key geht die Modelliste in den Fallback-Modus; der Build braucht einen
   erreichbaren Gateway (sonst steht die komplette Fehlerkette in den Logs).

Für OmniRouter lokal: Standard `http://localhost:20128/v1` im LXC anpassen auf die
tatsächliche Gateway-IP (der LXC sieht sein eigenes localhost, nicht den Host).

## Projektstruktur

```
install/text2cad.sh      Proxmox-Einzeiler (Host: erstellt LXC, ruft Provision auf)
app/app.py               FastAPI-Backend (Jobs, SSE/Polling, Gateway-Proxy, cadgen-Build, DfAM)
app/requirements.txt     cadgen[snapshot]==0.5.1, build123d, FastAPI, trimesh …
app/static/index.html    Web-UI (Dropdown, Progressbar, Three.js-STL-Viewer, Downloads)
systemd/text2cad.service reboot-sicher (enable, Restart=always, After=network-online)
```

## Update / Reboot-Test / Deinstall

```bash
# Update im LXC
pct exec <CTID> -- bash -c 'cd /opt/text2cad-src && git pull --ff-only && cp -r app/* /opt/text2cad/app/ && /opt/text2cad/.venv/bin/pip install -r /opt/text2cad/app/requirements.txt && systemctl restart text2cad'

# Reboot-Test (Pflicht nach Anforderung)
pct reboot <CTID> && sleep 60 && curl -fsS http://<CT-IP>:8080/api/health
pct exec <CTID> -- systemctl is-active text2cad

# Logs / Diagnose (komplette Kette, nie nur letzte Zeile)
pct exec <CTID> -- journalctl -u text2cad -n 200 --no-pager
pct exec <CTID> -- curl -v http://127.0.0.1:8080/api/health

# Deinstall
pct stop <CTID> && pct destroy <CTID>
```

## Wenn LXC zu schwach ist → VM

`build123d`/OCCT läuft i. d. R. gut im LXC (2 vCPU / 2 GB reichen für Kleinteile).
Für große Assemblies / Snapshots: VM mit 4 vCPU / 8 GB nehmen und **denselben**
Provision-Block aus `install/text2cad.sh` (ab `Provisioniere text2CAD`) auf einem
Debian-12-Server ausführen, Port `8080` öffnen, Service enablen — die App ist
identisch, nur die Hülle ändert sich.

## Lizenz / Quellen

- App-Wrapper: MIT (dieses Repo).
- CAD-Engine/`cadgen`-Skills: [earthtojake/text-to-cad](https://github.com/earthtojake/text-to-cad) (MIT).
- Installer-Stil angelehnt an [community-scripts/ProxmoxVE](https://github.com/community-scripts/ProxmoxVE) (MIT, tteck-Basis).
