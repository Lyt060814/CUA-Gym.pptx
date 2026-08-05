#!/usr/bin/env bash
# The one question a container answers and this machine cannot: does WPS start
# under Xvfb with no user profile, no desktop session, and 64 MB of /dev/shm?
#
# Deliberately self-contained — no repo, no credentials, no corpus. It builds
# its own deck and drives WPS the way `wps_roundtrip.py` does (open, wait for
# the title, Ctrl+S, wait for the bytes to change). If this passes, the rest
# of the runtime is `apt-get` and the full `bootstrap.sh` is worth its build
# time. If it fails, it fails in about six minutes for a few cents.
set -uo pipefail
export DEBIAN_FRONTEND=noninteractive
say() { printf '\n### %s\n' "$*"; }

say "environment before we touch anything"
df -h /dev/shm /tmp | sed 's/^/    /'
nproc | sed 's/^/    cpus: /'
id | sed 's/^/    /'

say "apt"
apt-get update -qq
apt-get install -y --no-install-recommends \
    ca-certificates wget xvfb xdotool x11-utils procps psmisc \
    python3 python3-pip fonts-liberation fonts-dejavu-core >/dev/null 2>&1
echo "    ok"

say "WPS 11.1.0.11723 (319 MB)"
wget -q -O /tmp/wps.deb \
  https://wdl1.pcfg.cache.wpscdn.com/wpsdl/wpsoffice/download/linux/11723/wps-office_11.1.0.11723.XA_amd64.deb \
  || { echo "    FETCH FAILED"; exit 1; }
ls -l /tmp/wps.deb | sed 's/^/    /'
apt-get install -y --no-install-recommends /tmp/wps.deb >/tmp/wpsinstall.log 2>&1 \
  || { echo "    INSTALL FAILED"; tail -30 /tmp/wpsinstall.log | sed 's/^/    /'; exit 1; }
command -v wpp | sed 's/^/    wpp at /' || { echo "    NO wpp BINARY"; exit 1; }

say "seed the EULA key"
mkdir -p /root/.config/Kingsoft
printf '[6.0]\ncommon\\AcceptedEULA=true\n' > /root/.config/Kingsoft/Office.conf
cat /root/.config/Kingsoft/Office.conf | sed 's/^/    /'

say "a deck to open"
pip install --quiet --break-system-packages python-pptx >/dev/null 2>&1
python3 - <<'PY'
from pptx import Presentation
from pptx.util import Inches
p = Presentation()
s = p.slides.add_slide(p.slide_layouts[5])
s.shapes.title.text = "probe"
s.shapes.add_textbox(Inches(1), Inches(2), Inches(4), Inches(1)).text_frame.text = "hello"
p.save("/tmp/probe.pptx")
print("    wrote /tmp/probe.pptx")
PY

say "Xvfb"
Xvfb :99 -screen 0 1280x1024x24 >/tmp/xvfb.log 2>&1 &
XPID=$!
for i in $(seq 1 30); do DISPLAY=:99 xdotool getdisplaygeometry >/dev/null 2>&1 && break; sleep 1; done
DISPLAY=:99 xdotool getdisplaygeometry | sed 's/^/    geometry: /' || { echo "    XVFB NEVER CAME UP"; cat /tmp/xvfb.log; exit 1; }

say "open the deck in WPS"
BEFORE=$(md5sum /tmp/probe.pptx | cut -d' ' -f1)
DISPLAY=:99 wpp /tmp/probe.pptx >/tmp/wpp.log 2>&1 &
WPID=$!
WIN=""
for i in $(seq 1 90); do
    WIN=$(DISPLAY=:99 xdotool search --name "probe" 2>/dev/null | head -1)
    [ -n "$WIN" ] && break
    kill -0 $WPID 2>/dev/null || { echo "    WPS EXITED after ${i}s"; tail -40 /tmp/wpp.log | sed 's/^/    /'; break; }
    sleep 1
done
if [ -z "$WIN" ]; then
    echo "    NO WINDOW — this is the failure we came to find"
    say "every window on the display (a dialog we have not seen would show here)"
    DISPLAY=:99 xdotool search --name "." 2>/dev/null | while read -r w; do
        printf '    %s  %s\n' "$w" "$(DISPLAY=:99 xdotool getwindowname "$w" 2>/dev/null)"
    done
    tail -40 /tmp/wpp.log | sed 's/^/    log: /'
    kill $WPID $XPID 2>/dev/null
    exit 1
fi
echo "    window $WIN: $(DISPLAY=:99 xdotool getwindowname "$WIN")"

say "Ctrl+S, then wait for the bytes to change"
sleep 5
DISPLAY=:99 xdotool windowactivate --sync "$WIN" 2>/dev/null
DISPLAY=:99 xdotool key --window "$WIN" ctrl+s
SAVED=no
for i in $(seq 1 60); do
    [ "$(md5sum /tmp/probe.pptx | cut -d' ' -f1)" != "$BEFORE" ] && { SAVED=yes; break; }
    sleep 1
done
echo "    saved: $SAVED after ${i}s"

say "what the display held at the end"
DISPLAY=:99 xdotool search --name "." 2>/dev/null | while read -r w; do
    printf '    %s  %s\n' "$w" "$(DISPLAY=:99 xdotool getwindowname "$w" 2>/dev/null)"
done
df -h /dev/shm | sed 's/^/    /'

kill $WPID $XPID 2>/dev/null
say "VERDICT: $([ "$SAVED" = yes ] && echo 'WPS OPENS AND SAVES IN A CONTAINER' || echo 'opened but did not save')"
[ "$SAVED" = yes ]
