#!/usr/bin/env python3
"""Build Throne Score's offline restroom tiles from OpenStreetMap.

Downloads every restroom record (amenity=toilets, and places tagged toilets=yes)
through the Overpass API in 10°x10° chunks, one request at a time, then writes
one compact JSON file per 1°x1° cell:

  out/tiles/<lat>_<lng>.json   e.g. 40_-74.json covers lat 40..41, lng -74..-73
  out/index.json               {cells: {"40_-74": [count, bytes, sha256]}, generated, license}

The app fetches the cell(s) around the user from a static CDN; it never queries
Overpass itself. Data © OpenStreetMap contributors, ODbL 1.0 — the published tiles
are a derived database and must stay under ODbL with attribution.

Record layout (array, to keep tiles small):
  [id, lat, lng, name, kind, fee, key, wheelchair, changing, unisex, access, hours]
  id        "n123" / "w456" / "r789"
  kind      "p" public toilet (amenity=toilets) | "c" toilets inside a business (toilets=yes)
  fee       "y" | "n" | ""        key   1 | 0 | null (toilets:key / locked=yes / access=key)
  wheelchair "y" | "l" | "n" | "" changing "y" | "n" | ""   unisex 1 | 0 | null
  access    "p" public/yes | "c" customers | "x" private/no | ""   hours: raw opening_hours or ""

Usage:  python3 build_tiles.py --qlever out/qlever.tsv   (fast path; refresh.sh does the export)
        python3 build_tiles.py [--chunk 10] [--only lat0,lng0,lat1,lng1]   (Overpass fallback, slow)
Resumable: finished chunks are cached in out/chunks/.
"""
import argparse, hashlib, json, math, os, sys, time, urllib.parse, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "out")
# Main server only: the public mirrors were hanging (Oct 2026). Its 504s are
# short "busy" rejections, so retry the same server with backoff.
ENDPOINTS = [("https://overpass-api.de/api/interpreter", 360)]
UA = {"User-Agent": "ThroneScore-restroom-tiles/1.0 (weekly build; https://thronescore.app)"}


def overpass(bbox, q=None, timeout_override=None):
    if q is None:
        s, w, n, e = bbox
        q = (f'[out:json][timeout:180][maxsize:536870912];'
             f'(nwr["amenity"="toilets"]({s},{w},{n},{e});nwr["toilets"="yes"]({s},{w},{n},{e}););'
             f'out tags center qt;')
    last = None
    for attempt in range(8):
        for ep, timeout in ENDPOINTS:
            try:
                req = urllib.request.Request(ep, data=urllib.parse.urlencode({"data": q}).encode(), headers=UA)
                body = urllib.request.urlopen(req, timeout=timeout_override or timeout).read()
                data = json.loads(body)
                if "remark" in data and "error" in data.get("remark", "").lower():
                    raise RuntimeError(data["remark"][:200])
                return data["elements"]
            except Exception as ex:  # 429/504/timeouts: back off and try the next mirror
                last = ex
                print(f"  retry {bbox} via next server: {str(ex)[:120]}", flush=True)
                time.sleep(15 + attempt * 15)
    return None  # caller splits the box


def fetch_box(bbox, depth=0):
    """Fetch a box; if it keeps failing (too big / timeouts), split it into quarters."""
    els = overpass(bbox)
    if els is not None:
        return els
    s, w, n, e = bbox
    if depth >= 3:
        raise SystemExit(f"Overpass failed for {bbox}")
    ms, mw = (s + n) / 2, (w + e) / 2
    out = []
    for q in ((s, w, ms, mw), (s, mw, ms, e), (ms, w, n, mw), (ms, mw, n, e)):
        out.extend(fetch_box(q, depth + 1))
        time.sleep(3)
    return out


def yn(v):
    v = (v or "").strip().lower()
    return "y" if v in ("yes", "designated") else "n" if v == "no" else "l" if v == "limited" else ""


def compact(el):
    t = el.get("tags", {})
    lat = el.get("lat", el.get("center", {}).get("lat"))
    lng = el.get("lon", el.get("center", {}).get("lon"))
    if lat is None or lng is None:
        return None
    is_public = t.get("amenity") == "toilets"
    access_raw = (t.get("access") or t.get("toilets:access") or "").lower()
    access = ("p" if access_raw in ("yes", "public", "permissive") else
              "c" if access_raw in ("customers", "customer") else
              "x" if access_raw in ("private", "no", "permit") else "")
    key_raw = (t.get("toilets:key") or t.get("locked") or "").lower()
    key = 1 if (key_raw == "yes" or access_raw == "key") else 0 if key_raw == "no" else None
    wheel = yn(t.get("wheelchair") if is_public else (t.get("toilets:wheelchair") or t.get("wheelchair")))
    changing = yn(t.get("changing_table") or t.get("baby_changing") or t.get("toilets:changing_table")
                  or t.get("diaper") or ("yes" if t.get("changing_table:location") else ""))
    unisex_raw = (t.get("unisex") or t.get("gender_segregated") or "").lower()
    unisex = 1 if t.get("unisex", "").lower() == "yes" or t.get("gender_segregated", "").lower() == "no" else \
        0 if unisex_raw in ("no",) or t.get("gender_segregated", "").lower() == "yes" else None
    fee = yn(t.get("fee") if is_public else (t.get("toilets:fee") or t.get("fee")))
    fee = "y" if fee == "y" else "n" if fee == "n" else ""
    name = t.get("name") or t.get("brand") or ""
    hours = t.get("opening_hours") or ""
    return [f"{el['type'][0]}{el['id']}", round(lat, 5), round(lng, 5), name[:80],
            "p" if is_public else "c", fee, key, wheel,
            changing if changing in ("y", "n") else "", unisex, access, hours[:200]]


QLEVER_COLS = ["osm", "wkt", "amenity", "toilets", "name", "brand", "access", "toilets:access", "fee",
               "toilets:fee", "wheelchair", "toilets:wheelchair", "changing_table", "baby_changing",
               "toilets:changing_table", "toilets:key", "locked", "unisex", "gender_segregated", "opening_hours"]


def unquote(v):
    """A QLever TSV term: "text"^^<type> / "text"@lang / <iri> / bare -> plain text."""
    v = v.strip()
    if v.startswith('"'):
        end = v.rfind('"')
        v = v[1:end] if end > 0 else v[1:]
        return v.replace('\\t', ' ').replace('\\n', ' ').replace('\\"', '"').replace('\\\\', '\\')
    return v


def wkt_center(wkt):
    """Point, or the mean of the first ring's coordinates for lines/polygons."""
    import re
    nums = re.findall(r"(-?\d+(?:\.\d+)?) (-?\d+(?:\.\d+)?)", wkt[:20000])
    if not nums:
        return None
    if wkt.startswith("POINT"):
        lng, lat = map(float, nums[0])
        return lat, lng
    ring = nums[:-1] if len(nums) > 1 and nums[0] == nums[-1] else nums
    lng = sum(float(a) for a, _ in ring) / len(ring)
    lat = sum(float(b) for _, b in ring) / len(ring)
    return lat, lng


def qlever_elements(path):
    """Rows from scripts/restroom-tiles/qlever_restrooms.rq (TSV) as Overpass-style elements."""
    seen = {}
    with open(path, encoding="utf-8") as f:
        next(f)  # header
        for line in f:
            parts = line.rstrip("\n").split("\t")
            if len(parts) < len(QLEVER_COLS):
                continue
            kind, osm_id = parts[0].strip("<>").rsplit("/", 2)[-2:]
            center = wkt_center(unquote(parts[1]))
            if not center or kind not in ("node", "way", "relation"):
                continue
            tags = {}
            for col, raw in zip(QLEVER_COLS[2:], parts[2:]):
                if raw:
                    tags[col] = unquote(raw)
            key = (kind, osm_id)
            prev = seen.get(key)
            if prev:  # same object matched both patterns: merge tags
                prev["tags"].update({k: v for k, v in tags.items() if v})
                continue
            seen[key] = {"type": kind, "id": int(osm_id), "lat": center[0], "lon": center[1], "tags": tags}
    return list(seen.values())


def cell_of(lat, lng):
    return f"{math.floor(lat)}_{math.floor(lng)}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--chunk", type=int, default=10)
    ap.add_argument("--only", help="lat0,lng0,lat1,lng1 (for a quick partial build)")
    ap.add_argument("--shard", help="k/n: only fetch every n-th box starting at k (run n in parallel, then once without --shard to assemble)")
    ap.add_argument("--qlever", help="build from a QLever TSV export (fast path; see qlever_restrooms.rq)")
    ap.add_argument("--global", dest="whole", action="store_true",
                    help="two whole-planet tag queries instead of 504 boxes (fast when the main server is idle)")
    args = ap.parse_args()
    os.makedirs(os.path.join(OUT, "chunks"), exist_ok=True)
    os.makedirs(os.path.join(OUT, "tiles"), exist_ok=True)

    if args.whole:
        boxes = []
        for tag in ('"amenity"="toilets"', '"toilets"="yes"'):
            name = "global_" + tag.replace('"', "").replace("=", "_")
            path = os.path.join(OUT, "chunks", name + ".json")
            if not os.path.exists(path):
                q = f'[out:json][timeout:1800][maxsize:4294967296];nwr[{tag}];out tags center qt;'
                els = overpass(None, q=q, timeout_override=1900)
                if els is None:
                    raise SystemExit(f"global query failed: {tag}")
                json.dump(els, open(path, "w"))
            boxes.append(name)
    elif args.only:
        a, b, c, d = map(float, args.only.split(","))
        boxes = [(a, b, c, d)]
    else:
        boxes = [(la, lo, la + args.chunk, lo + args.chunk)
                 for la in range(-60, 80, args.chunk) for lo in range(-180, 180, args.chunk)]
        # Where the users are first, so a partial build is already useful.
        PRIORITY = [(24, -125, 50, -66),   # contiguous US
                    (35, -11, 72, 40),     # Europe
                    (42, -141, 60, -52),   # southern Canada
                    (30, 125, 46, 146),    # Japan + Korea
                    (-45, 110, -10, 155),  # Australia
                    (-35, -75, 5, -34),    # Brazil / Southern Cone
                    (14, -118, 33, -86)]   # Mexico
        def rank(b):
            for i, (s0, w0, n0, e0) in enumerate(PRIORITY):
                if b[0] < n0 and b[2] > s0 and b[1] < e0 and b[3] > w0:
                    return i
            return len(PRIORITY)
        boxes.sort(key=rank)

    if args.qlever:
        cells = {}
        els = qlever_elements(args.qlever)
        for el in els:
            rec = compact(el)
            if rec:
                cells.setdefault(cell_of(rec[1], rec[2]), {})[rec[0]] = rec
        print(f"qlever: {len(els)} objects", flush=True)
        write_tiles(cells)
        return

    if args.shard:
        k, n = map(int, args.shard.split("/"))
        for i, box in enumerate(boxes):
            if i % n != k:
                continue
            path = os.path.join(OUT, "chunks", "%s_%s_%s_%s.json" % box)
            if not os.path.exists(path):
                els = fetch_box(box)
                json.dump(els, open(path + ".tmp", "w"))
                os.replace(path + ".tmp", path)
                time.sleep(1)
                print(f"[shard {k}] [{i + 1}/{len(boxes)}] {box}: {len(els)} elements", flush=True)
        return

    cells = {}
    for i, box in enumerate(boxes):
        path = os.path.join(OUT, "chunks", (box + ".json") if isinstance(box, str) else "%s_%s_%s_%s.json" % box)
        if os.path.exists(path):
            els = json.load(open(path))
        else:
            els = fetch_box(box)
            json.dump(els, open(path, "w"))
            time.sleep(1)  # be gentle with the public servers
        n = 0
        for el in els:
            rec = compact(el)
            if not rec:
                continue
            # a chunk query is inclusive on its edges: keep each record in exactly one cell
            cells.setdefault(cell_of(rec[1], rec[2]), {})[rec[0]] = rec
            n += 1
        print(f"[{i + 1}/{len(boxes)}] {box}: {n} records", flush=True)
        if (i + 1) % 100 == 0:
            write_tiles(cells, partial=True)
    write_tiles(cells)


def write_tiles(cells, partial=False):
    index = {"v": 1, "generated": time.strftime("%Y-%m-%d"), "cell": 1, "partial": partial,
             "license": "Data © OpenStreetMap contributors, ODbL 1.0 (https://www.openstreetmap.org/copyright)",
             "cells": {}}
    for cell, recs in cells.items():
        body = json.dumps({"v": 1, "cell": cell, "records": sorted(recs.values())},
                          ensure_ascii=False, separators=(",", ":")).encode()
        open(os.path.join(OUT, "tiles", cell + ".json"), "wb").write(body)
        index["cells"][cell] = [len(recs), len(body), hashlib.sha256(body).hexdigest()[:16]]
    json.dump(index, open(os.path.join(OUT, "index.json"), "w"), separators=(",", ":"))
    total = sum(v[0] for v in index["cells"].values())
    size = sum(v[1] for v in index["cells"].values())
    print(f"cells={len(cells)} records={total} bytes={size}")


if __name__ == "__main__":
    main()
