# Frame Atelier — Home Assistant Add-on

Curate **Unsplash** images onto a **Samsung The Frame** TV's Art Mode, straight
from your Home Assistant sidebar. A free alternative to the Samsung Art Store —
search, send, organise, and auto-rotate art, all over your local network.

Unlike a pure browser tool, this add-on runs a small Python backend that can
open the raw socket Samsung requires to transfer images, so **uploads,
thumbnails, library management and live art-mode settings all work**.

---

## Contents
- [Install](#install)
- [First run](#first-run)
- [Library tab](#library-tab)
- [Upload tab](#upload-tab)
- [Settings tab](#settings-tab)
- [The slideshow, in detail](#the-slideshow-in-detail)
- [Effects &amp; behaviour reference](#effects--behaviour-reference)
- [Notes &amp; caveats](#notes--caveats)

---

## Install

> Requires Home Assistant **OS** or **Supervised** — add-ons aren't available on
> HA **Container** or **Core**. (This is an *add-on*, not a HACS integration;
> HACS does not install add-ons.)

**Option A — local add-on (quick):**
1. Copy the `frame_atelier` folder into your HA `/addons` directory (via the
   Samba or SSH add-on).
2. **Settings → Add-ons → Add-on Store → ⋮ → Check for updates**.
3. Find **Frame Atelier** under *Local add-ons* → **Install** → **Start**.

**Option B — repository:** push this folder to a GitHub repo and add its URL via
**Add-on Store → ⋮ → Repositories**.

Open the UI from the sidebar or **Open Web UI**.

## First run

1. **Settings → TV Connection** — enter the TV's IP (or press **Scan**). A static
   IP in your router is recommended so it never changes.
2. **Settings → Unsplash API** — paste a free **Access Key** from
   [unsplash.com/developers](https://unsplash.com/developers) → *New Application*.
3. If the TV shows an **"Allow connection?"** prompt the first time art is sent,
   accept it. The pairing token is saved to `/data` and you won't be asked again.
   (Some TVs don't prompt at all.)

Everything (settings, pairing token, thumbnail cache) persists in `/data` across
restarts and updates. Nothing leaves your network except calls to the Unsplash API.

---

## Library tab

The default tab. Shows everything stored in the TV's *My Photos* art collection.

- **Currently displayed (hero):** the image the TV currently has selected is shown
  enlarged at the top with a green border, and marked **Current** in the grid. If
  the slideshow advances while you're watching, the marker follows it automatically.
- **Thumbnails:** fetched from the TV once and cached on disk, so re-opening the
  Library is instant. They reload automatically after a matte/filter change.
- **Set as current:** hover any image → **Set as current**. If Art Mode is on it
  shows immediately; if you're watching live TV it's **queued** for next time Art
  Mode comes up (so it never interrupts your viewing). The button is hidden on the
  image that's already current.
- **Select / delete:** tap images to multi-select; **Delete selected**, **Delete
  all**, or **Select all**. Deleting frees up the TV's art memory.
- **Matte** (per image, changeable any time):
  - Pick a **style** + **colour**, then **Apply to current** (or **Apply to
    selected** when images are ticked).
  - A live **preview** of the matte colour is overlaid on the hero image.
  - `none` = full screen. A matte **crops the photo edges** (see effects below).
  - Mattes are applied to *existing* images — no re-upload needed.
- **Filter** (per image): pick a photo filter (None / Aqua / ArtDeco / Ink / Wash /
  Pastel / Feuve) and **Apply to current / selected**.

## Upload tab

- **Search Unsplash** — type a query (or tap a favourite chip). A **×** clears the
  box and re-focuses it.
- **Favourite searches** — tap the **☆** star to save the current search (★ when
  saved; tap to remove). Favourites appear as chips (15 per page, **‹ ›** for the
  rest) and are managed under *Settings → Favourite searches*. They persist in
  `/data`. Long terms are shown shortened with "…" (hover for the full text).
- **Infinite scroll** — more results load automatically as you scroll; no paging.
- **Multi-select** — tap any result to select it (works across multiple searches
  and reappearing duplicates stay in sync).
- **Floating send bar** (appears when ≥1 selected, works on mobile):
  - A **thumbnail strip** of your picks — tap one to choose **which image displays
    immediately** after sending (marked "shows").
  - **Show one now** toggle — turn off to add everything to the library without
    changing what's on screen.
  - **Send to TV** uploads each image (cropped to native 4K, full-screen), with a
    progress counter, then shows your chosen pick (respecting Art Mode). A warning
    reminds you to keep the tab open until it finishes.
  - **Clear** empties the selection.
- **Back-to-top** button appears once you scroll down.

Uploads are always sent **full-screen**; add a matte later from the Library if you
want one.

## Settings tab

- **TV Connection:** IP, **Scan** (probes common subnets), **Test reach**.
- **Unsplash API:** save your Access Key.
- **Favourite searches:** add/remove the terms shown as chips on the Upload tab.
- **Art Mode:**
  - **Slideshow:** auto-rotate on/off, interval (1 min → 1 day), **Shuffle**.
  - **Brightness** and **Colour temperature** sliders (live, global Art-Mode settings).
  - **Show matte controls** — hide the Library matte row if you don't use mattes.
  - **Debug mode** — logs every TV send/acknowledgement to the add-on Log.
  - **Probe TV art settings** — logs your TV's real matte/filter/colour options.
  - Press **Apply** to save the slideshow settings.

---

## The slideshow, in detail

The TV's *native* auto-rotation hangs on current Frame firmware, so Frame Atelier
runs the slideshow itself — switching the displayed image on a timer using a call
the TV handles reliably. It's designed to never get in your way:

| Situation | Behaviour |
|---|---|
| You're watching a **live channel** | Ticks are **skipped** (Art Mode is off) — it won't pull you back to art. |
| You **set an image** from this app while watching | Queued silently for next Art Mode; not shown over your channel. |
| You change the image **with this app** | Slideshow keeps running. |
| You change the image **on the TV remote** | Slideshow **pauses itself** and unticks in Settings (you get a toast). Re-enable when ready. |

State is saved, so the slideshow resumes after a restart.

---

## Effects &amp; behaviour reference

- **Matte** = a border the TV draws *over* the photo, which **crops the edges**.
  It is **not** a scale-to-fit frame — there is no API to shrink/zoom an image, so
  a matte always covers some of the picture. `none` = full bleed. Per-image and
  changeable live.
- **Photo filter** = a colour/tone treatment (sepia-like, etc.), per image, live.
- **Brightness / Colour temperature** = global Art-Mode display settings (affect
  everything, like the TV's own art menu).
- **Full-screen uploads** — images are centre-cropped to native 4K (3840×2160).
- **Set as current / slideshow** never interrupt live TV; they respect Art Mode.

## Notes &amp; caveats

- `samsungtvws` Art-Mode support is community-maintained and pinned to a known-good
  version. Some **2022+ Frame models** restrict art endpoints; if a specific action
  errors while the TV is otherwise reachable, that's usually a model/firmware limit.
  Turn on **Debug mode** and check the **Log** — then it's easy to diagnose.
- The TV must be **awake / reachable** for uploads and slideshow ticks; a fully
  powered-off TV won't respond.
- The add-on talks to the TV directly over your LAN. Only Unsplash API calls leave
  your network. Per Unsplash's API guidelines, each search result shows the
  photographer's name and a link to their Unsplash profile, and the required
  download event is registered on send — note the credit does not appear on the TV.
