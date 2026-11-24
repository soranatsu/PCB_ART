"""Create the portable skill ZIP deterministically, without build/private files."""
from pathlib import Path
import argparse
import zipfile


ROOT = Path(__file__).resolve().parents[1]


def package(destination):
    files = [ROOT/n for n in ("SKILL.md","README.md","LICENSE","VERSION","CHANGELOG.md","requirements.txt")]
    for directory in ("agents","references","scripts","examples","assets","tests"):
        files += [p for p in (ROOT/directory).rglob("*") if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc"]
    with zipfile.ZipFile(destination,"w",zipfile.ZIP_DEFLATED,compresslevel=9) as z:
        for p in sorted(files):
            info = zipfile.ZipInfo("pcb-art-enig/"+p.relative_to(ROOT).as_posix(),(2026,11,24,0,0,0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            z.writestr(info,p.read_bytes())
    with zipfile.ZipFile(destination) as z:
        if z.testzip() is not None:
            raise ValueError("ZIP CRC failure")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out",type=Path,default=ROOT/"PCB_ART.zip")
    package(parser.parse_args().out)
