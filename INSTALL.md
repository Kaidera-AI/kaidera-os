# Installing Kaidera OS — Step-by-Step

A complete walkthrough for installing on a **Mac** or a **Linux machine** (including a cloud VM),
written for someone who hasn't used Linux or the command line much. Just follow the steps in order
and copy-paste the commands. It takes about **15–20 minutes**.

## What you'll end up with
- The Kaidera OS console open in your web browser at **http://localhost:8765**
- Its memory system (Cortex) + database running quietly in the background (inside Docker)
- A clean app ready for you to add an AI provider key and start working

## System requirements (especially on a cloud VM)
The app runs a multi-container Docker stack, so don't use a tiny instance:
- **Disk: ~50 GB** (the memory-embedding worker builds **PyTorch (~3 GB)**; with the images +
  build cache, 25–30 GB fills up mid-build with a confusing `No space left on device`. 50 GB is
  comfortable. A **DB+API-only** deploy fits in ~20 GB — `install.sh` prints the exact bring-up
  command if it detects a tight disk.)
- **RAM: 4 GB minimum** (8 GB comfortable)
- **CPU: 2 cores**
- A **minimal** Ubuntu image is fine, but it ships missing some tools — Step 1 below installs them.
- **Herdr runtime:** required for the Kaidera OS terminal/runtime layer. `./install.sh` installs it
  from upstream if it is missing; Kaidera OS does **not** bundle the Herdr binary or source.

> 🧹 **Need to start over?** `./cleanup.sh` tears the whole stack down (containers, images, build
> cache, volumes, venv) so you can re-run `./install.sh` from a clean slate. It deletes the Cortex
> database too, so only use it on a deployment you're happy to reset.

---

## How to use this guide
You type commands into a program called the **Terminal**:
- **Mac:** press `Cmd`+`Space`, type `Terminal`, press `Enter`.
- **Linux desktop:** open `Terminal` from your apps.
- **Cloud VM:** you're already in a terminal once you connect (SSH).

Copy each grey command block, paste it into the Terminal (`Cmd`+`V` on Mac, `Ctrl`+`Shift`+`V` on
Linux), and press `Enter`. When a command starts with `sudo`, it may ask for your password — type it
(you won't see characters appear, that's normal) and press `Enter`.

---

## STEP 1 — Install the prerequisites (the tools the app needs)

Pick **your** system.

### 🐧 Linux (Ubuntu / Debian — what most cloud VMs run)

**1a. Basic tools.** On a **minimal** Ubuntu image, `ca-certificates` + `gnupg` are often missing —
without them, downloads pipe-fail with confusing errors like `curl: (23)`. Install everything up
front:
```
sudo apt update
sudo apt install -y ca-certificates curl wget gnupg git python3 python3-venv
```

**1b. Docker** (the app runs its database + memory inside Docker):
```
curl -fsSL https://get.docker.com -o get-docker.sh
sudo sh get-docker.sh
sudo usermod -aG docker $USER
```
> ⚠️ **Important:** after this, **close your terminal and open a new one** (on a cloud VM:
> disconnect and reconnect / SSH back in). This lets you use Docker without `sudo`. If you skip
> this you'll get "permission denied" errors later.

**1c. GitHub CLI** (`gh`, used to download the app). The official method:
```
(type -p wget >/dev/null || (sudo apt update && sudo apt install wget -y)) \
&& sudo mkdir -p -m 755 /etc/apt/keyrings \
&& out=$(mktemp) && wget -nv -O$out https://cli.github.com/packages/githubcli-archive-keyring.gpg \
&& cat $out | sudo tee /etc/apt/keyrings/githubcli-archive-keyring.gpg > /dev/null \
&& sudo chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg \
&& sudo mkdir -p -m 755 /etc/apt/sources.list.d \
&& echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" | sudo tee /etc/apt/sources.list.d/github-cli.list > /dev/null \
&& sudo apt update \
&& sudo apt install gh -y
```
> 💡 *Simpler one-liner alternative* (officially listed): `curl -sS https://webi.sh/gh | sh`
> (then close + reopen the terminal). **Do NOT install `gh` via snap** — GitHub discourages it.

### 🍎 macOS

**1a. Docker Desktop:** download from <https://www.docker.com/products/docker-desktop>, install it,
then **open it** (you must see the Docker whale icon in your top menu bar — it needs to be running).

**1b. Homebrew** (a tool that installs other tools) — skip if you already have it:
```
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
```

**1c. The tools:**
```
brew install git python gh
```
`./install.sh` installs Herdr automatically later. If you want to install it yourself first:
`brew install herdr`.

---

## STEP 2 — Download the public source

No GitHub login is required:

```
git clone https://github.com/Kaidera-AI/kaidera-os.git ~/kaidera-os
cd ~/kaidera-os
```
This copies the app into **`~/kaidera-os`** and moves you into it. A source checkout installs the
**development edition**, with every configured provider and harness available for development and
testing. Signed release archives carry a `public` edition marker and expose only Kaidera AI
Manifold; use [dist/README.md](dist/README.md) for that installation path.

GitHub CLI login is optional. Use `gh auth login` only if you want to create issues, open pull
requests, or perform maintainer actions from the terminal.

---

## STEP 3 — Check the source boundary

The Git checkout is for development and testing. Installers downloaded from the public release
channel use a separately baked public edition. Do not copy customer projects, API keys, local
runtime state, or chat history into this source tree.

---

## STEP 4 — Install it
```
./install.sh
```
This checks your tools, then starts the database + memory system in Docker (the **first** time it
builds them, which can take a few minutes — that's normal), installs/verifies the external Herdr
runtime, and sets up the app. Watch for green **✓** marks. If a tool is missing it tells you
exactly which one.

---

## STEP 5 — Start the app
```
./run-kaidera-os-console.sh
```
**Leave this terminal window running** (it's the app). Then open your web browser to:

### **http://localhost:8765**

> ☁️ **On a cloud VM (no browser on the box)?** By default the console listens on `localhost` only.
> Two ways to reach it from your own computer:
> - **Port-forward over SSH** (safest — nothing extra is exposed):
>   - Generic: `ssh -L 8765:localhost:8765 YOUR_USER@YOUR_VM_IP`
>   - Google Cloud: `gcloud compute ssh YOUR_VM_NAME -- -L 8765:localhost:8765`
>
>   Keep that window open, then browse to <http://localhost:8765> on your own computer.
> - **Bind to the VM's IP** (for a private network / VPN like Tailscale): install with
>   `KAIDERA_CONSOLE_HOST=0.0.0.0 ./install.sh`, then open `http://<vm-ip>:8765`.
>   ⚠️ For shared use, enable `KAIDERA_AUTH_ENABLED=1` and put the console behind HTTPS. Keep
>   port 8765 firewalled from the public internet.

---

## STEP 6 — First setup
1. In the app, go to **Settings → Providers**.
2. Add at least **one** AI provider API key (e.g. Ollama Cloud, OpenAI, or Anthropic). Paste your
   key and **Save**. The app calls the provider directly with this key — nothing is shared.
3. You're ready. Create your first project and AI worker from the console — the
   startup wizard and Add Project flow are live.

To stop the app later: go to the terminal running it and press `Ctrl`+`C`. To start it again:
`cd ~/kaidera-os && ./run-kaidera-os-console.sh`.

---

## Troubleshooting (common first-time issues)

| Symptom | Fix |
|---|---|
| `docker: permission denied` / `Cannot connect to the Docker daemon` (Linux) | You didn't reopen the terminal after Step 1b. Close it and open a new one (reconnect to the VM), then retry. If still stuck: `sudo systemctl start docker`. |
| Docker errors on **Mac** | Docker Desktop isn't running — open it and wait for the whale icon to go steady. |
| Browser page won't load on a **cloud VM** | You need the port-forward from Step 5 (the ☁️ box). |
| `Repository not found` when cloning | Confirm the URL is `https://github.com/Kaidera-AI/kaidera-os.git` and that GitHub is reachable. |
| `port 8765 already in use` | Use another port: `KAIDERA_CONSOLE_PORT=8770 ./install.sh` then `./run-kaidera-os-console.sh` and open `:8770`. |
| `command not found: gh` (Mac) | Re-run `brew install gh`, then close + reopen the terminal. |
| Herdr install/check fails | Install it manually (`brew install herdr` on macOS, or use <https://herdr.dev/docs/install/>), then re-run with `KAIDERA_OS_HERDR_BIN=/path/to/herdr ./install.sh`. |
| `curl: (23)` during prerequisites (minimal Ubuntu) | A missing tool / broken pipe — run **Step 1a first** (it installs `ca-certificates`/`gnupg`), and never `curl … \| sh` (download, then run). |
| `env file … local-cortex/.env not found` | The latest `install.sh` auto-creates it. On an older copy: `touch ~/kaidera-os/local-cortex/.env` then re-run `./install.sh`. |

If a step fails, copy the exact error message — that's the fastest way to get help.
