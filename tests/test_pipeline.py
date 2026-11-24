from pathlib import Path
import math
import sys
import tempfile
import unittest

import numpy as np
from PIL import Image

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"scripts"))
import pcb_art as art
import nfc_analyze as nfc


def config():
    return {"px_per_mm":20,"board":{"width_mm":12,"height_mm":20,"radius_mm":1,
            "slots":[{"center_mm":[6,2],"width_mm":4,"height_mm":1}]},
        "rules":{"edge_clearance_mm":.3,"gold_copper_margin_mm":.15,"silk_to_mask_mm":.15,
                 "copper_feature_mm":.15,"silk_feature_mm":.18,"island_area_mm2":.02},
        "nfc_keepouts_mm":[[8,14,11,18]],"faces":{"F":{"labels":"F.png"},"B":{"labels":"B.png"}}}


class ArtworkTests(unittest.TestCase):
    def test_rectangles_preserve_holes_and_diagonals(self):
        rng=np.random.default_rng(164)
        for mask in [rng.random((53,37))<.4,np.eye(15,dtype=bool),np.zeros((5,4),bool),np.ones((4,7),bool)]:
            recovered=np.zeros_like(mask)
            for x0,y0,x1,y1 in art.rectangles(mask):
                recovered[y0:y1,x0:x1]=True
            np.testing.assert_array_equal(mask,recovered)

    def test_palette_identity(self):
        rgb=art.rgb_colors(art.PALETTE)
        np.testing.assert_array_equal(art.separate(rgb[None,:,:],rgb),[[0,1,2,3]])

    def test_RGB_master_and_input_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); c=config()
            labels=np.zeros(art.shape_of(c),np.uint8)
            labels[100:120,50:80]=2; labels[160:170,60:120]=3
            Image.fromarray(art.rgb_colors(art.PALETTE)[labels]).save(root/"rgb.png")
            original,after,_=art.prepare_face({"image":"rgb.png"},root,c,{})
            np.testing.assert_array_equal(after,labels)
            with self.assertRaises(ValueError):
                art.prepare_face({"image":"rgb.png","labels":"rgb.png"},root,c,{})
            Image.new("RGBA",labels.shape[::-1],(255,255,255,0)).save(root/"transparent.png")
            with self.assertRaises(ValueError):
                art.prepare_face({"image":"transparent.png"},root,c,{})

    def test_exact_square_gap_and_margin(self):
        a=np.zeros((30,30),bool); b=a.copy()
        a[3:8,3:8]=True; b[3:8,10:15]=True
        self.assertAlmostEqual(art.gap(a,b,10),.2)
        b[3:8,8:12]=True
        self.assertEqual(art.gap(a,b,10),0)
        copper=np.zeros((30,30),bool); copper[3:23,3:23]=True
        opening=np.zeros_like(copper); opening[5:21,5:21]=True
        self.assertAlmostEqual(art.copper_margin(copper,opening,10),.2)
        copper[10,10]=False
        self.assertEqual(art.copper_margin(copper,opening,10),-1)

    def test_slot_is_transparent_and_keepout_mirrors_once(self):
        c=config(); safe,solid,rf=art.geometry_masks(c)
        self.assertFalse(solid[40,120]); self.assertFalse(safe[40,120])
        self.assertTrue(solid[100,120])
        labels=np.ones(art.shape_of(c),np.uint8)
        for side in ("F","B"):
            s,k=(safe,rf) if side=="F" else (np.fliplr(safe),np.fliplr(rf))
            layers=art.compile_layers(labels,c,s,k)
            common=layers["Cu"] if side=="F" else np.fliplr(layers["Cu"])
            self.assertFalse((common & rf).any())
        layers=art.compile_layers(np.full_like(labels,2),c,safe,rf)
        self.assertTrue(layers["SilkS"][320,190])

    def test_cleanup_preserves_eyes_mouth_and_outside_roi(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); c=config(); shape=art.shape_of(c)
            labels=np.zeros(shape,np.uint8)
            labels[100,60]=3; labels[100,120]=3; labels[100,180]=3
            Image.fromarray(labels).save(root/"L.png")
            protected=np.zeros(shape,bool); protected[99:102,59:62]=True
            art.save_mask(root/"protected.png",protected)
            spec={"labels":"L.png","protect":[{"mask":"protected.png"}],"cleanup":[{
                "polygon_mm":[[1,4],[8,4],[8,6],[1,6]],"material":"gold","replace_with":"dark","below_area_mm2":.02}]}
            original,actual,_=art.prepare_face(spec,root,c,{})
            self.assertEqual(actual[100,60],3)
            self.assertEqual(actual[100,120],0)
            self.assertEqual(actual[100,180],3)
            self.assertEqual(np.count_nonzero(original!=actual),1)

    def test_explicit_override_restores_skin_triangle(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); c=config(); labels=np.ones(art.shape_of(c),np.uint8)
            Image.fromarray(labels).save(root/"L.png")
            roi={"polygon_mm":[[3,5],[4,5],[3.5,4]]}
            spec={"labels":"L.png","overrides":[dict(roi,material="white")]}
            before,after,_=art.prepare_face(spec,root,c,{})
            mask=art.region(roi,root,labels.shape,20,{})
            np.testing.assert_array_equal(after[~mask],before[~mask])
            self.assertTrue((after[mask]==2).all())

    def test_full_build_readback_and_fail_closed_on_corruption(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); c=config(); labels=np.zeros(art.shape_of(c),np.uint8)
            labels[80:200,60:140]=2; labels[95:115,80:100]=0  # negative eye/mouth hole
            labels[230:240,70:150]=3
            for s in ("F","B"):
                Image.fromarray(labels if s=="F" else np.flipud(labels)).save(root/f"{s}.png")
            art.write_json(root/"project.json",c)
            report=art.build(root/"project.json",root/"build")
            self.assertTrue(report["geometry_checks_passed"],report["errors"])
            self.assertTrue(report["PCB_readback"]["analytic_edges_match"])
            self.assertTrue(all(v==0 for v in report["PCB_readback"]["pixel_mismatch"].values()))
            with self.assertRaises(ValueError):
                art.build(root/"project.json",root/"build")
            pcb=root/"build/artwork.kicad_pcb"
            pcb.write_text(pcb.read_text().replace('(xy 3 ', '(xy 3.025 ',1))
            expected={s:{n:art.binary(root/f"build/{s}_{n}.png",labels.shape) for n in art.LAYER_NAMES} for s in ("F","B")}
            with self.assertRaises(ValueError):
                art.roundtrip_pcb(pcb,expected,c)

    def test_gold_at_boundary_reports_insufficient_margin(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); c=config(); c["faces"]={"F":{"labels":"F.png"}}
            Image.fromarray(np.full(art.shape_of(c),3,np.uint8)).save(root/"F.png")
            art.write_json(root/"project.json",c)
            r=art.build(root/"project.json",root/"build")
            self.assertFalse(r["geometry_checks_passed"])

    def test_dimension_and_polarity_rejected(self):
        with self.assertRaises(ValueError):
            c=config(); c["board"]["width_mm"]=12.001; art.shape_of(c)
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/"bad.png"; Image.new("L",(4,4),128).save(p)
            with self.assertRaises(ValueError): art.binary(p,(4,4))


class NFCTests(unittest.TestCase):
    def test_arc_primitive_closure_matches_polyline_reference(self):
        elems=[]
        for i in range(4):
            pts=[[10*math.cos(a),10*math.sin(a),0] for a in (i*math.pi/2,(i+.5)*math.pi/2,(i+1)*math.pi/2)]
            elems.append({"type":"arc","points_mm":pts})
        path=nfc.sample_elements(elems,.02)
        self.assertLess(np.linalg.norm(path[0]-path[-1]),1e-12)
        self.assertAlmostEqual(np.linalg.norm(np.diff(path,axis=0),axis=1).sum(),.02*math.pi,places=7)
        theory=4*math.pi*1e-7*.01*(math.log(8*.01/.00005)-2)
        self.assertLess(abs(nfc.neumann(path,.00005)/theory-1),.001)
        elems[1]["points_mm"][0]=[99,0,0]
        with self.assertRaises(ValueError): nfc.sample_elements(elems,.1)

    def test_tuning_reciprocal_units(self):
        L,chip,cp,f=2.12,28.5,0,13.56
        cap=nfc.target_capacitance(L,f,chip,cp)
        self.assertAlmostEqual(nfc.resonance(L,cap,chip,cp),f,places=12)
        self.assertAlmostEqual(nfc.retune(27,28.5,0,nfc.resonance(L,27,28.5,0),f),cap,places=10)
        self.assertAlmostEqual(nfc.target_capacitance(L,f,chip,3),cap-3)

    def test_circular_integral_reference_and_convergence(self):
        R,a=.01,.00005
        theory=4*math.pi*1e-7*R*(math.log(8*R/a)-2)
        results=[]
        for n in (1500,3000):
            t=np.linspace(0,2*math.pi,n+1)
            path=np.column_stack([R*np.cos(t),R*np.sin(t),np.zeros_like(t)])
            results.append(nfc.neumann(path,a))
        self.assertLess(abs(results[-1]/theory-1),.001)
        self.assertLess(abs(results[-1]-theory),abs(results[0]-theory))

    def test_gmd_scaling_and_rotation_invariance(self):
        g=nfc.gmd_rectangle(.00018,.000035)
        self.assertAlmostEqual(g*1000,.048092,places=5)
        self.assertAlmostEqual(nfc.gmd_rectangle(.00036,.00007)/g,2,places=10)
        p=nfc.discretize([[0,0,0],[4,0,0],[4,5,0],[0,5,0],[0,0,0]],.05)
        self.assertAlmostEqual(nfc.neumann(p,g),nfc.neumann(p[:,[1,0,2]]+1,g),places=14)

    def test_open_path_nonfinite_and_negative_cap_rejected(self):
        for points in ([[0,0,0],[1,0,0],[1,1,0],[0,1,0]],[[0,0,0],[1,0,0],[math.nan,0,0],[0,0,0]]):
            with self.assertRaises(ValueError): nfc.discretize(points,.1)
        with self.assertRaises(ValueError): nfc.resonance(2.12,-1,28.5,0)
        with self.assertRaises(ValueError): nfc.retune(27,28.5,-1,14,13.56)


if __name__=="__main__":
    unittest.main()
