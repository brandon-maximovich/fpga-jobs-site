"""Probe Workday job board endpoints to find which FPGA-hiring tenants work.

Workday URL pattern:
  https://{tenant}.{cluster}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs

The endpoint accepts POST with JSON body {"appliedFacets":{}, "limit":50, "offset":0, "searchText":"FPGA"}
and returns {"jobPostings": [...], "total": N}.
"""
from __future__ import annotations

import json
import urllib.request
import urllib.error

UA = "Mozilla/5.0 FPGAJobAggregator/1.0"
TIMEOUT = 20

CANDIDATES = [
    # (tenant, cluster, site_path)
    ("amd", "wd1", "External_Search"),
    ("nvidia", "wd5", "NVIDIAExternalCareerSite"),
    ("intel", "wd1", "External"),
    ("intelcareers", "wd1", "External"),
    ("cadence", "wd1", "External_Careers"),
    ("synopsys", "wd1", "Synopsys_Careers"),
    ("synopsys", "wd1", "External"),
    ("microchip", "wd1", "Microchip_Technology"),
    ("microchip", "wd5", "External_Career"),
    ("marvell", "wd1", "MarvellCareers2"),
    ("marvell", "wd1", "Careers"),
    ("broadcom", "wd1", "External_Career"),
    ("ericsson", "wd3", "ECSDExternal"),
    ("nokia", "wd3", "nokia_careers"),
    ("siemens", "wd3", "siemens_careers"),
    ("siemens", "wd3", "Search"),
    ("infineon", "wd3", "GBL_Careers"),
    ("rambus", "wd1", "Rambus"),
    ("rambus", "wd1", "External"),
    ("renesas", "wd1", "External"),
    ("nxp", "wd1", "careers"),
    ("nxp", "wd1", "nxp_careers"),
    ("analog", "wd1", "External"),
    ("ti", "wd1", "TexasInstruments"),
    ("qualcomm", "wd1", "External"),
    ("juniper", "wd1", "Juniper_Careers"),
    ("ibm", "wd1", "External"),
    ("hpe", "wd5", "Jobsathpe"),
    ("dell", "wd1", "External"),
    ("apple", "wd1", "External"),  # unlikely but try
    ("teradyne", "wd1", "External"),
    ("keysight", "wd1", "External"),
    ("rohde-schwarz", "wd3", "rscareers"),
    ("astera-labs", "wd1", "External"),
    ("astera", "wd1", "External"),
    ("western-digital", "wd1", "External_Career"),
    ("seagate", "wd1", "External"),
    ("micron", "wd1", "External"),
    ("global-foundries", "wd1", "External"),
    ("globalfoundries", "wd1", "External"),
    ("mediatek", "wd3", "External"),
    ("achronix", "wd1", "External"),
    ("cisco", "wd1", "External_Career"),
]


def probe(tenant: str, cluster: str, site: str):
    url = f"https://{tenant}.{cluster}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs"
    body = json.dumps({
        "appliedFacets": {},
        "limit": 20,
        "offset": 0,
        "searchText": "FPGA",
    }).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={
            "User-Agent": "Mozilla/5.0",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Referer": f"https://{tenant}.{cluster}.myworkdayjobs.com/{site}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
        jobs = data.get("jobPostings", [])
        total = data.get("total", len(jobs))
        # Count remote-looking
        remote = sum(1 for j in jobs if "remote" in (j.get("locationsText", "") or "").lower()
                     or "anywhere" in (j.get("locationsText", "") or "").lower())
        return resp.status, total, len(jobs), remote
    except urllib.error.HTTPError as e:
        return e.code, 0, 0, 0
    except Exception as e:
        return f"ERR:{type(e).__name__}", 0, 0, 0


def main():
    print("tenant,cluster,site,http,total_fpga,returned,remote")
    keepers = []
    for tenant, cluster, site in CANDIDATES:
        code, total, returned, remote = probe(tenant, cluster, site)
        print(f"{tenant},{cluster},{site},{code},{total},{returned},{remote}")
        if code == 200 and total > 0:
            keepers.append((tenant, cluster, site, total, remote))

    print("\n=== VALID WORKDAY TENANTS ===")
    keepers.sort(key=lambda x: -x[4])  # by remote count desc
    for tenant, cluster, site, total, remote in keepers:
        tag = " [REMOTE!]" if remote > 0 else ""
        print(f"  {tenant:25s} {cluster}/{site:30s} fpga_total={total:4d}  remote={remote}{tag}")


if __name__ == "__main__":
    main()
