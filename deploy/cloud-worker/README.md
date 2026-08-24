# DicePilot Cloud Worker — Deployment Runbook

Moves the persistent DicePilot worker off a Mac + localhost Chrome onto an
always-on cloud VM, while `https://dice-beta-eight.vercel.app/` stays the
only normal user-facing surface. Nothing about the actual Dice automation
engine changes — this only relocates where the worker and its browser run.

```
Vercel UI  →  Supabase (runs/applications/events/heartbeat)  →  Cloud Worker  →  Playwright + Chrome  →  persistent browser profile  →  Dice
```

**Preferred browser layer: self-hosted Steel Browser** (Docker,
[steel-dev/steel-browser](https://github.com/steel-dev/steel-browser)) —
verified live during the Phase 7.2 compatibility spike (2026-08-23):
`playwright.chromium.connect_over_cdp()` works against it completely
unmodified, and an authenticated Dice session survives same-session
reconnect, container restart, full `docker compose down`/`up`, and
`--force-recreate`, once `CHROME_USER_DATA_DIR` is pointed at a durable
named volume (see `deploy/cloud-worker/steel/docker-compose.yml`). Steps
5–7 below cover Steel; the earlier local-Chrome-under-Xvfb approach
(`start-browser.sh`, `dicepilot-browser.service`) is kept as a documented
fallback for a VM where Docker isn't available or preferred.

This is still single-user V1: one candidate, one Dice account, one browser
profile, one worker, sequential applications. No multi-tenant browser pool,
no Kubernetes, no autoscaling — see the parent task for the explicit
non-goals.

## Prerequisites

- One always-on Linux VM (a single AWS EC2 instance is the V1 target — any
  equivalent always-on Linux VM works the same way). Ubuntu 22.04+ or
  similar, 2 vCPU / 4 GB RAM is comfortably enough for one Chrome + one
  Python worker.
- A durable, persistent disk/volume for the browser profile — not the
  VM's ephemeral instance storage if your provider distinguishes the two.
- SSH access as a non-root operator user.
- This repository's Supabase project already exists and is migrated
  (`supabase db push` from a machine with CLI access — this runbook does
  not re-derive that; see the repo's own migration history).

## 1. Provision the VM

Standard cloud VM provisioning (AWS EC2 or equivalent) — out of scope for
this runbook's specifics beyond: pick a region close to you, attach a
durable EBS/persistent volume, open only SSH (port 22) to the internet.
**Never open the CDP port (default 9333) or any display/VNC port publicly.**

## 2. Install dependencies

```bash
sudo apt update
sudo apt install -y python3.11 python3.11-venv git xvfb curl
# Real Google Chrome (not Chromium) -- required for Google OAuth sign-in
# to work at all; see start-browser.sh's own comment.
curl -fsSL https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb -o /tmp/chrome.deb
sudo apt install -y /tmp/chrome.deb
sudo useradd -m -s /bin/bash dicepilot
```

## 3. Clone the Dice repo

```bash
sudo -u dicepilot -i
mkdir -p /opt/dicepilot && sudo chown dicepilot:dicepilot /opt/dicepilot
git clone <this repo's URL> /opt/dicepilot/dice
cd /opt/dicepilot/dice
python3.11 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/playwright install --with-deps chromium  # dependencies only; Chrome itself is the real installed binary above
```

## 4. Configure environment variables

Copy `.env.example` to `.env` and fill in real values (never commit them):

```bash
cp .env.example .env
```

Required for the worker:

| Variable | Purpose |
|---|---|
| `SUPABASE_URL` | Same Supabase project as the deployed Vercel app |
| `SUPABASE_SERVICE_ROLE_KEY` | Server-side only — never in frontend code |
| `APPLYWIZZ_API_BASE_URL` / `APPLYWIZZ_API_TOKEN` | Candidate profile source |
| `DICEPILOT_CANDIDATE_ID` | Which candidate this worker processes runs for |
| `DICEPILOT_CDP_URL` | Steel: `ws://localhost:3000/`. Local Chrome: `http://127.0.0.1:9333`. Same-host either way |
| `DICEPILOT_BROWSER_PROFILE_DIR` | `/data/steel/chrome-profile` (Steel) or `/opt/dicepilot/browser-profile` (local Chrome) — see step 6 |
| `DICEPILOT_BROWSER_PROVIDER` | `steel` (preferred) or `local` — selects which of the two below is active, and whether the worker runs Steel's stale-lock cleanup at startup |
| `DICEPILOT_SUBMISSION_MODE` | Leave unset (defaults to `AUTHORIZED_AUTONOMOUS`, the intended normal mode) unless you specifically want new runs to default to manual confirmation |
| `DICEPILOT_RESUME_PATH` | Absolute path to the candidate's resume file on this VM (e.g. `/opt/dicepilot/dice/.runtime/resume/test_resume.pdf`). **Mandatory** -- the worker refuses to start without it (see step 9's readiness check) |

## 5. Configure the persistent browser profile disk

### Option A — Steel Browser (preferred)

Requires Docker + Docker Compose on the VM (`sudo apt install -y docker.io docker-compose-plugin`).

```bash
cd /opt/dicepilot/dice/deploy/cloud-worker/steel
docker compose up -d
```

This starts Steel's API (port 3000, plus 9223 for its own raw CDP
console — keep both bound to `127.0.0.1`/internal only, never public)
and its UI (port 5173, for the live-viewer re-auth procedure below). The
compose file already points `CHROME_USER_DATA_DIR` at a durable named
Docker volume (`chrome_profile`) — that volume is what makes the Dice
login survive container restarts, recreation, and VM reboots (verified
live; see the spike notes in `docker-compose.yml`'s own comment).

Set `DICEPILOT_BROWSER_PROVIDER=steel`, `DICEPILOT_CDP_URL=ws://localhost:3000/`,
and `DICEPILOT_BROWSER_PROFILE_DIR=/data/steel/chrome-profile` (the path
*as seen by whatever process runs `clean_stale_singleton_locks` — i.e.
the worker needs filesystem access to that same volume; run the worker
on the same VM as Steel, which is the whole point of this setup).

### Option B — local Chrome under Xvfb (fallback/reference)

```bash
sudo mkdir -p /opt/dicepilot/browser-profile
sudo chown dicepilot:dicepilot /opt/dicepilot/browser-profile
```

This directory **must** be on the durable/persistent volume, not
`/tmp` or ephemeral instance storage — it's the entire reason the Dice
login survives worker restarts and VM reboots. Use this path if Docker
isn't available or preferred on the target VM; it's otherwise equivalent.

## 6. Start the browser service

**Steel (Option A):** already started in step 5 (`docker compose up -d`).
Add a systemd unit if you want it to survive a reboot without a manual
`docker compose up`:

```ini
# /etc/systemd/system/dicepilot-steel.service
[Unit]
Description=DicePilot Steel Browser service
After=docker.service network-online.target
Requires=docker.service

[Service]
Type=oneshot
RemainAfterExit=true
WorkingDirectory=/opt/dicepilot/dice/deploy/cloud-worker/steel
ExecStart=/usr/bin/docker compose up -d
ExecStop=/usr/bin/docker compose down

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now dicepilot-steel
```

**Local Chrome (Option B):**

```bash
sudo cp deploy/cloud-worker/dicepilot-browser.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now dicepilot-browser
sudo systemctl status dicepilot-browser
```

## 7. Perform the initial manual Dice login (secure operator procedure)

DicePilot never automates or bypasses login — a human logs in once,
interactively, and the persistent profile remembers it from then on.

**Steel (Option A):** Steel's own UI (`http://localhost:5173`, or
tunneled — see below) shows a live view of the browser and lets you
click into it directly; no X11 forwarding needed. Never expose port 5173
or 3000 publicly — reach them only via:

```bash
ssh -L 5173:localhost:5173 -L 3000:localhost:3000 dicepilot@<vm-host>
```

then open `http://localhost:5173` in your own browser and log into Dice
normally, including any OTP/CAPTCHA/security step.

**Local Chrome (Option B): SSH + X11 forwarding.** From your own machine:

```bash
ssh -X dicepilot@<vm-host>
DISPLAY=:99 google-chrome --user-data-dir=/opt/dicepilot/browser-profile \
  --remote-debugging-port=9333 --remote-debugging-address=127.0.0.1
```

X11-forwards the *already-running* Xvfb display (`:99`, matching
`start-browser.sh`) to your local screen. Log into Dice normally, then
close the window — the browser service keeps running headless-side.

Alternative for a VM without X11 forwarding support: a restricted,
authentication-gated noVNC/Xpra session tunneled over SSH — never bind
VNC/noVNC to a public interface.

**Never, for either option**: automate the login form, inject cookies
from another device, or attempt to bypass CAPTCHA/OTP. If Dice presents
a challenge, a human resolves it directly in that window.

**After any Steel container recreation** (`down`/`up`, `--force-recreate`,
an image update), Chrome can leave a stale `SingletonLock` behind and
refuse to launch — the worker clears this automatically at startup when
`DICEPILOT_BROWSER_PROVIDER=steel` (see
`dice_browser.session.clean_stale_singleton_locks`); it never touches
cookies, Local Storage, or any other real profile data, only the three
lock artifact files.

## 8. Start the worker service

```bash
sudo cp deploy/cloud-worker/dicepilot-worker.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now dicepilot-worker
sudo systemctl status dicepilot-worker
```

The worker checks its own configuration before doing anything else and
refuses to start if anything mandatory is missing (Supabase reachable,
`DICEPILOT_CANDIDATE_ID` set, `DICEPILOT_RESUME_PATH` set and the file
actually exists, a recognized browser provider) -- `journalctl -u
dicepilot-worker -n 30` shows a PASS/FAIL line per check if it exits
immediately after starting.

No `--submission-policy` flag — the worker reads each run's own
persisted policy (Phase 6.4). Adding one here would override *every* run
this worker ever claims; only do that for an explicit, temporary debug
session, never for the standing service.

## 9. Start the Telegram consumer service

Independent of the worker/browser stack above -- it only needs
`TELEGRAM_BOT_TOKEN` and Supabase, never touches the browser, and long-
polls Telegram continuously so an Apply/Skip/Confirm/Edit tap is picked
up within seconds instead of only when someone happens to run a manual
poll script.

```bash
sudo cp deploy/cloud-worker/dicepilot-telegram-consumer.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now dicepilot-telegram-consumer
sudo systemctl status dicepilot-telegram-consumer
```

Same readiness-check-before-starting behavior as the worker (Telegram
reachable, Supabase reachable) -- `journalctl -u
dicepilot-telegram-consumer -n 30` shows PASS/FAIL per check if it
exits immediately after starting.

## 10. Verify heartbeat ONLINE

```bash
deploy/cloud-worker/healthcheck.sh
```

Or from the repo root:

```bash
.venv/bin/python -c "import run_registry; print(run_registry.worker_status())"
```

Expect `{'online': True, 'status': 'ONLINE', ...}`.

## 11. Verify Vercel shows worker/browser/auth truth

Open `https://dice-beta-eight.vercel.app/worker` — should show `Worker:
ONLINE` and `Browser / Dice Login: Online`. Create a small test run (or
check an existing one's Run Progress page) and confirm the same status
appears there.

## 12. Reboot the VM

```bash
sudo reboot
```

## 13. Verify services return automatically

After the VM comes back:

```bash
systemctl status dicepilot-browser dicepilot-worker dicepilot-telegram-consumer
```

All three should show `active (running)` without any manual
intervention — `Restart=always` plus `WantedBy=multi-user.target` in
every unit file handles both crash-restart and boot-start.

## 14. Confirm the browser profile survives reboot

```bash
deploy/cloud-worker/healthcheck.sh
```

Should show `AUTH_REQUIRED` only if Dice itself expired the session
server-side (normal, occasional) — **not** as a result of the reboot
itself. If it shows `AUTH_REQUIRED` immediately after every reboot, the
profile directory isn't actually on durable storage — revisit step 5.

## Ongoing operation

- Normal flow needs zero terminal access: Discover → Filter & Select →
  Review → Start Applications → watch Run Progress, all from
  `dice-beta-eight.vercel.app`.
- If Vercel shows **Dice Login Required**, repeat step 7 (log back in);
  the worker detects and clears this automatically within about a minute
  of a successful re-login — no restart needed.
- If Vercel shows **Security Challenge**, same procedure — resolve the
  CAPTCHA/OTP/verification directly in the forwarded browser window.
- `journalctl -u dicepilot-worker -f` / `journalctl -u dicepilot-browser -f`
  / `journalctl -u dicepilot-telegram-consumer -f` for live logs.
- Apply/Skip/Confirm/Edit taps in Telegram now reach the system within
  seconds on their own, with the always-on consumer running — no manual
  poll script, no terminal, ever, for normal operation.
