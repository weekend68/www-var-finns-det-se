"""Auto-generated FAQ content for lakemedel.html/kategori.html/index.html --
no hand-written medical copy per medication/category, only sentences built
from data the app already has (stock status, national shortage forecast,
polling history, sibling packages). The same list this module returns is
used both for the visible <dl> FAQ HTML and the FAQPage JSON-LD block, so
the two can never drift apart (see routes/lakemedel.py and
routes/kategori.py, which build the list once and hand it to the template
for both purposes).

Question phrasing for build_medication_faq()'s first two questions is
deliberately verbatim against real (low-volume but verified) Google Search
Console query data -- do not casually reword them.
"""


def build_medication_faq(med, pharmacies, in_stock_now, shortage_info, history, siblings):
    """Return [{"question", "answer"}, ...] for one medication. Every
    question that depends on optional data (shortage_info/history/siblings)
    is only included if that data actually exists -- never a half-built or
    empty question."""
    name = med["name"]
    items = []

    # Q1: verbatim GSC phrasing. The question specifically asks WHICH
    # pharmacy, so the answer names actual pharmacies rather than just a
    # count -- up to the first 3, "med flera" if there are more.
    if in_stock_now:
        names = [p["name"] for p in pharmacies[:3] if p.get("name")]
        if names:
            who = ", ".join(names)
            if len(pharmacies) > 3:
                who += " med flera"
            answer = (
                f"Just nu finns {name} hos {who} — totalt {len(pharmacies)} apotek. "
                "Se hela listan ovan för alla apotek och adresser."
            )
        else:
            answer = f"Just nu finns {name} hos {len(pharmacies)} apotek — se listan ovan för alla apotek och adresser."
    else:
        answer = (
            f"Just nu hittar vi inget apotek som har {name} i lager. Bevaka läkemedlet med din "
            "e-postadress ovan, så skickar vi ett e-postmeddelande så fort det finns igen på något "
            "apotek i Sverige."
        )
    items.append({"question": f"Vilket apotek har {name} just nu?", "answer": answer})

    # Q2: verbatim GSC phrasing. Explains the watch/notify mechanism in
    # plain language -- same informational, non-promotional tone as the rest
    # of the site (see routes/lakemedel.py's module-level comment).
    items.append({
        "question": f"Hur vet jag om {name} finns på mitt apotek?",
        "answer": (
            f"Bevaka {name} med din e-postadress, så kontrollerar vi automatiskt lagerstatusen hos "
            "Sveriges apotek åt dig och skickar ett e-postmeddelande så fort det finns i lager igen — "
            "du behöver inte kolla manuellt själv."
        ),
    })

    # Q3/Q4: spirit of the two remaining static questions previously
    # hardcoded in lakemedel.html's <dl class="faq">, now generated here
    # instead so every FAQ question comes from this one place.
    items.append({
        "question": f"Hur ofta uppdateras lagerstatusen för {name}?",
        "answer": (
            "Vi kontrollerar bevakade läkemedel regelbundet mot Fass.se. Ovanpå det görs en färsk "
            "koll när den här sidan besöks, om läkemedlet inte redan bevakas aktivt av någon annan."
        ),
    })
    items.append({
        "question": f"Vad gör jag om {name} är slut?",
        "answer": (
            f'Klicka på "Bevaka det här läkemedlet" ovan så får du ett e-postmeddelande så fort '
            f"{name} finns i lager igen på något apotek i Sverige."
        ),
    })

    # Q5: national shortage status -- only if Läkemedelsverket has an active
    # registration for this medication (shortage.py's shortage_data.json).
    if shortage_info:
        forecasted_start = shortage_info.get("forecasted_start")
        actual_end = shortage_info.get("actual_end")
        forecasted_end = shortage_info.get("forecasted_end")
        if actual_end:
            shortage_answer = (
                f"Läkemedelsverket registrerade tidigare en nationell bristsituation för {name}, "
                f"sedan {forecasted_start}. Enligt Läkemedelsverket är den bristen nu avslutad, "
                f"sedan {actual_end}."
            )
        elif forecasted_end:
            shortage_answer = (
                f"Ja. Läkemedelsverket har registrerat en nationell bristsituation för {name}, "
                f"sedan {forecasted_start} (prognos: tillbaka omkring {forecasted_end})."
            )
        else:
            shortage_answer = (
                f"Ja. Läkemedelsverket har registrerat en nationell bristsituation för {name}, "
                f"sedan {forecasted_start}."
            )
        items.append({"question": f"Är {name} en nationell bristvara just nu?", "answer": shortage_answer})

    # Q6: how long it's been in/out of stock -- ports the exact logic/wording
    # already used by lakemedel.html's Historik card (history.at_least /
    # reliable_since_date / days / since_date) so the FAQ never diverges
    # from what's visibly shown there. See routes/lakemedel.py's
    # _stock_history() docstring for why the at_least/reliable_since_date
    # split exists.
    if history:
        if history["at_least"]:
            if history["in_stock"]:
                history_answer = (
                    f"Vi har bevakat {name} sedan {history['reliable_since_date']} och inte sett "
                    "det restnoterat under den tiden."
                )
            else:
                history_answer = (
                    f"Vi har bevakat {name} sedan {history['reliable_since_date']} och det har "
                    "varit restnoterat under hela den tiden."
                )
        else:
            days_text = f"{history['days']} dagar" if history["days"] is not None else "en tid"
            if history["in_stock"]:
                history_answer = f"{name} kom tillbaka i lager för {days_text} sedan (sedan {history['since_date']})."
            else:
                history_answer = f"{name} har varit restnoterat i {days_text} (sedan {history['since_date']})."
        items.append({"question": f"Hur länge har {name} varit i lager/restnoterat?", "answer": history_answer})

    # Q7: sibling packages -- only if there actually are any.
    if siblings:
        sibling_names = [s["name"] for s in siblings[:5]]
        items.append({
            "question": f"Finns {name} i andra styrkor eller förpackningar?",
            "answer": f"Ja, bland annat: {', '.join(sibling_names)}.",
        })

    return items


def build_category_faq(cat):
    """Return [{"question", "answer"}, ...] for a national-shortage category
    page (routes/kategori.py). `cat` is expected to carry a "products" list
    of display-ready {"name", ...} dicts (the route's own enriched list,
    with real medications.name lookups already applied -- not the raw
    national_shortages feed rows), so the FAQ never shows catalogue-only
    product_name strings the visible product list itself doesn't use."""
    atc_name = cat.get("atc_term") or cat.get("atc_code")
    items = [{
        "question": f"Hur många läkemedel inom {atc_name} är restnoterade just nu?",
        "answer": (
            f"Just nu är {cat['product_count']} olika läkemedel inom {atc_name} registrerade som "
            "restnoterade hos Läkemedelsverket."
        ),
    }]

    seen = []
    for p in cat.get("products") or []:
        name = p.get("name") or p.get("product_name")
        if name and name not in seen:
            seen.append(name)
        if len(seen) >= 5:
            break
    if seen:
        items.append({
            "question": f"Vilka läkemedel ingår i gruppen {atc_name}?",
            "answer": f"Bland annat: {', '.join(seen)}. Se hela listan nedan.",
        })

    return items


def build_homepage_faq():
    """Return [{"question", "answer"}, ...] for the homepage. No per-item
    data needed -- a short, static list pointing visitors at the search
    field and the watch/notify flow."""
    return [
        {
            "question": "Hur hittar jag mitt läkemedel?",
            "answer": (
                'Använd sökfältet ovan och börja skriva läkemedlets namn, t.ex. "Estradot" eller '
                '"Metformin" — träffar dyker upp direkt så att du kan välja rätt förpackning.'
            ),
        },
        {
            "question": "Hur söker jag efter ett apotek som har min medicin?",
            "answer": (
                "Sök upp läkemedlet och öppna dess sida för att se vilka apotek som har det i lager "
                "just nu. Är det slut kan du bevaka det med din e-postadress, så får du ett "
                "e-postmeddelande så fort det finns igen på något apotek i Sverige."
            ),
        },
    ]
