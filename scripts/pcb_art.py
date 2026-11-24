"""Deterministic four-material artwork compiler. Millimetres, white selects masks.

Inputs for BOTH faces are normal reading view. Only PCB export mirrors B once.
Automatic cleanup is opt-in and component-based. No blanket stroke inflation.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import subprocess
import sys
import uuid
import zipfile

import numpy as np
from PIL import Image, ImageDraw
from scipy import ndimage as ndi
import sexpdata

VERSION = "0.2.1"
MATERIALS = ("dark", "light", "white", "gold")
PALETTE = ("#092b61", "#3477ab", "#faf8ee", "#be9c43")
LAYER_NAMES = ("Cu", "Mask", "SilkS")


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def write_json(path, data):
    Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def positive(value, name, zero=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0 or (value == 0 and not zero):
        raise ValueError(f"{name}: expected {'nonnegative' if zero else 'positive'} finite number")
    return value


def shape_of(config):
    b = config["board"]
    ppm = positive(config["px_per_mm"], "px_per_mm")
    w, h = [positive(b[k], k) for k in ("width_mm", "height_mm")]
    shape = (round(h * ppm), round(w * ppm))
    if max(abs(shape[0] - h * ppm), abs(shape[1] - w * ppm)) > 1e-7:
        raise ValueError("Board dimensions must be exact integer pixels at this px_per_mm")
    if min(shape) < 2 or np.prod(shape) > 40_000_000:
        raise ValueError("Canvas must contain 2..40,000,000 pixels with each dimension >=2")
    radius = positive(b.get("radius_mm", 0), "radius_mm", zero=True)
    if radius > min(w, h) / 2:
        raise ValueError("Corner radius exceeds half the shorter side")
    positive(b.get("thickness_mm", 1.6), "thickness_mm")
    for k, v in config["rules"].items():
        positive(v, k, zero=True)
    for k in ("copper_feature_mm","silk_feature_mm"):
        if config["rules"][k]*ppm < 3:
            raise ValueError(f"{k}: fewer than three grid cells. Increase px_per_mm or revise the explicit feature target")
    return shape


def binary(path, shape):
    with Image.open(path) as im:
        if im.mode != "L":
            raise ValueError(f"{Path(path).name}: binary masks must be opaque single-channel L images")
        values = np.asarray(im.convert("L"))
    if values.shape != shape or not np.isin(values, (0, 255)).all():
        raise ValueError(f"{Path(path).name}: expected {shape[::-1]}, black=0 / white=255 only")
    return values == 255


def save_mask(path, mask):
    Image.fromarray(mask.astype(np.uint8) * 255).save(path)


def rgb_colors(colors):
    if len(colors) != 4:
        raise ValueError("Exactly four material colors required in dark/light/white/gold order")
    return np.array([tuple(bytes.fromhex(c.lstrip("#"))) for c in colors], dtype=np.uint8).reshape(4, 3)


def lab(rgb):
    """sRGB D65 -> CIELAB. Used for a starting separation, not semantic recognition."""
    c = np.asarray(rgb, dtype=np.float32) / 255
    c = np.where(c <= .04045, c / 12.92, ((c + .055) / 1.055) ** 2.4)
    xyz = c @ np.array([[.4124564, .3575761, .1804375], [.2126729, .7151522, .0721750], [.0193339, .1191920, .9503041]], dtype=np.float32).T
    xyz /= np.array([.95047, 1., 1.08883], dtype=np.float32)
    f = np.where(xyz > (6/29)**3, np.cbrt(xyz), xyz / (3*(6/29)**2) + 4/29)
    return np.stack((116*f[..., 1]-16, 500*(f[..., 0]-f[..., 1]), 200*(f[..., 1]-f[..., 2])), axis=-1)


def separate(rgb, colors):
    target = lab(colors)
    out = np.empty(rgb.shape[:2], np.uint8)
    for y in range(0, len(rgb), 64):
        block = lab(rgb[y:y+64])
        out[y:y+64] = ((block[..., None, :] - target)**2).sum(axis=-1).argmin(axis=-1)
    return out


def region(spec, root, shape, ppm, record):
    if ("mask" in spec)==("polygon_mm" in spec):
        raise ValueError("Specify exactly one of mask or polygon_mm in a region")
    if "mask" in spec:
        p = root / spec["mask"]
        record[spec["mask"]] = sha(p)
        return binary(p, shape)
    pts = spec["polygon_mm"]
    if np.asarray(pts).ndim!=2 or np.asarray(pts).shape[1]!=2 or len(pts) < 3 or not np.isfinite(pts).all():
        raise ValueError("A region needs >=3 finite polygon vertices")
    if any(not (0<=x<=shape[1]/ppm and 0<=y<=shape[0]/ppm) for x,y in pts):
        raise ValueError("Region polygon lies outside the common canvas")
    im = Image.new("1", shape[::-1])
    ImageDraw.Draw(im).polygon([(round(x*ppm), round(y*ppm)) for x, y in pts], fill=1)
    return np.asarray(im, dtype=bool)


def geometry_masks(config):
    """Conservative cell-square containment in analytic rounded board minus slots.

    Half-diagonal reserves the uncertainty of testing pixel centres. Holes and
    keepouts are common TOP-view physical coordinates, unlike B artwork inputs.
    """
    shape = shape_of(config)
    b = config["board"]
    w, h, ppm = b["width_mm"], b["height_mm"], config["px_per_mm"]
    y = (np.arange(shape[0])[:, None] + .5) / ppm
    x = (np.arange(shape[1])[None, :] + .5) / ppm
    r = b.get("radius_mm", 0)
    qx, qy = np.abs(x-w/2)-(w/2-r), np.abs(y-h/2)-(h/2-r)
    inside = r - (np.hypot(np.maximum(qx, 0), np.maximum(qy, 0)) + np.minimum(np.maximum(qx, qy), 0))
    slots = []
    for s in b.get("slots", []):
        sw, sh = positive(s["width_mm"], "slot width"), positive(s["height_mm"], "slot height")
        if sw < sh:
            raise ValueError("Only horizontal capsules supported, width >= height")
        cx, cy = s["center_mm"]
        if not np.isfinite([cx, cy]).all() or not (cx-sw/2 > 0 and cx+sw/2 < w and cy-sh/2 > 0 and cy+sh/2 < h):
            raise ValueError("Slot must lie wholly inside the board")
        # Exact capsule containment in the convex rounded outer outline via its
        # bounding rectangle corners (conservative for very large corner radii).
        for sx in (cx-sw/2, cx+sw/2):
            for sy in (cy-sh/2, cy+sh/2):
                a, c = abs(sx-w/2)-(w/2-r), abs(sy-h/2)-(h/2-r)
                if math.hypot(max(a, 0), max(c, 0)) + min(max(a, c), 0) >= r:
                    raise ValueError("Slot approaches rounded board corner")
        distance = np.hypot(np.maximum(np.abs(x-cx)-(sw-sh)/2, 0), y-cy) - sh/2
        slots.append(distance)
    if len(slots) > 1:
        raise ValueError("v0.2 supports at most one capsule slot")
    halfdiag = math.sqrt(2) / (2 * ppm)
    clear = config["rules"]["edge_clearance_mm"]
    safe = inside >= clear + halfdiag
    for distance in slots:
        safe &= distance >= clear + halfdiag
    # Physical transparent cutout for previews, using pixel centres.
    solid = inside >= 0
    for distance in slots:
        solid &= distance > 0
    rf = np.zeros(shape, bool)
    for box in config.get("nfc_keepouts_mm", []):
        x0, y0, x1, y1 = box
        if not np.isfinite(box).all() or not (0 <= x0 < x1 <= w and 0 <= y0 < y1 <= h):
            raise ValueError("NFC keepout rectangle outside canvas")
        # Any cell touching the rectangle is excluded from decorative copper.
        rf[max(0, math.floor(y0*ppm)):math.ceil(y1*ppm), max(0, math.floor(x0*ppm)):math.ceil(x1*ppm)] = True
    return safe, solid, rf


def disk(radius_mm, ppm):
    r = math.ceil(radius_mm * ppm)
    yy, xx = np.ogrid[-r:r+1, -r:r+1]
    return xx*xx + yy*yy <= (radius_mm * ppm + 1e-9)**2


def expand(mask, radius_mm, ppm):
    if not mask.any() or radius_mm <= 0:
        return mask.copy()
    # Any touching cell is included. Conservatively overbuilds at most one
    # cell diagonal, to provide coverage for the exact grid-square geometry.
    return ndi.distance_transform_edt(~mask) <= radius_mm * ppm + math.sqrt(2)


def vertices(mask):
    v = np.zeros((mask.shape[0]+1, mask.shape[1]+1), bool)
    for dy, dx in ((0, 0), (1, 0), (0, 1), (1, 1)):
        v[dy:dy+mask.shape[0], dx:dx+mask.shape[1]] |= mask
    return v


def gap(a, b, ppm):
    if not a.any() or not b.any():
        return None
    return float(ndi.distance_transform_edt(~vertices(b), sampling=1/ppm)[vertices(a)].min())


def copper_margin(copper, opening, ppm):
    if not opening.any():
        return None
    if (opening & ~copper).any():
        return -1.
    # Cu boundary vertices are touched by both selected and unselected cells.
    outside = vertices(np.pad(~copper, 1, constant_values=True))[1:-1, 1:-1]
    boundary = vertices(copper) & outside
    return float(ndi.distance_transform_edt(~boundary, sampling=1/ppm)[vertices(opening)].min())


def feature_report(mask, ppm, nominal_mm, island_mm2):
    labels, count = ndi.label(mask, np.ones((3, 3)))
    areas = np.bincount(labels.ravel())[1:] / ppm**2
    opened = ndi.binary_opening(mask, structure=disk(nominal_mm/2, ppm))
    removed = mask & ~opened
    return {"components_8": count, "small_islands": int((areas < island_mm2).sum()),
            "opening_loss_pixels": int(removed.sum()),
            "opening_loss_fraction": float(removed.sum()/max(1, mask.sum())),
            "interpretation": "Discrete opening flags narrow detail, tips and corners. It does not measure minimum functional stroke width."}, removed


def prepare_face(spec, root, config, record):
    shape, ppm = shape_of(config), config["px_per_mm"]
    colors = rgb_colors(config.get("palette", PALETTE))
    if ("labels" in spec)==("image" in spec):
        raise ValueError("Specify exactly one of image or labels for each face")
    if "labels" in spec:
        p = root / spec["labels"]
        with Image.open(p) as im:
            if im.mode != "L":
                raise ValueError("Labels must be single-channel L, not a palette/RGB/transparent PNG")
            labels = np.asarray(im)
        if labels.shape != shape or not np.isin(labels, range(4)).all():
            raise ValueError("Label PNG must be single-channel 0/1/2/3 at exact canvas dimensions")
        labels = labels.astype(np.uint8)
        record[spec["labels"]] = sha(p)
    else:
        p = root / spec["image"]
        with Image.open(p) as im:
            im = im.convert("RGBA")
            if im.size != shape[::-1]:
                raise ValueError(f"{p.name}: {im.size} must equal {shape[::-1]}. Explicitly compose/resample the master first.")
            if np.asarray(im.getchannel("A")).min() != 255:
                raise ValueError("RGB master must be opaque. Composite it explicitly before separation")
            labels = separate(np.asarray(im.convert("RGB")), colors)
        record[spec["image"]] = sha(p)
    original = labels.copy()
    protected = np.zeros(shape, bool)
    for item in spec.get("protect", []):
        protected |= region(item, root, shape, ppm, record)
    # Whole connected islands can be removed only when fully contained in the
    # explicit edit region and completely outside protected semantic features.
    for edit in spec.get("cleanup", []):
        area = positive(edit["below_area_mm2"], "below_area_mm2")
        roi = region(edit, root, shape, ppm, record)
        material, replace = MATERIALS.index(edit["material"]), MATERIALS.index(edit["replace_with"])
        cc, count = ndi.label(labels == material, np.ones((3, 3)))
        sizes = np.bincount(cc.ravel()) / ppm**2
        forbidden = np.unique(cc[(~roi) | protected])
        allow = sizes < area
        allow[0] = False
        allow[forbidden] = False
        labels[allow[cc]] = replace
    # Explicit overrides intentionally have priority over protection. They can
    # restore skin triangles, eyebrows, mouth or editable title masks exactly.
    for edit in spec.get("overrides", []):
        roi = region(edit, root, shape, ppm, record)
        labels[roi] = MATERIALS.index(edit["material"])
    return original, labels, protected


def compile_layers(labels, config, safe, rf):
    ppm, rules = config["px_per_mm"], config["rules"]
    gold = (labels == 3) & safe & ~rf
    copper = ((labels == 1) | (labels == 3) | expand(gold, rules["gold_copper_margin_mm"], ppm)) & safe & ~rf
    white = (labels == 2) & safe
    # Keepout permits white drawing. Only remove silk near actual exposed gold.
    silk = white & ~expand(gold, rules["silk_to_mask_mm"], ppm)
    return {"Cu": copper, "Mask": gold, "SilkS": silk}


def preview(layers, solid, colors, low_contrast=False):
    colors = colors.copy()
    if low_contrast:
        colors[1] = np.rint(.8 * colors[0].astype(float) + .2 * colors[1]).astype(np.uint8)
    label = np.zeros(solid.shape, np.uint8)
    label[layers["Cu"]] = 1
    label[layers["Mask"] & layers["Cu"]] = 3
    label[layers["SilkS"]] = 2
    rgba = np.dstack((colors[label], solid.astype(np.uint8)*255))
    return Image.fromarray(rgba)


def rectangles(mask):
    """Exact union of closed pixel squares, preserving holes and negative lines."""
    active = {}
    for y, row in enumerate(mask):
        edge = np.flatnonzero(np.diff(np.r_[False, row, False].astype(np.int8)))
        runs = set(zip(edge[::2].tolist(), edge[1::2].tolist()))
        for run in sorted(active.keys() - runs):
            yield active[run]
        active = {run: (run[0], active[run][1] if run in active else y, run[1], y+1) for run in sorted(runs)}
    yield from active.values()


def outline(width, height, radius):
    if radius == 0:
        return [("line", a, b) for a, b in zip([(0, 0), (width, 0), (width, height), (0, height)], [(width, 0), (width, height), (0, height), (0, 0)])]
    k, w, h, r = radius/math.sqrt(2), width, height, radius
    return [("line", (r, 0), (w-r, 0)), ("arc", (w-r, 0), (w-r+k, r-k), (w, r)),
            ("line", (w, r), (w, h-r)), ("arc", (w, h-r), (w-r+k, h-r+k), (w-r, h)),
            ("line", (w-r, h), (r, h)), ("arc", (r, h), (r-k, h-r+k), (0, h-r)),
            ("line", (0, h-r), (0, r)), ("arc", (0, r), (r-k, r-k), (r, 0))]


def slot_outline(slot):
    cx, cy = slot["center_mm"]
    w, h = slot["width_mm"], slot["height_mm"]
    r, a, b = h/2, cx-(w-h)/2, cx+(w-h)/2
    if w == h:
        return [("arc", (a, cy-r), (a+r, cy), (a, cy+r)), ("arc", (a, cy+r), (a-r, cy), (a, cy-r))]
    return [("line", (a, cy-r), (b, cy-r)), ("arc", (b, cy-r), (b+r, cy), (b, cy+r)),
            ("line", (b, cy+r), (a, cy+r)), ("arc", (a, cy+r), (a-r, cy), (a, cy-r))]


def uid(value):
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "pcb-art-enig/0.2/" + str(value)))


def fmt(x):
    return f"{x:.6f}".rstrip("0").rstrip(".") or "0"


def write_pcb(path, sides, config):
    b, ppm = config["board"], config["px_per_mm"]
    counts = {}
    with Path(path).open("w", encoding="utf-8", newline="\n") as f:
        f.write(f'''(kicad_pcb (version 20240108) (generator "pcb_art_enig")
  (general (thickness {fmt(b.get('thickness_mm', 1.6))}))
  (paper "A4")
  (layers (0 "F.Cu" signal) (31 "B.Cu" signal)
    (36 "B.SilkS" user "b.silkscreen") (37 "F.SilkS" user "f.silkscreen")
    (38 "B.Mask" user) (39 "F.Mask" user) (44 "Edge.Cuts" user))
  (setup (pad_to_mask_clearance 0)) (net 0 "")
''')
        for side, layers in sides.items():
            for name, mask in layers.items():
                layer, count = f"{side}.{name}", 0
                manufactured = np.fliplr(mask) if side == "B" else mask
                for x0, y0, x1, y1 in rectangles(manufactured):
                    pts = " ".join(f"(xy {fmt(x/ppm)} {fmt(y/ppm)})" for x, y in ((x0,y0),(x1,y0),(x1,y1),(x0,y1)))
                    f.write(f'  (gr_poly (pts {pts}) (stroke (width 0) (type default)) (fill solid) (layer "{layer}") (uuid "{uid((layer,x0,y0,x1,y1))}"))\n')
                    count += 1
                counts[layer] = count
        edges = [outline(b["width_mm"], b["height_mm"], b.get("radius_mm", 0))]
        edges += [slot_outline(s) for s in b.get("slots", [])]
        for loop_id, loop in enumerate(edges):
            for i, geo in enumerate(loop):
                keys = ("start", "end") if geo[0] == "line" else ("start", "mid", "end")
                attrs = " ".join(f"({key} {fmt(p[0])} {fmt(p[1])})" for key, p in zip(keys, geo[1:]))
                f.write(f'  (gr_{geo[0]} {attrs} (stroke (width 0.05) (type default)) (layer "Edge.Cuts") (uuid "{uid((loop_id,i))}"))\n')
        f.write(")\n")
    return counts


def roundtrip_pcb(path, expected, config):
    """Independent S-expression parse, exact rectangle raster and analytic edges."""
    root = sexpdata.loads(Path(path).read_text(encoding="utf-8"))
    def attr(node, key):
        return next((v[1:] for v in node[1:] if isinstance(v, list) and str(v[0]) == key), None)
    if str(root[0]) != "kicad_pcb":
        raise ValueError("Invalid PCB root")
    shape, ppm = shape_of(config), config["px_per_mm"]
    actual = {f"{s}.{n}": np.zeros(shape, bool) for s, ls in expected.items() for n in ls}
    edges = []
    for node in root[1:]:
        if not isinstance(node, list):
            continue
        if str(node[0]) == "gr_poly":
            layer = attr(node, "layer")[0]
            pts = np.array([[float(p[1]),float(p[2])] for p in attr(node,"pts")]) * ppm
            if np.max(abs(pts-np.rint(pts))) > 1e-4:
                raise ValueError("Exported polygon drifted off the input grid")
            lo, hi = np.rint(pts.min(axis=0)).astype(int), np.rint(pts.max(axis=0)).astype(int)
            x0,y0 = lo; x1,y1 = hi
            if len(pts) != 4 or not (0 <= x0 < x1 <= shape[1] and 0 <= y0 < y1 <= shape[0]):
                raise ValueError("Invalid exported rectangle")
            actual[layer][y0:y1,x0:x1] = True
        elif str(node[0]) in ("gr_line", "gr_arc") and attr(node,"layer") == ["Edge.Cuts"]:
            keys = ("start", "end") if str(node[0]) == "gr_line" else ("start", "mid", "end")
            edges.append((str(node[0])[3:], *[tuple(map(float, attr(node,k))) for k in keys]))
    differences = {}
    for side, ls in expected.items():
        for n, mask in ls.items():
            wanted = np.fliplr(mask) if side == "B" else mask
            differences[f"{side}.{n}"] = int(np.count_nonzero(actual[f"{side}.{n}"] != wanted))
    b = config["board"]
    expected_edges = outline(b["width_mm"],b["height_mm"],b.get("radius_mm",0))
    expected_edges += [g for s in b.get("slots", []) for g in slot_outline(s)]
    if len(edges) != len(expected_edges) or any(a[0] != b[0] or not np.allclose(a[1:], b[1:], rtol=0, atol=1e-6) for a,b in zip(edges,expected_edges)):
        raise ValueError("Outline/cutout geometry mismatch")
    if any(differences.values()):
        raise ValueError(f"PCB raster mismatch {differences}")
    return {"parser": "sexpdata", "pixel_mismatch": differences, "analytic_edges_match": True,
            "native_drc": "not_run", "factory_CAM": "not_verified"}


def build(config_path, output):
    config_path, output = Path(config_path).resolve(), Path(output).resolve()
    config = read_json(config_path)
    shape = shape_of(config)
    # Refuse overwrite, including incomplete previous output.
    if output.exists():
        raise ValueError("Output directory already exists. Choose a new revision directory")
    output.mkdir(parents=True)
    root, ppm, rules = config_path.parent, config["px_per_mm"], config["rules"]
    safe_top, solid_top, rf_top = geometry_masks(config)
    record, result, reports, errors = {config_path.name: sha(config_path)}, {}, {}, []
    colors = rgb_colors(config.get("palette", PALETTE))
    for side, spec in config["faces"].items():
        if side not in ("F", "B"):
            raise ValueError("Face keys must be F and/or B")
        safe, solid, rf = [(np.fliplr(m) if side == "B" else m) for m in (safe_top, solid_top, rf_top)]
        original, labels, protected = prepare_face(spec, root, config, record)
        layers = compile_layers(labels, config, safe, rf)
        result[side] = layers
        Image.fromarray(labels).save(output/f"{side}_labels.png")
        Image.fromarray(colors[labels]).save(output/f"{side}_materials.png")
        save_mask(output/f"{side}_semantic_diff.png", original != labels)
        save_mask(output/f"{side}_protected.png", protected)
        rendered = np.zeros(shape, np.uint8)
        rendered[layers["Cu"]] = 1
        rendered[layers["Mask"]] = 3
        rendered[layers["SilkS"]] = 2
        changed = (rendered != labels) & solid
        if (changed & protected).any():
            errors.append(f"{side}: manufacturing constraints changed protected material pixels. Recompose or explicitly revise the protected ROI")
        save_mask(output/f"{side}_manufacturing_diff.png", changed)
        preview(layers, solid, colors).save(output/f"{side}_preview.png")
        preview(layers, solid, colors, True).save(output/f"{side}_low_contrast.png")
        margins = copper_margin(layers["Cu"], layers["Mask"], ppm)
        silk_gap = gap(layers["SilkS"], layers["Mask"], ppm)
        if margins is not None and margins+1e-8 < rules["gold_copper_margin_mm"]:
            errors.append(f"{side}: insufficient copper margin under gold openings ({margins:.4f} mm)")
        if silk_gap is not None and silk_gap+1e-8 < rules["silk_to_mask_mm"]:
            errors.append(f"{side}: silk too close to exposed gold ({silk_gap:.4f} mm)")
        features = {}
        for n, mask in layers.items():
            save_mask(output/f"{side}_{n}.png", mask)
            nominal = rules["silk_feature_mm"] if n == "SilkS" else rules["copper_feature_mm"]
            features[n], warnings = feature_report(mask, ppm, nominal, rules["island_area_mm2"])
            save_mask(output/f"{side}_{n}_fine_detail.png", warnings)
            if (mask & ~safe).any() or (n != "SilkS" and (mask & rf).any()):
                errors.append(f"{side}.{n}: forbidden geometry")
        reports[side] = {"semantic_changed_pixels": int((original!=labels).sum()),
                         "manufacturing_changed_pixels": int(changed.sum()),
                         "protected_manufacturing_changed_pixels": int((changed & protected).sum()),
                         "gold_clipped_pixels": int(((labels==3) & ~layers["Mask"]).sum()),
                         "copper_margin_mm": margins, "silk_to_opening_mm": silk_gap,
                         "feature_warnings": features}
    if not result:
        raise ValueError("At least one face required")
    counts = write_pcb(output/"artwork.kicad_pcb", result, config)
    readback = roundtrip_pcb(output/"artwork.kicad_pcb", result, config)
    write_json(output/"build_config.json", config)
    report = {"version": VERSION, "source_sha256": record, "pixel_pitch_mm": 1/ppm,
              "cell_center_boundary_uncertainty_mm": math.sqrt(2)/(2*ppm),
              "back_input_view": "normal_reading", "PCB_view": "common_top_B_mirrored_once",
              "outline": "analytic rounded rectangle with closed Edge.Cuts capsule cutout",
              "faces": reports, "PCB_readback": readback, "polygon_objects": counts,
              "errors": errors, "geometry_checks_passed": not errors,
              "visual_approval": "required", "RF_validation": "not_performed"}
    write_json(output/"report.json", report)
    write_json(output/"manifest.json", {p.name:sha(p) for p in sorted(output.iterdir()) if p.is_file()})
    return report


def export_gerber(build_dir, cli, output):
    build_dir, output = Path(build_dir).resolve(), Path(output).resolve()
    report = read_json(build_dir/"report.json")
    manifest = read_json(build_dir/"manifest.json")
    for name, digest in manifest.items():
        if sha(build_dir/name) != digest:
            raise ValueError(f"Build hash mismatch: {name}")
    if not report["geometry_checks_passed"]:
        raise ValueError("Fix report.json errors before export")
    if output.exists():
        raise ValueError("Gerber output directory already exists")
    output.mkdir(parents=True)
    version = subprocess.run([cli,"version"],check=True,capture_output=True,text=True).stdout.strip()
    pcb = str(build_dir/"artwork.kicad_pcb")
    drc = subprocess.run([cli,"pcb","drc","--severity-all","--exit-code-violations","--output",str(output/"drc.txt"),pcb],capture_output=True,text=True)
    (output/"drc_process.txt").write_text(drc.stdout+"\n"+drc.stderr,encoding="utf-8")
    if drc.returncode:
        raise ValueError("Native KiCad DRC failed. See drc.txt and drc_process.txt. No ZIP created")
    gerbers = output/"gerber"
    gerbers.mkdir()
    subprocess.run([cli,"pcb","export","gerbers","--layers","F.Cu,B.Cu,F.Mask,B.Mask,F.SilkS,B.SilkS,Edge.Cuts","--output",str(gerbers)+"/",pcb],check=True)
    # Slots are milled closed Edge.Cuts, not fake copper/mask or plated holes.
    files = sorted(p for p in gerbers.iterdir() if p.suffix.lower() in (".gtl",".gbl",".gts",".gbs",".gto",".gbo",".gm1"))
    if len(files) != 7:
        raise ValueError(f"Expected seven Gerber layers, found {len(files)}")
    write_json(output/"export_report.json", {"kicad_version":version,"source_PCB_sha256":sha(pcb),
        "native_DRC":"passed","Gerber_independent_geometry_readback":"not_run",
        "factory_CAM":"not_verified","physical_RF":"not_verified",
        "files_sha256":{p.name:sha(p) for p in files}})
    with zipfile.ZipFile(output/"Gerber.zip","w",zipfile.ZIP_DEFLATED) as z:
        for p in files:
            z.write(p,p.name)
    with zipfile.ZipFile(output/"Gerber.zip") as z:
        if z.testzip() is not None:
            raise ValueError("ZIP CRC failed")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--version",action="version",version=VERSION)
    sub = p.add_subparsers(dest="command", required=True)
    b = sub.add_parser("build",help="separate, protect, compile, audit and export artwork PCB")
    b.add_argument("config",type=Path)
    b.add_argument("--out",type=Path,required=True)
    g = sub.add_parser("gerber",help="run native DRC, export seven layers, hash and package")
    g.add_argument("build_dir",type=Path)
    g.add_argument("--kicad-cli",required=True)
    g.add_argument("--out",type=Path,required=True)
    args = p.parse_args()
    try:
        if args.command == "build":
            report = build(args.config,args.out)
            print(json.dumps({"geometry_checks_passed":report["geometry_checks_passed"],"errors":report["errors"]},ensure_ascii=False))
            return 0 if report["geometry_checks_passed"] else 2
        export_gerber(args.build_dir,args.kicad_cli,args.out)
        return 0
    except (ValueError,KeyError,OSError,subprocess.CalledProcessError) as e:
        print(str(e),file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
