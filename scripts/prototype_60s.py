"""Drive the running app over HTTP to make a 60-second prototype.

Start the app first, then run this from the repo root. It is safe to run
again: it finds the existing project by name rather than making a second
one, and nothing here can spend money.

Everything goes through the app's own endpoints, so what it proves is the
pipeline a user would drive, not a private shortcut around it.

The narration lengths below are set from the rate the voice was actually
measured at: 17.02 characters a second for en_US-joe-medium on this
machine. Twelve shots carry 0.6s of padding each, so about 53 seconds of
speech is what lands on sixty seconds of finished video.
"""

import json
import re
import sys

import httpx

BASE = "http://127.0.0.1:8765"
PROJECT_NAME = "Sixty second prototype"

# Roughly 75 characters each, which is 4.4s at the measured rate.
SCENES = [
    ("s01", "7_waiterapp.png",
     "A restaurant runs on small decisions, made hourly, usually without numbers."),
    ("s02", "1Overview.png",
     "This is one screen holding every branch, updating while service happens."),
    ("s03", "2_odering.png",
     "Orders arrive from four places. The kitchen sees a single, ordered queue."),
    ("s04", "8_casier.png",
     "Every sale now moves the stock, the costing and the forecast together."),
    ("s05", "9_Ingredient Forecast.png",
     "Tomorrow's ingredients come from what actually sold, not from a guess."),
    ("s06", "9_Production Capacity.png",
     "Capacity is a real limit, so the kitchen's true output is modelled."),
    ("s07", "10_supplier.png",
     "Suppliers are scored on reliability, lead time and invoice accuracy."),
    ("s08", "12_analysis.png",
     "Waste is measured, so every gram is traced to the dish that caused it."),
    ("s09", "13_Revenue & Profit Intelligence.png",
     "Revenue is easy. Profit per dish and per hour is what changes decisions."),
    ("s10", "11_reviews.png",
     "Customers said all this already. Nobody read it beside the real numbers."),
    ("s11", "4_aiActioncenter.png",
     "Now the system suggests, with reasoning. A person still decides."),
    ("s12", "14_resturentBrain.png",
     "It is only bookkeeping, done fast enough to still be worth acting on."),
]


def plan():
    return {
        "title": "The numbers a restaurant never had",
        "logline": "What happens when restaurant operations are measured properly.",
        "synopsis": "Twelve beats walking through an operations system, from the "
                    "problem it solves to the decisions it changes.",
        "scenes": [
            {
                "scene_key": key,
                "purpose": f"Beat {index}",
                "target_duration_s": 5.0,
                "narration": narration,
                "dialogue": [],
                "characters": [],
                "image_hint": image,
                "camera": "slow push in" if index % 2 else "gentle pan right",
                "transition": "cut",
                "preferred_method": "camera_move",
            }
            for index, (key, image, narration) in enumerate(SCENES, 1)
        ],
    }


def show(label, response, fatal=True):
    flash = response.headers.get("X-AVM-Flash", "")
    print(f"[{response.status_code}] {label}" + (f"\n    {flash}" if flash else ""))
    if response.status_code >= 400:
        print("    ERROR:", response.text[:900])
        if fatal:
            sys.exit(1)
    return response


def project_id(client):
    text = client.get("/projects").text
    for match in re.finditer(r"/projects/(\d+)", text):
        pid = int(match.group(1))
        if PROJECT_NAME in client.get(f"/projects/{pid}").text:
            return pid
    return None


with httpx.Client(base_url=BASE, timeout=7200) as c:
    pid = project_id(c)
    if pid is None:
        show("create project", c.post("/projects", data={
            "name": PROJECT_NAME,
            "niche": "restaurant operations",
            "audience": "owners and operators",
            "story_style": "plain documentary",
            "target_duration_s": "60",
            "resolution": "1080p",
            "aspect_ratio": "16:9",
            "fps": "24",
            "brief": "A measured walk through an operations system.",
        }))
        pid = project_id(c)
    print("project id:", pid)

    show("paste script", c.post(
        f"/projects/{pid}/script/paste",
        data={"pasted": json.dumps(plan())},
    ))

    page = c.get(f"/projects/{pid}/script").text
    versions = [int(m) for m in re.findall(r"/scripts/(\d+)/approve", page)]
    if versions:
        show("approve script", c.post(f"/scripts/{max(versions)}/approve"))
    else:
        print("    nothing approvable; the screen lists the validation problems")

    show("plan narration", c.post(f"/projects/{pid}/voice/plan"))
    show("synthesise narration", c.post(f"/projects/{pid}/voice/synthesise"))
    show("plan and render shots", c.post(f"/projects/{pid}/shots/render"))
    show("assemble and check", c.post(f"/projects/{pid}/render"))
    print("\nproject:", f"{BASE}/projects/{pid}/render")
