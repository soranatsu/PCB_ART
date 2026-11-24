"""KiCad-Python-only synthetic electrical fixture for merge regression tests."""
from pathlib import Path
import sys
import pcbnew as pcb

art_path,target=map(Path,sys.argv[1:3])
board=pcb.LoadBoard(str(art_path))
for d in list(board.GetDrawings()):
    if d.GetLayer()!=pcb.Edge_Cuts:
        board.Remove(d)
net=pcb.NETINFO_ITEM(board,"NFC_TEST")
board.Add(net)
fp=pcb.FOOTPRINT(board)
fp.SetReference("J1")
fp.SetPosition(pcb.VECTOR2I(pcb.FromMM(9),pcb.FromMM(15)))
board.Add(fp)
for i,y in enumerate((15.,16.2),1):
    p=pcb.PAD(fp)
    p.SetNumber(str(i))
    p.SetAttribute(pcb.PAD_ATTRIB_SMD)
    p.SetShape(pcb.PAD_SHAPE_RECT)
    p.SetSize(pcb.VECTOR2I(pcb.FromMM(.6),pcb.FromMM(.6)))
    layers=pcb.LSET()
    layers.AddLayer(pcb.B_Cu)
    layers.AddLayer(pcb.B_Mask)
    p.SetLayerSet(layers)
    p.SetPosition(pcb.VECTOR2I(pcb.FromMM(9),pcb.FromMM(y)))
    p.SetNet(net)
    fp.Add(p)
t=pcb.PCB_TRACK(board)
t.SetStart(pcb.VECTOR2I(pcb.FromMM(9),pcb.FromMM(15)))
t.SetEnd(pcb.VECTOR2I(pcb.FromMM(9),pcb.FromMM(16.2)))
t.SetWidth(pcb.FromMM(.2))
t.SetLayer(pcb.B_Cu)
t.SetNet(net)
board.Add(t)
coil=pcb.PCB_SHAPE(fp)
coil.SetShape(pcb.SHAPE_T_SEGMENT)
coil.SetStart(pcb.VECTOR2I(pcb.FromMM(8.8),pcb.FromMM(15.6)))
coil.SetEnd(pcb.VECTOR2I(pcb.FromMM(9.2),pcb.FromMM(15.6)))
coil.SetWidth(pcb.FromMM(.18))
coil.SetLayer(pcb.B_Cu)
fp.Add(coil)
pcb.SaveBoard(str(target),board)
