"""Add art to a native NFC circuit using the matching KiCad pcbnew Python.

The circuit is authoritative for nets, pads, routes, mask and holes. This script
adds only artwork drawings, keeps electrical objects unchanged, and refuses any
art overlap with the designated two-face RF keepout. Run native DRC afterwards.
"""
from pathlib import Path
import argparse
import hashlib
import json
import math
import shutil
import sys

try:
    import pcbnew as pcb
except ImportError:
    raise SystemExit("Run with KiCad's matching Python interpreter (pcbnew is required)")


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_json(path,data):
    Path(path).write_text(json.dumps(data,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")


def point(p):
    return [p.x,p.y]


def shape_snapshot(item):
    if not isinstance(item,pcb.PCB_SHAPE):
        raise ValueError("Functional copper drawing must be a PCB_SHAPE, review copper text separately")
    data={"layer":item.GetLayer(),"shape":int(item.GetShape()),"width":item.GetWidth(),
          "fill":int(item.GetFillMode()),"start":point(item.GetStart()),"end":point(item.GetEnd())}
    if item.GetShape()==pcb.SHAPE_T_ARC:
        data["mid"]=point(item.GetArcMid())
    elif item.GetShape()==pcb.SHAPE_T_POLY:
        poly=item.GetPolyShape()
        def points(chain):
            return [point(chain.CPoint(i)) for i in range(chain.PointCount())]
        data["outlines"]=[points(poly.COutline(i)) for i in range(poly.OutlineCount())]
        data["holes"]=[[points(poly.CHole(i,j)) for j in range(poly.HoleCount(i))] for i in range(poly.OutlineCount())]
    return data


def electrical_snapshot(board,original_graphic_ids=None):
    pads = sorted((f.GetReference(),p.GetNumber(),p.GetNetname(),p.GetLayerSet().FmtBin(),
        point(p.GetPosition()),point(p.GetSize()),point(p.GetDrillSize()),p.GetOrientationDegrees())
        for f in board.GetFootprints() for p in f.Pads())
    routes = sorted((t.m_Uuid.AsString(),t.GetNetname(),t.GetLayer(),point(t.GetStart()),point(t.GetEnd()),t.GetWidth(),
        t.GetDrillValue() if isinstance(t,pcb.PCB_VIA) else None,
        point(t.GetMid()) if isinstance(t,pcb.PCB_ARC) else None) for t in board.GetTracks())
    graphics=[d for d in board.GetDrawings() if d.GetLayer() in (pcb.F_Cu,pcb.B_Cu)]
    graphics += [d for f in board.GetFootprints() for d in f.GraphicalItems() if d.GetLayer() in (pcb.F_Cu,pcb.B_Cu)]
    copper={d.m_Uuid.AsString():shape_snapshot(d) for d in graphics if original_graphic_ids is None or d.m_Uuid.AsString() in original_graphic_ids}
    return {"pads":pads,"routes":routes,"footprints":sorted(f.GetReference() for f in board.GetFootprints()),
            "functional_copper_graphics":copper,"zones":board.GetAreaCount()}


def edges(board):
    result=[]
    for d in board.GetDrawings():
        if d.GetLayer()!=pcb.Edge_Cuts:
            continue
        row=[int(d.GetShape()),point(d.GetStart()),point(d.GetEnd())]
        if d.GetShape()==pcb.SHAPE_T_ARC:
            row.append(point(d.GetArcMid()))
        result.append(row)
    return sorted(result)


def bbox(item,inflate=0):
    b=item.GetBoundingBox()
    return [b.GetLeft()-inflate,b.GetTop()-inflate,b.GetRight()+inflate,b.GetBottom()+inflate]


def intersects(a,b):
    return not (a[2]<=b[0] or a[0]>=b[2] or a[3]<=b[1] or a[1]>=b[3])


def merge(build_dir,circuit_path,output_dir):
    build_dir,circuit_path,output_dir=map(lambda p:Path(p).resolve(),(build_dir,circuit_path,output_dir))
    if output_dir.exists():
        raise ValueError("Output directory exists. Choose a new circuit revision")
    report=json.loads((build_dir/"report.json").read_text(encoding="utf-8"))
    manifest=json.loads((build_dir/"manifest.json").read_text(encoding="utf-8"))
    for name,digest in manifest.items():
        if sha(build_dir/name)!=digest:
            raise ValueError("Artwork build hash changed: "+name)
    if not report["geometry_checks_passed"]:
        raise ValueError("Artwork geometry checks failed")
    config=json.loads((build_dir/"build_config.json").read_text(encoding="utf-8"))
    keepouts=[[pcb.FromMM(v) for v in box] for box in config.get("nfc_keepouts_mm",[])]
    if not keepouts:
        raise ValueError("At least one explicit common-top-view NFC keepout required")
    circuit=pcb.LoadBoard(str(circuit_path))
    art=pcb.LoadBoard(str(build_dir/"artwork.kicad_pcb"))
    if edges(circuit)!=edges(art):
        raise ValueError("Circuit/art outlines differ. Align board origin, profile and slot before merging")
    if circuit.GetAreaCount():
        raise ValueError("Remove/review all copper zones in the circuit template before art merging")
    if list(art.GetFootprints()) or list(art.GetTracks()):
        raise ValueError("Artwork input must contain drawings only")
    before=electrical_snapshot(circuit)
    functional=list(circuit.GetTracks())
    functional += [p for f in circuit.GetFootprints() for p in f.Pads() if p.IsOnLayer(pcb.F_Cu) or p.IsOnLayer(pcb.B_Cu)]
    functional += [d for d in circuit.GetDrawings() if d.GetLayer() in (pcb.F_Cu,pcb.B_Cu)]
    functional += [d for f in circuit.GetFootprints() for d in f.GraphicalItems() if d.GetLayer() in (pcb.F_Cu,pcb.B_Cu)]
    clearance=pcb.FromMM(config["rules"].get("nfc_copper_clearance_mm",.5))
    for item in functional:
        b=bbox(item,clearance)
        if not any(k[0]<=b[0] and k[1]<=b[1] and k[2]>=b[2] and k[3]>=b[3] for k in keepouts):
            raise ValueError("Functional copper plus requested clearance lies outside the NFC keepout. Use a circuit-only template and enlarge/recompose the keepout")
    exposed=[]
    for f in circuit.GetFootprints():
        for pad in f.Pads():
            for copper,mask,silk in ((pcb.F_Cu,pcb.F_Mask,pcb.F_SilkS),(pcb.B_Cu,pcb.B_Mask,pcb.B_SilkS)):
                if pad.IsOnLayer(copper) and pad.IsOnLayer(mask):
                    try:
                        margin=max(0,pad.GetSolderMaskExpansion(mask))
                    except TypeError:
                        margin=max(0,pad.GetSolderMaskExpansion())
                    exposed.append((silk,bbox(pad,margin+pcb.FromMM(config["rules"]["silk_to_mask_mm"]))))
    count=0
    drawings=list(art.GetDrawings())
    for d in drawings:
        if d.GetLayer()==pcb.Edge_Cuts:
            continue
        if d.GetLayer() in (pcb.F_Cu,pcb.B_Cu,pcb.F_Mask,pcb.B_Mask):
            if any(intersects(bbox(d),k) for k in keepouts):
                raise ValueError("Art copper/opening touches the RF keepout")
        if d.GetLayer() in (pcb.F_SilkS,pcb.B_SilkS):
            if any(layer==d.GetLayer() and intersects(bbox(d),b) for layer,b in exposed):
                raise ValueError("White art approaches a functional pad opening. Recompose the explicit ROI")
    existing={d.m_Uuid.AsString() for d in circuit.GetDrawings()}
    for d in drawings:
        if d.GetLayer()!=pcb.Edge_Cuts:
            if d.m_Uuid.AsString() in existing:
                raise ValueError("Artwork already present or UUID collision")
            circuit.Add(d.Duplicate())
            count+=1
    if electrical_snapshot(circuit,set(before["functional_copper_graphics"]))!=before:
        raise ValueError("Electrical geometry changed during merge")
    output_dir.mkdir(parents=True)
    output=output_dir/circuit_path.name
    pcb.SaveBoard(str(output),circuit)
    reopened=pcb.LoadBoard(str(output))
    if electrical_snapshot(reopened,set(before["functional_copper_graphics"]))!=before or edges(reopened)!=edges(art):
        raise ValueError("Saved circuit geometry/parity mismatch")
    # Preserve explicit project DRC rules, without guessing schematic hierarchy.
    for suffix in (".kicad_pro",".kicad_dru"):
        p=circuit_path.with_suffix(suffix)
        if p.exists():
            shutil.copy2(p,output.with_suffix(suffix))
    write_json(output_dir/"merge_report.json",{"kicad_version":pcb.Version(),"source_circuit_sha256":sha(circuit_path),
        "art_PCB_sha256":sha(build_dir/"artwork.kicad_pcb"),"output_PCB_sha256":sha(output),
        "art_drawings_added":count,"electrical_geometry_unchanged":True,"profile_and_cutout_match":True,
        "pad_silk_clearance":"conservative bounding-box screening passed",
        "native_DRC":"required","schematic_parity":"required with original full schematic hierarchy",
        "RF_validation":"not_performed","drill_export":"required for circuit vias/holes"})
    return output


if __name__=="__main__":
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("build_dir",type=Path)
    p.add_argument("circuit",type=Path)
    p.add_argument("--out",type=Path,required=True)
    a=p.parse_args()
    try:
        print(merge(a.build_dir,a.circuit,a.out))
    except (ValueError,OSError) as e:
        print(str(e),file=sys.stderr)
        raise SystemExit(2)
