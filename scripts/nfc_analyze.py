"""NFC quasi-static baseline and measured-frequency tuning, with explicit units.

Regularized Neumann integral over a CLOSED 3-D current contour. Not a FEM/BEM
solution. Cp=0 and absent external metal are the baseline, never fitted by guess.
"""
from __future__ import annotations

import argparse
import math
from pathlib import Path
import sys

import numpy as np
from numpy.polynomial.legendre import leggauss

from pcb_art import positive, read_json, sha, write_json


def gmd_rectangle(width_m, thickness_m, order=96):
    positive(width_m, "width_m")
    positive(thickness_m, "thickness_m")
    nodes, weights = leggauss(order)
    v, weights = (nodes+1)/2, weights/2
    distance = np.hypot(width_m*v[:,None], thickness_m*v[None,:])
    integrand = 4*(1-v[:,None])*(1-v[None,:])*np.log(distance)
    return float(np.exp(np.sum(integrand*weights[:,None]*weights[None,:])))


def discretize(points_mm, max_step_mm):
    points = np.asarray(points_mm, dtype=float)
    if points.ndim != 2 or points.shape[1] != 3 or len(points) < 4 or not np.isfinite(points).all():
        raise ValueError("points_mm must contain >=4 finite [x,y,z] vertices")
    if not np.allclose(points[0],points[-1],rtol=0,atol=1e-8):
        raise ValueError("Provide an explicit CLOSED contour including leads, vias and ideal component closure")
    positive(max_step_mm, "max_step_mm")
    lengths = np.linalg.norm(np.diff(points,axis=0),axis=1)
    if np.any(lengths < 1e-10):
        raise ValueError("Zero length consecutive segment")
    counts = np.ceil(lengths/max_step_mm).astype(int)
    if counts.sum() > 30_000:
        raise ValueError("More than 30,000 segments. Reduce contour extent or use a larger step")
    pieces = [np.linspace(a,b,n+1)[:-1] for a,b,n in zip(points,points[1:],counts)]
    return np.vstack([*pieces,points[-1:]]) * 1e-3


def neumann(path_m, gmd_m, block=96):
    dl = np.diff(path_m,axis=0)
    mid = (path_m[1:]+path_m[:-1])/2
    total = 0.
    for start in range(0,len(dl),block):
        r = mid[start:start+block,None,:]-mid[None,:,:]
        distance = np.sqrt(np.einsum("ijk,ijk->ij",r,r)+gmd_m*gmd_m)
        dot = np.einsum("ik,jk->ij",dl[start:start+block],dl)
        total += float((dot/distance).sum())
    return 1e-7 * total


def sample_elements(elements, step_mm):
    """Sample connected line/XY-arc primitives without changing arc radius."""
    positive(step_mm,"step_mm")
    pieces=[]
    for e in elements:
        p=np.asarray(e["points_mm"],float)
        if p.ndim!=2 or p.shape[1]!=3 or not np.isfinite(p).all():
            raise ValueError("Primitive points must be finite [x,y,z]")
        if e["type"]=="line" and len(p)==2:
            length=np.linalg.norm(p[1]-p[0])
            if length<=0:
                raise ValueError("Zero-length line")
            n=math.ceil(length/step_mm)
            q=np.linspace(p[0],p[1],n+1)
        elif e["type"]=="arc" and len(p)==3:
            if np.ptp(p[:,2])>1e-8:
                raise ValueError("Arc must be planar XY; use straight z segments for vias")
            a,m,b=p[:,:2]
            try:
                center=np.linalg.solve(2*np.array([m-a,b-a]),np.array([m@m-a@a,b@b-a@a]))
            except np.linalg.LinAlgError as e:
                raise ValueError("Arc points are collinear or degenerate") from e
            r=np.linalg.norm(a-center)
            angles=np.arctan2(p[:,1]-center[1],p[:,0]-center[0])
            delta=(angles[2]-angles[0])%(2*math.pi)
            if (angles[1]-angles[0])%(2*math.pi)>delta+1e-10:
                delta-=2*math.pi
            theta=np.linspace(angles[0],angles[0]+delta,math.ceil(abs(delta)*r/step_mm)+1)
            q=np.column_stack((center[0]+r*np.cos(theta),center[1]+r*np.sin(theta),np.full(len(theta),p[0,2])))
            q[0],q[-1]=p[0],p[-1]
        else:
            raise ValueError("Primitive needs type=line with 2 points or type=arc with start/mid/end")
        if pieces and not np.allclose(pieces[-1][-1],q[0],rtol=0,atol=1e-8):
            raise ValueError("Disconnected primitive path")
        pieces.append(q if not pieces else q[1:])
        if sum(map(len,pieces))>30_001:
            raise ValueError("More than 30,000 integration segments")
    if not pieces:
        raise ValueError("Empty primitive path")
    path=np.concatenate(pieces)
    if not np.allclose(path[0],path[-1],rtol=0,atol=1e-8):
        raise ValueError("Primitive path must explicitly close through the component and return")
    return path*1e-3


def target_capacitance(L_uH, f_MHz, chip_pF, board_pF):
    positive(L_uH,"L_uH")
    positive(f_MHz,"f_MHz")
    positive(chip_pF,"chip_pF",zero=True)
    positive(board_pF,"board_pF",zero=True)
    return 1e12 / ((2*math.pi*f_MHz*1e6)**2*L_uH*1e-6) - chip_pF - board_pF


def resonance(L_uH, external_pF, chip_pF, board_pF):
    positive(external_pF,"external_pF",zero=True)
    total = external_pF+chip_pF+board_pF
    positive(total,"total capacitance")
    positive(L_uH,"L_uH")
    return 1/(2*math.pi*math.sqrt(L_uH*1e-6*total*1e-12))/1e6


def retune(old_pF, chip_pF, board_pF, measured_MHz, target_MHz):
    for name, v in (("old_pF",old_pF),("chip_pF",chip_pF),("board_pF",board_pF)):
        positive(v,name,zero=True)
    positive(measured_MHz,"measured_MHz")
    positive(target_MHz,"target_MHz")
    return (old_pF+chip_pF+board_pF)*(measured_MHz/target_MHz)**2-chip_pF-board_pF


def baseline(spec):
    w = positive(spec["trace_width_mm"],"trace_width_mm")*1e-3
    t = positive(spec["copper_thickness_mm"],"copper_thickness_mm")*1e-3
    chip = positive(spec["chip_capacitance_pF"],"chip_capacitance_pF",zero=True)
    freq = positive(spec.get("target_MHz",13.56),"target_MHz")
    steps = spec.get("steps_mm",[.1,.05,.025])
    for step in steps:
        positive(step,"steps_mm")
    if len(steps) < 2 or any(a<=b for a,b in zip(steps,steps[1:])):
        raise ValueError("At least two strictly decreasing integration steps required")
    gmd = gmd_rectangle(w,t)
    rows = []
    for step in steps:
        if ("elements" in spec)==("points_mm" in spec):
            raise ValueError("Specify exactly one of elements or points_mm")
        path = sample_elements(spec["elements"],step) if "elements" in spec else discretize(spec["points_mm"],step)
        L = neumann(path,gmd)*1e6
        if L <= 0:
            raise ValueError("Non-positive inductance. Check contour and integration")
        rows.append({"step_mm":step,"segments":len(path)-1,"L_uH":L,
                     "target_external_pF":target_capacitance(L,freq,chip,0)})
    L = rows[-1]["L_uH"]
    # The ideal closure through a chip/capacitor is not copper. Resistive length
    # is explicitly provided instead of silently treating that closure as copper.
    length = positive(spec["resistive_length_mm"],"resistive_length_mm")*1e-3
    rho = positive(spec.get("resistivity_ohm_m",1.724e-8),"resistivity_ohm_m")
    Rdc = rho*length/(w*t)
    caps = spec.get("trial_capacitors_pF",[27,33,36,39])
    return {"method":"regularized midpoint Neumann integral with rectangular uniform-current GMD",
        "assumptions":{"board_parasitic_capacitance_pF":0,"external_art_copper":False,
            "skin_and_proximity_current_distribution":False,"chip_loading":False,
            "trace_cross_section":"uniform width/thickness, via/lead cross-sections approximated",
            "chip_capacitance_pF":chip,"target_MHz":freq},
        "gmd_mm":gmd*1e3,"convergence":rows,
        "last_step_delta_nH":abs(rows[-1]["L_uH"]-rows[-2]["L_uH"])*1000,
        "convergence_meaning":"Discretization sensitivity within this regularized model, not physical accuracy",
        "Rdc_ohm":Rdc,"Q_if_DC_resistance_only":2*math.pi*freq*L/Rdc,
        "Q_meaning":"DC-only reference. AC loss and loaded Q require additional models or measurements",
        "trial_resonances":[{"external_pF":c,"f_MHz":resonance(L,c,chip,0)} for c in caps],
        "negative_external_capacitance_means":"parallel capacitor cannot achieve this target at specified L and chip capacitance",
        "physical_RF_verified":False,"Cp_extracted":False,"scan_probability":None,"absolute_signal_dB":None}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="command",required=True)
    b = sub.add_parser("baseline")
    b.add_argument("geometry",type=Path)
    b.add_argument("--out",type=Path,required=True)
    t = sub.add_parser("retune")
    for name in ("old-pF","chip-pF","board-pF","measured-MHz","target-MHz"):
        t.add_argument("--"+name,type=float,required=True)
    args = p.parse_args()
    try:
        if args.command == "baseline":
            if args.out.exists():
                raise ValueError("Output exists. Choose a new analysis revision")
            report = baseline(read_json(args.geometry))
            report["source_geometry_sha256"] = sha(args.geometry)
            write_json(args.out,report)
            print(f"L={report['convergence'][-1]['L_uH']:.6f} uH, zero-board-Cp baseline")
        else:
            result = retune(args.old_pF,args.chip_pF,args.board_pF,args.measured_MHz,args.target_MHz)
            print(f"External capacitor {result:.4f} pF (fixed L, fixed Cp, same loading approximation)")
        return 0
    except (ValueError,KeyError,OSError) as e:
        print(str(e),file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
