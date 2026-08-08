#!/usr/bin/env python3
"""
Frame Atelier — Home Assistant add-on backend.

Runs inside the add-on container. The browser UI talks to this Flask server
(same-origin, through Ingress), and this server talks to the Samsung The Frame
TV using the `samsungtvws` library. The library opens the raw TCP side-channel
the TV requires for image transfer — something a browser cannot do — which is
why uploads work here but not in the standalone HTML file.

Persistent data (settings + TV pairing token) lives in /data, which Home
Assistant keeps across restarts and updates.
"""

import html
import io
import json
import socket
import sys
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests
from flask import Flask, jsonify, request, send_file, send_from_directory, Response
from PIL import Image

DATA_DIR = Path("/data")
DATA_DIR.mkdir(exist_ok=True)
CONFIG_FILE = DATA_DIR / "config.json"
TOKEN_FILE = str(DATA_DIR / "tv-token.txt")
CACHE_DIR = DATA_DIR / "thumb_cache"
CACHE_DIR.mkdir(exist_ok=True)

WWW_DIR = Path(__file__).parent / "www"
PORT = 8099

TARGET_W, TARGET_H = 3840, 2160  # native 4K, 16:9

app = Flask(__name__, static_folder=None)

DEFAULT_CONFIG = {"tv_ip": "", "unsplash_access_key": "", "matte": "none",
                  "debug": False, "ss_on": False, "ss_minutes": 10, "ss_shuffle": True,
                  "show_matte": True,
                  "favourites": ["misty forest", "japanese woodblock",
                                 "abstract minimal", "ocean waves", "mountain peaks",
                                 "desert dunes", "aurora borealis", "autumn leaves",
                                 "architecture", "macro flowers", "dark moody",
                                 "watercolor"]}


def log(*args):
    print("[frame-atelier]", *args, file=sys.stderr, flush=True)


# ───────────────────────── config ─────────────────────────
def load_config():
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_FILE.exists():
        try:
            cfg.update(json.loads(CONFIG_FILE.read_text()))
        except json.JSONDecodeError:
            pass
    return cfg


def save_config(updates):
    cfg = load_config()
    cfg.update(updates)
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2))
    return cfg


def safe_config(cfg):
    out = dict(cfg)
    out["unsplash_access_key"] = ""
    out["has_unsplash_key"] = bool(cfg.get("unsplash_access_key"))
    return out


# ───────────────────────── TV helpers ─────────────────────────
# Two hard problems with the Frame's art channel:
#  1. It can't handle simultaneous connections — concurrent clients corrupt each
#     other's responses. So we serialise ALL access through one lock.
#  2. Some calls (e.g. set_auto_rotation_status on this firmware) wait internally
#     for a response that never comes, ignoring the socket timeout and hanging
#     forever — which would freeze the lock and every request behind it.
# run_tv() solves both: serialised, and with a HARD timeout enforced by running
# the call in a worker thread and force-closing the socket if it overruns, so a
# hung call can never wedge the whole add-on.
TV_LOCK = threading.Lock()
_DEBUG = False


def dbg(msg):
    if _DEBUG:
        log("DEBUG", msg)


def run_tv(tv_ip, fn, timeout=12, label="tv"):
    """Run fn(art) against the TV, serialised and hard-bounded by `timeout`."""
    from samsungtvws import SamsungTVWS
    with TV_LOCK:
        box = {}

        def work():
            try:
                tv = SamsungTVWS(host=tv_ip, port=8002,
                                 token_file=TOKEN_FILE, timeout=timeout)
                box["tv"] = tv
                box["v"] = fn(tv.art())
            except Exception as e:  # noqa: BLE001
                box["e"] = e

        dbg(f"→ SEND {label}")
        t0 = time.time()
        th = threading.Thread(target=work, daemon=True)
        th.start()
        th.join(timeout)
        ms = int((time.time() - t0) * 1000)

        if th.is_alive():
            # Hung — force the socket closed so the worker's blocked read errors out.
            dbg(f"✗ HUNG {label} after {timeout}s — forcing socket close")
            tv = box.get("tv")
            if tv:
                try:
                    tv.close()
                except Exception:
                    pass
            th.join(2)
            raise TimeoutError(f"{label} timed out after {timeout}s")

        tv = box.get("tv")
        if tv:
            try:
                tv.close()
            except Exception:
                pass
        if "e" in box:
            dbg(f"✗ ERROR {label} in {ms}ms: {box['e']}")
            raise box["e"]
        dbg(f"← DONE {label} in {ms}ms")
        return box.get("v")


# ───────────────────────── app-driven slideshow ─────────────────────────
# The TV's native auto-rotation hangs on this firmware, so we run the slideshow
# ourselves: a background thread that switches the displayed image every N
# minutes using select_image (which works reliably).
_ss_state = {"on": False, "minutes": 10, "shuffle": True}
_ss_event = threading.Event()   # set to wake the loop when settings change
_ss_resync = False              # on enable: baseline against the live image, don't pause
_current_id = None              # last image WE set as current (cheap, in-memory)


def _select_respecting_artmode(art, cid):
    """Select an image, but only force it on-screen if Art Mode is already on — so
    we never pull the TV away from live viewing. Returns 'shown' or 'queued'."""
    show = True
    try:
        if "off" in str(art.get_artmode()).lower():
            show = False
    except Exception:
        pass
    art.select_image(cid, show=show)
    return "shown" if show else "queued"


def _ss_tick():
    cfg = load_config()
    if not cfg["tv_ip"]:
        return
    import random

    def pick(art):
        # 1. Don't take over live TV: only rotate while Art Mode is being displayed.
        try:
            mode = str(art.get_artmode()).lower()
        except Exception:
            mode = ""
        if "off" in mode:
            return ("skip", None)
        # 2. Read what's actually on screen.
        curid = None
        try:
            cur = art.get_current()
            curid = cur.get("content_id") if isinstance(cur, dict) else None
        except Exception:
            pass
        # Baseline = what WE last set. _ss_resync (just enabled) accepts whatever's
        # showing now, so we don't false-pause on a stale value.
        baseline = curid if _ss_resync else _current_id
        dbg(f"slideshow check: tv_current={curid} we_last_set={_current_id} "
            f"baseline={baseline} resync={_ss_resync} artmode={mode}")
        # 3. Pause only if changed by someone else (TV remote) — not via our tool,
        #    which keeps _current_id in sync.
        if baseline and curid and curid != baseline:
            return ("override", curid)
        # 4. Advance to the next image (relative to what's actually showing).
        try:
            items = art.available("MY-C0002")
        except Exception:
            items = art.available()
        ids = [it.get("content_id") for it in (items or []) if it.get("content_id")]
        if not ids:
            return ("none", None)
        ref = curid or baseline
        if _ss_state["shuffle"]:
            pool = [i for i in ids if i != ref] or ids
            choice = random.choice(pool)
        elif ref in ids:
            choice = ids[(ids.index(ref) + 1) % len(ids)]
        else:
            choice = ids[0]
        art.select_image(choice, show=True)  # Art Mode is on (gated above)
        return ("ok", choice)

    try:
        status, value = run_tv(cfg["tv_ip"], pick, timeout=25, label="slideshow.tick")
    except Exception as e:  # noqa: BLE001
        log(f"slideshow tick error: {e}")
        return

    global _current_id, _ss_resync
    if status == "ok":
        _ss_resync = False
        _current_id = value
        dbg(f"slideshow advanced to {value}")
    elif status == "override":
        _ss_resync = False
        _current_id = value
        _ss_state["on"] = False
        save_config({"ss_on": False})
        log(f"slideshow paused: image was changed on the TV to {value} "
            f"(re-enable it in Settings)")
    elif status == "skip":
        dbg("slideshow skipped this tick: TV not in Art Mode")


def _slideshow_loop():
    while True:
        if not _ss_state["on"]:
            _ss_event.wait()
            _ss_event.clear()
            continue
        _ss_tick()
        # Wait the interval, but wake immediately if the settings change.
        if _ss_event.wait(timeout=max(1, _ss_state["minutes"]) * 60):
            _ss_event.clear()


def crop_to_4k(raw_bytes):
    im = Image.open(io.BytesIO(raw_bytes)).convert("RGB")
    sw, sh = im.size
    tr, sr = TARGET_W / TARGET_H, sw / sh
    if sr > tr:
        nw = int(sh * tr)
        left = (sw - nw) // 2
        im = im.crop((left, 0, left + nw, sh))
    else:
        nh = int(sw / tr)
        top = (sh - nh) // 2
        im = im.crop((0, top, sw, top + nh))
    im = im.resize((TARGET_W, TARGET_H), Image.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=92)
    return buf.getvalue()


# ───────────────────────── static UI ─────────────────────────
@app.route("/")
def index():
    return send_from_directory(WWW_DIR, "index.html")


@app.route("/<path:path>")
def static_files(path):
    if (WWW_DIR / path).is_file():
        return send_from_directory(WWW_DIR, path)
    return send_from_directory(WWW_DIR, "index.html")


# ───────────────────────── config API ─────────────────────────
@app.route("/api/config", methods=["GET"])
def api_get_config():
    return jsonify(safe_config(load_config()))


@app.route("/api/config", methods=["POST"])
def api_set_config():
    data = request.get_json(force=True) or {}
    updates = {}
    if isinstance(data.get("tv_ip"), str):
        updates["tv_ip"] = data["tv_ip"].strip()
    if isinstance(data.get("matte"), str):
        updates["matte"] = data["matte"].strip()
    if data.get("unsplash_access_key"):
        updates["unsplash_access_key"] = data["unsplash_access_key"].strip()
    if "show_matte" in data:
        updates["show_matte"] = bool(data["show_matte"])
    if isinstance(data.get("favourites"), list):
        seen, out = set(), []
        for x in data["favourites"]:
            t = str(x).strip()
            if t and t.lower() not in seen:
                seen.add(t.lower())
                out.append(t)
        updates["favourites"] = out[:100]
    if "debug" in data:
        global _DEBUG
        _DEBUG = bool(data["debug"])
        updates["debug"] = _DEBUG
        log(f"debug mode {'ON' if _DEBUG else 'off'}")
    return jsonify(safe_config(save_config(updates)))


# ───────────────────────── TV status ─────────────────────────
@app.route("/api/tv-status")
def api_tv_status():
    cfg = load_config()
    if not cfg["tv_ip"]:
        return jsonify({"reachable": False, "reason": "no_ip"})
    try:
        r = requests.get(f"http://{cfg['tv_ip']}:8001/api/v2/", timeout=4)
        name = ""
        try:
            dev = r.json().get("device", {})
            name = html.unescape(dev.get("name") or dev.get("modelName", ""))
        except Exception:
            pass
        return jsonify({"reachable": r.status_code == 200, "name": name})
    except requests.RequestException:
        return jsonify({"reachable": False, "reason": "unreachable"})


# ───────────────────────── network scan ─────────────────────────
def _probe(ip, port=8002, timeout=0.4):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        return ip if s.connect_ex((ip, port)) == 0 else None
    except OSError:
        return None
    finally:
        s.close()


@app.route("/api/scan")
def api_scan():
    """Probe common home subnets for a device listening on the Samsung art port."""
    subnets = ["192.168.0", "192.168.1", "10.0.0", "192.168.2", "10.1.1", "172.16.0"]
    ips = [f"{s}.{i}" for s in subnets for i in range(1, 255)]
    with ThreadPoolExecutor(max_workers=100) as pool:
        for result in pool.map(_probe, ips):
            if result:
                # Confirm it's a Samsung TV via its info endpoint.
                try:
                    r = requests.get(f"http://{result}:8001/api/v2/", timeout=2)
                    if r.status_code == 200:
                        dev = r.json().get("device", {})
                        return jsonify({"ip": result,
                                        "name": html.unescape(dev.get("name") or dev.get("modelName", ""))})
                except requests.RequestException:
                    pass
    return jsonify({"ip": None})


# ───────────────────────── Unsplash search ─────────────────────────
@app.route("/api/search")
def api_search():
    cfg = load_config()
    key = cfg.get("unsplash_access_key")
    if not key:
        return jsonify({"error": "Add your Unsplash Access Key in Settings first."}), 400
    query = request.args.get("q", "").strip()
    page = request.args.get("page", "1")
    if not query:
        return jsonify({"error": "Empty search."}), 400
    try:
        r = requests.get(
            "https://api.unsplash.com/search/photos",
            params={"query": query, "per_page": 24, "page": page,
                    "orientation": "landscape"},
            headers={"Authorization": f"Client-ID {key}"},
            timeout=20,
        )
        r.raise_for_status()
        data = r.json()
    except requests.RequestException as e:
        return jsonify({"error": str(e)}), 502

    results = [{
        "id": p["id"],
        "thumb": p["urls"]["small"],
        "full": p["urls"]["full"],
        "author": p["user"]["name"],
        "author_url": p["user"]["links"]["html"],
        "download_location": p["links"]["download_location"],
    } for p in data.get("results", [])]
    try:
        page_num = int(page)
    except (TypeError, ValueError):
        page_num = 1
    return jsonify({"results": results, "page": page_num,
                    "total_pages": data.get("total_pages", 1),
                    "total": data.get("total", len(results))})


# ───────────────────────── upload ─────────────────────────
@app.route("/api/upload", methods=["POST"])
def api_upload():
    cfg = load_config()
    if not cfg["tv_ip"]:
        return jsonify({"error": "Set the TV IP in Settings first."}), 400
    data = request.get_json(force=True) or {}
    url = data.get("url")
    matte = data.get("matte", cfg.get("matte", "none"))
    set_first = bool(data.get("set_first"))
    if not url:
        return jsonify({"error": "No image URL."}), 400

    try:
        raw = requests.get(f"{url}&w={TARGET_W}&fit=max&q=90&fm=jpg", timeout=60).content
        jpeg = crop_to_4k(raw)
    except Exception as e:
        return jsonify({"error": f"Image prep failed: {e}"}), 500

    # Register the download with Unsplash (API guideline).
    if data.get("download_location") and cfg.get("unsplash_access_key"):
        try:
            requests.get(data["download_location"],
                         headers={"Authorization": f"Client-ID {cfg['unsplash_access_key']}"},
                         timeout=15)
        except requests.RequestException:
            pass

    def _do(art):
        cid = art.upload(jpeg, file_type="JPEG", matte=matte, portrait_matte=matte)
        if set_first and cid:
            try:
                _select_respecting_artmode(art, cid)
            except Exception:
                pass
        return cid

    try:
        content_id = run_tv(cfg["tv_ip"], _do, timeout=60, label="upload")
        if set_first and content_id:
            global _current_id
            _current_id = content_id
        return jsonify({"content_id": content_id})
    except Exception as e:
        return jsonify({"error": f"TV upload failed: {e}"}), 500


# ───────────────────────── library ─────────────────────────
@app.route("/api/library")
def api_library():
    cfg = load_config()
    if not cfg["tv_ip"]:
        return jsonify({"error": "Set the TV IP in Settings first."}), 400
    def _do(art):
        try:
            items = art.available("MY-C0002")
        except Exception:
            items = art.available()
        current = None
        try:
            cur = art.get_current()
            if isinstance(cur, dict):
                current = cur.get("content_id")
        except Exception:
            pass
        return items, current

    try:
        items, current = run_tv(cfg["tv_ip"], _do, timeout=15, label="library")
        # NOTE: deliberately do NOT update _current_id here. That value must mean
        # "what WE last set" so the slideshow can detect a manual TV-remote change;
        # syncing it from the observed current would mask exactly that.
        out = [{"content_id": it.get("content_id"),
                "matte": it.get("matte_id", "")}
               for it in (items or []) if it.get("content_id")]
        return jsonify({"items": out, "current": current})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/current")
def api_current():
    """Cheap in-memory read (no TV call) — UI polls this so the 'Current' marker
    tracks the slideshow and the Settings toggle reflects an auto-pause."""
    return jsonify({"current": _current_id, "ss_on": _ss_state["on"]})


def _extract_bytes(data):
    """samsungtvws may return raw bytes, a str, or a {name: bytes} mapping."""
    if isinstance(data, dict):
        data = next(iter(data.values()), None) if data else None
    if isinstance(data, str):
        data = data.encode("latin-1")
    return data if isinstance(data, (bytes, bytearray)) and len(data) else None


def _cache_path(content_id):
    safe = "".join(c for c in content_id if c.isalnum() or c in "._-")
    return CACHE_DIR / f"{safe}.jpg"


def _fetch_thumb(art, content_id):
    """Fetch one thumbnail's bytes over an already-open art connection."""
    for label, call in (
        ("get_thumbnail_list", lambda: art.get_thumbnail_list([content_id])),
        ("get_thumbnail",      lambda: art.get_thumbnail(content_id)),
        ("get_thumbnail+dict", lambda: art.get_thumbnail(content_id, True)),
    ):
        try:
            data = _extract_bytes(call())
            if data:
                return bytes(data)
        except TypeError:
            pass  # signature mismatch for this samsungtvws version — try next
        except Exception as e:
            log(f"thumbnail {content_id}: {label} failed: {e}")
    return None


@app.route("/api/thumbnails", methods=["POST"])
def api_thumbnails():
    """Warm the thumbnail cache for many IDs over a SINGLE TV connection.
    Cached IDs cost nothing; only missing ones touch the TV (one handshake)."""
    cfg = load_config()
    if not cfg["tv_ip"]:
        return jsonify({"error": "no_ip"}), 400
    ids = (request.get_json(force=True) or {}).get("ids", [])
    cached = [i for i in ids if _cache_path(i).exists()]
    missing = [i for i in ids if i not in cached]
    failed = []
    if missing:
        def _do(art):
            out = {}
            for cid in missing:
                out[cid] = _fetch_thumb(art, cid)
            return out
        try:
            results = run_tv(cfg["tv_ip"], _do,
                             timeout=max(15, 5 * len(missing)), label="thumbnails")
            for cid, data in results.items():
                if data:
                    _cache_path(cid).write_bytes(data)
                    cached.append(cid)
                else:
                    failed.append(cid)
        except Exception as e:
            log(f"thumbnails warm failed: {e}")
            failed += [m for m in missing if m not in cached]
    return jsonify({"cached": cached, "failed": failed})


@app.route("/api/thumbnail/<content_id>")
def api_thumbnail(content_id):
    p = _cache_path(content_id)
    if p.exists():
        return send_file(p, mimetype="image/jpeg")
    cfg = load_config()
    if not cfg["tv_ip"]:
        return Response(status=400)
    try:
        data = run_tv(cfg["tv_ip"], lambda art: _fetch_thumb(art, content_id),
                      timeout=12, label=f"thumbnail {content_id}")
    except Exception as e:
        log(f"thumbnail {content_id}: {e}")
        data = None
    if data:
        p.write_bytes(data)
        return Response(data, mimetype="image/jpeg")
    return Response(status=404)


@app.route("/api/delete", methods=["POST"])
def api_delete():
    cfg = load_config()
    if not cfg["tv_ip"]:
        return jsonify({"error": "Set the TV IP in Settings first."}), 400
    ids = (request.get_json(force=True) or {}).get("ids", [])
    if not ids:
        return jsonify({"deleted": 0})
    def _do(art):
        try:
            art.delete_list(ids)
        except Exception:
            for cid in ids:
                try:
                    art.delete(cid)
                except Exception:
                    pass

    try:
        run_tv(cfg["tv_ip"], _do, timeout=20, label="delete")
        for cid in ids:
            _cache_path(cid).unlink(missing_ok=True)
        return jsonify({"deleted": len(ids)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/select", methods=["POST"])
def api_select():
    cfg = load_config()
    if not cfg["tv_ip"]:
        return jsonify({"error": "Set the TV IP in Settings first."}), 400
    cid = (request.get_json(force=True) or {}).get("content_id")
    if not cid:
        return jsonify({"error": "No content_id."}), 400
    try:
        shown = run_tv(cfg["tv_ip"], lambda art: _select_respecting_artmode(art, cid),
                       timeout=14, label="select")
        global _current_id
        _current_id = cid
        return jsonify({"ok": True, "shown": shown == "shown"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ───────────────────────── art-mode controls ─────────────────────────
@app.route("/api/slideshow", methods=["POST"])
def api_slideshow():
    """Configure the app-driven slideshow (we rotate images ourselves, because the
    TV's native auto-rotation hangs on this firmware)."""
    d = request.get_json(force=True) or {}
    global _ss_resync
    _ss_state["on"] = bool(d.get("on"))
    _ss_state["minutes"] = max(1, int(d.get("minutes", 10)))
    _ss_state["shuffle"] = bool(d.get("shuffle", True))
    if _ss_state["on"]:
        _ss_resync = True       # baseline against the live image on the first tick
    save_config({"ss_on": _ss_state["on"], "ss_minutes": _ss_state["minutes"],
                 "ss_shuffle": _ss_state["shuffle"]})
    _ss_event.set()  # wake the loop to apply immediately
    log(f"slideshow set: {_ss_state}")
    return jsonify({"ok": True, **_ss_state})


@app.route("/api/matte", methods=["POST"])
def api_matte():
    """Change the matte of existing image(s) on the TV. matte 'none' = full screen."""
    cfg = load_config()
    if not cfg["tv_ip"]:
        return jsonify({"error": "Set the TV IP in Settings first."}), 400
    d = request.get_json(force=True) or {}
    ids = d.get("ids") or ([d["content_id"]] if d.get("content_id") else [])
    matte = d.get("matte", "none")
    if not ids:
        return jsonify({"error": "No images selected."}), 400

    def _do(art):
        for cid in ids:
            try:
                art.change_matte(cid, matte)
            except TypeError:
                art.change_matte(cid, matte, matte)  # variant needing portrait matte
        # Re-select the displayed image so the new matte actually shows on screen
        # (changing the matte alone doesn't refresh what's on the TV).
        refreshed = None
        try:
            cur = art.get_current()
            curid = cur.get("content_id") if isinstance(cur, dict) else None
            if curid in ids:
                _select_respecting_artmode(art, curid)
                refreshed = curid
        except Exception:
            pass
        return refreshed

    try:
        refreshed = run_tv(cfg["tv_ip"], _do, timeout=25, label="change_matte")
        for cid in ids:  # thumbnails change with the matte — drop stale cache
            _cache_path(cid).unlink(missing_ok=True)
        log(f"matte set to {matte} on {ids} (refreshed={refreshed})")
        return jsonify({"ok": True, "matte": matte, "count": len(ids), "refreshed": refreshed})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


_MATTE_CACHE = None


@app.route("/api/mattes")
def api_mattes():
    """Return the TV's real supported matte styles and colours (cached)."""
    global _MATTE_CACHE
    if _MATTE_CACHE:
        return jsonify(_MATTE_CACHE)
    cfg = load_config()
    if not cfg["tv_ip"]:
        return jsonify({"error": "no_ip"}), 400
    try:
        ml = run_tv(cfg["tv_ip"], lambda art: art.get_matte_list(),
                    timeout=8, label="get_matte_list")
        types, colors, rgb = [], [], {}
        color_dicts = []
        if isinstance(ml, dict):
            types = [t.get("matte_type") for t in (ml.get("matte_types") or [])]
            color_dicts = ml.get("matte_colors") or []
        elif isinstance(ml, (list, tuple)):  # some versions return (types, colors)
            types = [t.get("matte_type") for t in (ml[0] or [])] if len(ml) > 0 else []
            color_dicts = ml[1] if len(ml) > 1 else []
        for c in color_dicts:
            name = c.get("color")
            if name:
                colors.append(name)
                rgb[name] = [c.get("R", 60), c.get("G", 60), c.get("B", 60)]
        _MATTE_CACHE = {"types": types, "colors": colors, "rgb": rgb}
        return jsonify(_MATTE_CACHE)
    except Exception as e:
        log(f"mattes failed: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/brightness", methods=["GET", "POST"])
def api_brightness():
    cfg = load_config()
    if not cfg["tv_ip"]:
        return jsonify({"error": "no_ip"}), 400
    try:
        if request.method == "POST":
            value = int((request.get_json(force=True) or {}).get("value", 4))
            run_tv(cfg["tv_ip"], lambda art: art.set_brightness(value),
                   timeout=10, label="set_brightness")
            log(f"brightness set to {value}")
            return jsonify({"ok": True, "value": value})
        val = run_tv(cfg["tv_ip"], lambda art: art.get_brightness(),
                     timeout=8, label="get_brightness")
        try:
            val = int(str(val))
        except (TypeError, ValueError):
            pass
        return jsonify({"value": val})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/colortemp", methods=["GET", "POST"])
def api_colortemp():
    cfg = load_config()
    if not cfg["tv_ip"]:
        return jsonify({"error": "no_ip"}), 400
    try:
        if request.method == "POST":
            value = int((request.get_json(force=True) or {}).get("value", 0))
            run_tv(cfg["tv_ip"], lambda art: art.set_color_temperature(value),
                   timeout=10, label="set_color_temperature")
            log(f"colour temperature set to {value}")
            return jsonify({"ok": True, "value": value})
        val = run_tv(cfg["tv_ip"], lambda art: art.get_color_temperature(),
                     timeout=8, label="get_color_temperature")
        try:
            val = int(str(val))
        except (TypeError, ValueError):
            pass
        return jsonify({"value": val})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


_FILTER_CACHE = None


@app.route("/api/filters")
def api_filters():
    """List the TV's supported photo filters (cached)."""
    global _FILTER_CACHE
    if _FILTER_CACHE:
        return jsonify(_FILTER_CACHE)
    cfg = load_config()
    if not cfg["tv_ip"]:
        return jsonify({"error": "no_ip"}), 400
    try:
        fl = run_tv(cfg["tv_ip"], lambda art: art.get_photo_filter_list(),
                    timeout=8, label="get_photo_filter_list")
        ids = []
        if isinstance(fl, (list, tuple)):
            ids = [f.get("filter_id") for f in fl if isinstance(f, dict) and f.get("filter_id")]
        _FILTER_CACHE = {"filters": ids}
        return jsonify(_FILTER_CACHE)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/filter", methods=["POST"])
def api_filter():
    """Apply a photo filter to image(s) on the TV ('None' = no filter)."""
    cfg = load_config()
    if not cfg["tv_ip"]:
        return jsonify({"error": "Set the TV IP in Settings first."}), 400
    d = request.get_json(force=True) or {}
    ids = d.get("ids") or ([d["content_id"]] if d.get("content_id") else [])
    filt = d.get("filter", "None")
    if not ids:
        return jsonify({"error": "No images selected."}), 400

    def _do(art):
        for cid in ids:
            art.set_photo_filter(cid, filt)
        refreshed = None
        try:
            cur = art.get_current()
            curid = cur.get("content_id") if isinstance(cur, dict) else None
            if curid in ids:
                _select_respecting_artmode(art, curid)
                refreshed = curid
        except Exception:
            pass
        return refreshed

    try:
        run_tv(cfg["tv_ip"], _do, timeout=25, label="set_photo_filter")
        for cid in ids:
            _cache_path(cid).unlink(missing_ok=True)
        log(f"filter {filt} set on {ids}")
        return jsonify({"ok": True, "filter": filt, "count": len(ids)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/art-info")
def api_art_info():
    """Best-effort probe of the TV's current art settings. Logs the raw shapes
    so we can build matte/brightness/filter controls correctly for this firmware."""
    cfg = load_config()
    if not cfg["tv_ip"]:
        return jsonify({"error": "Set the TV IP in Settings first."}), 400
    info = {}
    # Each probe runs in its own short-lived connection with a tight timeout, so
    # one that hangs (waiting for a response this firmware never sends) can't
    # block the others — it just logs a timeout and we move on.
    probes = (
        ("current",     lambda a: a.get_current()),
        ("artmode",     lambda a: a.get_artmode()),
        ("brightness",  lambda a: a.get_brightness()),
        ("colortemp",   lambda a: a.get_color_temperature()),
        ("matte_list",  lambda a: a.get_matte_list()),
        ("filter_list", lambda a: a.get_photo_filter_list()),
    )
    for label, fn in probes:
        try:
            val = run_tv(cfg["tv_ip"], fn, timeout=6, label=f"probe.{label}")
            info[label] = val
            log(f"art-info {label}: {val!r}")
        except Exception as e:
            info[label] = None
            log(f"art-info {label} failed/timeout: {e}")
    return jsonify(info)


if __name__ == "__main__":
    _cfg = load_config()
    _DEBUG = bool(_cfg.get("debug"))
    _ss_state.update(on=bool(_cfg.get("ss_on")),
                     minutes=max(1, int(_cfg.get("ss_minutes", 10))),
                     shuffle=bool(_cfg.get("ss_shuffle", True)))
    threading.Thread(target=_slideshow_loop, daemon=True).start()
    try:
        from importlib.metadata import version
        log("samsungtvws version", version("samsungtvws"))
    except Exception:
        pass
    log(f"Frame Atelier add-on listening on :{PORT} "
        f"(debug={_DEBUG}, slideshow={_ss_state})")
    app.run(host="0.0.0.0", port=PORT, threaded=True)
