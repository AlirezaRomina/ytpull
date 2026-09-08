# Deploying to a fresh Ubuntu 24.04 EC2 instance

Everything below is run on the instance over SSH unless it says otherwise.
Plain HTTP on the instance's public IP — no nginx, TLS, domain, or Docker,
per the spec.

Two decisions in here differ from the letter of the spec, both explained in
place: the Python version (step 2) and how yt-dlp is installed (step 2).

---

## 1. Security group ports

Open these two inbound rules on the instance's security group (EC2 console →
Security Groups → Inbound rules):

| Port | Protocol | Source | Why |
|------|----------|--------|-----|
| 22 | TCP | **your IP only** ("My IP" in the console) | SSH, for you to administer the box. Restricting the source to your IP keeps the constant background noise of bots trying SSH logins away entirely. |
| 8000 | TCP | 0.0.0.0/0 (anywhere) | The app itself. Anywhere, so your phone works from cellular data too — phone IPs change constantly, so pinning this to an IP would lock you out. |

**Why port 8000 and not the normal web port 80:** only the root user may
listen on ports below 1024, and the app runs as the unprivileged `ubuntu`
user (a process that talks to the whole internet should not run as root).
The usual fix is putting nginx in front, which the spec rules out — so the
app listens on 8000 and the URL carries the port:
`http://<public-ip>:8000`.

Nothing else should be open. In particular there is no rule for port 80 or
443 — nothing is listening there, an open port would just be a door to an
empty room.

---

## 2. Install everything

SSH in, then:

```
sudo apt update
sudo apt install -y python3-venv ffmpeg
sudo curl -L https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp -o /usr/local/bin/yt-dlp
sudo chmod a+rx /usr/local/bin/yt-dlp
```

Two decisions here worth explaining:

- **Python version.** The spec says Python 3.11, but Ubuntu 24.04 ships
  Python 3.12 as its system `python3`, and getting 3.11 onto it would mean
  adding a third-party package archive. The app uses nothing specific to
  3.11, so it runs on the system 3.12 as-is — one less moving part, and
  security updates come from Ubuntu itself.

- **yt-dlp from GitHub, not `apt install yt-dlp`.** Ubuntu's packaged
  yt-dlp is frozen at whatever version existed when the OS was released.
  YouTube changes its internals every few weeks and old yt-dlp versions
  simply stop working, so you want the latest release and an easy way to
  update. The command above downloads the official standalone binary;
  when downloads start failing months from now, the fix will usually be
  just:

  ```
  sudo yt-dlp -U
  ```

---

## 3. Get the app onto the instance and set it up

```
sudo apt install -y git
git clone https://github.com/AlirezaRomina/ytpull.git /home/ubuntu/ytdownloader
cd /home/ubuntu/ytdownloader
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Quick smoke test before wiring up systemd (Ctrl+C to stop it afterwards):

```
.venv/bin/uvicorn main:app --host 0.0.0.0 --port 8000
```

While that runs, `http://<public-ip>:8000` from your phone should show the
page. **`--host 0.0.0.0` matters:** it tells the server to accept
connections from other machines. Without it uvicorn only listens to the
instance itself and the security group rule doesn't help — this is the
single most common "it works on the box but not from my browser" mistake.

---

## 4. systemd unit — restart on reboot and on crash

systemd is Ubuntu's built-in service manager. Registering the app with it
means it starts when the instance boots and gets restarted automatically if
it ever crashes — nobody has to SSH in and start it by hand.

Create the unit file:

```
sudo tee /etc/systemd/system/ytdownloader.service > /dev/null <<'EOF'
[Unit]
Description=YouTube downloader web app
# Don't start until the network is actually up
After=network-online.target
Wants=network-online.target

[Service]
User=ubuntu
WorkingDirectory=/home/ubuntu/ytdownloader
ExecStart=/home/ubuntu/ytdownloader/.venv/bin/uvicorn main:app --host 0.0.0.0 --port 8000
# Restart on any exit, crash or clean, after a 3-second pause
# (the pause stops a start-crash-start loop from spinning at full speed)
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
```

Enable it (start now + start on every boot, in one command):

```
sudo systemctl enable --now ytdownloader
```

Check it's alive:

```
systemctl status ytdownloader
```

You want `Active: active (running)` in the output. After any code change on
the instance (e.g. `git pull`), restart it with:

```
sudo systemctl restart ytdownloader
```

---

## 5. Logs — when it breaks

systemd captures everything the app prints. The two commands you'll
actually use:

**Follow the logs live** (leave this running while you reproduce the
problem from your browser; Ctrl+C to stop watching):

```
sudo journalctl -u ytdownloader -f
```

**Show the most recent logs** (jumps to the end; useful after the fact):

```
sudo journalctl -u ytdownloader -e
```

What you'll see: one line per web request from uvicorn, any `cleanup error:`
lines from the janitor task, and — most importantly — if the app itself
crashes, the Python error (a "traceback") appears here, and systemd's
restart messages around it tell you when it died and came back.

If the service won't start at all, `systemctl status ytdownloader` shows
the last few log lines inline, which is usually enough to see why.

---

## 6. The Phase 4 checkpoint

From your phone's browser (on cellular, not just your home Wi-Fi):

1. Open `http://<public-ip>:8000` — the page loads.
2. Paste a YouTube link, download an MP4 — progress bar, then the green
   Get file button, and the file plays on the phone.
3. On the instance, `ls /tmp/jobs/` shows the job directory now, and shows
   it gone again when you check back ~40 minutes later (30-minute age limit,
   checked every 10 minutes — so between 30 and 40 minutes after the job).

The public IP is on the instance's page in the EC2 console. Note it changes
if you stop and start the instance (a reboot keeps it).
