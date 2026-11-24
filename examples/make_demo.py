"""Create an original synthetic two-face pass. No character/private assets needed."""
from pathlib import Path
import argparse
import json
import math
import sys

import numpy as np
from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"scripts"))
from pcb_art import write_json


def make_demo(out, ppm=40):
    out = Path(out)
    if out.exists():
        raise ValueError("Choose a new demo directory")
    out.mkdir(parents=True)
    def xy(points):
        return [(round(x*ppm),round(y*ppm)) for x,y in points]
    def whale(draw,cx,cy,scale,material):
        # Broad tapered body, two flukes, ventral fin and a visible negative eye.
        pts = [(-9,0),(-7,-3),(-2,-4),(3,-2),(7,1),(10,-2),(9,1),(11,3),(7,3),(3,2),(-1,3),(-6,2)]
        draw.polygon(xy([(cx+x*scale,cy+y*scale) for x,y in pts]),fill=material)
        draw.polygon(xy([(cx-2*scale,cy+2*scale),(cx+1*scale,cy+6*scale),(cx+2*scale,cy+2*scale)]),fill=material)
        ex,ey=cx-5*scale,cy-.7*scale
        draw.ellipse([round((ex-.35)*ppm),round((ey-.35)*ppm),round((ex+.35)*ppm),round((ey+.35)*ppm)],fill=2 if material!=2 else 0)
    for side in ("F","B"):
        im = Image.new("L",(50*ppm,100*ppm),0 if side=="B" else 2)
        d = ImageDraw.Draw(im)
        def line(points,fill,width):
            d.line(xy(points),fill=fill,width=max(1,round(width*ppm)),joint="curve")
        if side=="F":
            # Two coherent blue volumes with a restrained gold orbit.
            d.ellipse([round(x*ppm) for x in (4,10,46,53)],fill=1)
            d.ellipse([round(x*ppm) for x in (7,13,43,50)],fill=0)
            d.arc([round(x*ppm) for x in (2,8,48,55)],15,330,fill=3,width=round(.45*ppm))
            whale(d,25,30,1.4,2)
            for i in range(3):
                pts=[(3+x*.5,64+i*4+2*math.sin(x*.11+i*.4)) for x in range(89)]
                line(pts,1 if i%2==0 else 0,.7)
            d.ellipse([round(x*ppm) for x in (34,47,37,50)],fill=3)
        else:
            # Sparse architectural layers. Foreground separated by white/gold,
            # rather than relying on dark/light blue contrast alone.
            for x in (4,18,32,46):
                line([(x,64),(x,21)],1,.8)
            for x in (4,18,32):
                d.arc([round(v*ppm) for v in (x,10,x+14,38)],180,360,fill=3,width=round(.4*ppm))
            whale(d,26,34,1.35,2)
            for i in range(3):
                line([(20,57+i*5),(28,55+i*5),(39,59+i*5),(46,57+i*5)],1,.5)
            # Physical top keepout [32,70,49,91] becomes normal-back left.
            # White decorative linework survives here while art Cu stays absent.
            whale(d,8,82,.5,2)
        font = ImageFont.load_default(size=round(3*ppm))
        title = "TIDAL LETTERS" if side=="F" else "RESONANCE"
        d.text((25*ppm,94*ppm),title,fill=3,font=font,anchor="mm")
        im.save(out/f"{side}_labels.png")
    config = {"schema_version":1,"px_per_mm":ppm,
        "board":{"width_mm":50,"height_mm":100,"radius_mm":3,"thickness_mm":1.6,
                 "slots":[{"center_mm":[25,4.5],"width_mm":18,"height_mm":3}]},
        "palette":["#092b61","#3477ab","#faf8ee","#be9c43"],
        "rules":{"edge_clearance_mm":.5,"gold_copper_margin_mm":.15,"silk_to_mask_mm":.15,
                 "copper_feature_mm":.15,"silk_feature_mm":.18,"island_area_mm2":.02},
        "nfc_keepouts_mm":[[32,70,49,91]],
        "faces":{"F":{"labels":"F_labels.png"},"B":{"labels":"B_labels.png"}}}
    write_json(out/"project.json",config)
    # Separate solver sanity example, a closed uniform square loop. It is not
    # the pass's NFC antenna and makes no claim of NFC functionality.
    write_json(out/"loop_baseline.json",{"points_mm":[[0,0,0],[14,0,0],[14,18,0],[0,18,0],[0,0,0]],
        "trace_width_mm":.18,"copper_thickness_mm":.035,"resistive_length_mm":64,
        "chip_capacitance_pF":28.5,"target_MHz":13.56,"steps_mm":[.1,.05,.025],
        "trial_capacitors_pF":[27,33,36,39]})
    return out/"project.json"


if __name__=="__main__":
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--out",type=Path,required=True)
    p.add_argument("--px-per-mm",type=int,default=40)
    a=p.parse_args()
    make_demo(a.out,a.px_per_mm)
