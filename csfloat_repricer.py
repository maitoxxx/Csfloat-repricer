#!/usr/bin/env python3
"""
CSFloat Repricer - ajuste automatiquement le prix de tes annonces CSFloat
à 1 % sous l'annonce "achat immédiat" la moins chère du même skin
AYANT UN FLOAT AUSSI BON OU MEILLEUR que le tien (à une petite tolérance près).

Pourquoi : un skin avec un meilleur float vaut plus cher. On ne se cale donc
jamais sur une annonce moins chère dont le float est nettement moins bon.
Si personne n'a un float aussi bon que le tien, ton prix ne bouge pas.

Installation :
    pip install requests

Utilisation :
    1. Crée une clé API sur csfloat.com (Profil > Developers).
    2. Mets-la dans une variable d'environnement :
         Windows (PowerShell) : $env:CSFLOAT_API_KEY="ta_cle"
         Mac / Linux          : export CSFLOAT_API_KEY="ta_cle"
    3. Test sans rien modifier :   python csfloat_repricer.py --dry-run
    4. Exécution réelle (1 fois) : python csfloat_repricer.py
    5. En boucle toutes les 30 min : python csfloat_repricer.py --loop 30
"""

import argparse
import os
import sys
import time

import requests

# ====================== RÉGLAGES ======================

UNDERCUT_PCT = 1.0  # prix = marché - 1 %

# FLOAT : une annonce concurrente compte si son float est meilleur que le tien,
# ou moins bon d'au plus FLOAT_TOLERANCE_PCT % (ex. ton float 0.1500 avec 5 %
# -> on compare aux annonces de float <= 0.1575).
# Mets 0 pour ne comparer qu'aux floats strictement aussi bons ou meilleurs.
FLOAT_TOLERANCE_PCT = 5.0

# Garde-fou : on ne baisse jamais une annonce de plus de X % en un seul passage
# (évite de brader un skin si quelqu'un poste une annonce à prix ridicule).
MAX_DROP_PCT_PER_RUN = 15.0

# Prix planchers optionnels, en dollars, par nom exact du skin sur le marché.
# Exemple : "AK-47 | Redline (Field-Tested)": 12.50
FLOORS_USD = {
    # "AK-47 | Redline (Field-Tested)": 12.50,
}

# Skins à ne jamais toucher (float rare, stickers chers, pattern spécial...).
EXCLUDE = {
    "MP7 | Fade (Factory New)",
}

# =====================================================

API = "https://csfloat.com/api/v1"
PAUSE_BETWEEN_CALLS = 1.5  # secondes, pour respecter les limites de l'API


def session_with_key():
    key = os.environ.get("CSFLOAT_API_KEY")
    if not key:
        sys.exit("Erreur : variable d'environnement CSFLOAT_API_KEY manquante.")
    s = requests.Session()
    s.headers.update({"Authorization": key, "Content-Type": "application/json"})
    return s


def get(s, path, **params):
    for attempt in range(3):
        r = s.get(f"{API}{path}", params=params, timeout=20)
        if r.status_code == 429:  # trop de requêtes : on attend
            time.sleep(10 * (attempt + 1))
            continue
        r.raise_for_status()
        time.sleep(PAUSE_BETWEEN_CALLS)
        return r.json()
    r.raise_for_status()


def as_list(payload):
    """L'API renvoie soit une liste, soit {"data": [...], "cursor": ...}."""
    if isinstance(payload, list):
        return payload, None
    return payload.get("data", []), payload.get("cursor")


def my_steam_id(s):
    me = get(s, "/me")
    user = me.get("user", me)
    return str(user["steam_id"])


def my_listings(s, steam_id):
    listings, cursor = [], None
    while True:
        params = {"limit": 40}
        if cursor:
            params["cursor"] = cursor
        page, cursor = as_list(get(s, f"/users/{steam_id}/stall", **params))
        listings.extend(page)
        if not cursor or not page:
            return listings


def float_cap(my_float):
    """Float maximum qu'une annonce concurrente peut avoir pour être comparable."""
    return my_float * (1 + FLOAT_TOLERANCE_PCT / 100)


def lowest_market_price(s, market_hash_name, my_id, my_float, my_listing_ids=frozenset()):
    """
    Annonce achat immédiat la moins chère du même skin, hors les miennes,
    avec un float <= float_cap(my_float). Renvoie (prix en cents, float) ou None.
    Si le skin n'a pas de float (sticker, caisse...), compare juste par nom.
    """
    params = dict(
        market_hash_name=market_hash_name,
        sort_by="lowest_price",
        type="buy_now",
        limit=40,
    )
    cap = None
    if my_float is not None:
        cap = float_cap(my_float)
        params["max_float"] = f"{cap:.6f}"

    page, _ = as_list(get(s, "/listings", **params))
    for l in page:
        # On ignore ses propres annonces : par leur id (fiable) ET par vendeur
        if str(l.get("id")) in my_listing_ids:
            continue
        asset = (l.get("item") or {}).get("asset_id")
        if asset and "asset:" + str(asset) in my_listing_ids:
            continue
        seller = str((l.get("seller") or {}).get("steam_id", ""))
        if seller and seller == my_id:
            continue
        f = (l.get("item") or {}).get("float_value")
        # Double vérification côté script, au cas où le filtre de l'API serait ignoré
        if cap is not None and (f is None or float(f) > cap):
            continue
        return int(l["price"]), (float(f) if f is not None else None)
    return None


def fmt_float(f):
    return f"{f:.6f}" if f is not None else "-"


def update_price(s, listing_id, new_price_cents):
    r = s.patch(f"{API}/listings/{listing_id}", json={"price": new_price_cents}, timeout=20)
    r.raise_for_status()
    time.sleep(PAUSE_BETWEEN_CALLS)


def usd(cents):
    return f"${cents / 100:,.2f}"


def run_once(s, dry_run):
    my_id = my_steam_id(s)
    all_mine = my_listings(s, my_id)
    # ids des annonces ET des objets (asset_id) : double protection anti auto-concurrence
    my_listing_ids = frozenset(str(l.get("id")) for l in all_mine) | frozenset(
        "asset:" + str((l.get("item") or {}).get("asset_id")) for l in all_mine
        if (l.get("item") or {}).get("asset_id"))
    listings = [l for l in all_mine if l.get("type") == "buy_now"]
    print(f"\n{time.strftime('%Y-%m-%d %H:%M:%S')} - {len(listings)} annonce(s) achat immédiat")

    for l in listings:
        name = l["item"]["market_hash_name"]
        current = int(l["price"])
        raw_float = l["item"].get("float_value")
        my_float = float(raw_float) if raw_float is not None else None
        label = f"{name} (float {fmt_float(my_float)})"

        if name in EXCLUDE:
            print(f"  [ignoré]  {label}")
            continue

        found = lowest_market_price(s, name, my_id, my_float, my_listing_ids)
        if found is None:
            print(f"  [meilleur] {label} : aucune annonce avec un float aussi bon, "
                  f"prix inchangé ({usd(current)})")
            continue
        market, market_float = found
        ref = f"marché {usd(market)}, float {fmt_float(market_float)}"

        target = int(market * (1 - UNDERCUT_PCT / 100))

        # HAUSSE : on ne monte que si l'annonce de référence a un float SIMILAIRE
        # (écart <= FLOAT_TOLERANCE_PCT %). Une annonce chère avec un bien meilleur
        # float ne prouve pas que ton skin vaut ce prix-là -> prix inchangé.
        if (target > current and my_float is not None and market_float is not None
                and abs(market_float - my_float) > my_float * FLOAT_TOLERANCE_PCT / 100):
            print(f"  [=]       {label} : {usd(current)} inchangé "
                  f"(seule réf. = float bien meilleur : {ref})")
            continue

        # Garde-fous
        floor = int(FLOORS_USD.get(name, 0) * 100)
        max_drop = int(current * (1 - MAX_DROP_PCT_PER_RUN / 100))
        notes = []
        if target < floor:
            target = floor
            notes.append("plancher")
        if target < max_drop:
            target = max_drop
            notes.append(f"baisse limitée à {MAX_DROP_PCT_PER_RUN:g} %")

        if target == current:
            print(f"  [ok]      {label} : {usd(current)} ({ref})")
            continue

        arrow = "↑" if target > current else "↓"
        extra = f" [{', '.join(notes)}]" if notes else ""
        print(f"  [{arrow}]       {label} : {usd(current)} -> {usd(target)} ({ref}){extra}")

        if not dry_run:
            try:
                update_price(s, l["id"], target)
            except requests.HTTPError as e:
                print(f"            échec : {e.response.status_code} {e.response.text[:200]}")


def main():
    p = argparse.ArgumentParser(description="Repricer CSFloat")
    p.add_argument("--dry-run", action="store_true", help="affiche les changements sans les appliquer")
    p.add_argument("--loop", type=int, metavar="MIN", help="relance toutes les MIN minutes")
    args = p.parse_args()

    s = session_with_key()
    if args.dry_run:
        print("Mode test : aucun prix ne sera modifié.")

    while True:
        try:
            run_once(s, args.dry_run)
        except requests.RequestException as e:
            print(f"Erreur réseau/API : {e}")
        if not args.loop:
            break
        time.sleep(args.loop * 60)


if __name__ == "__main__":
    main()
