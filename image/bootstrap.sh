#!/usr/bin/env bash
# Build the pipeline's runtime on a bare Debian/Ubuntu container.
#
# This is a script rather than only a Dockerfile because the two ways we can
# reach a container differ in what they let us do.  Baked into an image it is
# a `RUN` line; run as a job's first command on a stock `python:3.12` image it
# needs no registry and no local Docker daemon.  The same file serving both is
# the point: whatever we prove works here is what ships.
#
# Idempotent.  Every step checks for its own result first, so re-running after
# a failure resumes rather than repeats -- WPS alone is a 319 MB download.

set -euo pipefail

log() { printf '\n=== %s\n' "$*" >&2; }

# The version is pinned, and pinned to a specific number rather than "latest".
# 11.1.0.11723 is what this pipeline's WPS behaviour was measured against --
# `wps_roundtrip.py`'s waits, the notes-pane dirty trick, the ~2-in-80 startup
# segfault rate.  A different build is a different set of measurements, and we
# would not know which of our numbers had stopped being true.  Kingsoft ship no
# apt repository, so there is no "pinned" to express except the URL itself.
WPS_VERSION="${WPS_VERSION:-11.1.0.11723}"
WPS_BUILD="${WPS_VERSION##*.}"
WPS_URL="${WPS_URL:-https://wdl1.pcfg.cache.wpscdn.com/wpsdl/wpsoffice/download/linux/${WPS_BUILD}/wps-office_${WPS_VERSION}.XA_amd64.deb}"

export DEBIAN_FRONTEND=noninteractive

# --------------------------------------------------------------------------
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
log "apt packages"

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
apt-get update -qq
apt-get install -y --no-install-recommends \
    ca-certificates curl wget git unzip procps psmisc file \
    bsdextrautils xdg-utils shared-mime-info desktop-file-utils fontconfig \
    libxslt1.1 libsdl2-2.0-0 libasound2 libcurl4 \
    libgl1 libegl1 libglu1-mesa libsm6 libxrender1 libxext6 libxcb1 \
    libxkbcommon-x11-0 libxcb-icccm4 libxcb-image0 libxcb-keysyms1 \
    libxcb-randr0 libxcb-render-util0 libxcb-shape0 libxcb-xinerama0 \
    libxcomposite1 libxdamage1 libxrandr2 libxi6 libxtst6 libnss3 \
    libatk1.0-0 libpango-1.0-0 libcairo2 libfreetype6 libfontconfig1 \
    libcups2 libglib2.0-0 libbz2-1.0 dbus-x11 \
    xvfb xdotool x11-utils \
    libreoffice-impress libreoffice-core \
    poppler-utils \
    fonts-liberation fonts-dejavu-core fonts-noto-core fonts-noto-cjk \
    python3 python3-pip python3-venv \
    >/dev/null

# --------------------------------------------------------------------------
log "WPS Office ${WPS_VERSION}"

if ! command -v wpp >/dev/null 2>&1; then
    deb="/tmp/wps-office_${WPS_VERSION}.XA_amd64.deb"
    # -C - resumes a partial download; the file is 319 MB and a job that dies
    # mid-fetch should not start over.
    wget -q --show-progress -c -O "$deb" "$WPS_URL"

    # `apt-get install ./file.deb` rather than `dpkg -i` so apt resolves the
    # dependency list instead of leaving a half-configured package behind.
    apt-get install -y --no-install-recommends "$deb" >/dev/null
    rm -f "$deb"
fi
command -v wpp >/dev/null || { echo "wpp did not install" >&2; exit 1; }

# The first-run dialogs, suppressed by the keys that suppress them.  Read off
# a working installation rather than guessed, and extended after a container
# run showed two windows holding focus that this machine never shows:
# `WPS Office` and `System Check`.  `AcceptedEULA` alone is not enough on a
# machine with no user profile at all.
#
# Only this key is seeded.  The rest of that file is telemetry counters and
# machine identifiers (`infoGUID`, `deviceid`, `VLGDeviceKey`); copying it
# wholesale would stamp one developer's device identity onto every container.
seed_wps_config() {
    local home="$1"
    mkdir -p "$home/.config/Kingsoft"
    cat > "$home/.config/Kingsoft/Office.conf" <<'CONF'
[6.0]
common\AcceptedEULA=true
common\SystemCheck\DoNotReport=true
wpp\Application%20Settings\ShowStartUpTaskPane=0
wpp\Application%20Settings\ShowTipDialogCount=0
CONF
}

# Seeded for every home a job might run under, not just $HOME.  The first
# container run of the real smoke seeded only $HOME and WPS never showed a
# window -- and WPS reads $HOME as the *wrapper script* sees it, which is not
# necessarily what this script sees.  Two directories cost nothing; guessing
# wrong costs a job.
seed_wps_config "${HOME:-/root}"
[ "${HOME:-/root}" = /root ] || seed_wps_config /root

# --------------------------------------------------------------------------
log "Node and the Claude CLI"

if ! command -v node >/dev/null 2>&1; then
    curl -fsSL https://deb.nodesource.com/setup_22.x | bash - >/dev/null
    apt-get install -y --no-install-recommends nodejs >/dev/null
fi
command -v claude >/dev/null 2>&1 || npm install -g @anthropic-ai/claude-code >/dev/null

# --------------------------------------------------------------------------
log "Python dependencies"

# `--break-system-packages` only exists from pip 23; Ubuntu 22.04 ships 22,
# where passing it fails the whole install.  Added only when supported.
PIPFLAGS=""
python3 -m pip install --help 2>/dev/null | grep -q -- --break-system-packages \
    && PIPFLAGS="--break-system-packages"
python3 -m pip install --no-cache-dir $PIPFLAGS -q \
    "python-pptx>=0.6.23" "lxml>=4.9" "Pillow>=10.0" \
    pandas requests huggingface_hub pytest

# --------------------------------------------------------------------------
log "what we ended up with"

{
    echo "wps        $(dpkg-query -W -f='${Version}' wps-office 2>/dev/null || echo MISSING)"
    echo "soffice    $(soffice --version 2>/dev/null | head -1 || echo MISSING)"
    echo "pdftoppm   $(pdftoppm -v 2>&1 | head -1 || echo MISSING)"
    echo "xvfb       $(command -v Xvfb || echo MISSING)"
    echo "xdotool    $(xdotool --version 2>&1 || echo MISSING)"
    echo "node       $(node --version 2>/dev/null || echo MISSING)"
    echo "claude     $(claude --version 2>/dev/null || echo MISSING)"
    echo "python     $(python3 --version)"
    echo "shm        $(df -h /dev/shm | tail -1)"
} >&2
