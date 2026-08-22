# DicePilot Cloud Worker — Deployment Runbook

Moves the persistent DicePilot worker off a Mac + localhost Chrome onto an
always-on cloud VM, while `https://dice-beta-eight.vercel.app/` stays the
only normal user-facing surface. Nothing about the actual Dice automation
engine changes — this only relocates where the worker and its browser run.

```
Vercel UI  →  Supabase (runs/applications/events/heartbeat)  →  Cloud Worker  →  Playwright + Chrome  →  persistent browser profile  →  Dice
```

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
| `DICEPILOT_CDP_URL` | `http://127.0.0.1:9333` — worker and browser are same-host |
| `DICEPILOT_BROWSER_PROFILE_DIR` | `/opt/dicepilot/browser-profile` — see step 6 |
| `DICEPILOT_SUBMISSION_MODE` | Leave unset (defaults to `AUTHORIZED_AUTONOMOUS`, the intended normal mode) unless you specifically want new runs to default to manual confirmation |

## 5. Configure the persistent browser profile disk

```bash
sudo mkdir -p /opt/dicepilot/browser-profile
sudo chown dicepilot:dicepilot /opt/dicepilot/browser-profile
```

This directory **must** be on the durable/persistent volume, not
`/tmp` or ephemeral instance storage — it's the entire reason the Dice
login survives worker restarts and VM reboots.

## 6. Start the browser service

```bash
sudo cp deploy/cloud-worker/dicepilot-browser.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now dicepilot-browser
sudo systemctl status dicepilot-browser
```

## 7. Perform the initial manual Dice login (secure operator procedure)

DicePilot never automates or bypasses login — a human logs in once,
interactively, and the persistent profile remembers it from then on.

**Recommended: SSH + X11 forwarding.** From your own machine:

```bash
ssh -X dicepilot@<vm-host>
DISPLAY=:99 google-chrome --user-data-dir=/opt/dicepilot/browser-profile \
  --remote-debugging-port=9333 --remote-debugging-address=127.0.0.1
```

X11-forwards the *already-running* Xvfb display (`:99`, matching
`start-browser.sh`) to your local screen. Log into Dice normally —
including any OTP/CAPTCHA/security step — exactly as you would locally.
Close the window when done; the browser service keeps running headless-side.

Alternative for a VM without X11 forwarding support: a restricted,
authentication-gated noVNC/Xpra session tunneled over SSH (e.g.
`ssh -L 6080:localhost:6080 ...` to a noVNC server bound to `127.0.0.1`
only) — never bind VNC/noVNC to a public interface.

**Never**: automate the login form, inject cookies from another device, or
attempt to bypass CAPTCHA/OTP. If Dice presents a challenge, a human
resolves it directly in that window, same as local operation always required.

## 8. Start the worker service

```bash
sudo cp deploy/cloud-worker/dicepilot-worker.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now dicepilot-worker
sudo systemctl status dicepilot-worker
```

No `--submission-policy` flag — the worker reads each run's own
persisted policy (Phase 6.4). Adding one here would override *every* run
this worker ever claims; only do that for an explicit, temporary debug
session, never for the standing service.

## 9. Verify heartbeat ONLINE

```bash
deploy/cloud-worker/healthcheck.sh
```

Or from the repo root:

```bash
.venv/bin/python -c "import run_registry; print(run_registry.worker_status())"
```

Expect `{'online': True, 'status': 'ONLINE', ...}`.

## 10. Verify Vercel shows worker/browser/auth truth

Open `https://dice-beta-eight.vercel.app/worker` — should show `Worker:
ONLINE` and `Browser / Dice Login: Online`. Create a small test run (or
check an existing one's Run Progress page) and confirm the same status
appears there.

## 11. Reboot the VM

```bash
sudo reboot
```

## 12. Verify services return automatically

After the VM comes back:

```bash
systemctl status dicepilot-browser dicepilot-worker
```

Both should show `active (running)` without any manual intervention —
`Restart=always` plus `WantedBy=multi-user.target` in both unit files
handles both crash-restart and boot-start.

## 13. Confirm the browser profile survives reboot

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
  for live logs.
