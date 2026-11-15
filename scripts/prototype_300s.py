"""Drive the running app over HTTP to make a five-minute video.

Start the app first, then run this from the repo root. It is safe to run
again: it finds the existing project by name rather than making a second
one, and nothing here can spend money.

The narration lengths are written to the rate the narrator was actually
measured at rather than to an assumption, which is the whole reason the
sixty-second cut landed on sixty seconds. Each line below is about 125
characters, which is roughly 7.4 seconds at 17.02 characters a second,
and each shot carries 0.6 seconds of padding on top of its own narration.
"""

import json
import re
import sys

import httpx

BASE = "http://127.0.0.1:8765"
PROJECT_NAME = "Five minute cut"

# (scene key, image file, narration). The images are the real screenshots
# in the library; nothing here refers to a picture that does not exist.
SCENES = [
    ("s01", "1front_page of site.png",
     "Two systems were built here over several months, and neither of them "
     "started as software. Both started as a problem nobody had measured."),
    ("s02", "1nishes.png",
     "The first one picks a niche, because a tool claiming to serve every "
     "business usually ends up serving none of them properly."),
    ("s03", "1Overview.png",
     "This is the restaurant side. One screen holding every branch, updating "
     "itself while service is actually happening in the dining room."),
    ("s04", "3locations.png",
     "A second location doubles the guesswork, unless the numbers arrive in "
     "the same place, in the same shape, on the same day."),
    ("s05", "2_odering.png",
     "Orders come in from four different places at once. The kitchen is shown "
     "a single ordered queue, which is the only view that matters."),
    ("s06", "5_odered.png",
     "Once an order is placed it stops being a slip of paper and becomes a row "
     "that stock, costing and forecasting all read from."),
    ("s07", "6_bookinonline.png",
     "Bookings sit in the same system, so a full dining room on Friday is "
     "already visible on Tuesday, when the ordering gets decided."),
    ("s08", "7_waiterapp.png",
     "Floor staff get a phone rather than a terminal, because the fastest way "
     "to lose a table is to walk away from it in order to type."),
    ("s09", "8_casier.png",
     "At the till, every sale moves the stock, the costing and the forecast "
     "together, instead of all three being reconciled a week later."),
    ("s10", "9_Ingredient Forecast.png",
     "Tomorrow's ingredient list comes from what actually sold, not from what "
     "somebody remembers selling the last time it rained."),
    ("s11", "9_Production Capacity.png",
     "Capacity is a hard limit, so the kitchen's real output is modelled "
     "rather than assumed, and the forecast is made to respect it."),
    ("s12", "10_supplier.png",
     "Suppliers are scored on three things that can be checked: reliability, "
     "lead time, and whether their invoices match the delivery."),
    ("s13", "12_analysis.png",
     "Waste is measured by the gram and traced to the dish that caused it, "
     "which is uncomfortable reading and the most useful page here."),
    ("s14", "13_Revenue & Profit Intelligence.png",
     "Revenue is easy to see. Profit per dish and per hour is the number that "
     "changes what goes on the menu next season."),
    ("s15", "11_reviews.png",
     "Customers had already said most of this. Nobody had read it sitting "
     "beside the real numbers until the two were put on one page."),
    ("s16", "6analysis.png",
     "Patterns only become decisions when somebody can act before the week is "
     "over, so the reporting runs daily rather than monthly."),
    ("s17", "4_aiActioncenter.png",
     "The system suggests, and it shows its reasoning. A person still decides, "
     "because a wrong order gets paid for in real money."),
    ("s18", "14_resturentBrain.png",
     "None of that is intelligence. It is bookkeeping done fast enough to "
     "still be worth acting on, which is a lower and far more useful bar."),
    ("s19", "3 overview.png",
     "The second system started somewhere else entirely: businesses that lose "
     "customers because nobody answers the phone after six."),
    ("s20", "2create new agent.png",
     "An agent here is configured rather than trained. What it may say, what "
     "it must never promise, and when it hands over to a person."),
    ("s21", "3agent rag information.png",
     "It answers from documents the business actually owns, so it cannot "
     "invent a price, a policy, or an opening time that nobody ever set."),
    ("s22", "9knowledge.png",
     "That knowledge base is maintained like any other record, because a stale "
     "answer delivered confidently is worse than no answer at all."),
    ("s23", "5datasets.png",
     "Datasets are kept apart per client, since one business's pricing has no "
     "business turning up in another business's conversation."),
    ("s24", "4playground.png",
     "Before anything goes live it is tested here, against real questions, by "
     "the person who will be answerable for the answers."),
    ("s25", "5callhistory.png",
     "Every call is kept with its transcript, so a complaint can be checked "
     "against what was said rather than what was remembered."),
    ("s26", "7calender.png",
     "Bookings the agent makes land straight in the calendar, which removes "
     "the one step where confirmations usually go missing."),
    ("s27", "6waha.png",
     "Messaging runs through the channel people already use, because asking a "
     "customer to install something is asking them to leave."),
    ("s28", "8campaign differnt bussiness same time.png",
     "Campaigns for several businesses run at once without their contacts, "
     "their templates or their results ever touching each other."),
    ("s29", "collected_bussiness details bulks.png",
     "Contact lists are imported in bulk and checked on arrival, since a "
     "thousand bad numbers cost far more than an hour of cleaning."),
    ("s30", "4collected data in csv files.png",
     "Everything is exportable as plain files, which matters on the day you "
     "want to leave, and that day arrives for every tool eventually."),
    ("s31", "12billing.png",
     "Usage is metered and shown before it is charged, so a surprise bill is "
     "a bug in the product rather than a feature of it."),
    ("s32", "11teams.png",
     "Access is granted by role, because most data problems are not attacks. "
     "They are ordinary people holding more access than they need."),
    ("s33", "10inegration.png",
     "Both systems speak to what the business already runs, since the fastest "
     "way to kill software is to insist that it be the only one."),
    ("s34", "13verseldeplyed.png",
     "They are deployed and reachable. Shipped is not the same as finished, "
     "but it is the only state from which anything is ever learned."),
    ("s35", "Screenshot 2026-10-03 193032.png",
     "What both refuse to do is present an estimate as a measurement, which is "
     "a harder discipline to keep than it sounds. Software fixes nothing on "
     "its own. It removes the excuse that nobody could have known."),
]


def plan():
    return {
        "title": "Two systems, and the numbers underneath them",
        "logline": "What it takes to build software that an operation can "
                   "actually make decisions from.",
        "synopsis": "Thirty-five beats across two working systems, from the "
                    "problem each was built for to what running them taught.",
        "scenes": [
            {
                "scene_key": key,
                "purpose": f"Beat {index}",
                "target_duration_s": 8.5,
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
            "niche": "software for small operations",
            "audience": "owners and operators",
            "story_style": "plain documentary",
            "target_duration_s": "300",
            "resolution": "1080p",
            "aspect_ratio": "16:9",
            "fps": "24",
            "brief": "What it takes to build software an operation can decide from.",
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
