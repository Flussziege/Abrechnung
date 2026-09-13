"""
Urlaub Abrechnung – Datenmodell & Abrechnungslogik.

Änderungen gegenüber der ursprünglichen Tkinter-Version:
  1. Jede Entität (Familie, Person, Tag, Essen, Gruppe, Einkauf) hat eine
     stabile UUID ("id"). Attendance-Keys referenzieren IDs statt
     Listenindizes -> Löschen einer Familie/Person/eines Tages/Essens
     verschiebt keine anderen Zuordnungen mehr.
  2. Einkäufe referenzieren die Käufer-Familie über "buyer_family_id"
     statt über den Namen -> Umbenennen einer Familie verliert keine
     Gutschriften mehr.
  3. Der "Faktor" eines Essens/einer Gruppe wird jetzt sowohl auf die
     Gutschrift des Käufers als auch auf den Anteil der Teilnehmer
     angewendet -> Summe(bought) == Summe(owed) gilt immer, unabhängig
     vom Faktor (vorher konnte bei Faktor != 1.0 Geld "verschwinden"
     oder "entstehen").
  4. Käufe ohne gültige Käufer-Familie oder ohne Teilnehmer erzeugen
     eine Warnung statt eines stillen Fehlers.
"""

import uuid
from collections import defaultdict


def new_id():
    return str(uuid.uuid4())[:8]


# ── Default-Konstruktoren ───────────────────────────────────────────────

def default_person(name):
    return {"id": new_id(), "name": name, "eat_factor": 1.0}


def default_family(name, persons=None):
    return {"id": new_id(), "name": name, "persons": persons or [default_person("Person 1")]}


def default_purchase(buyer_family_id="", amount=0.0):
    return {"id": new_id(), "buyer_family_id": buyer_family_id, "amount": amount}


def default_meal(name, group_id=None):
    return {"id": new_id(), "name": name, "group_id": group_id, "factor": 1.0, "purchases": []}


def default_day(name, breakfast_gid=None):
    return {
        "id": new_id(),
        "name": name,
        "meals": [default_meal("Frühstück", group_id=breakfast_gid), default_meal("Essen 1")],
    }


def default_group(name, factor=1.0):
    return {"id": new_id(), "name": name, "purchases": [], "factor": factor}


def new_state():
    day_names = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag"]
    breakfast_group = default_group("Frühstück (alle Tage)")
    return {
        "days": [default_day(d, breakfast_gid=breakfast_group["id"]) for d in day_names],
        "families": [
            default_family(
                "Familie Meier",
                persons=[default_person("Anne"), default_person("Tom"), default_person("Werner")],
            )
        ],
        "attendance": {},
        "groups": {breakfast_group["id"]: breakfast_group},
    }


# ── Lookups ──────────────────────────────────────────────────────────────

def att_key(day_id, meal_id, family_id, person_id):
    return f"{day_id}:{meal_id}:{family_id}:{person_id}"


def find_family(state, family_id):
    return next((f for f in state["families"] if f["id"] == family_id), None)


def find_person(state, family_id, person_id):
    fam = find_family(state, family_id)
    if not fam:
        return None
    return next((p for p in fam["persons"] if p["id"] == person_id), None)


def find_day(state, day_id):
    return next((d for d in state["days"] if d["id"] == day_id), None)


def find_meal(state, day_id, meal_id):
    day = find_day(state, day_id)
    if not day:
        return None
    return next((m for m in day["meals"] if m["id"] == meal_id), None)


def family_name_by_id(state, family_id):
    fam = find_family(state, family_id)
    return fam["name"] if fam else None


# ── Aufräumen nach dem Löschen ──────────────────────────────────────────

def prune_attendance(state):
    """Entfernt Attendance-Einträge, deren Tag/Essen/Familie/Person nicht
    mehr existiert. Da Keys auf IDs statt Indizes basieren, ist das ein
    reines Aufräumen ohne Risiko einer Fehlzuordnung."""
    day_ids = {d["id"] for d in state["days"]}
    meal_ids = {m["id"] for d in state["days"] for m in d["meals"]}
    fam_ids = {f["id"] for f in state["families"]}
    person_ids = {p["id"] for f in state["families"] for p in f["persons"]}

    def key_ok(k):
        parts = k.split(":")
        if len(parts) != 4:
            return False
        di, mi, fi, pi = parts
        return di in day_ids and mi in meal_ids and fi in fam_ids and pi in person_ids

    state["attendance"] = {k: v for k, v in state["attendance"].items() if key_ok(k)}


def _is_new_format(data):
    fams = data.get("families", [])
    if not fams:
        return True
    return all("id" in f and all("id" in p for p in f.get("persons", [])) for f in fams)


def migrate_state(data):
    """Lädt sowohl neue (ID-basierte) als auch alte, aus der Tkinter-Version
    stammende (index-/namensbasierte) JSON-Speicherstände. Alte Stände
    werden dabei automatisch auf stabile IDs umgestellt – das behebt beim
    Laden gleichzeitig die alten Zuordnungsfehler nach Löschungen/
    Umbenennungen."""
    if _is_new_format(data):
        data.setdefault("days", [])
        data.setdefault("families", [])
        data.setdefault("attendance", {})
        data.setdefault("groups", {})
        return data

    fam_id_map, person_id_map = {}, {}
    new_families = []
    for fi, fam in enumerate(data.get("families", [])):
        new_fam_id = new_id()
        fam_id_map[fi] = new_fam_id
        new_persons = []
        for pi, person in enumerate(fam.get("persons", [])):
            new_person_id = new_id()
            person_id_map[(fi, pi)] = new_person_id
            new_persons.append({
                "id": new_person_id,
                "name": person.get("name", "Person"),
                "eat_factor": float(person.get("eat_factor", 1.0)),
            })
        new_families.append({"id": new_fam_id, "name": fam.get("name", "Familie"), "persons": new_persons})

    name_to_new_fam_id = {f["name"]: f["id"] for f in new_families}

    def migrate_purchases(old_purchases):
        migrated = []
        for p in old_purchases or []:
            migrated.append({
                "id": new_id(),
                "buyer_family_id": name_to_new_fam_id.get(p.get("buyer_family")),
                "amount": float(p.get("amount", 0.0)),
            })
        return migrated

    old_groups = data.get("groups", {})
    new_groups = {}
    for gid, grp in old_groups.items():
        new_groups[gid] = {
            "id": gid,
            "name": grp.get("name", "Gruppe"),
            "factor": float(grp.get("factor", 1.0)),
            "purchases": migrate_purchases(grp.get("purchases", [])),
        }

    day_id_map, meal_id_map = {}, {}
    new_days = []
    for di, day in enumerate(data.get("days", [])):
        new_day_id = new_id()
        day_id_map[di] = new_day_id
        new_meals = []
        for mi, meal in enumerate(day.get("meals", [])):
            new_meal_id = new_id()
            meal_id_map[(di, mi)] = new_meal_id
            gid = meal.get("group_id")
            new_meals.append({
                "id": new_meal_id,
                "name": meal.get("name", "Essen"),
                "group_id": gid if gid in new_groups else None,
                "factor": float(meal.get("factor", 1.0)),
                "purchases": migrate_purchases(meal.get("purchases", [])),
            })
        new_days.append({"id": new_day_id, "name": day.get("name", "Tag"), "meals": new_meals})

    new_attendance = {}
    for key, val in data.get("attendance", {}).items():
        parts = key.split(":")
        if len(parts) != 4:
            continue
        try:
            di, mi, fi, pi = (int(x) for x in parts)
        except ValueError:
            continue
        ids = (day_id_map.get(di), meal_id_map.get((di, mi)), fam_id_map.get(fi), person_id_map.get((fi, pi)))
        if None in ids:
            continue
        new_attendance[att_key(*ids)] = bool(val)

    return {"days": new_days, "families": new_families, "attendance": new_attendance, "groups": new_groups}


def prune_purchases_of_deleted_family(state, family_id):
    """Löscht keine Einkäufe, entfernt aber die Käufer-Referenz, damit
    compute_settlement eine Warnung statt eines stillen Datenverlusts zeigt."""
    for group in state.get("groups", {}).values():
        for p in group.get("purchases", []):
            if p.get("buyer_family_id") == family_id:
                p["buyer_family_id"] = None
    for day in state["days"]:
        for meal in day["meals"]:
            for p in meal.get("purchases", []):
                if p.get("buyer_family_id") == family_id:
                    p["buyer_family_id"] = None


# ── Abrechnung ───────────────────────────────────────────────────────────

def compute_settlement(state):
    families = state["families"]
    days = state["days"]
    att = state["attendance"]
    groups = state.get("groups", {})

    fam_bought = defaultdict(float)
    fam_owed = defaultdict(float)
    warnings = []
    fam_ids = {f["id"] for f in families}

    def credit_and_split(purchases, factor, participants_ef, label):
        total_purchase = sum(p["amount"] for p in purchases)
        effective_total = total_purchase * factor

        for p in purchases:
            buyer_id = p.get("buyer_family_id")
            amount = p.get("amount", 0.0)
            if buyer_id in fam_ids:
                # Faktor wird auch der Gutschrift zugeschlagen, damit
                # Gutschrift und Aufteilung immer in Summe übereinstimmen.
                fam_bought[buyer_id] += amount * factor
            elif amount:
                warnings.append(
                    f'"{label}": Einkauf über {amount:.2f} € hat keine gültige Käufer-Familie '
                    f"mehr (z. B. gelöscht) – wurde niemandem gutgeschrieben."
                )

        total_ef = sum(ef for _, ef in participants_ef)
        if total_ef == 0:
            if effective_total > 0.001:
                warnings.append(
                    f'"{label}": Einkauf von {effective_total:.2f} € (nach Faktor) hat keine '
                    f"markierten Teilnehmer:innen – Betrag bleibt unausgeglichen bei der Käufer-Familie."
                )
            return

        cost_per_unit = effective_total / total_ef
        for fam_id, ef in participants_ef:
            fam_owed[fam_id] += cost_per_unit * ef

    def participants_for(instances):
        result = []
        for day, meal in instances:
            for fam in families:
                for person in fam["persons"]:
                    key = att_key(day["id"], meal["id"], fam["id"], person["id"])
                    if att.get(key, False):
                        result.append((fam["id"], person.get("eat_factor", 1.0)))
        return result

    # ── Gruppen-Essen (z. B. Frühstück über mehrere Tage zusammengefasst) ──
    group_instances = defaultdict(list)
    for day in days:
        for meal in day["meals"]:
            gid = meal.get("group_id")
            if gid and gid in groups:
                group_instances[gid].append((day, meal))

    for gid, instances in group_instances.items():
        grp = groups[gid]
        credit_and_split(
            grp.get("purchases", []),
            grp.get("factor", 1.0),
            participants_for(instances),
            grp.get("name", "Gruppe"),
        )

    # ── Normale Essen ──
    for day in days:
        for meal in day["meals"]:
            if meal.get("group_id"):
                continue
            credit_and_split(
                meal.get("purchases", []),
                meal.get("factor", 1.0),
                participants_for([(day, meal)]),
                f'{day["name"]} – {meal["name"]}',
            )

    # ── Nettosaldo & Ausgleichszahlungen ──
    net = {fam["id"]: fam_bought.get(fam["id"], 0.0) - fam_owed.get(fam["id"], 0.0) for fam in families}

    creditors = sorted([[v, fid] for fid, v in net.items() if v > 0.001], reverse=True)
    debtors = sorted([[-v, fid] for fid, v in net.items() if v < -0.001], reverse=True)

    id_to_name = {fam["id"]: fam["name"] for fam in families}
    transactions = []
    ci, di2 = 0, 0
    while ci < len(creditors) and di2 < len(debtors):
        camt, cfid = creditors[ci]
        damt, dfid = debtors[di2]
        transfer = min(camt, damt)
        transactions.append({"from": id_to_name[dfid], "to": id_to_name[cfid], "amount": transfer})
        creditors[ci][0] -= transfer
        debtors[di2][0] -= transfer
        if creditors[ci][0] < 0.001:
            ci += 1
        if debtors[di2][0] < 0.001:
            di2 += 1

    return {
        "net": {id_to_name[fid]: v for fid, v in net.items()},
        "bought": {id_to_name[fid]: fam_bought.get(fid, 0.0) for fid in id_to_name},
        "owed": {id_to_name[fid]: fam_owed.get(fid, 0.0) for fid in id_to_name},
        "transactions": transactions,
        "warnings": warnings,
    }
