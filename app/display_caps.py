"""What the screen on the other end can actually render.

Picking has always had to guess at this. `DV_REJECT` assumes the display cannot
decode Dolby Vision; `HDR_DEMOTE` assumes it cannot get bright enough for HDR.
Both are per-household facts configured once, which is fine until one server
serves two rooms — the projector that needs SDR and the OLED that should get
every bit of the grade, from the same addon URL.

A player that knows its display can say so. The header is optional in the
strongest sense: absent, every decision falls back to the configured default
and behaves exactly as it does today. Stock Stremio, Jellyfin and unmodified
Nuvio send nothing and retain the default ranking.

## The header

    X-Display-Caps: hdr=hdr10,hlg; dv=0; maxlum=215; audio=ac3,eac3; override=sdr

Every field is optional and an unparseable one is dropped rather than failing
the request — a bad header must never cost somebody their film.

| field | meaning |
|---|---|
| `hdr` | transfer functions the display accepts: `hdr10`, `hdr10plus`, `hlg` |
| `dv` | `1` if the chain can decode Dolby Vision, `0` if not |
| `maxlum` | peak brightness in nits, as reported by the display |
| `audio` | codecs the chain can pass through: `truehd`, `eac3`, `dts`, … |
| `override` | `sdr` forces SDR-first, `hdr` forces HDR-ok, absent means decide |

`override` exists because display EDID is routinely absent or optimistic,
especially on projectors. The automatic path handles honest displays; the
override is the guarantee.
"""

from __future__ import annotations

import re
from contextvars import ContextVar
from dataclasses import dataclass, field

# Below this peak brightness, an HDR grade mastered for 1000+ nits renders dark
# and flat rather than bright. Matches DolbyVisionBaseLayerPolicy.DIM_DISPLAY_NITS
# in the player, deliberately — the two have to agree or a title gets tone
# mapped by one of them and demoted by the other.
DIM_DISPLAY_NITS = 400

_HDR_TOKENS = {"hdr10", "hdr10plus", "hdr10+", "hlg", "pq", "hdr"}
_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}
# Codec names as the picker's own _AUDIO_RES patterns would recognise them.
_AUDIO_ALIASES = {
    "truehd": "truehd", "true-hd": "truehd", "mlp": "truehd",
    "eac3": "eac3", "e-ac3": "eac3", "eac-3": "eac3", "ddp": "eac3",
    "ac3": "ac3", "ac-3": "ac3", "dd": "ac3",
    "dts": "dts", "dtshd": "dtshd", "dts-hd": "dtshd", "dtsx": "dtsx",
    "dts-x": "dtsx", "atmos": "atmos", "aac": "aac", "opus": "opus",
    "flac": "flac", "pcm": "pcm", "lpcm": "pcm", "mp3": "mp3",
}
# Passing these through needs a capable receiver; everything else is either
# universally decodable or gets downmixed without anyone noticing.
_PREMIUM_AUDIO = {"truehd", "dtshd", "dtsx", "atmos"}

_MAX_HEADER = 512          # a real one is ~60 bytes; anything longer is noise


@dataclass(frozen=True)
class DisplayCaps:
    """One display's abilities. Every field may be unknown."""

    hdr: frozenset[str] = field(default_factory=frozenset)
    dv: bool | None = None
    max_nits: int | None = None
    audio: frozenset[str] = field(default_factory=frozenset)
    override: str = ""
    #: Stable per-television id, so what one screen cannot decode is learned
    #: against that screen rather than the household. Opaque to us by design —
    #: the player generates it once and it identifies nothing but itself.
    device: str = ""

    # ── the questions picking actually asks ──────────────────────────────────

    def demotes_hdr(self) -> bool | None:
        """Should HDR sort below SDR here? None when the header says nothing
        either way, so the configured default still decides."""
        if self.override == "sdr":
            return True
        if self.override == "hdr":
            return False
        if self.hdr:
            # It accepts HDR. Whether it can *show* it is a brightness question,
            # and one it can only answer if it reported brightness.
            if self.max_nits is not None:
                return self.max_nits < DIM_DISPLAY_NITS
            return False
        if self.dv is not None or self.max_nits is not None:
            # It described itself and listed no HDR transfer at all: SDR panel.
            return True
        return None

    def passes_through(self, codec: str) -> bool | None:
        """Whether a premium audio codec survives to the speakers. None when
        unknown — the picker then scores it as it always has."""
        if not self.audio:
            return None
        return codec in self.audio

    def cache_token(self) -> str:
        """Short, stable tag for the parts that change the ranking.

        Two displays that would be ranked for identically share a cached answer;
        two that would not, must not. Audio is in here because it moves the
        tie-break score, and `override` because it moves everything.
        """
        if not self.known():
            return ""
        parts = []
        demote = self.demotes_hdr()
        if demote is not None:
            parts.append("sdr" if demote else "hdr")
        if self.dv is not None:
            parts.append("dv" if self.dv else "nodv")
        if self.audio:
            parts.append("a" + "".join(sorted(c[:2] for c in self.audio
                                              if c in _PREMIUM_AUDIO)))
        if self.device:
            # Two televisions with identical panels can still have learned
            # different things, and the learned record changes the ranking.
            parts.append(self.device[:8])
        return "-".join(parts)

    def known(self) -> bool:
        return bool(self.hdr or self.audio or self.override or self.device
                    or self.dv is not None or self.max_nits is not None)

    def header(self) -> str:
        """These caps as an `X-Display-Caps` value: `parse(c.header()) == c`.

        Lets a playback session carry the caps of the screen that asked for it
        into work the proxy starts later — the next-episode prefetch — where
        there is no request header to read them from. Without it the prefetch
        ranked for no display in particular and stored its answer under a key
        no caps-reporting player ever looks up."""
        parts = []
        if self.hdr:
            parts.append("hdr=" + ",".join(sorted(self.hdr)))
        if self.dv is not None:
            parts.append(f"dv={1 if self.dv else 0}")
        if self.max_nits is not None:
            parts.append(f"maxlum={self.max_nits}")
        if self.audio:
            parts.append("audio=" + ",".join(sorted(self.audio)))
        if self.override:
            parts.append(f"override={self.override}")
        if self.device:
            parts.append(f"dev={self.device}")
        return "; ".join(parts)


#: The caps for the request being served, set once per request in `app.main`.
#: A ContextVar rather than a parameter because the sort-key functions it
#: reaches are called from comparison callbacks eight frames down, and because
#: the background finisher inherits the context of the task that spawned it —
#: so the slow picker keeps ranking for the display that asked.
current_caps: ContextVar[DisplayCaps | None] = ContextVar("display_caps",
                                                          default=None)

UNKNOWN = DisplayCaps()


def current() -> DisplayCaps | None:
    """Caps for this request, or None when the player told us nothing."""
    caps = current_caps.get()
    return caps if caps is not None and caps.known() else None


def parse(raw: str | None) -> DisplayCaps | None:
    """Parse an `X-Display-Caps` value. None when there is nothing usable in it.

    Deliberately forgiving: unknown keys, junk values and duplicate fields are
    ignored rather than rejected. The cost of misreading one field is a slightly
    worse pick; the cost of raising is no film.
    """
    if not raw:
        return None
    raw = raw.strip()
    if not raw or len(raw) > _MAX_HEADER:
        return None

    hdr: set[str] = set()
    audio: set[str] = set()
    dv: bool | None = None
    max_nits: int | None = None
    override = ""
    device = ""

    for part in raw.split(";"):
        key, _, value = part.partition("=")
        key = key.strip().lower()
        value = value.strip().lower()
        if not key or not value:
            continue
        if key == "hdr":
            for tok in _split(value):
                if tok in _HDR_TOKENS:
                    hdr.add("hdr10plus" if tok == "hdr10+" else tok)
        elif key == "dv":
            if value in _TRUE:
                dv = True
            elif value in _FALSE:
                dv = False
        elif key == "maxlum":
            nits = _int(value)
            # 0 is how a display says "I don't know"; absurd values are a bug
            # at the other end, not a 100,000-nit screen.
            if nits and 0 < nits <= 20_000:
                max_nits = nits
        elif key == "audio":
            for tok in _split(value):
                mapped = _AUDIO_ALIASES.get(tok)
                if mapped:
                    audio.add(mapped)
        elif key == "override":
            if value in ("sdr", "hdr"):
                override = value
        elif key == "dev":
            # Opaque and self-assigned; bound and sanitised because it becomes
            # part of a stored key and a cache key.
            cleaned = re.sub(r"[^a-z0-9._-]", "", value)[:64]
            if cleaned:
                device = cleaned

    caps = DisplayCaps(hdr=frozenset(hdr), dv=dv, max_nits=max_nits,
                       audio=frozenset(audio), override=override, device=device)
    return caps if caps.known() else None


def _split(value: str) -> list[str]:
    return [t for t in re.split(r"[,\s]+", value) if t]


def _int(value: str) -> int | None:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def describe(caps: DisplayCaps | None) -> str:
    """One-line summary for the log."""
    if caps is None or not caps.known():
        return "unknown display"
    bits = []
    if caps.override:
        bits.append(f"override={caps.override}")
    bits.append("hdr=" + (",".join(sorted(caps.hdr)) if caps.hdr else "none"))
    if caps.dv is not None:
        bits.append("dv=" + ("yes" if caps.dv else "no"))
    if caps.max_nits is not None:
        bits.append(f"{caps.max_nits}nits")
    if caps.audio:
        bits.append("audio=" + ",".join(sorted(caps.audio)))
    if caps.device:
        bits.append(f"dev={caps.device[:12]}")
    return " ".join(bits)
