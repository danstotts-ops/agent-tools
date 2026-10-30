"""Runpod brand tokens for fleet agents, loaded from brand.json.

Vendored from the marketing plugin's runpod-decks `brand_tokens.py`
(runpod/claude-plugins, marketing 0.10.2) so every agent reads the same canon
with the same rules. Import it, never copy hex codes:

    from agent_tools import brand
    brand.BRAND["purple"]      # Runpod Purple
    brand.THEMES["dark"]["bg"] # Runpod Black
    brand.TYPEFACE             # primary family name

The offline snapshot is the public variant published to S3 by
brand-design-agent's scripts/publish_brand_json.py. Refresh it from
https://runpod-brand-json.s3.us-east-1.amazonaws.com/brand.json when the
canon changes; tests/test_brand.py fails if it stops matching what this module
renders.

The original module docstring follows.

Runpod brand tokens for slide work, loaded from brand.json, plus a WCAG
contrast check.

Standard library only, on purpose: the tokens are useful to anything that draws
a Runpod surface, and depending on python-pptx to read a hex code would be
silly. `deck_style.py` imports these and wraps them for python-pptx.

Where the values come from
--------------------------
BRAND, ACCENT, UTILITY, RAMP, STYLE, SURFACES, LAYOUT, ACCESSIBILITY, the
typeface constants and WEIGHTS are read from Runpod's `brand.json` (contract:
runpod/brand, `brand.json` v1), not typed in here. Top-level keys this module
does not read are ignored, so a newer minor version of the document still
loads. SURFACES, LAYOUT and ACCESSIBILITY are `{}` when the loaded document
does not carry them. SURFACES holds surface ids mapped to dicts and LAYOUT the
token groups (space, radius, type, button), never the block-level `note`,
`source`, `status` or `$source`: those are in SURFACES_META, LAYOUT_META and
ACCESSIBILITY_META (ACCESSIBILITY keeps its scalar thresholds). The one thing
here that is not a brand value is PORTABLE_FALLBACK: see its comment.

Resolution order, first source that loads and validates wins:

1. `BRAND_JSON_PATH`, a local file. For pointing at a branch of the brand repo.
2. https://runpod-brand-json.s3.us-east-1.amazonaws.com/brand.json, the public
   URL of record since 2026-10-29: the public variant of brand.json, published
   from the private repo with attribution strings reduced to roles. Values and
   statuses are identical to the private file.
3. https://raw.githubusercontent.com/runpod/brand/main/brand.json. The repo is
   private, so this answers only with a credential: it is sent an
   `Authorization: Bearer` header when `GITHUB_TOKEN` is set and nothing
   otherwise (an anonymous request gets a 404). The token goes to this host
   only, never to the S3 URL.
4. `brand.snapshot.json`, committed beside this file. Always present, so a deck
   builds on a plane.

Steps 2 and 3 use urllib and are cached on disk for 24 hours under the user
cache directory (`runpod-brand`), one cache file per URL. Any network, HTTP or
parse failure is swallowed and the next URL is tried; once every URL has
failed, a stale cache (S3 first, then raw) beats the snapshot. Set
`BRAND_JSON_OFFLINE=1` to skip the network entirely (tests do). Requests carry
the User-Agent `runpod-brand-tokens/<plugin version>`.

`BRAND_SOURCE` says which one won: "env:<path>", "url:<url>", "cache:<url>" or
"snapshot". `source_used()` folds that to one of "env", "s3", "github" or
"snapshot".
The snapshot is a copy of the S3 brand.json at a point in time. Refresh it from
there when the upstream file changes; `tests/test_brand_tokens.py` fails
when the snapshot stops matching what this module renders.

Status handling
---------------
Every brand.json colour carries a `status`: canonical, provisional or
deprecated. A deprecated colour is never loaded. Optional colours (accents) that
are deprecated are skipped and listed in `SKIPPED_DEPRECATED`. A colour this
module cannot work without (purple, purple-200, purple-950, midnight, white, the
five utility colours) being deprecated makes that whole source invalid, and
resolution moves on to the next one. Same for weights: only non-deprecated ones
reach `WEIGHTS`.

What is not in brand.json
-------------------------
THEMES and STATUS below are the derived layer. The brand guidelines specify no
light-mode variants, no card fills, no hairlines, no secondary text, and no
contrast ratios, so those are worked out here and contrast-checked in
`tests/test_brand_tokens.py`. Every derived value is marked DERIVED. The
`gaps` list in brand.json says the same thing from the other side.

Two earlier facts still hold. The system has no pure #000000 (`BRAND["black"]`
aliases midnight, which is Runpod Black), and it has no red: the error state is
an orange.
"""

import copy
import json
import logging
import os
import sys
import hashlib
import tempfile
import time
import urllib.parse
import urllib.request

log = logging.getLogger("agent_tools.brand")


def _plugin_version():
    """The installed agent-tools version, for the User-Agent."""
    try:
        from importlib.metadata import version
        return version("agent-tools")
    except Exception:
        return "unknown"


USER_AGENT = "runpod-agent-tools-brand/" + _plugin_version()

PUBLIC_URL = "https://runpod-brand-json.s3.us-east-1.amazonaws.com/brand.json"
BRAND_JSON_URL = "https://raw.githubusercontent.com/runpod/brand/main/brand.json"
RAW_HOST = "raw.githubusercontent.com"
SNAPSHOT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "brand.snapshot.json")
CACHE_DIR_NAME = "runpod-brand"
CACHE_TTL_SECONDS = 24 * 60 * 60
FETCH_TIMEOUT_SECONDS = 3
MAX_BYTES = 1024 * 1024
STATUSES = ("canonical", "provisional", "deprecated")

# BRAND key -> brand.json colour token. `black` is not a token: the system has
# no pure black, so it aliases midnight. `purple_light` is purple-200, the
# dark-surface accent, and deliberately not color.brand.purple-light (that one
# is a Radiant Pink duplicate and is deprecated).
_BRAND_TOKENS = {
    "purple": "color.brand.purple",
    "purple_light": "color.brand.purple-200",
    "purple_950": "color.brand.purple-950",
    "midnight": "color.brand.midnight",
    "white": "color.neutral.white",
}
_UTILITY_TOKENS = {
    "information": "color.utility.information",
    "success": "color.utility.success",
    "data": "color.utility.data",
    "error": "color.utility.error",
    "warning": "color.utility.warning",
}
# Optional: loaded when present and not deprecated, otherwise left out.
_ACCENT_TOKENS = {
    "electric_purple": "color.accent.electric-purple",
    "radiant_pink": "color.accent.radiant-pink",
    "ambient_pink": "color.accent.ambient-pink",
    "pink_glow": "gradient.anchor.glow",
}
_FAMILIES = ("primary", "developer", "code", "fallback")


class BrandJsonError(ValueError):
    """A brand.json source that cannot be used."""


def parse_hex(value):
    """'#5D29F0' -> (93, 41, 240). Accepts a leading '#' or not."""
    h = value.lstrip("#")
    if len(h) != 6:
        raise ValueError("expected a 6-digit hex color, got %r" % (value,))
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def relative_luminance(value):
    """WCAG 2.x relative luminance for a hex color."""
    channels = []
    for raw in parse_hex(value):
        c = raw / 255.0
        channels.append(c / 12.92 if c <= 0.03928
                        else ((c + 0.055) / 1.055) ** 2.4)
    r, g, b = channels
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast_ratio(fg, bg):
    """WCAG contrast ratio between two hex colors. 1.0 to 21.0.

    The guidelines state no ratios and no WCAG target anywhere, so the
    thresholds this skill holds itself to are WCAG 2.1 AA and are this file's
    choice: 4.5:1 for body text, 3:1 for large text and non-text UI.
    """
    a, b = relative_luminance(fg), relative_luminance(bg)
    lighter, darker = max(a, b), min(a, b)
    return (lighter + 0.05) / (darker + 0.05)


# --------------------------------------------------------------------- loading
def _colors(doc):
    colors = doc.get("color") if isinstance(doc, dict) else None
    if not isinstance(colors, dict):
        raise BrandJsonError("brand.json has no 'color' object")
    return colors


def _color_entry(doc, token):
    """Find one colour entry. The contract keys `color` by token path
    ("color.brand.purple", "gradient.anchor.glow"); a nested layout and a key
    without the leading "color." are accepted too so a generator tweak does not
    break every installed copy."""
    colors = _colors(doc)
    candidates = [token]
    if token.startswith("color."):
        candidates.append(token[len("color."):])
    for key in candidates:
        if key in colors:
            return colors[key]
    node = colors
    for part in candidates[-1].split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node if isinstance(node, dict) and "value" in node else None


def _hex(entry, token):
    value = entry.get("value") if isinstance(entry, dict) else None
    try:
        parse_hex(value)
    except (ValueError, AttributeError, TypeError):
        raise BrandJsonError("%s has no valid hex value: %r" % (token, value))
    return "#" + value.lstrip("#").upper()


def _status(entry, token):
    status = entry.get("status")
    if status not in STATUSES:
        raise BrandJsonError("%s has status %r, expected one of %s"
                             % (token, status, ", ".join(STATUSES)))
    return status


def _required_color(doc, token):
    entry = _color_entry(doc, token)
    if entry is None:
        raise BrandJsonError("brand.json is missing %s" % token)
    if _status(entry, token) == "deprecated":
        raise BrandJsonError("%s is deprecated and no substitute exists here"
                             % token)
    return _hex(entry, token)


def _all_color_entries(colors, prefix=""):
    """Yield (token, entry) for flat or nested colour layouts."""
    for key, value in colors.items():
        if not isinstance(value, dict):
            continue
        if "value" in value:
            yield prefix + key, value
        else:
            for pair in _all_color_entries(value, prefix + key + "."):
                yield pair


def _ramp(doc):
    """{"purple": {"50": "#DBD8FF", ...}, "neutral": {...}} from color.purple.*
    and color.neutral.*. Only numeric steps count (neutral also carries white
    and black, which BRAND covers or deprecates), deprecated steps are left
    out, and a ramp the document does not carry is {}."""
    ramp = {"purple": {}, "neutral": {}}
    for token, entry in _all_color_entries(_colors(doc)):
        parts = token.split(".")
        if parts[0] == "color":
            parts = parts[1:]
        if (len(parts) != 2 or parts[0] not in ramp or not parts[1].isdigit()
                or entry.get("status") == "deprecated"):
            continue
        ramp[parts[0]][parts[1]] = _hex(entry, token)
    for name in ramp:
        ramp[name] = dict(sorted(ramp[name].items(), key=lambda kv: int(kv[0])))
    return ramp


def _style(doc):
    """The brand.json `style` block. em_dashes is always a bool (False when the
    document does not say); casing and name_forms are present only when it
    carries them."""
    block = doc.get("style")
    block = block if isinstance(block, dict) else {}
    style = {"em_dashes": block.get("em_dashes") is True}
    for key in ("casing", "name_forms"):
        if key in block:
            style[key] = copy.deepcopy(block[key])
    return style


# Block-level keys that describe a whole block (where it came from, how settled
# it is) rather than naming one of its entries.
_META_KEYS = ("note", "source", "status", "$source")


def _section(doc, key, entries_are_dicts):
    """Split an optional top-level block (surfaces, layout, accessibility) into
    (entries, meta). Both are {} when the document predates the block.

    The block-level metadata keys always go to meta. With `entries_are_dicts`
    (surfaces, layout), any other non-dict value goes to meta as well, so every
    value left in entries is a dict and `for k, v in SURFACES.items(): v["width"]`
    cannot meet a string. accessibility keeps its scalar fields as entries."""
    block = doc.get(key)
    if not isinstance(block, dict):
        return {}, {}
    entries, meta = {}, {}
    for name, value in block.items():
        if name in _META_KEYS or (entries_are_dicts
                                  and not isinstance(value, dict)):
            meta[name] = copy.deepcopy(value)
        else:
            entries[name] = copy.deepcopy(value)
    return entries, meta


def derive(doc):
    """Turn a brand.json document into the module's constants.

    Raises BrandJsonError when the document cannot supply them. Pure: no I/O.
    """
    try:
        return _derive(doc)
    except (AttributeError, KeyError, TypeError) as exc:
        raise BrandJsonError("malformed brand.json (%s: %s)"
                             % (type(exc).__name__, exc))


def _derive(doc):
    if not isinstance(doc, dict):
        raise BrandJsonError("brand.json is not an object")
    version = str(doc.get("version", ""))
    if version.split(".")[0] != "1":
        raise BrandJsonError("unsupported brand.json version %r" % version)

    skipped = []
    for token, entry in _all_color_entries(_colors(doc)):
        if entry.get("status") == "deprecated":
            skipped.append("%s %s" % (token, entry.get("value")))

    brand = {key: _required_color(doc, token)
             for key, token in _BRAND_TOKENS.items()}
    brand["black"] = brand["midnight"]
    utility = {key: _required_color(doc, token)
               for key, token in _UTILITY_TOKENS.items()}
    accent = {}
    for key, token in _ACCENT_TOKENS.items():
        entry = _color_entry(doc, token)
        if entry is None or _status(entry, token) == "deprecated":
            continue
        accent[key] = _hex(entry, token)

    typo = doc.get("typography")
    families = typo.get("families") if isinstance(typo, dict) else None
    if not isinstance(families, dict):
        raise BrandJsonError("brand.json has no typography.families")
    names = {}
    for fam in _FAMILIES:
        entry = families.get(fam)
        if not isinstance(entry, dict) or not entry.get("name"):
            raise BrandJsonError("typography.families.%s has no name" % fam)
        if entry.get("status") == "deprecated":
            raise BrandJsonError("typography.families.%s is deprecated" % fam)
        names[fam] = entry["name"]
    stack = families["fallback"].get("stack")
    if (not isinstance(stack, list) or not stack
            or not all(isinstance(s, str) for s in stack)):
        raise BrandJsonError("typography.families.fallback.stack is not a "
                             "list of font names")

    weights = {}
    for name, entry in (typo.get("weights") or {}).items():
        if entry.get("status") == "deprecated":
            skipped.append("typography.weights.%s %s" % (name,
                                                         entry.get("value")))
        else:
            weights[name] = entry["value"]
    if not weights:
        raise BrandJsonError("typography.weights has no usable weight")

    surfaces, surfaces_meta = _section(doc, "surfaces", True)
    layout, layout_meta = _section(doc, "layout", True)
    accessibility, accessibility_meta = _section(doc, "accessibility", False)

    return {
        "BRAND": brand,
        "ACCENT": accent,
        "UTILITY": utility,
        "RAMP": _ramp(doc),
        "STYLE": _style(doc),
        "SURFACES": surfaces,
        "SURFACES_META": surfaces_meta,
        "LAYOUT": layout,
        "LAYOUT_META": layout_meta,
        "ACCESSIBILITY": accessibility,
        "ACCESSIBILITY_META": accessibility_meta,
        "TYPEFACE": names["primary"],
        "MONO_DISPLAY_TYPEFACE": names["developer"],
        "MONO_CODE_TYPEFACE": names["code"],
        "FALLBACK_TYPEFACE": names["fallback"],
        "FALLBACK_STACK": tuple(stack),
        "WEIGHTS": weights,
        "SKIPPED_DEPRECATED": skipped,
    }


def _read_file(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _fetch(url, token=None, timeout=FETCH_TIMEOUT_SECONDS):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    parts = urllib.parse.urlparse(url)
    if token and parts.scheme == "https" and parts.hostname == RAW_HOST:
        # Unredirected, so a redirect to another host never carries the token.
        req.add_unredirected_header("Authorization", "Bearer " + token)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read(MAX_BYTES).decode("utf-8"))


def cache_path(url=BRAND_JSON_URL):
    """Where the brand.json fetched from `url` lives between runs."""
    base = os.environ.get("BRAND_JSON_CACHE_DIR")
    if not base:
        xdg = os.environ.get("XDG_CACHE_HOME")
        if xdg:
            base = os.path.join(xdg, CACHE_DIR_NAME)
        elif sys.platform == "darwin":
            base = os.path.expanduser("~/Library/Caches/" + CACHE_DIR_NAME)
        else:
            base = os.path.expanduser("~/.cache/" + CACHE_DIR_NAME)
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:12]
    return os.path.join(base, "brand-%s.json" % digest)


def _write_cache(path, doc):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(doc, fh)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _fresh_cache(url, now):
    try:
        path = cache_path(url)
        age = now() - os.path.getmtime(path)
        if 0 <= age < CACHE_TTL_SECONDS:
            return derive(_read_file(path)), "cache:" + url
    except (OSError, ValueError):
        pass
    return None


def _from_urls(fetch, now, token):
    """Per URL, in order: a fresh cache, else a fetch. When every URL has
    failed, a stale cache in the same order. None if nothing worked."""
    sources = ((PUBLIC_URL, None), (BRAND_JSON_URL, token))
    for url, url_token in sources:
        found = _fresh_cache(url, now)
        if found:
            return found
        try:
            doc = fetch(url, url_token)
            tokens = derive(doc)
        except Exception as exc:  # fail soft: any network or parse problem
            log.info("brand.json fetch from %s failed (%s: %s)", url,
                     type(exc).__name__, exc)
            continue
        try:
            _write_cache(cache_path(url), doc)
        except OSError as exc:
            log.info("could not write brand.json cache: %s", exc)
        return tokens, "url:" + url
    for url, _ in sources:
        try:
            return derive(_read_file(cache_path(url))), "cache:" + url
        except (OSError, ValueError):
            pass
    return None


def resolve(env=None, fetch=_fetch, now=time.time):
    """Return (tokens, source) from the first source that loads and validates.

    `env` defaults to os.environ. `fetch(url, token)` and `now` are injectable
    so tests never touch the network or the clock.
    """
    env = os.environ if env is None else env
    override = env.get("BRAND_JSON_PATH")
    if override:
        try:
            return derive(_read_file(override)), "env:" + override
        except (OSError, ValueError) as exc:
            log.warning("BRAND_JSON_PATH %s is unusable (%s); falling back",
                        override, exc)
    if env.get("BRAND_JSON_OFFLINE") != "1":
        found = _from_urls(fetch, now, env.get("GITHUB_TOKEN") or None)
        if found:
            return found
        log.warning("brand.json: every URL failed (%s, %s) and no cache is "
                    "usable; falling back to the committed snapshot, which "
                    "may be out of date", PUBLIC_URL, BRAND_JSON_URL)
    return derive(_read_file(SNAPSHOT_PATH)), "snapshot"


_TOKENS, BRAND_SOURCE = resolve()
if _TOKENS["SKIPPED_DEPRECATED"]:
    log.info("brand.json source %s: skipped deprecated entries: %s",
             BRAND_SOURCE, "; ".join(_TOKENS["SKIPPED_DEPRECATED"]))

# Names kept from the hardcoded version so `deck_style.py` and callers keep
# working. Official names are in brand.json's `name` field; use those when
# talking to a designer.
BRAND = _TOKENS["BRAND"]
ACCENT = _TOKENS["ACCENT"]
UTILITY = _TOKENS["UTILITY"]
TYPEFACE = _TOKENS["TYPEFACE"]
MONO_DISPLAY_TYPEFACE = _TOKENS["MONO_DISPLAY_TYPEFACE"]
MONO_CODE_TYPEFACE = _TOKENS["MONO_CODE_TYPEFACE"]
FALLBACK_TYPEFACE = _TOKENS["FALLBACK_TYPEFACE"]
FALLBACK_STACK = _TOKENS["FALLBACK_STACK"]
WEIGHTS = _TOKENS["WEIGHTS"]
RAMP = _TOKENS["RAMP"]
STYLE = _TOKENS["STYLE"]
SURFACES = _TOKENS["SURFACES"]
SURFACES_META = _TOKENS["SURFACES_META"]
LAYOUT = _TOKENS["LAYOUT"]
LAYOUT_META = _TOKENS["LAYOUT_META"]
ACCESSIBILITY = _TOKENS["ACCESSIBILITY"]
ACCESSIBILITY_META = _TOKENS["ACCESSIBILITY_META"]
SKIPPED_DEPRECATED = _TOKENS["SKIPPED_DEPRECATED"]

# Medium portability exceptions, NOT brand values. brand.json names no font for
# a .pptx or .docx opened on a machine without the brand typefaces; these are
# the faces those two formats fall back to when a file must render anywhere.
# Treat them as an exception to the brand type system, never as part of it, and
# never cite them as Runpod's typography.
PORTABLE_FALLBACK = {"pptx": "Arial", "docx": "Aptos"}
"""Medium portability exceptions for .pptx and .docx output, not brand values.
brand.json does not specify them. Use only when the brand typefaces cannot
travel with the file."""


def source_used():
    """Which source the constants above were loaded from: "env", "s3",
    "github" or "snapshot"."""
    kind, _, where = BRAND_SOURCE.partition(":")
    if kind == "env":
        return "env"
    if kind in ("url", "cache"):
        return "s3" if where == PUBLIC_URL else "github"
    return "snapshot"


# --------------------------------------------------------------------- themes
# Mirrors the two official Google Slides templates: light for in-person and
# printed, dark for virtual and evening.
#
# Canon per theme: bg, ink, accent, and the dark surface. Everything marked
# DERIVED is this file's own work because the guidelines do not cover it.
THEMES = {
    "light": {
        "bg": BRAND["white"],
        "ink": BRAND["midnight"],
        "ink_soft": "#5C5770",              # DERIVED
        # p.23: "Use Runpod Purple for readable accents on White Background".
        "accent": BRAND["purple"],
        "surface": "#F5F4FA",               # DERIVED
        "hairline": "#E5E2F0",              # DERIVED
        "head_bg": BRAND["midnight"],
        "head_ink": BRAND["white"],
    },
    "dark": {
        "bg": BRAND["midnight"],
        "ink": BRAND["white"],
        "ink_soft": "#A9A4BD",              # DERIVED
        # p.23: "Use Purple 200 for readable accents on dark background and
        # White for a maximum contrast", and "Purple 200 provides better
        # contrast than the primary purple on dark backgrounds". Runpod Purple
        # on Runpod Black is 3.02:1, which fails body text and sits on the
        # floor for large text. Purple 200 is 7.18:1.
        "accent": BRAND["purple_light"],
        # Purple 950 is canon for exactly this, p.23: "Use as an alternative
        # dark canvas for large surfaces and layouts when you want to introduce
        # depth without relying on pure black."
        "surface": BRAND["purple_950"],
        "hairline": "#332766",              # DERIVED, a tint of Purple 950
        # p.23: "For CTA on Runpod Black background use the Runpod Purple
        # Colour". White on Runpod Purple is 6.91:1.
        "head_bg": BRAND["purple"],
        "head_ink": BRAND["white"],
    },
}

# Scorecard cells. beat, watch and miss map onto the canonical Success, Warning
# and Error. Each value is (ink, fill).
#
# Dark uses the official utility hex directly as the ink; all three clear 6.5:1
# on their fill. Light cannot: the official colors sit between 1.5:1 and 2.9:1
# on white, so the ink is darkened along the same hue until it clears 4.5:1 on
# both the fill and the slide, and the fill is a pale tint of that hue. Those
# six light values are DERIVED. If the guidelines ever publish light-mode
# variants, replace them.
STATUS = {
    "light": {
        "beat": ("#1A8306", "#EAFEE7"),     # DERIVED from Success  #41F420
        "watch": ("#946A00", "#FEF7E7"),    # DERIVED from Warning  #FFC01F
        "miss": ("#C24000", "#FEEEE7"),     # DERIVED from Error    #FF661A
    },
    "dark": {
        "beat": (UTILITY["success"], "#091806"),   # fill DERIVED
        "watch": (UTILITY["warning"], "#181306"),  # fill DERIVED
        "miss": (UTILITY["error"], "#180C06"),     # fill DERIVED
    },
}
