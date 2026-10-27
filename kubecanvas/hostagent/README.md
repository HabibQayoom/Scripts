# KubeCanvas host agent (Linux)

The installer and agent binaries for connecting a Linux VM to KubeCanvas. The agent only connects **out** to the hub
(TLS, port 443), opens no port, and in its default read-only mode runs as its own unprivileged user.

| File | What it is |
| --- | --- |
| `install.sh` | The installer. Read it before you run it. Its default download location is this folder. |
| `install.sh.sha256` | Checksum of the installer. |
| `kubecanvas-hostagent-linux-amd64`, `-arm64` | The agent, version 0.1.0, static Linux binaries. |
| `*.manifest` | Version, file name and SHA-256 of each binary. The installer checks the download against it. |

## Connect a VM

1. In KubeCanvas open **Hosts**, then **Add host**, and copy the install command it shows.
2. On the VM:

   ```bash
   curl -fsSLO https://raw.githubusercontent.com/HabibQayoom/Scripts/main/kubecanvas/hostagent/install.sh
   curl -fsSLO https://raw.githubusercontent.com/HabibQayoom/Scripts/main/kubecanvas/hostagent/install.sh.sha256
   sha256sum -c install.sh.sha256
   less install.sh          # read it
   sudo bash install.sh --hub <hub from the Add host screen> --token <token from the Add host screen> \
     --insecure-skip-signature-check
   ```

   Easiest: paste the whole command from the Add host screen. On staging it already contains
   `--insecure-skip-signature-check` and the other flags it needs (for example `--command-key` and `--host-id`).

## Important: these builds are not signed yet

The binaries are **not signed** with a release key. The checksum in each manifest proves the download arrived intact,
not who built it. That is why the command above needs `--insecure-skip-signature-check`. It is fine for a staging or
test VM. Do not use it on production machines until signed releases exist: then the Add host screen pins the public
key and the installer verifies every download with `cosign`, and the flag is no longer needed.

To install without any download, build or copy the binary yourself and pass `--binary /path/to/kubecanvas-hostagent`.

## Remove the agent

```bash
sudo bash install.sh --uninstall
```
