# Setup

Checked against this machine's layout. Do these in order.

1. Python 3.14 is already used by the project virtualenv in `.venv`.
2. Copy `.env.example` to `.env`. Leave every provider key blank until you
   choose a service. Do not commit `.env`.
3. `AVM_DATA_ROOT` defaults to `D:\AIVideoManager`. Change it if D: is not
   the drive you want large files on.
4. Leave `AVM_PAID_ENABLED=false` and `AVM_MONTHLY_SPEND_CAP_USD=0`.
5. Leave `AVM_HOST=127.0.0.1` so the app is not reachable from the network.
6. Start with `.\start.ps1` and open http://127.0.0.1:8765 .
7. On Image Library, add the folder that holds your stills. The app does not
   guess where they are.
8. FFmpeg 9.0.2 and Piper are already installed on this machine. Piper and
   the `en_US-joe-medium` voice live under `D:\AIVideoManager\tools`. If
   Diagnostics on another machine says FFmpeg is missing:

```powershell
winget install --id Gyan.FFmpeg -e
```

Or, in an admin terminal: `choco install ffmpeg -y`. Then restart the app.
Official builds: https://ffmpeg.org/download.html and
https://www.gyan.dev/ffmpeg/builds/ . You can also paste `ffmpeg.exe` and
`ffprobe.exe` paths into Settings.

9. Provider keys go in `.env`, then restart. The Providers screen shows which
   field each adapter needs. Enabling a provider does not verify it.
10. Manual-assist sites need no key. Open Manual Assist, pick a site, and
    put stills on its worksheet. Drop downloads into
    `D:\AIVideoManager\inbox\<site-key>\` with the match code in the filename.

Nothing in this list buys a plan or turns paid generation on.
