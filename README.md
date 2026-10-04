# Waveshare 8-ch Ethernet Relay — Simulateur

Simulateur logiciel haute fidélité du module **Waveshare Modbus POE ETH Relay (8 canaux)** :
serveur Modbus TCP (pymodbus, asyncio) + interface web temps réel en lecture seule (aiohttp + WebSocket).

## Installation & lancement (Python ≥ 3.10)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python main.py            # Modbus :5020, Web http://localhost:8080
python main.py --help     # options : --ip, --mac, --device-address, --strict-unit-id, -v ...
python -m unittest -v     # tests end-to-end (sockets réels)
```

## Déploiement Docker

```bash
WAVESHARE_IP=192.168.1.50 docker compose up -d --build   # IP LAN de l'hôte Docker
docker compose logs -f
docker compose down                                       # arrêt propre (SIGTERM)
```

Sans compose :

```bash
docker build -t waveshare-relay-sim .
docker run -d --name waveshare-relay -p 5020:5020 -p 8080:8080 \
  -e WAVESHARE_IP=192.168.1.50 waveshare-relay-sim
```

Toutes les options CLI sont configurables par variable d'environnement `WAVESHARE_<OPTION>`
(priorité : argument CLI > variable d'env > défaut) :

| Variable | Défaut | Rôle |
|---|---|---|
| `WAVESHARE_IP` | auto-détection | IP annoncée en FC04 (**à définir en Docker**, sinon IP interne 172.x) |
| `WAVESHARE_MAC` | `02:57:53:52:4C:08` | MAC annoncée |
| `WAVESHARE_NETMASK` / `WAVESHARE_GATEWAY` | `255.255.255.0` / 1er hôte | Réseau annoncé |
| `WAVESHARE_MODBUS_PORT` / `WAVESHARE_WEB_PORT` | `5020` / `8080` | Ports d'écoute dans le conteneur |
| `WAVESHARE_ADVERTISED_PORT` | = port d'écoute | Port annoncé en FC04 (ex. `502` si mapping `502:5020`) |
| `WAVESHARE_DEVICE_ADDRESS` | `1` | Adresse esclave Modbus |
| `WAVESHARE_STRICT_UNIT_ID` | `false` | Ne répondre qu'à l'adresse esclave configurée |
| `WAVESHARE_VERBOSE` | `false` | Logs debug |

Pour exposer le port Modbus standard : `ports: ["502:5020"]` + `WAVESHARE_ADVERTISED_PORT=502`.

## Table Modbus

| FC | Adresse | Valeur | Effet |
|----|---------|--------|-------|
| 01 | 0x0000–0x0007 | – | Lecture état relais CH1–CH8 |
| 05 | 0x0000–0x0007 | FF00 / 0000 / 5500 | ON / OFF / Toggle |
| 05 | 0x00FF | FF00 / 0000 / 5500 | Tous ON / OFF / Toggle |
| 05 | 0x0200+ch | délai ×100 ms (1–0x7FFF) | **Flash ON** (ON puis OFF après délai) |
| 05 | 0x0400+ch | délai ×100 ms (1–0x7FFF) | **Flash OFF** (OFF puis ON après délai) |
| 0F | 0x0000–0x0007 | bitfield | Écriture multiple |
| 03 | 0x4000 / 0x8000 | – | Adresse esclave / version firmware (0x00C8 = V2.00) |
| 06 | 0x4000 | 1–247 | Change l'adresse esclave |
| 06 | 0x0200+ch / 0x0400+ch | délai | *Extension simulateur* : alias Flash (pour outils qui n'écrivent que des registres, ex. HA `write_register`) |
| 04 | 0x0000–0x0010 | – | *Mock* : MAC(3) IP(2) masque(2) passerelle(2) port, FW, HW, nb relais, adresse, uptime(2, u32), bitmap relais |

Toute autre fonction → exception `ILLEGAL FUNCTION` ; adresse hors plage → `ILLEGAL ADDRESS` ; valeur invalide → `ILLEGAL VALUE`.

Exemple (trame Waveshare) : `05 02 00 00 07` = Flash ON du relais 1 pendant 700 ms.

## Architecture

```
main.py                      CLI + orchestration asyncio + arrêt propre
waveshare_sim/
  config.py                  Table de registres + identité simulée (DeviceInfo)
  relay_board.py             Modèle matériel : états, timers flash, journal, pub/sub
  modbus_server.py           PDU pymodbus personnalisées → RelayBoard
  web_server.py              aiohttp : /, /api/state, /ws (push only)
  static/index.html          UI autonome (CSS/JS inline, aucune dépendance externe)
tests/test_simulator.py      Tests bout-en-bout
```

## Déploiement Proxmox via GitHub

Voir [DEPLOY_PROXMOX.md](DEPLOY_PROXMOX.md) : GitHub Actions publie l'image sur `ghcr.io`, un script installe et met à jour le conteneur sur Proxmox.
