#!/usr/bin/env bash
# Signinator's AirPlay side:
#   - shairport-sync (AirPlay 2) + nqptp: a speaker whose audio goes into a pipe
#     for Signinator (album art on the sign, music relayed to another speaker)
#   - cliraop: the AirPlay sender for the music relay, built from source with a
#     small patch so it sleeps between packets (the prebuilt one busy-waits at
#     100% of a CPU core); falls back to the pinned prebuilt binary if the
#     build fails.
#
#   sudo bash /opt/signinator/deploy/install-airplay.sh
#
# Safe to run again: it only rebuilds what's missing or out of date, and only
# restarts nqptp / shairport-sync when something they use actually changed,
# so music that's playing isn't interrupted for nothing.
set -euo pipefail

NQPTP_TAG=1.2.8   # must speak the same shared-memory version (smi10) as shairport-sync 4.3.7; 1.2.9 is smi11
SPS_TAG=4.3.7
NAME="${AIRPLAY_NAME:-Signinator}"
META_DIR=/var/lib/signinator-airplay
META_PIPE=$META_DIR/metadata
AUDIO_PIPE=$META_DIR/audio
# AirPlay sender (philippe44/libraop), pinned commit; prebuilt fallback pinned by checksum
LIBRAOP_REPO=https://github.com/philippe44/libraop.git
LIBRAOP_COMMIT=c14088da44df37f58ef7a45b8ce8a3cbc051c5e4
CLIRAOP_URL=https://raw.githubusercontent.com/philippe44/libraop/$LIBRAOP_COMMIT/bin/cliraop-linux-x86_64
CLIRAOP_SHA256=1fedd3db970e00af58036ac659334346affeff84da7ff6488383bc0c41859a92
CLIRAOP_MARK=/usr/local/share/signinator/cliraop-built   # records what /usr/local/bin/cliraop is
SRC=/usr/local/src

NQPTP_CHANGED=0
SPS_CHANGED=0
CONF_CHANGED=0

[ "$(id -u)" = 0 ] || { echo "Run with sudo." >&2; exit 1; }
if dpkg -s shairport-sync >/dev/null 2>&1; then
  echo "The Debian shairport-sync package (AirPlay 1 only) is installed; remove it first: apt remove shairport-sync" >&2
  exit 1
fi

echo "== Build tools and libraries"
apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends \
  build-essential git cmake curl python3 autoconf automake libtool pkgconf xxd systemd-dev \
  libpopt-dev libconfig-dev libasound2-dev avahi-daemon libavahi-client-dev \
  libssl-dev libsoxr-dev libplist-dev libsodium-dev libgcrypt20-dev uuid-dev \
  libavutil-dev libavcodec-dev libavformat-dev libswresample-dev >/dev/null

fetch() {  # fetch <repo> <tag> <dir>
  if [ -d "$3/.git" ]; then git -C "$3" fetch --depth 1 origin tag "$2" && git -C "$3" checkout -q "$2"
  else git clone -q --depth 1 --branch "$2" "$1" "$3"; fi
}

echo "== nqptp $NQPTP_TAG (AirPlay 2 timing; uses UDP 319/320)"
if /usr/local/bin/nqptp -V 2>/dev/null | grep -q "Version: $NQPTP_TAG\." && [ -f /lib/systemd/system/nqptp.service ] && [ -z "${FORCE_BUILD:-}" ]; then
  echo "already installed: $(/usr/local/bin/nqptp -V)"
else
  fetch https://github.com/mikebrady/nqptp.git "$NQPTP_TAG" "$SRC/nqptp"
  cd "$SRC/nqptp"
  autoreconf -fi >/dev/null
  ./configure --with-systemd-startup >/dev/null
  make -j"$(nproc)" >/dev/null
  make install >/dev/null
  NQPTP_CHANGED=1
fi

echo "== shairport-sync $SPS_TAG with AirPlay 2 + metadata, pipe output"
sps_ok() {  # installed build has AirPlay 2 + pipe output + metadata
  local v; v=$(/usr/local/bin/shairport-sync -V 2>/dev/null) || return 1
  case "$v" in "$SPS_TAG-AirPlay2"*) ;; *) return 1;; esac
  for f in pipe metadata; do echo "$v" | grep -q -- "-$f" || return 1; done
}
if sps_ok && [ -z "${FORCE_BUILD:-}" ]; then
  echo "already installed: $(/usr/local/bin/shairport-sync -V)"
else
  fetch https://github.com/mikebrady/shairport-sync.git "$SPS_TAG" "$SRC/shairport-sync"
  cd "$SRC/shairport-sync"
  autoreconf -fi >/dev/null
  make distclean >/dev/null 2>&1 || true
  ./configure --sysconfdir=/etc --with-airplay-2 --with-metadata --with-dummy --with-pipe \
    --with-avahi --with-ssl=openssl --with-soxr --with-systemd >/dev/null
  make -j"$(nproc)" >/dev/null
  make install >/dev/null
  SPS_CHANGED=1
fi

# AirPlay 2 only works when both speak the same shared-memory version (smiNN).
want=$(/usr/local/bin/shairport-sync -V | grep -o 'smi[0-9]*')
have=$(/usr/local/bin/nqptp -V | grep -o 'smi[0-9]*')
if [ "$want" != "$have" ]; then
  echo "nqptp speaks $have but shairport-sync needs $want: pick matching NQPTP_TAG/SPS_TAG." >&2
  exit 1
fi
echo "nqptp and shairport-sync both use $have"

# The service runs as user shairport-sync (its unit says so); make sure it exists.
getent group shairport-sync >/dev/null || groupadd --system shairport-sync
getent passwd shairport-sync >/dev/null || useradd --system --gid shairport-sync \
  --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin shairport-sync

echo "== cliraop (AirPlay sender for the music relay)"
build_cliraop() {
  local d="$SRC/libraop"
  # (set -e is off inside a function used as a condition: check every step)
  # Needs ~1 GB free while building; the sources are deleted afterwards.
  local free; free=$(df -Pm "$SRC" | awk 'NR==2{print $4}')
  if [ "${free:-0}" -lt 1000 ]; then
    echo "Only ${free} MB free on $SRC; need about 1000 MB to build cliraop." >&2
    return 1
  fi
  rm -rf "$d"
  mkdir -p "$d" && git -C "$d" init -q || return 1
  git -C "$d" remote add origin "$LIBRAOP_REPO" || return 1
  git -C "$d" fetch -q --depth 1 origin "$LIBRAOP_COMMIT" || return 1
  git -C "$d" checkout -q FETCH_HEAD || return 1
  # Only the submodules the Linux build uses, one level deep, no history.
  # (NOT --recursive: libopenssl's own submodules are the whole OpenSSL source
  # tree and its dependencies, gigabytes that the build doesn't need.)
  git -C "$d" submodule update -q --init --depth 1 \
      libopenssl libcodecs libmdns crosstools dmap-parser curve25519 || return 1
  # The patch: sleep 2 ms when it isn't time to send yet, instead of spinning.
  python3 - "$d/src/cliraop.c" <<'PY'
import sys
p = sys.argv[1]; s = open(p).read()
old = """			raopcl_send_chunk(raopcl, buf, n / 4, &playtime);
			frames += n / 4;
		}
"""
new = """			raopcl_send_chunk(raopcl, buf, n / 4, &playtime);
			frames += n / 4;
		} else {
			usleep(2000);	/* signinator: don't busy-wait between packets */
		}
"""
if "signinator: don't busy-wait" not in s:
    if s.count(old) != 1:
        sys.exit("cliraop.c changed upstream; patch doesn't apply")
    s = s.replace(old, new)
    if "#include <unistd.h>" not in s:
        s = "#include <unistd.h>\n" + s
    open(p, "w").write(s)
PY
  [ $? -eq 0 ] || return 1
  grep -q "signinator: don't busy-wait" "$d/src/cliraop.c" || return 1
  rm -rf "$d/build"
  cmake -S "$d" -B "$d/build" -DCMAKE_BUILD_TYPE=Release >/dev/null || return 1
  cmake --build "$d/build" -j"$(nproc)" >/dev/null || return 1
  local bin; bin=$(find "$d/build" -maxdepth 1 -type f -name 'raop_play-*' -perm -u+x | head -1)
  [ -n "$bin" ] || return 1
  # smoke test: with no arguments it prints its usage (and exits non-zero)
  { "$bin" 2>&1 || true; } | grep -qi "usage" || return 1
  install -m 0755 "$bin" /usr/local/bin/cliraop.new || return 1
}
if [ -f "$CLIRAOP_MARK" ] && grep -qx "patched $LIBRAOP_COMMIT" "$CLIRAOP_MARK" && [ -x /usr/local/bin/cliraop ] && [ -z "${FORCE_BUILD:-}" ]; then
  echo "already installed (patched build of $LIBRAOP_COMMIT)"
elif build_cliraop; then
  rm -rf "$SRC/libraop"      # free the space; only the binary is needed
  # atomic swap; a relay that's playing keeps its running copy until the next song
  mv -f /usr/local/bin/cliraop.new /usr/local/bin/cliraop
  install -d /usr/local/share/signinator
  echo "patched $LIBRAOP_COMMIT" > "$CLIRAOP_MARK"
  echo "built and installed the patched cliraop (sleeps between packets)"
else
  echo "Couldn't build cliraop; using the prebuilt one (it busy-waits, Signinator runs it at low priority)." >&2
  rm -f /usr/local/bin/cliraop.new
  rm -rf "$SRC/libraop"
  if ! { [ -x /usr/local/bin/cliraop ] && echo "$CLIRAOP_SHA256  /usr/local/bin/cliraop" | sha256sum -c --quiet 2>/dev/null; }; then
    tmp=$(mktemp)
    curl -fsSL -o "$tmp" "$CLIRAOP_URL"
    echo "$CLIRAOP_SHA256  $tmp" | sha256sum -c --quiet || { echo "cliraop checksum mismatch" >&2; rm -f "$tmp"; exit 1; }
    install -m 0755 "$tmp" /usr/local/bin/cliraop
    rm -f "$tmp"
  fi
  install -d /usr/local/share/signinator
  echo "prebuilt $LIBRAOP_COMMIT" > "$CLIRAOP_MARK"
fi

echo "== Config"
install -d -o shairport-sync -g shairport-sync -m 0755 "$META_DIR"
CONF=/etc/shairport-sync.conf
conf_ok() {  # ours already, with the pipes Signinator reads (name/volume lines are Signinator's)
  [ -f "$CONF" ] && grep -q "signinator" "$CONF" && grep -q 'output_backend = "pipe"' "$CONF" \
    && grep -q "$AUDIO_PIPE" "$CONF" && grep -q "$META_PIPE" "$CONF"
}
if conf_ok; then
  echo "$CONF is already set up (Signinator keeps the name and volume lines in step)"
else
  if [ -f "$CONF" ]; then cp "$CONF" "$CONF.bak.$(date +%s)"; fi
  cat > "$CONF" <<EOF
// Written by signinator/deploy/install-airplay.sh
// An AirPlay 2 speaker whose audio and metadata go to Signinator.
general = {
  name = "$NAME";
  // Music goes into a pipe that Signinator reads: it's either thrown away
  // (relay off) or sent on to the AirPlay speaker picked in Signinator's settings.
  output_backend = "pipe";
};
pipe = {
  name = "$AUDIO_PIPE";          // raw PCM, 44.1 kHz, 16-bit stereo
};
metadata = {
  enabled = "yes";
  include_cover_art = "yes";
  pipe_name = "$META_PIPE";      // Signinator reads this (airplay.metadata_pipe)
  pipe_timeout = 5000;
};
EOF
  CONF_CHANGED=1
fi

echo "== Services (restarted only if something changed)"
systemctl daemon-reload
systemctl enable -q nqptp shairport-sync
if [ "$NQPTP_CHANGED" = 1 ] || ! systemctl is-active -q nqptp; then
  systemctl restart nqptp; echo "nqptp restarted"
else
  echo "nqptp left running"
fi
if [ "$NQPTP_CHANGED$SPS_CHANGED$CONF_CHANGED" != 000 ] || ! systemctl is-active -q shairport-sync; then
  systemctl restart shairport-sync; echo "shairport-sync restarted"
else
  echo "shairport-sync left running (music isn't interrupted)"
fi

echo "== Point Signinator at the metadata pipe"
if curl -fsS -m 5 -X POST http://127.0.0.1:8080/api/config -H 'Content-Type: application/json' \
     -d "{\"airplay\":{\"metadata_pipe\":\"$META_PIPE\"}}" >/dev/null; then
  echo "Signinator reads $META_PIPE"
else
  echo "Couldn't reach Signinator on :8080; set airplay.metadata_pipe to $META_PIPE yourself." >&2
fi

sleep 1
systemctl --no-pager --lines=0 status nqptp shairport-sync | grep -E "●|Active:" || true
echo "cliraop: $(cat "$CLIRAOP_MARK" 2>/dev/null)"
