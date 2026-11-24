"""Optional native integration. Set KICAD_CLI and KICAD_PYTHON to run."""
from pathlib import Path
import os
import subprocess
import sys
import tempfile
import unittest

import numpy as np
from PIL import Image

from test_pipeline import art,config,ROOT


@unittest.skipUnless(os.environ.get("KICAD_CLI") and os.environ.get("KICAD_PYTHON"),"native KiCad paths not provided")
class NativeTests(unittest.TestCase):
    def test_native_DRC_export_and_NFC_merge(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); c=config()
            labels=np.zeros(art.shape_of(c),np.uint8)
            labels[160:200,80:160]=3
            for side in ("F","B"):
                Image.fromarray(labels).save(root/f"{side}.png")
            art.write_json(root/"project.json",c)
            art.build(root/"project.json",root/"build")
            art.export_gerber(root/"build",os.environ["KICAD_CLI"],root/"export")
            self.assertTrue((root/"export/Gerber.zip").is_file())
            py=os.environ["KICAD_PYTHON"]
            fixture=subprocess.run([py,str(ROOT/"tests/native_fixture.py"),str(root/"build/artwork.kicad_pcb"),str(root/"circuit.kicad_pcb")],capture_output=True,text=True)
            self.assertEqual(fixture.returncode,0,(fixture.stdout+fixture.stderr)[-4000:])
            result=subprocess.run([py,str(ROOT/"scripts/merge_nfc.py"),str(root/"build"),str(root/"circuit.kicad_pcb"),"--out",str(root/"merged")],capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
            r=art.read_json(root/"merged/merge_report.json")
            self.assertTrue(r["electrical_geometry_unchanged"])
            # A white region aligned to the back-side pad must be refused.
            # Common-top x=9 -> normal-back x=3 on a 12 mm board.
            bad=labels.copy()
            bad[296:304,56:64]=2
            Image.fromarray(bad).save(root/"B.png")
            art.build(root/"project.json",root/"bad-silk-build")
            collision=subprocess.run([py,str(ROOT/"scripts/merge_nfc.py"),str(root/"bad-silk-build"),str(root/"circuit.kicad_pcb"),"--out",str(root/"bad-silk-merged")],capture_output=True,text=True)
            self.assertNotEqual(collision.returncode,0)
            self.assertIn("functional pad",collision.stderr)
            again=subprocess.run([py,str(ROOT/"scripts/merge_nfc.py"),str(root/"build"),str(root/"merged/circuit.kicad_pcb"),"--out",str(root/"twice")],capture_output=True,text=True)
            self.assertNotEqual(again.returncode,0)
            self.assertTrue("Functional copper" in again.stderr or "already present" in again.stderr,again.stderr[-4000:])
            # Input manifest mismatch blocks Gerber packaging as well.
            with (root/"build/artwork.kicad_pcb").open("a") as f:
                f.write("\n")
            with self.assertRaises(ValueError):
                art.export_gerber(root/"build",os.environ["KICAD_CLI"],root/"changed")


if __name__=="__main__":
    unittest.main()
