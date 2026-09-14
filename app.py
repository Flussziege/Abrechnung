"""
Urlaub Abrechnung – Streamlit-Version.

Kein serverseitiges Speichern: der Stand lebt nur in der Browser-Session
(st.session_state). Zum Mitnehmen/Sichern gibt es einen Download-Button
(JSON), zum Fortsetzen einen Upload in der Sidebar.
"""

import json

import pandas as pd
import streamlit as st

from logic import (
    att_key,
    compute_settlement,
    default_day,
    default_family,
    default_group,
    default_meal,
    default_person,
    default_purchase,
    find_family,
    migrate_state,
    new_id,
    new_state,
    prune_attendance,
    prune_purchases_of_deleted_family,
)

st.set_page_config(page_title="Urlaub Abrechnung", page_icon="🧳", layout="wide")

# ── Look & Feel ──────────────────────────────────────────────────────────
st.markdown(
    """
    <style>
    @import url('https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,500;9..144,600&family=Inter:wght@400;500;600&display=swap');

    html, body, [class*="css"]  { font-family: 'Inter', sans-serif; }
    h1, h2, h3 { font-family: 'Fraunces', serif; font-weight: 600; letter-spacing: -0.01em; }
    h1 { color: #17414F; }
    h2, h3 { color: #1F5C74; }

    [data-testid="stSidebar"] { background-color: #EFE9DD; }
    [data-testid="stSidebar"] h1, [data-testid="stSidebar"] h2 { color: #17414F; }

    .stTabs [data-baseweb="tab-list"] { gap: 4px; }
    .stTabs [data-baseweb="tab"] {
        font-family: 'Inter', sans-serif; font-weight: 500;
        background-color: #EFEAE1; border-radius: 8px 8px 0 0; padding: 8px 16px;
    }
    .stTabs [aria-selected="true"] { background-color: #FFFFFF; color: #17414F; }

    div[data-testid="stExpander"] {
        border: 1px solid #E4DFD2; border-radius: 10px; background-color: #FFFFFF;
    }

    hr { border-color: #E4DFD2; }
    </style>
    """,
    unsafe_allow_html=True,
)

CARD_ACCENTS = ["#1F5C74", "#B98B3E", "#5C7280", "#3E88A6", "#8A6A9E", "#4E8F6B"]


# ── State-Initialisierung ────────────────────────────────────────────────
if "state" not in st.session_state:
    st.session_state.state = new_state()

state = st.session_state.state


def confirm_action(key, ask_label, warning, danger=False):
    """Zwei-Schritt-Bestätigung für destruktive Aktionen."""
    pending_key = f"_confirm_{key}"
    if st.session_state.get(pending_key):
        st.warning(warning)
        c1, c2 = st.columns(2)
        if c1.button("Ja, endgültig löschen", key=f"{key}_yes", type="primary"):
            st.session_state[pending_key] = False
            return True
        if c2.button("Abbrechen", key=f"{key}_no"):
            st.session_state[pending_key] = False
            st.rerun()
        return False
    if st.button(ask_label, key=f"{key}_ask", type="primary" if danger else "secondary"):
        st.session_state[pending_key] = True
        st.rerun()
    return False


# ── Sidebar: Laden / Neu / Herunterladen ─────────────────────────────────
with st.sidebar:
    st.markdown("## 🧳 Urlaub Abrechnung")
    st.caption("Kostenteilung für gemeinsame Reisen")

    st.markdown("#### Stand laden")
    uploaded = st.file_uploader("JSON-Datei hochladen", type=["json"], label_visibility="collapsed")
    if uploaded is not None:
        sig = (uploaded.name, uploaded.size)
        if st.session_state.get("_last_upload_sig") != sig:
            try:
                data = json.load(uploaded)
                st.session_state.state = migrate_state(data)
                st.session_state["_last_upload_sig"] = sig
                st.success(f'„{uploaded.name}" geladen.')
                st.rerun()
            except Exception as ex:
                st.error(f"Datei konnte nicht geladen werden: {ex}")

    st.markdown("#### Stand sichern")
    st.download_button(
        "⬇️ Als JSON herunterladen",
        data=json.dumps(state, ensure_ascii=False, indent=2).encode("utf-8"),
        file_name="urlaub_abrechnung.json",
        mime="application/json",
        use_container_width=True,
    )
    st.caption("Es wird nichts auf dem Server gespeichert – nur der Download hier und Upload oben.")

    st.divider()
    if confirm_action("new_state", "🆕 Neu beginnen", "Wirklich alle Daten verwerfen und neu beginnen?"):
        st.session_state.state = new_state()
        st.rerun()


# ── Header ────────────────────────────────────────────────────────────────
st.title("Urlaub Abrechnung")
st.caption("Wer hat eingekauft, wer hat mitgegessen – und wer schuldet wem was.")

tab_att, tab_fam, tab_days, tab_settle = st.tabs(
    ["📋 Anwesenheit", "👪 Familien & Personen", "🍽️ Tage & Essen", "💶 Abrechnung"]
)


# ── Tab: Anwesenheit ──────────────────────────────────────────────────────
with tab_att:
    if not state["families"] or not state["days"]:
        st.info("Lege zuerst mindestens eine Familie (Tab „Familien & Personen“) und einen Tag (Tab „Tage & Essen“) an.")
    else:
        st.caption("🔗 = Essen ist Teil einer zusammengefassten Gruppe (z. B. Frühstück über alle Tage).")
        day_subtabs = st.tabs([f"📅 {day['name']}" for day in state["days"]])

        for day, day_tab in zip(state["days"], day_subtabs):
            with day_tab:
                col_defs = []
                for meal in day["meals"]:
                    icon = "🔗 " if meal.get("group_id") else ""
                    col_key = f"{day['id']}|{meal['id']}"
                    col_defs.append((col_key, meal["id"], f"{icon}{meal['name']}"))

                if not col_defs:
                    st.info("Für diesen Tag sind noch keine Essen angelegt (Tab „Tage & Essen“).")
                    continue

                rows, row_keys = [], []
                for fam in state["families"]:
                    for person in fam["persons"]:
                        row = {"Familie": fam["name"], "Person": person["name"]}
                        for col_key, meal_id, _ in col_defs:
                            key = att_key(day["id"], meal_id, fam["id"], person["id"])
                            row[col_key] = bool(state["attendance"].get(key, False))
                        rows.append(row)
                        row_keys.append((fam["id"], person["id"]))

                df = pd.DataFrame(rows)
                column_config = {
                    "Familie": st.column_config.TextColumn("Familie", disabled=True),
                    "Person": st.column_config.TextColumn("Person", disabled=True),
                }
                for col_key, _, label in col_defs:
                    column_config[col_key] = st.column_config.CheckboxColumn(label)

                edited = st.data_editor(
                    df,
                    column_config=column_config,
                    hide_index=True,
                    use_container_width=True,
                    key=f"attendance_editor_{day['id']}",
                )

                for (fam_id, person_id), (_, row) in zip(row_keys, edited.iterrows()):
                    for col_key, meal_id, _ in col_defs:
                        state["attendance"][att_key(day["id"], meal_id, fam_id, person_id)] = bool(row[col_key])


# ── Tab: Familien & Personen ──────────────────────────────────────────────
with tab_fam:
    for fi, fam in enumerate(state["families"]):
        accent = CARD_ACCENTS[fi % len(CARD_ACCENTS)]
        with st.expander(f"👪 {fam['name']}", expanded=False):
            new_name = st.text_input("Familienname", value=fam["name"], key=f"famname_{fam['id']}")
            if new_name.strip() and new_name != fam["name"]:
                fam["name"] = new_name.strip()

            st.markdown("**Personen**")
            for pi, person in enumerate(fam["persons"]):
                c1, c2, c3 = st.columns([3, 2, 1])
                pname = c1.text_input("Name", value=person["name"], key=f"pname_{person['id']}", label_visibility="collapsed")
                if pname.strip() and pname != person["name"]:
                    person["name"] = pname.strip()
                ef = c2.number_input(
                    "Essensfaktor", value=float(person.get("eat_factor", 1.0)), min_value=0.0, step=0.1,
                    key=f"pef_{person['id']}", label_visibility="collapsed", help="z. B. 0.5 für Kinder",
                )
                person["eat_factor"] = ef
                if c3.button("🗑", key=f"pdel_{person['id']}", help="Person entfernen"):
                    if len(fam["persons"]) <= 1:
                        st.warning("Mindestens eine Person muss bleiben.")
                    else:
                        fam["persons"].pop(pi)
                        prune_attendance(state)
                        st.rerun()

            new_person_name = st.text_input("Neue Person", key=f"newperson_{fam['id']}", placeholder="Name…")
            if st.button("+ Person hinzufügen", key=f"addperson_{fam['id']}"):
                if new_person_name.strip():
                    fam["persons"].append(default_person(new_person_name.strip()))
                    st.rerun()

            st.divider()
            if confirm_action(
                f"famdel_{fam['id']}", "🗑 Familie entfernen",
                f'„{fam["name"]}“ und alle Personen darin wirklich entfernen? Einkäufe dieser Familie bleiben als '
                f"„Käufer unbekannt“ erhalten und werden in der Abrechnung als Warnung angezeigt.",
            ):
                if len(state["families"]) <= 1:
                    st.warning("Mindestens eine Familie muss bleiben.")
                else:
                    prune_purchases_of_deleted_family(state, fam["id"])
                    state["families"].pop(fi)
                    prune_attendance(state)
                    st.rerun()

    st.divider()
    new_fam_name = st.text_input("Neue Familie", key="newfam", placeholder="Name…")
    if st.button("+ Familie hinzufügen", key="addfam"):
        if new_fam_name.strip():
            state["families"].append(default_family(new_fam_name.strip()))
            st.rerun()


# ── Tab: Tage & Essen ──────────────────────────────────────────────────────
def render_purchases(purchases, families, key_prefix):
    fam_options = {f["id"]: f["name"] for f in families}
    if not fam_options:
        st.info("Erst eine Familie anlegen, um Einkäufe zuordnen zu können.")
        return
    for idx, purchase in enumerate(purchases):
        c1, c2, c3 = st.columns([3, 2, 1])
        current = purchase.get("buyer_family_id")
        options = list(fam_options.keys())
        if current not in options:
            options = [current] + options if current else options
        buyer = c1.selectbox(
            "Käufer:in", options=options, index=options.index(current) if current in options else 0,
            format_func=lambda fid: fam_options.get(fid, "— gelöschte Familie —"),
            key=f"{key_prefix}_buyer_{purchase['id']}", label_visibility="collapsed",
        )
        purchase["buyer_family_id"] = buyer
        amount = c2.number_input(
            "Betrag (€)", value=float(purchase.get("amount", 0.0)), min_value=0.0, step=1.0,
            key=f"{key_prefix}_amt_{purchase['id']}", label_visibility="collapsed",
        )
        purchase["amount"] = amount
        if c3.button("🗑", key=f"{key_prefix}_del_{purchase['id']}"):
            purchases.remove(purchase)
            st.rerun()
    if st.button("+ Einkauf hinzufügen", key=f"{key_prefix}_addpurchase"):
        buyer_default = families[0]["id"] if families else None
        purchases.append(default_purchase(buyer_default, 0.0))
        st.rerun()


with tab_days:
    for di, day in enumerate(state["days"]):
        with st.expander(f"📅 {day['name']}", expanded=False):
            new_day_name = st.text_input("Tagesname", value=day["name"], key=f"dayname_{day['id']}")
            if new_day_name.strip() and new_day_name != day["name"]:
                day["name"] = new_day_name.strip()

            for mi, meal in enumerate(day["meals"]):
                gid = meal.get("group_id")
                grp = state["groups"].get(gid) if gid else None
                icon = "🔗 " if grp else ""
                with st.container(border=True):
                    st.markdown(f"**{icon}{meal['name']}**")
                    mc1, mc2 = st.columns([3, 2])
                    new_meal_name = mc1.text_input(
                        "Name", value=meal["name"], key=f"mealname_{meal['id']}", label_visibility="collapsed"
                    )
                    if new_meal_name.strip() and new_meal_name != meal["name"]:
                        meal["name"] = new_meal_name.strip()

                    target = grp if grp else meal
                    factor = mc2.number_input(
                        "Faktor", value=float(target.get("factor", 1.0)), min_value=0.0, step=0.1,
                        key=f"factor_{meal['id']}", label_visibility="collapsed",
                        help="Multipliziert die eingegebenen Einkaufsbeträge (z. B. 1.2 für 20% zusätzliche Kosten). "
                             "Der Faktor wird sowohl der Käufer-Familie gutgeschrieben als auch unter den "
                             "Teilnehmenden aufgeteilt – die Abrechnung bleibt dadurch immer ausgeglichen.",
                    )
                    target["factor"] = factor

                    if grp:
                        members = [
                            f'{d["name"]}'
                            for d in state["days"]
                            for m in d["meals"]
                            if m.get("group_id") == gid
                        ]
                        st.caption(f"🔗 Zusammengefasst mit: {', '.join(members)} – Einkäufe gelten für alle gemeinsam.")
                        if st.button("Aus Gruppe herausnehmen", key=f"ungroup_{meal['id']}"):
                            meal["group_id"] = None
                            meal["factor"] = grp.get("factor", 1.0)
                            meal["purchases"] = []
                            st.rerun()
                    else:
                        other_groups = {k: v["name"] for k, v in state["groups"].items()}
                        if other_groups:
                            gc1, gc2 = st.columns([3, 1])
                            chosen = gc1.selectbox(
                                "Zu bestehender Gruppe hinzufügen", options=list(other_groups.keys()),
                                format_func=lambda k: other_groups[k], key=f"joingroup_{meal['id']}",
                                label_visibility="collapsed",
                            )
                            if gc2.button("Beitreten", key=f"joinbtn_{meal['id']}"):
                                meal["group_id"] = chosen
                                meal["purchases"] = []
                                st.rerun()
                        if st.button("+ Neue Gruppe aus diesem Essen erstellen", key=f"newgroup_{meal['id']}"):
                            new_gid = new_id()
                            state["groups"][new_gid] = {
                                "id": new_gid, "name": meal["name"],
                                "purchases": list(meal.get("purchases", [])), "factor": meal.get("factor", 1.0),
                            }
                            meal["group_id"] = new_gid
                            meal["purchases"] = []
                            st.rerun()

                    st.markdown(
                        "Einkäufe" + (" (gelten für die ganze Gruppe)" if grp else "") + ":"
                    )
                    render_purchases(target["purchases"], state["families"], key_prefix=f"m{meal['id']}")

                    if len(day["meals"]) > 1:
                        if st.button("🗑 Essen entfernen", key=f"mealdel_{meal['id']}"):
                            day["meals"].pop(mi)
                            prune_attendance(state)
                            st.rerun()

            if st.button("+ Essen hinzufügen", key=f"addmeal_{day['id']}"):
                n = len(day["meals"])
                day["meals"].append(default_meal(f"Essen {n}"))
                st.rerun()

            st.divider()
            if confirm_action(f"daydel_{day['id']}", "🗑 Tag entfernen", f'„{day["name"]}“ und alle Essen/Anwesenheiten darin wirklich entfernen?'):
                if len(state["days"]) <= 1:
                    st.warning("Mindestens ein Tag muss bleiben.")
                else:
                    state["days"].pop(di)
                    prune_attendance(state)
                    st.rerun()

    st.divider()
    new_day_name = st.text_input("Neuer Tag", key="newday", placeholder="z. B. Samstag")
    if st.button("+ Tag hinzufügen", key="addday"):
        if new_day_name.strip():
            breakfast_gid = None
            if state["days"]:
                breakfast_gid = state["days"][0]["meals"][0].get("group_id")
            state["days"].append(default_day(new_day_name.strip(), breakfast_gid=breakfast_gid))
            st.rerun()


# ── Tab: Abrechnung ──────────────────────────────────────────────────────
with tab_settle:
    result = compute_settlement(state)

    for w in result["warnings"]:
        st.warning(w)

    st.markdown("### Kostenübersicht nach Familie")
    rows = []
    for fam in state["families"]:
        name = fam["name"]
        rows.append({
            "Familie": name,
            "Eingekauft (€)": result["bought"].get(name, 0.0),
            "Gegessen (€)": result["owed"].get(name, 0.0),
            "Saldo (€)": result["net"].get(name, 0.0),
        })
    df = pd.DataFrame(rows)

    def _style_saldo(v):
        color = "#3F8F5F" if v >= 0 else "#B4453A"
        return f"color: {color}; font-weight: 600;"

    if not df.empty:
        st.dataframe(
            df.style.format({"Eingekauft (€)": "{:.2f}", "Gegessen (€)": "{:.2f}", "Saldo (€)": "{:+.2f}"})
            .map(_style_saldo, subset=["Saldo (€)"]),
            hide_index=True, use_container_width=True,
        )
    st.caption("Saldo: + bedeutet mehr eingekauft als gegessen (bekommt Geld zurück).")

    st.markdown("### Vorschlag: Ausgleichszahlungen")
    if result["transactions"]:
        for t in result["transactions"]:
            st.markdown(f"**{t['from']}** → **{t['to']}** &nbsp;·&nbsp; {t['amount']:.2f} €")
    else:
        st.success("✓ Keine Ausgleichszahlungen nötig.")

    st.divider()
    st.download_button(
        "⬇️ Abrechnung als JSON exportieren",
        data=json.dumps(result, ensure_ascii=False, indent=2).encode("utf-8"),
        file_name="abrechnung.json",
        mime="application/json",
    )
