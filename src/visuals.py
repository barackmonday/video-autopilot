"""Stage 4 — gather one vertical visual per beat. Pexels or Pixabay stock (free) or AI images.

If provider is "pexels" but no PEXELS_API_KEY is set, Pixabay is used automatically
(needs PIXABAY_API_KEY). You can also set provider: pixabay directly.
"""
from __future__ import annotations

import math
import time
from pathlib import Path
from typing import Optional

import requests

from .config import env
from .utils import log


def gather_visuals(cfg: dict, script: dict, out_dir: Path, duration: float) -> list[Path]:
    """Return an ordered list of media files (videos/images) to cover `duration`."""
    clips_dir = out_dir / "clips"
    clips_dir.mkdir(exist_ok=True)
    cut = cfg["video"]["cut_every_seconds"]
    needed = max(cfg["visuals"]["clips_per_video"], math.ceil(duration / cut))

    # Map each beat's visual cue across the segments it covers, so visuals track the
    # words being spoken. Pad/repeat to fill, fall back to the channel's visual style.
    cues = [b.get("visual_cue", "").strip() for b in script.get("beats", [])
            if b.get("visual_cue")]
    style = cfg["channel"].get("visual_style", "")
    if cues:
        # stretch the cue list to `needed` items, preserving order
        queries = [cues[int(i * len(cues) / needed)] for i in range(needed)]
    else:
        queries = [style] * needed

    provider = cfg["visuals"]["provider"]
    used_ids: set = set()  # avoid downloading the same Pexels clip twice in a row
    paths: list[Path] = []
    for i, q in enumerate(queries):
        dest = clips_dir / f"{i:02d}"
        try:
            if provider == "pixabay" or (provider == "pexels" and not env("PEXELS_API_KEY")
                                         and env("PIXABAY_API_KEY")):
                paths.append(_pixabay_video(q, dest, cfg, used_ids))
            elif provider == "pexels":
                paths.append(_pexels_video(q, dest, cfg, used_ids))
            elif provider in ("fal", "replicate"):
                paths.append(_ai_image(q, dest, cfg, provider))
            else:
                raise ValueError(f"Unknown visuals.provider: {provider}")
        except Exception as e:  # one bad clip shouldn't kill the run
            log(f"visual {i} ('{q[:30]}') failed: {e}; reusing previous", "warn")
            if paths:
                paths.append(paths[-1])
    if not paths:
        raise RuntimeError("No visuals could be gathered.")
    log(f"visuals: {len(paths)} clips ({provider})", "ok")
    return paths


def _pexels_video(query: str, dest: Path, cfg: dict, used_ids: Optional[set] = None) -> Path:
    key = env("PEXELS_API_KEY")
    if not key:
        raise SystemExit("PEXELS_API_KEY missing in .env")
    used_ids = used_ids if used_ids is not None else set()
    r = requests.get(
        "https://api.pexels.com/videos/search",
        headers={"Authorization": key},
        params={"query": query, "orientation": "portrait", "per_page": 12, "size": "large"},
        timeout=30,
    )
    r.raise_for_status()
    videos = r.json().get("videos", [])
    if not videos:
        raise RuntimeError("no Pexels results")

    # Prefer a fresh result (not already used) so consecutive clips differ.
    fresh = [v for v in videos if v.get("id") not in used_ids] or videos
    video = fresh[0]
    used_ids.add(video.get("id"))

    # Pick a crisp portrait file: tall enough for 1080x1920 but not absurdly large.
    portrait = [f for f in video["video_files"]
                if (f.get("height") or 0) >= (f.get("width") or 0)]
    pool = portrait or video["video_files"]
    files = sorted(
        pool,
        key=lambda f: (abs((f.get("height") or 0) - 1920), -(f.get("width") or 0)),
    )
    url = files[0]["link"]
    out = dest.with_suffix(".mp4")
    out.write_bytes(requests.get(url, timeout=120).content)
    return out


def _pixabay_video(query: str, dest: Path, cfg: dict, used_ids: Optional[set] = None) -> Path:
    """Free stock video from Pixabay. Clips are mostly landscape; the assembler crops to 9:16."""
    key = env("PIXABAY_API_KEY")
    if not key:
        raise SystemExit("PIXABAY_API_KEY missing (add it as a secret / in .env)")
    used_ids = used_ids if used_ids is not None else set()

    # Pixabay matches best on short queries, so fall back from the full phrase to fewer words.
    words = query.split()
    attempts = [query[:100]]
    for n in (3, 2, 1):
        short = " ".join(words[:n])
        if short and short not in attempts:
            attempts.append(short)

    hits: list = []
    for q in attempts:
        r = None
        for _try in range(2):  # one retry if we hit the rate limit (100 requests/minute)
            r = requests.get(
                "https://pixabay.com/api/videos/",
                params={"key": key, "q": q, "per_page": 20, "safesearch": "true",
                        "order": "popular", "video_type": "film"},
                timeout=30,
            )
            if r.status_code == 429:
                wait = min(int(r.headers.get("X-RateLimit-Reset", "30") or 30), 65)
                time.sleep(wait + 1)
                continue
            break
        r.raise_for_status()
        hits = r.json().get("hits", [])
        if hits:
            break
    if not hits:
        raise RuntimeError("no Pixabay results")

    fresh = [h for h in hits if h.get("id") not in used_ids] or hits
    hit = fresh[0]
    used_ids.add(hit.get("id"))

    # Choose a rendition: prefer portrait, otherwise the smallest one that is at least
    # 1080px tall (avoids huge 4K downloads); fall back to the largest available.
    renditions = [v for v in (hit.get("videos") or {}).values() if v.get("url")]
    if not renditions:
        raise RuntimeError("Pixabay result has no downloadable file")
    portrait = [v for v in renditions if (v.get("height") or 0) >= (v.get("width") or 0)]
    pool = portrait or renditions
    big_enough = [v for v in pool if (v.get("height") or 0) >= 1080]
    if big_enough:
        chosen = min(big_enough, key=lambda v: v.get("height") or 0)
    else:
        chosen = max(pool, key=lambda v: v.get("height") or 0)
    out = dest.with_suffix(".mp4")
    out.write_bytes(requests.get(chosen["url"], timeout=180).content)
    return out


def _ai_image(prompt: str, dest: Path, cfg: dict, provider: str) -> Path:
    style = cfg["visuals"].get("ai_image_style", "")
    full = f"{prompt}, {style}"
    out = dest.with_suffix(".png")
    if provider == "fal":
        key = env("FAL_KEY")
        if not key:
            raise SystemExit("FAL_KEY missing in .env")
        r = requests.post(
            "https://fal.run/fal-ai/flux/schnell",
            headers={"Authorization": f"Key {key}", "Content-Type": "application/json"},
            json={"prompt": full, "image_size": "portrait_16_9"},
            timeout=120,
        )
        r.raise_for_status()
        img_url = r.json()["images"][0]["url"]
    else:  # replicate
        key = env("REPLICATE_API_TOKEN")
        if not key:
            raise SystemExit("REPLICATE_API_TOKEN missing in .env")
        r = requests.post(
            "https://api.replicate.com/v1/models/black-forest-labs/flux-schnell/predictions",
            headers={"Authorization": f"Bearer {key}", "Prefer": "wait"},
            json={"input": {"prompt": full, "aspect_ratio": "9:16"}},
            timeout=180,
        )
        r.raise_for_status()
        out_field = r.json().get("output")
        img_url = out_field[0] if isinstance(out_field, list) else out_field
    out.write_bytes(requests.get(img_url, timeout=120).content)
    return out
