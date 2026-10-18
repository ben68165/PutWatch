# Put.io Tracker Monitor & Auto-Canceler

A 24/7 background service and web application for monitoring active [Put.io](https://put.io) transfers (`GET /v2/transfers/list`), automatically identifying BitTorrent trackers from active transfers, and canceling & removing transfers based on per-tracker **seeding time** and/or **share ratio** rules.

---

## Features

- **Automatic Tracker Identification**: Extracts tracker domains from active Put.io transfers via the `tracker` field, `trackers` array, and `source` magnet `tr=` announce parameters.
- **Per-Tracker Rule Engine**:
  - Set a **Time Limit** (Seeding Time `seconds_seeding` or Total Transfer Age in hours/minutes).
  - Set a **Ratio Limit** (`current_ratio`).
  - Combine conditions per tracker using **OR** (*cancel when either time or ratio is reached*) or **AND** (*cancel only when both time and ratio are reached*).
  - Optional **Global Fallback Rule** for transfers from unconfigured trackers.
- **Safe Cancellation & Transfer List Cleanup**:
  - Stops seeding via `POST /v2/transfers/cancel` and cleans the finished entry from Put.io's transfer list via `POST /v2/transfers/clean` while **keeping your downloaded files in Put.io storage**.
- **24/7 Web GUI + Dry-Run Mode**:
  - Real-time transfer dashboard showing identified trackers and live progress bars toward time/ratio thresholds.
  - Built-in Put.io OAuth token setup **plus** 1-click Put.io Device Link Code (`app.put.io/link`) authentication.
  - Full cancellation audit log and simulation (**Dry-Run**) mode.

---

## Deploying in Portainer (Recommended for 24/7 Server)

### Option A: Portainer Stack (with Git Repository or Build Context)
1. In **Portainer**, go to **Stacks** &rarr; **Add stack**.
2. Paste the contents of [`docker-compose.yml`](./docker-compose.yml):
   ```yaml
   services:
     putio-tracker-monitor:
       build:
         context: .
         dockerfile: Dockerfile
       container_name: putio-tracker-monitor
       restart: unless-stopped
       ports:
         - "8080:8080"
       environment:
         - CONFIG_PATH=/data/config.json
         - PUTIO_TOKEN=${PUTIO_TOKEN:-}
         - TZ=Europe/Berlin
       volumes:
         - putio_monitor_data:/data

   volumes:
     putio_monitor_data:
   ```
3. Open `http://<your-server-ip>:8080` in your browser.
4. Go to **Put.io API & Service Settings** to paste your Put.io OAuth token or click **Generate Link Code** to link your account at `https://app.put.io/link`.

---

## Running Locally (Python 3.12+)

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8080 --reload
```

Then open `http://localhost:8080`.
