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

# WPS's postinst calls six external commands that its own dependency list does
# not declare.  On a desktop system they are always there; in a minimal
# container they are not, and dpkg fails to configure the package -- which
# leaves `wpp` on disk but half-installed.  Read straight out of
# /var/lib/dpkg/info/wps-office.postinst on a working machine rather than
# discovered one HF Jobs round trip at a time:
#   hexdump              bsdextrautils
#   xdg-icon-resource    xdg-utils
#   update-mime-database shared-mime-info
#   update-desktop-database  desktop-file-utils
#   fc-cache             fontconfig
#   ldconfig             libc-bin (always present)
# The library set a GUI office suite needs that a minimal image does not have.
# `libxslt1.1` is not a guess: the container's own dlopen said
#   libwppmain.so failed, error: libxslt.so.1: cannot open shared object file
# and libxslt is in neither the package's Depends nor its Recommends.  The
# rest is the usual Qt/X11 runtime surface, added in one go rather than one
# five-minute job per library.
#
# Note what is NOT here: libcrypto.so.1.1, libQtCore.so.4 and most of the
# `ldd ... not found` list.  This machine, where WPS works, is missing exactly
# the same ones -- they resolve through the wrapper's RPATH or are never
# loaded.  A naive `ldd` over office6 reports 70 unresolved libraries on a
# working install, so that list is noise and the dlopen message is signal.
say "apt"
apt-get update -qq
apt-get install -y --no-install-recommends \
    ca-certificates wget xvfb xdotool x11-utils procps psmisc bsdextrautils xdg-utils shared-mime-info desktop-file-utils fontconfig \
    libxslt1.1 libsdl2-2.0-0 libasound2 libcurl4 \
    libgl1 libegl1 libglu1-mesa libsm6 libxrender1 libxext6 libxcb1 \
    libxkbcommon-x11-0 libxcb-icccm4 libxcb-image0 libxcb-keysyms1 \
    libxcb-randr0 libxcb-render-util0 libxcb-shape0 libxcb-xinerama0 \
    libxcomposite1 libxdamage1 libxrandr2 libxi6 libxtst6 libnss3 \
    libatk1.0-0 libpango-1.0-0 libcairo2 libfreetype6 libfontconfig1 \
    libcups2 libglib2.0-0 libbz2-1.0 dbus-x11 \
    python3 python3-pip fonts-liberation fonts-dejavu-core >/dev/null 2>&1
echo "    ok"

say "WPS 11.1.0.11723 (319 MB)"
wget -q -O /tmp/wps.deb \
  https://wdl1.pcfg.cache.wpscdn.com/wpsdl/wpsoffice/download/linux/11723/wps-office_11.1.0.11723.XA_amd64.deb \
  || { echo "    FETCH FAILED"; exit 1; }
ls -l /tmp/wps.deb | sed 's/^/    /'
# WITH recommends, unlike everything else here.  A proprietary GUI package
# puts the libraries it needs-but-does-not-strictly-require in Recommends,
# and `wpp` exiting in one second with an empty log is what that looks like.
apt-get install -y /tmp/wps.deb >/tmp/wpsinstall.log 2>&1 \
  || { echo "    INSTALL FAILED"; tail -30 /tmp/wpsinstall.log | sed 's/^/    /'; exit 1; }
command -v wpp | sed 's/^/    wpp at /' || { echo "    NO wpp BINARY"; exit 1; }

say "seed the EULA key"
mkdir -p /root/.config/Kingsoft
# Read off a working install.  `SystemCheck\DoNotReport` is the one that
# matters here: the container showed a modal "System Check" window that took
# focus, which is precisely the dialog `wps_roundtrip._settle_dialogs` was
# written to close.
cat > /root/.config/Kingsoft/Office.conf <<'CONF'
[6.0]
common\AcceptedEULA=true
common\SystemCheck\DoNotReport=true
wpp\Application%20Settings\ShowStartUpTaskPane=0
wpp\Application%20Settings\ShowTipDialogCount=0
CONF
cat /root/.config/Kingsoft/Office.conf | sed 's/^/    /'

say "a deck to open"
# `python3 -m pip`, not `pip`, and errors NOT swallowed: the first run of
# this probe hid a pip failure behind `>/dev/null 2>&1` and then blamed WPS
# for a missing file.  Ubuntu 22.04 ships pip 22, which predates
# --break-system-packages, so passing it is itself the error.
python3 -m pip install --quiet python-pptx || { echo "    PIP FAILED"; exit 1; }
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
    echo "    --- wpp.log (empty means it said nothing, which is itself the clue) ---"
    tail -40 /tmp/wpp.log | sed 's/^/    log: /'

    say "run it in the foreground for an exit code"
    DISPLAY=:99 timeout 30 /opt/kingsoft/wps-office/office6/wpp /tmp/probe.pptx 2>&1 | tail -20 | sed 's/^/    /'
    echo "    exit: ${PIPESTATUS[0]}"

    say "shared libraries office6 cannot resolve"
    MISSING=$(for f in /opt/kingsoft/wps-office/office6/wpp /opt/kingsoft/wps-office/office6/*.so*; do
        ldd "$f" 2>/dev/null | awk '/not found/ {print $1}'
    done | sort -u)
    if [ -n "$MISSING" ]; then
        echo "$MISSING" | sed 's/^/    MISSING /'
        say "which packages would supply them"
        for so in $MISSING; do
            printf '    %-32s %s\n' "$so" "$(apt-file search --package-only "/$so" 2>/dev/null | head -3 | tr '\n' ' ')"
        done
    else
        echo "    none — every library resolves, so this is not a linking failure"
    fi
    kill $WPID $XPID 2>/dev/null
    exit 1
fi
echo "    window $WIN: $(DISPLAY=:99 xdotool getwindowname "$WIN")"

say "close whatever else is on the display first"
# What `_settle_dialogs` does in production, minus the patience.  The first
# container run found "WPS Office" and "System Check" windows holding focus,
# and a modal dialog swallows every key that follows it.
for round in 1 2 3; do
    DISPLAY=:99 xdotool search --name "System Check|WPS Office|Tip|Prompt" 2>/dev/null | while read -r w; do
        [ "$w" = "$WIN" ] && continue
        printf '    closing %s (%s)\n' "$w" "$(DISPLAY=:99 xdotool getwindowname "$w" 2>/dev/null)"
        DISPLAY=:99 xdotool windowactivate "$w" 2>/dev/null
        DISPLAY=:99 xdotool key --window "$w" Escape 2>/dev/null
        DISPLAY=:99 xdotool windowclose "$w" 2>/dev/null
    done
    sleep 2
done

say "dirty the document, then Ctrl+S"
# WPS treats saving an unmodified document as a no-op, exactly as PowerPoint
# does — pressing Ctrl+S on an untouched file writes nothing and proves
# nothing.  The previous run of this probe reported "opened but did not save"
# for that reason and blamed the container for its own mistake.
sleep 5
DISPLAY=:99 xdotool windowactivate --sync "$WIN" 2>/dev/null
DISPLAY=:99 xdotool key --window "$WIN" ctrl+a 2>/dev/null
sleep 1
DISPLAY=:99 xdotool type --window "$WIN" --delay 60 "x"
sleep 2
DISPLAY=:99 xdotool key --window "$WIN" ctrl+z
sleep 2
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
