#!/usr/bin/env python3
"""Extract per-style product photos + spec text from Lily & Taylor catalog PSDs.

Runs on a GitHub Actions runner. Streams: download one PSD from the public
Drive folder -> extract -> delete PSD -> commit outputs every few pages.

Outputs:
  images/{catalog}/p{page:02d}_{group}.jpg   per-style photo (max 2000px)
  pages/{catalog}/p{page:02d}.jpg            small full-page render (QA/matching)
  meta/{catalog}/p{page:02d}.json            layer groups, bboxes, type-layer text
  report.json                                run summary incl. failures
"""
import io, json, os, re, subprocess, sys, time, traceback
import urllib.request

from psd_tools import PSDImage
from PIL import Image

Image.MAX_IMAGE_PIXELS = None

# catalog -> list of Drive folder ids (134 was uploaded as two folders)
FOLDERS = {
    "133": ["1Kl4Ud8s8wVj_cMHon0uggjjVbStjKFyc"],
    "134": ["1gF-ES9MZ9uohbBqvVYoptzUOmakYEX3m",   # pages 1-14 ("Catlog 134")
            "1HKIk9nCobUJ-p8rbz_ovJU-SRQ6eqDFP"],  # pages 15-32
}

UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64)"}


def http_get(url, tries=4):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.read()
        except Exception:
            if i == tries - 1:
                raise
            time.sleep(10 * (i + 1))


def list_folder(folder_id):
    """Return [(file_id, name)] from the public embedded folder view."""
    html = http_get(
        f"https://drive.google.com/embeddedfolderview?id={folder_id}").decode(
        "utf-8", "replace")
    entries = re.findall(
        r'id="entry-([\w-]+)".*?flip-entry-title">([^<]+)<', html, flags=re.S)
    return [(fid, name.strip()) for fid, name in entries]


def download(file_id, dest, tries=3):
    """Download a Drive file: gdown first, curl confirm-endpoint fallback."""
    for i in range(tries):
        try:
            import gdown
            gdown.download(id=file_id, output=dest, quiet=True)
            if os.path.exists(dest) and os.path.getsize(dest) > 1_000_000:
                return
        except Exception:
            pass
        subprocess.run(
            ["curl", "-sL", "--retry", "3", "-o", dest,
             f"https://drive.usercontent.google.com/download?id={file_id}"
             f"&export=download&confirm=t"], check=False)
        if os.path.exists(dest) and os.path.getsize(dest) > 1_000_000:
            return
        time.sleep(20 * (i + 1))
    raise RuntimeError(f"download failed for {file_id}")


def sanitize(s):
    s = re.sub(r"[^\w-]+", "_", s.strip())
    return re.sub(r"_+", "_", s).strip("_") or "unnamed"


def to_rgb(img):
    return img.convert("RGB") if img.mode != "RGB" else img


def save_jpg(img, path, max_side=2000, q=85):
    img = to_rgb(img)
    img.thumbnail((max_side, max_side))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    img.save(path, "JPEG", quality=q, optimize=True)
    return list(img.size)


def page_num(name):
    m = re.match(r"(\d+)", name)
    return int(m.group(1)) if m else 0


def extract_page(psd_path, cat, page):
    psd = PSDImage.open(psd_path)
    meta = {"catalog": cat, "page": page, "file": os.path.basename(psd_path),
            "canvas": [psd.width, psd.height], "groups": [], "texts": []}

    for ly in psd.descendants():
        if ly.kind == "type":
            try:
                txt = str(ly.text)
            except Exception:
                txt = ""
            meta["texts"].append({"layer": ly.name, "text": txt,
                                  "bbox": [ly.left, ly.top, ly.right, ly.bottom]})

    for grp in psd:
        if not grp.is_group():
            continue
        cands = [l for l in grp.descendants()
                 if l.kind in ("smartobject", "pixel") and not l.is_group()
                 and l.width * l.height > 300_000]
        if not cands:
            continue
        smart = [l for l in cands if l.kind == "smartobject"]
        pick = max(smart or cands, key=lambda l: l.width * l.height)
        gname = sanitize(grp.name)
        out = f"images/{cat}/p{page:02d}_{gname}.jpg"
        try:
            img = pick.composite()
            size = save_jpg(img, out)
            meta["groups"].append({
                "group": grp.name, "layer": pick.name, "kind": pick.kind,
                "orig_size": [pick.width, pick.height],
                "bbox": [grp.left, grp.top, grp.right, grp.bottom],
                "image": out, "out_size": size})
        except Exception as e:
            meta["groups"].append({"group": grp.name, "error": str(e)})

    # small full-page render for QA / manual matching fallback
    try:
        comp = psd.composite()
        save_jpg(comp, f"pages/{cat}/p{page:02d}.jpg", max_side=1400, q=80)
    except Exception as e:
        meta["page_render_error"] = str(e)

    os.makedirs(f"meta/{cat}", exist_ok=True)
    with open(f"meta/{cat}/p{page:02d}.json", "w") as f:
        json.dump(meta, f, indent=1, ensure_ascii=False)
    return meta


def git_push(msg):
    subprocess.run(["git", "add", "-A"], check=False)
    r = subprocess.run(["git", "commit", "-m", msg], capture_output=True)
    if r.returncode == 0:
        for _ in range(3):
            if subprocess.run(["git", "push"], capture_output=True).returncode == 0:
                return
            subprocess.run(["git", "pull", "--rebase"], capture_output=True)
            time.sleep(5)


def main():
    report = {"pages": [], "failures": []}
    work = []
    for cat, fids in FOLDERS.items():
        for fid in fids:
            for file_id, name in list_folder(fid):
                if name.lower().endswith(".psd"):
                    work.append((cat, page_num(name), file_id, name))
    work.sort()
    print(f"work list: {len(work)} PSDs", flush=True)

    done = 0
    for cat, page, file_id, name in work:
        out_meta = f"meta/{cat}/p{page:02d}.json"
        if os.path.exists(out_meta):
            done += 1
            continue  # resumable
        t0 = time.time()
        psd_path = f"/tmp/{cat}_{page}.psd"
        try:
            download(file_id, psd_path)
            meta = extract_page(psd_path, cat, page)
            n = len([g for g in meta["groups"] if "image" in g])
            report["pages"].append({"cat": cat, "page": page, "photos": n,
                                    "secs": round(time.time() - t0)})
            print(f"[{cat} p{page:02d}] {n} photos, "
                  f"{len(meta['texts'])} text layers, "
                  f"{round(time.time()-t0)}s", flush=True)
        except Exception as e:
            report["failures"].append({"cat": cat, "page": page, "file": name,
                                       "error": str(e)})
            print(f"[{cat} p{page:02d}] FAILED: {e}", flush=True)
            traceback.print_exc()
        finally:
            if os.path.exists(psd_path):
                os.remove(psd_path)
        done += 1
        if done % 4 == 0:
            git_push(f"extract progress {done}/{len(work)}")

    with open("report.json", "w") as f:
        json.dump(report, f, indent=1)
    git_push(f"extraction complete: {len(report['pages'])} ok, "
             f"{len(report['failures'])} failed")


if __name__ == "__main__":
    main()
