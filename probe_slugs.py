"""Probe ATS endpoints to find which company slugs are valid.

Run once, manually, when expanding company coverage. Not part of the deployed pipeline.
Outputs CSV-style: <ats>,<slug>,<status>,<job_count>,<fpga_match_count>
"""
from __future__ import annotations

import json
import re
import urllib.request
import urllib.error

UA = "Mozilla/5.0 FPGAJobAggregator/1.0"
TIMEOUT = 15
FPGA_RE = re.compile(r"\bfpga\b", re.I)

GREENHOUSE_CANDIDATES = [
    # HFT
    "citadel", "citadelsecurities", "jumptrading", "optiverus", "optiver",
    "mavensecurities", "twosigma", "janestreet", "imc",
    # AI / accelerators
    "celestialai", "celestial-ai", "lightmatter", "enfabrica", "dmatrix",
    "d-matrix", "groq", "tenstorrent", "rain-ai", "rainai", "samba-nova", "sambanova",
    "cerebras", "rivosinc", "rivos", "tensorblade",
    # Quantum / computing
    "atomcomputing", "atom-computing", "rigetti", "quantinuum", "psiquantum",
    "ionq", "ionq-quantum", "xanadu", "xanaduai",
    # Autonomy / robotics
    "waymo", "cruise", "zoox", "aurora-innovation", "aurorainnovation",
    "nuro", "joby", "archer-aviation", "skydio", "saronic", "saronicllc",
    "bostondynamics", "boston-dynamics", "waabi",
    # Space / RF
    "loftorbital", "kaptaspace", "kapta-space", "starfishspace",
    "varda", "vast", "stoke-space", "stokespace", "trueanomaly",
    # Other
    "arista", "arrcus", "kalray", "wintermute", "antmicro",
]

LEVER_CANDIDATES = [
    "mythic", "mythicai", "mythic.ai", "mythic-ai",
    "honeybee", "honeybeerobotics", "honeybee-robotics",
    "antmicro", "celestialai", "tenstorrent", "mavenir",
    "lightmatter", "waabi", "skydio", "joby", "luminartech",
    "luminar", "innoviz", "argoai", "rigettiandco", "ionq",
    "snorkelai", "groq", "rain", "rainai",
]

ASHBY_CANDIDATES = [
    "tenstorrent", "groq", "rain", "rainai", "rain-ai",
    "lightmatter", "dmatrix", "d-matrix", "enfabrica",
    "atomcomputing", "atom-computing", "snorkel", "snorkel-ai",
    "weaviate", "celestialai", "celestial-ai", "anyscale",
    "modular", "perplexity", "rivos", "kepler",
    "saronic", "anduril",  # may still have non-cleared SDE roles
]


def fetch_url(url: str, method="GET") -> tuple[int, bytes]:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, b""
    except Exception:
        return 0, b""


def probe_greenhouse(slug: str):
    code, body = fetch_url(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true")
    if code != 200:
        return code, 0, 0
    try:
        data = json.loads(body)
        jobs = data.get("jobs", [])
        n_total = len(jobs)
        n_fpga = sum(1 for j in jobs if FPGA_RE.search(j.get("title", "") + " " + (j.get("content", "") or "")))
        return code, n_total, n_fpga
    except Exception:
        return code, 0, 0


def probe_lever(slug: str):
    code, body = fetch_url(f"https://api.lever.co/v0/postings/{slug}?mode=json")
    if code != 200:
        return code, 0, 0
    try:
        data = json.loads(body)
        if not isinstance(data, list):
            return code, 0, 0
        n_total = len(data)
        n_fpga = sum(1 for j in data if FPGA_RE.search(j.get("text", "") + " " + (j.get("descriptionPlain", "") or "")))
        return code, n_total, n_fpga
    except Exception:
        return code, 0, 0


def probe_ashby(slug: str):
    code, body = fetch_url(f"https://api.ashbyhq.com/posting-api/job-board/{slug}")
    if code != 200:
        return code, 0, 0
    try:
        data = json.loads(body)
        jobs = data.get("jobs", [])
        n_total = len(jobs)
        n_fpga = sum(1 for j in jobs if FPGA_RE.search(j.get("title", "") + " " + (j.get("descriptionPlain", "") or "")))
        return code, n_total, n_fpga
    except Exception:
        return code, 0, 0


def main():
    print("ats,slug,http,jobs,fpga_matches")
    keepers = {"greenhouse": [], "lever": [], "ashby": []}

    for slug in GREENHOUSE_CANDIDATES:
        code, total, fpga = probe_greenhouse(slug)
        print(f"greenhouse,{slug},{code},{total},{fpga}")
        if code == 200:
            keepers["greenhouse"].append((slug, total, fpga))

    for slug in LEVER_CANDIDATES:
        code, total, fpga = probe_lever(slug)
        print(f"lever,{slug},{code},{total},{fpga}")
        if code == 200:
            keepers["lever"].append((slug, total, fpga))

    for slug in ASHBY_CANDIDATES:
        code, total, fpga = probe_ashby(slug)
        print(f"ashby,{slug},{code},{total},{fpga}")
        if code == 200:
            keepers["ashby"].append((slug, total, fpga))

    print("\n=== VALID SLUGS (200 OK) ===")
    for ats, items in keepers.items():
        items.sort(key=lambda x: -x[2])  # sort by FPGA match count desc
        print(f"\n{ats.upper()}:")
        for slug, total, fpga in items:
            tag = " [FPGA!]" if fpga > 0 else ""
            print(f"  {slug:30s} jobs={total:4d}  fpga={fpga}{tag}")


if __name__ == "__main__":
    main()
