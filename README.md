# Plaid

Plaid installs a home media stack in one sitting, without a terminal, and removes it without leaving a trace.

At the end of the assistant you get seven apps, already linked together:

| App | What it does |
|---|---|
| Jellyfin | watch your films and series |
| Seerr | request a film or a series |
| Radarr | finds and organises films |
| Sonarr | finds and organises series |
| qBittorrent | downloads, through your VPN |
| Prowlarr | searches your trackers |
| Bazarr | fetches subtitles |

Prowlarr hands its trackers to Radarr and Sonarr, both download with qBittorrent, Jellyfin refreshes its library as soon as a file arrives, Bazarr follows Radarr and Sonarr, and Seerr uses your Jellyfin account. One username and one password work everywhere.

Plaid adds one tracker for you: **Internet Archive**, which only lists public-domain works, so you can try the whole chain legally on day one. Add your own trackers in Prowlarr (see below).

## Install

### Windows or Mac

1. [Install Docker Desktop](#install-docker-desktop).
2. Download the launcher from the [latest release](https://github.com/arimet/plaid/releases/latest): `Plaid-windows.zip` or `Plaid-mac.zip`.
3. Unzip it and double-click `install.bat` (Windows) or `install.command` (Mac).
   - Mac: the first time, macOS refuses a file from an unidentified developer. Right-click the file, choose **Open**, then **Open** again.
   - Windows: SmartScreen may warn you. Click **More info**, then **Run anyway**.
4. The assistant opens in your browser at <http://localhost:7575>.

### Linux PC or mini PC

Copy this line into a terminal, then open the address it prints from any browser on your network:

```bash
curl -fsSL https://raw.githubusercontent.com/arimet/plaid/main/launchers/get.sh | sh
```

It installs Docker if it is missing. To answer the questions in the terminal instead of a browser:

```bash
curl -fsSL https://raw.githubusercontent.com/arimet/plaid/main/launchers/get.sh | sh -s -- --cli
```

The terminal version shows one step per screen: arrow keys to choose, Enter to confirm. Press Ctrl-C during the questions and nothing is changed; press it during the install and everything installed so far is removed again.

On a machine where Plaid is already installed, the same command opens a menu instead: see your apps and their addresses, add trackers, reach your apps away from home, or remove the stack. Nothing needs a browser.

### Synology

1. In DSM, open **Package Center** and install **Container Manager**.
2. In **Control Panel › Terminal & SNMP**, enable SSH.
3. Connect with SSH and run the Linux command above.

Synology support targets x86_64 models on DSM 7.2 or later. It has not been tested on real hardware yet: please open an issue if something goes wrong.

### Raspberry Pi

A Raspberry Pi 4 or 5 with 4 GB of RAM or more runs the stack with Raspberry Pi OS 64-bit: use the Linux command. Put the system on an SSD rather than a microSD card, and plug your disks into the USB 3 ports. A Pi is too slow for Jellyfin to convert videos on the fly: play files your TV or phone reads directly.

## Install Docker Desktop

Docker Desktop runs the seven apps on Windows and Mac. It installs like any other app.

1. Download it from <https://www.docker.com/products/docker-desktop/> and install it.
2. Windows: accept the WSL 2 update if asked, then restart the PC.
3. Open Docker Desktop once and accept its terms.
4. In **Settings › General**, tick **Start Docker Desktop when you sign in**, or the apps stop when the computer restarts. The launcher tries to tick it for you.

Leave the launcher window open while you install Docker Desktop: it carries on by itself once Docker answers.

## Reach your apps away from home (Tailscale)

[Tailscale](https://tailscale.com) links your devices in a private network, so your apps answer on your phone or laptop anywhere, without opening anything on your router. It is free for personal use.

### Linux PC, mini PC, Synology, Raspberry Pi

The assistant has an optional step for it.

1. Create an account on [tailscale.com](https://tailscale.com) (sign in with Google, Microsoft or Apple).
2. Open [login.tailscale.com › Settings › Keys](https://login.tailscale.com/admin/settings/keys), click **Generate auth key**, leave the options off, and copy the key. It starts with `tskey-auth-` and works once, for 90 days.
3. Paste it in the assistant's **Away from home** step. Plaid joins the machine to your network under the name `plaid`.
4. Install the Tailscale app on your phone or laptop and sign in with the same account.
5. Open `http://plaid:8096` for Jellyfin. Every app answers at `http://plaid:<port>` (see [Ports](#ports)).
6. In [login.tailscale.com › Machines](https://login.tailscale.com/admin/machines), open **plaid › … › Disable key expiry**, or the machine leaves your network after 180 days.

If Tailscale already runs on the machine, leave the key empty: your apps are already reachable through it. Removing the stack also takes `plaid` off your Tailscale network.

### Windows or Mac

Docker Desktop cannot put a container on your computer's network, so install the Tailscale app instead:

1. Install Tailscale from [tailscale.com/download](https://tailscale.com/download) on this computer and sign in.
2. Install it on your phone with the same account.
3. In the Tailscale app on the computer, note its name (for example `zoe-macbook`).
4. On your phone, open `http://zoe-macbook:8096`. The other apps use their own port.

## Add your trackers

1. Open Prowlarr, then **Indexers › Add Indexer**.
2. Pick a tracker and fill in its fields (your account on that tracker if it asks for one).
3. Click **Test**, then **Save**. Prowlarr hands it to Radarr and Sonarr by itself.

Trackers protected by Cloudflare do not work. To stop using Internet Archive, disable or delete it in the same list. See the [Prowlarr guide](https://wiki.servarr.com/prowlarr/indexers).

## Remove

Open the assistant again (<http://localhost:7575>, or the address printed by the Linux command), sign in, and click **Remove**. It deletes the apps, their settings, your password and the assistant itself. Your media folder is deleted only if you tick the box, which shows its path and size.

From a terminal: run the `--cli` command again and choose **Remove the stack**, or go straight to it with `curl -fsSL https://raw.githubusercontent.com/arimet/plaid/main/launchers/get.sh | sh -s -- --uninstall`.

Docker stays installed. One small `alpine` image, used for the cleanup itself, may remain: `docker rmi alpine:3.20` removes it.

## Limits

- Plaid installs and removes. To change a setting (new VPN key, other folder), remove the stack and install it again; your media folder can stay.
- The apps are not updated by Plaid.
- No 4K profile, no FlareSolverr.
- Without a VPN, your internet provider and other peers see what you download. The assistant asks you to confirm.

## Ports

| | Port |
|---|---|
| Assistant | 7575 |
| Jellyfin | 8096 |
| Seerr | 5055 |
| Radarr | 7878 |
| Sonarr | 8989 |
| qBittorrent | 8080 |
| Prowlarr | 9696 |
| Bazarr | 6767 |

## Development

The assistant is Python (standard library only) and runs in its own container, talking to Docker through its socket.

```bash
python3 -m unittest                                # unit tests
ARR_LIVE=1 python3 -m unittest tests.test_apps_live  # starts a real stack and checks every link
python3 tests/demo.py 7700                         # the assistant with a simulated install
python3 tests/demo_cli.py linux                    # the terminal version with a simulated install
docker build -t plaid:dev . && PLAID_IMAGE=plaid:dev ./launchers/install.command
```

## Licence

MIT. App logos belong to their projects (see `installer/static/icons/NOTICE.md`).
