# -*- coding: utf-8 -*-
"""Bot de tendance quotidienne - SIMULATION PAPIER (aucun argent reel, aucune cle API).

Tourne sur GitHub Actions toutes les heures. Prix publics (Coinbase, sinon Kraken, sinon Hyperliquid).
Strategie testee dans les etudes (etude_tendance, etude_levier, etude_levier2) :
  - a la cloture quotidienne (00:00 UTC), pour BTC, ETH, SOL : etage f = (nb de moyennes 50/100/200 jours depassees) / 3
  - exposition cible d'un token = (capital / 3) x levier x f ; jamais de vente a decouvert
  - 3 variantes tournent sur les MEMES prix : A = x1 ; B = x1.5 fixe ; C = module (x1 si f < 100 %, x1.5 si les 3 moyennes sont depassees)
  - stop de protection a 8 ATR sous l'entree (filet de securite), re-entree possible 2 jours apres
  - frais 0.075 % par transaction, funding simule 10 %/an sur l'exposition
Etat enregistre dans docs/data/state.json (lu par la page web).
"""
import os, sys, json, math, time, urllib.request, urllib.error
from datetime import datetime, timezone, timedelta

RACINE = os.path.dirname(os.path.abspath(__file__))
ETAT = os.path.join(RACINE, "docs", "data", "state.json")
CAPITAL = 1000.0
TOKENS = ["BTC", "ETH", "SOL"]
FRAIS = 0.00075
FUNDING_AN = 0.10
STOP_ATR = 8.0
ATTENTE_JOURS = 2
SEUIL_REEQUILIBRAGE = 0.15      # on ajuste si l'ecart a la cible depasse 15 % de la part du token
VARIANTES = {
    "A": {"nom": "A - Prudent x1", "mode": "fixe", "L": 1.0},
    "B": {"nom": "B - x1,5 fixe", "mode": "fixe", "L": 1.5},
    "C": {"nom": "C - Module (max x1,5)", "mode": "module", "L": 1.5},
}
PAIRES_KRAKEN = {"BTC": "XBTUSD", "ETH": "ETHUSD", "SOL": "SOLUSD"}

# ------------------------------------------------------------------ donnees de marche
def http_json(url, corps=None, timeout=25):
    req = urllib.request.Request(url, data=json.dumps(corps).encode() if corps is not None else None,
                                 headers={"User-Agent": "bot-tendance/1.0", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())

def bougies_coinbase(tok):
    d = http_json(f"https://api.exchange.coinbase.com/products/{tok}-USD/candles?granularity=86400")
    return sorted((int(x[0]) * 1000, float(x[3]), float(x[2]), float(x[1]), float(x[4])) for x in d)

def bougies_kraken(tok):
    d = http_json(f"https://api.kraken.com/0/public/OHLC?pair={PAIRES_KRAKEN[tok]}&interval=1440")
    res = d["result"]; cle = [k for k in res if k != "last"][0]
    return sorted((int(x[0]) * 1000, float(x[1]), float(x[2]), float(x[3]), float(x[4])) for x in res[cle])

def bougies_hyperliquid(tok):
    fin = int(time.time() * 1000); deb = fin - 400 * 86400000
    d = http_json("https://api.hyperliquid.xyz/info",
                  {"type": "candleSnapshot", "req": {"coin": tok, "interval": "1d", "startTime": deb, "endTime": fin}})
    return sorted((int(x["t"]), float(x["o"]), float(x["h"]), float(x["l"]), float(x["c"])) for x in d)

SOURCES = [("Coinbase", bougies_coinbase), ("Kraken", bougies_kraken), ("Hyperliquid", bougies_hyperliquid)]

def jour_utc(ts_ms):
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")

def charger_donnees(maintenant):
    """Retourne {token: {"fermees": [...], "aujourdhui": bougie|None, "prix": float, "source": str}}."""
    aujourdhui = maintenant.strftime("%Y-%m-%d")
    out = {}
    for tok in TOKENS:
        derniere_erreur = None
        for nom, f in SOURCES:
            try:
                b = f(tok)
                fermees = [x for x in b if jour_utc(x[0]) < aujourdhui]
                if len(fermees) < 215:
                    raise RuntimeError(f"historique trop court ({len(fermees)} jours)")
                partielle = [x for x in b if jour_utc(x[0]) == aujourdhui]
                prix = partielle[-1][4] if partielle else fermees[-1][4]
                out[tok] = {"fermees": fermees, "aujourdhui": partielle[-1] if partielle else None, "prix": prix, "source": nom}
                break
            except Exception as e:
                derniere_erreur = f"{nom}: {e}"
        else:
            raise RuntimeError(f"Donnees indisponibles pour {tok} ({derniere_erreur})")
    return out

# ------------------------------------------------------------------ indicateurs
def indicateurs(fermees):
    c = [x[4] for x in fermees]; h = [x[2] for x in fermees]; l = [x[3] for x in fermees]
    m = {n: sum(c[-n:]) / n for n in (50, 100, 200)}
    tr = [h[0] - l[0]] + [max(h[i] - l[i], abs(h[i] - c[i-1]), abs(l[i] - c[i-1])) for i in range(1, len(c))]
    atr = tr[0]
    for x in tr[1:]: atr = (atr * 13 + x) / 14
    dessus = {n: c[-1] > m[n] for n in (50, 100, 200)}
    f = sum(dessus.values()) / 3
    return {"cloture": c[-1], "jour": jour_utc(fermees[-1][0]), "m50": m[50], "m100": m[100], "m200": m[200],
            "atr": atr, "dessus": dessus, "f": f, "dist": (c[-1] - m[50]) / atr if atr > 0 else 0.0}

def levier(var, f):
    if var["mode"] == "fixe": return var["L"]
    return 1.0 if f < 0.999 else var["L"]

# ------------------------------------------------------------------ etat
def etat_initial(maintenant, prix):
    return {"version": 1, "cree": maintenant.isoformat(), "capital": CAPITAL, "derniere_maj": None, "dernier_jour": None,
            "base_prix": prix, "bh_historique": [],
            "variantes": {k: {"cash": CAPITAL, "positions": {}, "attente": {t: 0 for t in TOKENS}, "trades": [], "historique": [],
                              "frais": 0.0, "funding": 0.0, "pic": CAPITAL, "dd_max": 0.0} for k in VARIANTES}}

def charger_etat():
    if os.path.exists(ETAT):
        with open(ETAT, encoding="utf-8") as f: return json.load(f)
    return None

def sauver_etat(etat):
    os.makedirs(os.path.dirname(ETAT), exist_ok=True)
    tmp = ETAT + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f: json.dump(etat, f, ensure_ascii=False, indent=1)
    os.replace(tmp, ETAT)

def equity(v, prix):
    return v["cash"] + sum(p["unites"] * prix[t] for t, p in v["positions"].items())

def notifier(titre, message):
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    if not topic: return
    try:
        req = urllib.request.Request("https://ntfy.sh/" + topic, data=message.encode("utf-8"),
                                     headers={"Title": titre.encode("utf-8").decode("latin-1", "ignore"), "User-Agent": "bot-tendance/1.0"})
        urllib.request.urlopen(req, timeout=10).read()
    except Exception:
        pass

# ------------------------------------------------------------------ operations papier
def acheter(v, tok, notionnel, prix, maintenant, raison, lev, f, atr):
    unites = notionnel / prix; frais = notionnel * FRAIS
    v["cash"] -= notionnel + frais; v["frais"] += frais
    p = v["positions"].get(tok)
    if p is None:
        v["positions"][tok] = {"unites": unites, "prix_moyen": prix, "ouverture": maintenant.isoformat(), "stop": prix - STOP_ATR * atr,
                               "frais": frais, "funding": 0.0, "realise": 0.0, "levier_max": lev, "etape": f, "levier": lev,
                               "raison": raison, "mise_initiale": notionnel}
    else:
        p["prix_moyen"] = (p["unites"] * p["prix_moyen"] + unites * prix) / (p["unites"] + unites)
        p["unites"] += unites; p["frais"] += frais; p["levier_max"] = max(p["levier_max"], lev)
        p["etape"], p["levier"], p["raison"] = f, lev, raison

def vendre(v, tok, unites, prix, maintenant, raison, total=False):
    p = v["positions"][tok]
    unites = min(unites, p["unites"]); notionnel = unites * prix; frais = notionnel * FRAIS
    v["cash"] += notionnel - frais; v["frais"] += frais
    p["realise"] += unites * (prix - p["prix_moyen"]); p["frais"] += frais; p["unites"] -= unites
    if total or p["unites"] < 1e-12:
        duree = (maintenant - datetime.fromisoformat(p["ouverture"])).total_seconds() / 86400
        net = p["realise"] - p["frais"] - p["funding"]
        v["trades"].append({"token": tok, "ouverture": p["ouverture"], "cloture": maintenant.isoformat(), "duree_jours": round(duree, 1),
                            "prix_entree": round(p["prix_moyen"], 6), "prix_sortie": round(prix, 6),
                            "variation_prix_pct": round((prix / p["prix_moyen"] - 1) * 100, 2), "net_usd": round(net, 2),
                            "levier_max": p["levier_max"], "raison_sortie": raison})
        v["trades"] = v["trades"][-200:]
        del v["positions"][tok]
        return net
    return None

def reequilibrer(v, tok, cible, prix, maintenant, raison, lev, f, atr, budget, evenements, nom):
    p = v["positions"].get(tok)
    actuel = p["unites"] * prix if p else 0.0
    if cible <= 0:
        if p:
            net = vendre(v, tok, p["unites"], prix, maintenant, raison, total=True)
            evenements.append(f"{nom} {tok} : sortie ({raison}), net {net:+.2f} $")
        return
    if not p:
        acheter(v, tok, cible, prix, maintenant, raison, lev, f, atr)
        evenements.append(f"{nom} {tok} : entree x{lev:g}, {cible:.0f} $ ({raison})")
        return
    change = abs(p["etape"] - f) > 1e-9 or abs(p["levier"] - lev) > 1e-9
    if change or abs(cible - actuel) > SEUIL_REEQUILIBRAGE * budget:
        delta = cible - actuel
        if delta > 0: acheter(v, tok, delta, prix, maintenant, raison, lev, f, atr)
        else: vendre(v, tok, -delta / prix, prix, maintenant, raison)
        p = v["positions"].get(tok)
        if p: p["etape"], p["levier"], p["raison"] = f, lev, raison
        if change: evenements.append(f"{nom} {tok} : ajustement a x{lev:g}, etage {round(f*3)}/3")

def phrase(ind, lev):
    mm = ", ".join(f"MM{n} {'ok' if ind['dessus'][n] else 'non'}" for n in (50, 100, 200))
    return f"{round(ind['f']*3)}/3 moyennes depassees ({mm}), distance MM50 {ind['dist']:.1f} ATR -> levier x{lev:g}"

# ------------------------------------------------------------------ cycle
def cycle(etat, donnees, maintenant):
    evenements = []
    prix = {t: donnees[t]["prix"] for t in TOKENS}
    ind = {t: indicateurs(donnees[t]["fermees"]) for t in TOKENS}
    # 1. funding simule depuis la derniere mise a jour
    if etat["derniere_maj"]:
        heures = min(48.0, max(0.0, (maintenant - datetime.fromisoformat(etat["derniere_maj"])).total_seconds() / 3600))
        for v in etat["variantes"].values():
            for t, p in v["positions"].items():
                cout = p["unites"] * prix[t] * FUNDING_AN / 8760 * heures
                v["cash"] -= cout; v["funding"] += cout; p["funding"] += cout
    # 2. stops de protection (plus bas du jour en cours)
    for k, var in VARIANTES.items():
        v = etat["variantes"][k]
        for t in list(v["positions"]):
            p = v["positions"][t]; auj = donnees[t]["aujourdhui"]
            plus_bas = min(auj[3], prix[t]) if auj else prix[t]
            if plus_bas <= p["stop"]:
                net = vendre(v, t, p["unites"], p["stop"], maintenant, "stop de protection touche", total=True)
                v["attente"][t] = ATTENTE_JOURS
                evenements.append(f"{var['nom']} {t} : STOP de protection touche, net {net:+.2f} $")
    # 3. decision quotidienne (une fois par bougie journaliere fermee)
    jour = ind["BTC"]["jour"]
    if etat["dernier_jour"] != jour:
        for k, var in VARIANTES.items():
            v = etat["variantes"][k]; eq = equity(v, prix); budget = eq / 3
            for t in TOKENS:
                if v["attente"].get(t, 0) > 0: v["attente"][t] -= 1
                i = ind[t]; lev = levier(var, i["f"])
                if i["f"] == 0:
                    reequilibrer(v, t, 0.0, prix[t], maintenant, "prix sous les 3 moyennes (fin de tendance)", lev, 0.0, i["atr"], budget, evenements, var["nom"])
                elif v["attente"].get(t, 0) == 0:
                    reequilibrer(v, t, budget * lev * i["f"], prix[t], maintenant, phrase(i, lev), lev, i["f"], i["atr"], budget, evenements, var["nom"])
        etat["dernier_jour"] = jour
        pts = {k: round(equity(etat["variantes"][k], prix), 2) for k in VARIANTES}
        bh = CAPITAL * sum(prix[t] / etat["base_prix"][t] for t in TOKENS) / 3
        for k in VARIANTES:
            etat["variantes"][k]["historique"].append([jour, pts[k]])
            etat["variantes"][k]["historique"] = etat["variantes"][k]["historique"][-800:]
        etat["bh_historique"].append([jour, round(bh, 2)]); etat["bh_historique"] = etat["bh_historique"][-800:]
    # 4. pic / baisse maximale
    for k in VARIANTES:
        v = etat["variantes"][k]; eq = equity(v, prix)
        v["pic"] = max(v["pic"], eq); v["dd_max"] = max(v["dd_max"], 1 - eq / v["pic"])
    etat["derniere_maj"] = maintenant.isoformat()
    etat["vue"] = construire_vue(etat, donnees, ind, prix, maintenant)
    return evenements

def construire_vue(etat, donnees, ind, prix, maintenant):
    vue = {"maj": maintenant.isoformat(), "sources": {t: donnees[t]["source"] for t in TOKENS}, "parametres": {
        "capital": CAPITAL, "frais_pct": FRAIS * 100, "funding_an_pct": FUNDING_AN * 100, "stop_atr": STOP_ATR},
        "tokens": {}, "variantes": {}}
    for t in TOKENS:
        i = ind[t]
        vue["tokens"][t] = {"prix": prix[t], "cloture": i["cloture"], "m50": i["m50"], "m100": i["m100"], "m200": i["m200"],
                            "atr": i["atr"], "etage": round(i["f"] * 3), "dist_atr": round(i["dist"], 2),
                            "sortie_totale": min(i["m50"], i["m100"], i["m200"]), "jour": i["jour"]}
    bh = CAPITAL * sum(prix[t] / etat["base_prix"][t] for t in TOKENS) / 3
    vue["garder"] = {"equity": round(bh, 2), "pct": round((bh / CAPITAL - 1) * 100, 2)}
    for k, var in VARIANTES.items():
        v = etat["variantes"][k]; eq = equity(v, prix)
        expo = sum(p["unites"] * prix[t] for t, p in v["positions"].items())
        pos = []
        for t, p in v["positions"].items():
            val = p["unites"] * prix[t]; latent = p["unites"] * (prix[t] - p["prix_moyen"])
            i = ind[t]
            niveaux = sorted(m for m in (i["m50"], i["m100"], i["m200"]) if m < prix[t])
            pos.append({"token": t, "sens": "LONG", "levier": p["levier"], "etage": round(p["etape"] * 3), "valeur": round(val, 2),
                        "pct_compte": round(val / eq * 100, 1) if eq > 0 else 0, "prix_entree": p["prix_moyen"], "prix": prix[t],
                        "variation_prix_pct": round((prix[t] / p["prix_moyen"] - 1) * 100, 2), "pnl_latent_usd": round(latent, 2),
                        "pnl_net_usd": round(latent + p["realise"] - p["frais"] - p["funding"], 2),
                        "pnl_pct_compte": round((latent + p["realise"] - p["frais"] - p["funding"]) / eq * 100, 2) if eq > 0 else 0,
                        "stop_protection": p["stop"], "stop_dist_pct": round((p["stop"] / prix[t] - 1) * 100, 1),
                        "sortie_totale": vue["tokens"][t]["sortie_totale"], "sortie_dist_pct": round((vue["tokens"][t]["sortie_totale"] / prix[t] - 1) * 100, 1),
                        "reduction_prochaine": niveaux[-1] if niveaux else None, "ouverture": p["ouverture"], "raison": p["raison"],
                        "frais": round(p["frais"], 2), "funding": round(p["funding"], 2)})
        trades = v["trades"]
        gagnants = sum(1 for x in trades if x["net_usd"] > 0)
        vue["variantes"][k] = {"nom": var["nom"], "equity": round(eq, 2), "pct": round((eq / CAPITAL - 1) * 100, 2), "cash": round(v["cash"], 2),
                               "exposition": round(expo, 2), "levier_effectif": round(expo / eq, 2) if eq > 0 else 0,
                               "baisse_max_pct": round(v["dd_max"] * 100, 1), "nb_trades": len(trades), "gagnants": gagnants,
                               "frais": round(v["frais"], 2), "funding": round(v["funding"], 2), "positions": pos,
                               "attente": v["attente"], "trades_recents": trades[-15:][::-1]}
    return vue

def main():
    maintenant = datetime.now(timezone.utc)
    donnees = charger_donnees(maintenant)
    etat = charger_etat() or etat_initial(maintenant, {t: donnees[t]["prix"] for t in TOKENS})
    evenements = cycle(etat, donnees, maintenant)
    sauver_etat(etat)
    for e in evenements: print(e)
    importants = [e for e in evenements if "ajustement" not in e]
    if importants:
        notifier("Bot tendance (simulation)", "\n".join(importants))
    print(f"OK {maintenant:%Y-%m-%d %H:%M} UTC - " + " | ".join(f"{k}: {etat['vue']['variantes'][k]['pct']:+.2f} %" for k in VARIANTES))

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print("ERREUR:", e)
        sys.exit(1)
