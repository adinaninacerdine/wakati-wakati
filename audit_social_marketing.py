"""
AUDIT SOCIAL MARKETING — Huri Money SA / WAKATI
================================================

Objectif : verifier via XML-RPC si le module Social Marketing d'Odoo est
installe et exploitable sur hurimoney2.odoo.com, et extraire toutes les
metriques disponibles (comptes connectes, audience, posts, engagement,
clics UTM) pour alimenter le rapport de communication digitale.

Le script est 100% LECTURE SEULE. Aucune ecriture n'est effectuee.

Usage :
    python audit_social_marketing.py
    python audit_social_marketing.py --depuis 2026-07-06
    python audit_social_marketing.py --depuis 2026-07-06 --sortie C:\\temp\\audit

Sorties :
    audit_social_YYYYMMDD.txt    rapport lisible
    social_accounts.csv          comptes sociaux + audience
    social_posts.csv             posts + engagement
    social_live_posts.csv        publications par compte
    utm_campaigns.csv            campagnes UTM
    link_tracker.csv             liens traces + clics
"""

import xmlrpc.client
import re
import argparse
import csv
import os
import sys
import time
from datetime import datetime

# ============================================================
# CONNEXION
# ============================================================
ODOO_URL = 'https://hurimoney2.odoo.com'
ODOO_DB = 'hurimoney2'
ODOO_USER = 'nacer.adinani@gmail.com'
ODOO_PASS = 'ac5e68b02aa8c2f65426946cde8fde3be7c00d9c'   # <-- coller la cle API hurimoney2

parser = argparse.ArgumentParser(description="Audit Social Marketing WAKATI")
parser.add_argument('--depuis', type=str, default='2026-06-01',
                    help='Date de debut de la periode analysee (YYYY-MM-DD)')
parser.add_argument('--sortie', type=str, default='.',
                    help='Repertoire de sortie des fichiers')
parser.add_argument('--url', type=str, default=ODOO_URL)
parser.add_argument('--db', type=str, default=ODOO_DB)
args = parser.parse_args()

OUT = args.sortie
os.makedirs(OUT, exist_ok=True)

common = xmlrpc.client.ServerProxy(f'{args.url}/xmlrpc/2/common')
uid = common.authenticate(args.db, ODOO_USER, ODOO_PASS, {})
if not uid:
    print("ECHEC AUTHENTIFICATION — verifier la cle API et le nom de base.")
    sys.exit(1)
models = xmlrpc.client.ServerProxy(f'{args.url}/xmlrpc/2/object', allow_none=True)


def odoo(model, method, *a, **kw):
    """Wrapper XML-RPC : retry sur 429/timeout, et auto-correction si un champ
    demande n'existe pas dans cette version d'Odoo (Invalid field)."""
    for attempt in range(4):
        try:
            return models.execute_kw(args.db, uid, ODOO_PASS, model, method, *a, **kw)
        except Exception as e:
            msg = str(e)
            m = re.search(r"Invalid field '([^']+)'", msg)
            if m and a and isinstance(a[-1], dict) and 'fields' in a[-1]:
                mauvais = m.group(1)
                champs = [c for c in a[-1]['fields'] if c != mauvais]
                if champs and champs != a[-1]['fields']:
                    print(f"    [info] champ '{mauvais}' absent sur {model}, ignore")
                    a = list(a)
                    a[-1] = dict(a[-1], fields=champs)
                    a = tuple(a)
                    continue
            if '429' in msg:
                print("    [rate limit] pause 30s...")
                time.sleep(30)
            elif attempt < 3 and ('timeout' in msg.lower() or 'recv' in msg.lower()):
                time.sleep(5)
            else:
                raise


LOG = []


def log(txt=''):
    print(txt)
    LOG.append(txt)


def model_exists(model):
    """Retourne True si le modele est present ET accessible."""
    try:
        res = odoo('ir.model', 'search_count', [[['model', '=', model]]])
        return bool(res)
    except Exception:
        return False


def champs_dispos(model, souhaites):
    """Filtre une liste de champs sur ceux reellement presents dans la base."""
    try:
        f = odoo(model, 'fields_get', [], {'attributes': ['string']})
        return [c for c in souhaites if c in f], f
    except Exception:
        return [], {}


def export_csv(nom, lignes, entetes):
    if not lignes:
        return None
    path = os.path.join(OUT, nom)
    with open(path, 'w', newline='', encoding='utf-8-sig') as fh:
        w = csv.DictWriter(fh, fieldnames=entetes, extrasaction='ignore')
        w.writeheader()
        for l in lignes:
            w.writerow({k: (v[1] if isinstance(v, list) and len(v) == 2 else v)
                        for k, v in l.items()})
    return path


def plat(v):
    """Aplatit un many2one [id, nom] en nom."""
    if isinstance(v, list) and len(v) == 2:
        return v[1]
    return v


log("=" * 68)
log("  AUDIT SOCIAL MARKETING — WAKATI / Huri Money SA")
log(f"  Instance : {args.url}  |  UID={uid}")
log(f"  Periode  : depuis {args.depuis}")
log(f"  Execute  : {datetime.now().strftime('%d/%m/%Y %H:%M')}")
log("=" * 68)

# ============================================================
# 1. MODULES SOCIAL INSTALLES
# ============================================================
log("\n[1] MODULES SOCIAL / MARKETING\n")

mods = odoo('ir.module.module', 'search_read',
            [['|', '|', ['name', 'like', 'social'],
              ['name', 'like', 'utm'], ['name', 'like', 'link_tracker']]],
            {'fields': ['name', 'shortdesc', 'state', 'application'],
             'order': 'name'})

installes = [m for m in mods if m['state'] == 'installed']
dispo = [m for m in mods if m['state'] != 'installed']

if installes:
    for m in installes:
        log(f"  [INSTALLE]     {m['name']:32s} {m['shortdesc'][:40]}")
else:
    log("  AUCUN module social installe.")

log("")
for m in dispo[:20]:
    log(f"  [{m['state']:12s}] {m['name']:32s} {m['shortdesc'][:40]}")

SOCIAL_OK = any(m['name'] == 'social' for m in installes)
log(f"\n  >>> Module 'social' (Social Marketing) : "
    f"{'DISPONIBLE ET INSTALLE' if SOCIAL_OK else 'NON INSTALLE'}")

# ============================================================
# 2. MEDIAS ET COMPTES CONNECTES
# ============================================================
log("\n[2] MEDIAS ET COMPTES SOCIAUX CONNECTES\n")

comptes_lignes = []

if model_exists('social.media'):
    chm, _ = champs_dispos('social.media', [
        'name', 'media_type', 'has_account', 'csrf_token', 'max_post_length'])
    if 'name' not in chm:
        chm = ['name']
    medias = odoo('social.media', 'search_read', [[]], {'fields': chm})
    for m in medias:
        extra = ''
        if 'has_account' in m:
            extra = f" | compte lie : {m.get('has_account')}"
        log(f"  Media : {str(m.get('name')):22s} | type : "
            f"{str(m.get('media_type', 'n/d')):14s}{extra}")

    cha, _ = champs_dispos('social.account', [
        'name', 'media_id', 'media_type', 'audience', 'audience_trend',
        'is_media_disconnected', 'engagement', 'engagement_trend',
        'stats_link', 'active', 'has_account_stats', 'company_id'])
    if not cha:
        cha = ['name']
    comptes = odoo('social.account', 'search_read', [[]],
                   {'fields': cha, 'order': 'id'})
    log("")
    if comptes:
        for c in comptes:
            log(f"  COMPTE : {str(c.get('name')):28s} | "
                f"{str(plat(c.get('media_id')) or c.get('media_type') or '-'):14s} "
                f"| audience={c.get('audience', 'n/d')} "
                f"| engagement={c.get('engagement', 'n/d')} "
                f"| deconnecte={c.get('is_media_disconnected', 'n/d')}")
        comptes_lignes = comptes
        p = export_csv('social_accounts.csv', comptes, cha)
        log(f"\n  -> {p}")
    else:
        log("  AUCUN compte social n'est connecte dans Odoo.")
        log("  Le module est installe mais aucune page/compte n'a ete relie :")
        log("  Marketing Social > Configuration > Comptes sociaux > Ajouter.")
else:
    log("  Modele social.media INEXISTANT -> le module n'est pas installe.")
    log("  Odoo ne contient donc AUCUNE metrique reseaux sociaux.")

# ============================================================
# 3. POSTS ET ENGAGEMENT
# ============================================================
log("\n[3] PUBLICATIONS (social.post)\n")

if model_exists('social.post'):
    ch, _ = champs_dispos('social.post', [
        'message', 'state', 'post_method', 'scheduled_date', 'create_date',
        'engagement', 'account_ids', 'media_ids', 'utm_campaign_id',
        'click_count', 'source_id', 'is_content_scheduled'])
    posts = odoo('social.post', 'search_read',
                 [[['create_date', '>=', args.depuis]]],
                 {'fields': ch, 'order': 'create_date desc', 'limit': 500})
    log(f"  {len(posts)} publications depuis {args.depuis}")

    total_eng = sum(p.get('engagement') or 0 for p in posts)
    total_clic = sum(p.get('click_count') or 0 for p in posts)
    par_etat = {}
    for p in posts:
        par_etat[p.get('state')] = par_etat.get(p.get('state'), 0) + 1

    log(f"  Engagement cumule : {total_eng}")
    log(f"  Clics cumules     : {total_clic}")
    for k, v in par_etat.items():
        log(f"  Etat '{k}' : {v}")

    log("")
    for p in posts[:15]:
        msg = (p.get('message') or '').replace('\n', ' ')[:52]
        log(f"  {str(p.get('scheduled_date') or p.get('create_date'))[:10]} | "
            f"eng={str(p.get('engagement') or 0):>5s} | {msg}")

    if posts:
        log(f"\n  -> {export_csv('social_posts.csv', posts, ch)}")

    # Detail par compte
    if model_exists('social.live.post'):
        ch2, _ = champs_dispos('social.live.post', [
            'post_id', 'account_id', 'state', 'engagement',
            'live_post_link', 'create_date', 'failure_reason'])
        lives = odoo('social.live.post', 'search_read',
                     [[['create_date', '>=', args.depuis]]],
                     {'fields': ch2, 'order': 'create_date desc', 'limit': 1000})
        log(f"\n  {len(lives)} publications reelles (social.live.post)")
        par_compte = {}
        for l in lives:
            k = plat(l.get('account_id')) or 'n/d'
            d = par_compte.setdefault(k, {'nb': 0, 'eng': 0, 'ko': 0})
            d['nb'] += 1
            d['eng'] += l.get('engagement') or 0
            if l.get('state') == 'failed':
                d['ko'] += 1
        for k, d in par_compte.items():
            moy = round(d['eng'] / d['nb'], 1) if d['nb'] else 0
            log(f"    {k:28s} | {d['nb']:3d} posts | eng total {d['eng']:5d} "
                f"| moy {moy:6.1f} | echecs {d['ko']}")
        if lives:
            log(f"\n  -> {export_csv('social_live_posts.csv', lives, ch2)}")
else:
    log("  Modele social.post INEXISTANT.")

# ============================================================
# 4. FLUX ENTRANTS (mentions, commentaires, messages)
# ============================================================
log("\n[4] FLUX ENTRANTS (social.stream.post)\n")

if model_exists('social.stream.post'):
    ch, _ = champs_dispos('social.stream.post', [
        'message', 'author_name', 'published_date', 'stream_id',
        'account_id', 'link_url'])
    streams = odoo('social.stream.post', 'search_count',
                   [[['published_date', '>=', args.depuis]]])
    log(f"  {streams} messages entrants captures depuis {args.depuis}")
    if streams:
        ech = odoo('social.stream.post', 'search_read',
                   [[['published_date', '>=', args.depuis]]],
                   {'fields': ch, 'limit': 10, 'order': 'published_date desc'})
        for s in ech:
            log(f"    {str(s.get('published_date'))[:10]} | "
                f"{str(s.get('author_name'))[:18]:18s} | "
                f"{(s.get('message') or '')[:50]}")
    else:
        log("  Aucun flux entrant -> le module de veille n'est pas alimente.")
        log("  Consequence : impossible de mesurer les sollicitations client")
        log("  (commentaires, DM) traitees via les reseaux sociaux.")
else:
    log("  Modele social.stream.post INEXISTANT.")

# ============================================================
# 5. UTM / TRACABILITE DES LIENS
# ============================================================
log("\n[5] CAMPAGNES UTM ET TRACABILITE DES LIENS\n")

if model_exists('utm.campaign'):
    ch, _ = champs_dispos('utm.campaign', ['name', 'user_id', 'stage_id',
                                           'create_date', 'active'])
    camps = odoo('utm.campaign', 'search_read', [[]],
                 {'fields': ch, 'order': 'create_date desc', 'limit': 100})
    log(f"  {len(camps)} campagnes UTM")
    for c in camps[:15]:
        log(f"    {str(c.get('create_date'))[:10]} | {c.get('name')}")
    if camps:
        log(f"  -> {export_csv('utm_campaigns.csv', camps, ch)}")
else:
    log("  utm.campaign indisponible.")

if model_exists('link.tracker'):
    ch, _ = champs_dispos('link.tracker', [
        'url', 'short_url', 'title', 'count', 'campaign_id',
        'source_id', 'medium_id', 'create_date'])
    liens = odoo('link.tracker', 'search_read', [[]],
                 {'fields': ch, 'order': 'count desc', 'limit': 200})
    log(f"\n  {len(liens)} liens traces | clics cumules : "
        f"{sum(l.get('count') or 0 for l in liens)}")
    for l in liens[:15]:
        log(f"    {str(l.get('count') or 0):>6s} clics | "
            f"{str(plat(l.get('campaign_id')) or '-')[:18]:18s} | "
            f"{(l.get('url') or '')[:44]}")
    if liens:
        log(f"  -> {export_csv('link_tracker.csv', liens, ch)}")
    if not liens:
        log("  AUCUN lien trace : les posts renvoient vers des URL brutes,")
        log("  aucune attribution possible entre publication et inscription.")
else:
    log("  link.tracker indisponible.")

# ============================================================
# 6. SYNTHESE / VERDICT
# ============================================================
log("\n" + "=" * 68)
log("  VERDICT")
log("=" * 68)

if not SOCIAL_OK:
    log("""
  Le module Social Marketing n'est PAS installe sur cette instance.
  Aucune metrique reseaux sociaux n'est donc disponible dans l'ERP.

  Options :
   A. Installer 'social' + connecteurs (Facebook Page, Instagram,
      LinkedIn, X) — necessite une edition Odoo qui l'inclut, et une
      page Facebook/compte Instagram Business relie a un compte Meta
      dont on detient les droits d'administration.
   B. Si l'edition ne le permet pas : reporting hybride —
      export mensuel Meta Business Suite + TikTok Analytics, injecte
      dans un classeur de suivi normalise, et tracabilite via
      link.tracker (UTM) pour attribuer les inscriptions aux posts.
""")
else:
    log("""
  Le module Social Marketing est installe. Verifier ci-dessus :
   - le nombre de comptes reellement CONNECTES (audience > 0),
   - l'anciennete de la connexion (Odoo ne recupere l'historique
     qu'a partir de la date de connexion du compte),
   - la presence de social.live.post (preuve que les posts ont bien
     ete publies DEPUIS Odoo et non directement depuis Meta).

  Si les posts ont ete publies hors Odoo, l'engagement ne remontera
  pas : il faudra republier depuis Odoo a partir de maintenant.
""")

log("""
  A REALISER DANS TOUS LES CAS :
   1. Systematiser les liens UTM (link.tracker) dans chaque post
      renvoyant vers wakati.km / le lien de telechargement.
   2. Creer une campagne UTM par thematique (KYC, Hotline, Tutoriel)
      pour mesurer la contribution reelle de chaque post au volume
      d'inscriptions et de sollicitations support.
   3. Rendre obligatoire le renseignement des colonnes de suivi
      (date reelle de publication, lien, portee, engagement) par le
      prestataire, sous forme de livrable contractuel mensuel.
""")

# ============================================================
# ECRITURE DU RAPPORT
# ============================================================
rap = os.path.join(OUT, f"audit_social_{datetime.now().strftime('%Y%m%d')}.txt")
with open(rap, 'w', encoding='utf-8') as fh:
    fh.write('\n'.join(LOG))
print(f"\nRapport ecrit : {rap}")
