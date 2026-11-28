# -*- coding: utf-8 -*-
"""bri v9 - verifications avant le lot 1 (lecture seule, bibliotheque standard).
Usage : py verifs_v9.py [chemin_de_la_base] [fichier_de_sortie]
Aucune ecriture dans la base (ouverture en lecture seule). Aucune donnee nominative affichee."""
import sqlite3, sys, collections, datetime, re

DB = sys.argv[1] if len(sys.argv) > 1 else r"C:\Users\BWT40FH\BITBUCKET\V6\bri\instance.db"
OUT = sys.argv[2] if len(sys.argv) > 2 else "verifs_v9.txt"
con = sqlite3.connect("file:" + DB.replace("\\", "/") + "?mode=ro", uri=True)
out = open(OUT, "w", encoding="utf-8")

def w(*a):
    out.write(" | ".join("" if x is None else str(x) for x in a) + "\n")

def titre(code, t):
    out.write("\n=== " + code + " " + t + " ===\n")

def q(sql, p=()):
    return con.execute(sql, p).fetchall()

def cols(t):
    return [r[1] for r in q("pragma table_info(" + t + ")")]

def essai(code, t, f):
    titre(code, t)
    try:
        f()
    except Exception as e:  # une verification en echec n'arrete pas les autres
        w("ERREUR", type(e).__name__, e)

AVEC = "cia_effective e join refa_applications a on a.cia=e.cia"   # applications ayant des depots
RA = cols("refa_applications")

def pct(n, d):
    return "%.1f %%" % (100.0 * n / d) if d else "n/a"

# ---------------------------------------------------------------- A
def a():
    w("base", DB); w("date", datetime.datetime.now().isoformat(timespec="seconds"))
    for name, typ in q("select name,type from sqlite_master where type in ('table','view') order by 1"):
        try:
            n = q("select count(*) from [" + name + "]")[0][0]
        except Exception as e:
            n = "erreur " + type(e).__name__
        w(typ, name, n)
essai("A", "Tables et vues, nombre de lignes", a)

# ---------------------------------------------------------------- B
def b():
    w("projets (inventaire)", q("select count(*) from projects")[0][0])
    w("depots (inventaire)", q("select count(*) from repos")[0][0])
    w("applications REFA", q("select count(*) from refa_applications")[0][0])
    w("projets rattaches", *q("select count(*),sum(n_repos) from cia_effective where length(cia)>0")[0])
    w("projets sans application", *q("select count(*),sum(n_repos) from cia_effective where cia is null or length(cia)=0")[0])
    w("codes rattaches inconnus de REFA", q("select count(distinct e.cia) from cia_effective e left join refa_applications a on a.cia=e.cia where length(e.cia)>0 and a.cia is null")[0][0])
    w("applications avec depots", q("select count(distinct a.cia) from " + AVEC)[0][0])
    for r in q("select a.status_code,count(distinct a.cia),count(*),sum(e.n_repos) from " + AVEC + " group by 1 order by 2 desc"):
        w("statut", *r)
essai("B", "Totaux de controle (statut : applications, projets, depots)", b)

# ---------------------------------------------------------------- C
def c():
    for k in ("review_state", "confidence", "source", "cia_status"):
        for r in q("select " + k + ",count(*),sum(n_repos) from cia_effective group by 1 order by 2 desc"):
            w(k, *r)
essai("C", "Rattachement : etats, confiance, origine (projets, depots)", c)

# ---------------------------------------------------------------- D
def d():
    for r in q("select a.entity,count(distinct a.cia),count(*),sum(e.n_repos) from " + AVEC + " group by 1 order by 2 desc"):
        w(*r)
essai("D", "Par entite : applications avec depots, projets, depots", d)

# ---------------------------------------------------------------- E
apps = q("select distinct a.cia,a.entity,a.prod_team,a.it_org,a.csl_org,a.bo_org from " + AVEC)
par_ent = collections.defaultdict(list)
for r in apps:
    par_ent[r[1]].append(r)
gros = [e for e, v in sorted(par_ent.items(), key=lambda kv: -len(kv[1])) if len(v) >= 10]

def fam(v, n=2):
    return "-".join(v.split("-")[:n]) if v else "(vide)"

def e_():
    w("entite", "applications", "prod_team rempli", "commence par PRD-", "1 segment", "2", "3", "4", "5+")
    for e in gros:
        v = par_ent[e]; n = len(v); rem = [x[2] for x in v if x[2]]
        seg = collections.Counter(min(len(x.split("-")), 5) for x in rem)
        w(e, n, pct(len(rem), n), pct(sum(1 for x in rem if x.upper().startswith("PRD-")), n), seg[1], seg[2], seg[3], seg[4], seg[5])
    tot = collections.Counter(fam(x[2]) for x in apps)
    w("--- familles, toutes entites ---")
    for k, n in tot.most_common(40):
        w(k, n, pct(n, len(apps)))
essai("E", "Format de prod_team par entite (>= 10 applications)", e_)

def e2():
    for e in gros[:12]:
        c2 = collections.Counter(fam(x[2]) for x in par_ent[e]); c3 = collections.Counter(fam(x[2], 3) for x in par_ent[e])
        w(e, "familles=%d" % len(c2), "domaines=%d" % len(c3), "equipes=%d" % len({x[2] for x in par_ent[e]}),
          " ; ".join("%s %d" % kv for kv in c2.most_common(8)))
essai("E2", "Familles de production des 12 plus grosses entites", e2)

# ---------------------------------------------------------------- F
def f():
    w("entite", "applications", "axe", "valeurs", "plus gros groupe", "part", "vides", "verdict")
    for e in gros:
        v = par_ent[e]; n = len(v)
        for nom, vals in (("famille", [fam(x[2]) for x in v]), ("domaine", [fam(x[2], 3) for x in v]), ("it_org", [x[3] or "(vide)" for x in v]),
                          ("csl_org", [x[4] or "(vide)" for x in v]), ("bo_org", [x[5] or "(vide)" for x in v])):
            cnt = collections.Counter(vals); vide = cnt.pop("(vide)", 0)
            if not cnt:
                w(e, n, nom, 0, 0, "n/a", vide, "vide"); continue
            top = cnt.most_common(1)[0][1]; part = top / float(n)
            verdict = "concentre" if part > 0.6 else ("fin" if n / float(len(cnt)) < 5 else "utile")
            w(e, n, nom, len(cnt), top, pct(top, n), vide, verdict)
essai("F", "Axe utile par entite (concentre > 60 %, fin < 5 applications par valeur)", f)

def f2():
    ok = sum(1 for x in apps if x[3] and re.match(r"^[A-Za-z0-9]{3}-", x[3]))
    w("it_org au format trigramme-libelle", ok, pct(ok, len(apps)))
    for e in gros[:12]:
        v = par_ent[e]; k = sum(1 for x in v if x[3] and re.match(r"^[A-Za-z0-9]{3}-", x[3]))
        w(e, len(v), pct(k, len(v)))
essai("F2", "Format de it_org", f2)

# ---------------------------------------------------------------- G
def g():
    n = q("select count(distinct a.cia) from " + AVEC)[0][0]
    liste = ["type_code", "level_code", "nature_code", "nature", "internet_exposed", "ssi_top", "dora_id", "pci", "gdpr_code", "pdds_code", "rfim", "bcbs", "sirene",
             "availability_code", "integrity_code", "confidentiality_code", "traceability_code", "sensibility_code", "drp_code",
             "management_type_code", "info_system_code", "service_class", "tech_legacy", "domain"]
    w("colonne", "applications renseignees", "part", "valeurs", "valeurs les plus frequentes (valeur=applications)")
    for k in liste:
        if k not in RA:
            w(k, "COLONNE ABSENTE"); continue
        rows = q("select a.%s,count(distinct a.cia) from %s group by 1 order by 2 desc" % (k, AVEC))
        rem = sum(r[1] for r in rows if r[0] is not None and str(r[0]) != "")
        w(k, rem, pct(rem, n), len(rows), " ; ".join("%s=%d" % ("(vide)" if r[0] is None or str(r[0]) == "" else str(r[0])[:28], r[1]) for r in rows[:7]))
essai("G", "Attributs REFA utiles aux filtres, sur les applications avec depots", g)

def g2():
    w("sous-applications (racine differente)", q("select count(distinct a.cia) from " + AVEC + " where a.root_group_id is not null and a.root_group_id<>a.group_id")[0][0])
    w("applications avec parent", q("select count(distinct a.cia) from " + AVEC + " where length(a.parent_group_id)>0")[0][0])
    w("applications sur plusieurs projets", q("select count(*) from (select cia from cia_effective where length(cia)>0 group by 1 having count(*)>1)")[0][0])
    for r in q("select case when k=1 then '1 projet' when k<=3 then '2-3' when k<=10 then '4-10' else '11+' end,count(*) from (select cia,count(*) k from cia_effective where length(cia)>0 group by 1) group by 1"):
        w("projets par application", *r)
    for r in q("select case when k=0 then '0' when k<=5 then '1-5' when k<=20 then '6-20' when k<=100 then '21-100' else '101+' end,count(*) from (select cia,sum(n_repos) k from cia_effective where length(cia)>0 group by 1) group by 1"):
        w("depots par application", *r)
essai("G2", "Structure : sous-applications, projets et depots par application", g2)

# ---------------------------------------------------------------- H
RC = cols("repos")
def h():
    for k in ("content_status", "state", "archived", "public", "is_fork", "level_reached", "nature", "lifecycle", "status_q", "files_truncated"):
        if k in RC:
            w(k, " ; ".join("%s=%d" % ("(vide)" if r[0] is None else str(r[0])[:24], r[1]) for r in q("select %s,count(*) from repos group by 1 order by 2 desc limit 12" % k)))
    for r in q("select substr(error,1,40),count(*) from repos where length(error)>0 group by 1 order by 2 desc limit 10"):
        w("erreur", *r)
essai("H", "Depots : collecte, etat, classification", h)

def h2():
    w("entite", "depots", "en erreur", "part", "archives", "publics")
    for r in q("""select a.entity,count(*),sum(case when length(r.error)>0 then 1 else 0 end),sum(case when r.archived then 1 else 0 end),sum(case when r.public then 1 else 0 end)
                  from repos r join cia_effective e on e.project_key=r.project_key join refa_applications a on a.cia=e.cia group by 1 order by 2 desc limit 25"""):
        w(r[0], r[1], r[2], pct(r[2] or 0, r[1]), r[3], r[4])
    w("depots de projets sans application", q("select count(*) from repos r join cia_effective e on e.project_key=r.project_key where e.cia is null or length(e.cia)=0")[0][0])
essai("H2", "Depots non collectes par entite (25 premieres)", h2)

# ---------------------------------------------------------------- I
def i():
    noms = [r[0] for r in q("select name from sqlite_master where type='table' order by 1")]
    for t in noms:
        if not (t.startswith("apitools_") or t.startswith("refa_") or t.startswith("repo_") or t.startswith("project_")):
            continue
        cs = cols(t); n = q("select count(*) from [" + t + "]")[0][0]
        cle = "cia" if "cia" in cs else ("project_key" if "project_key" in cs else None)
        nd = q("select count(distinct %s) from [%s]" % (cle, t))[0][0] if cle else ""
        w(t, n, (cle + " distincts=%s" % nd) if cle else "", ", ".join(cs)[:400])
essai("I", "Sources complementaires (activite, presence, preuves DevSecOps, details REFA) : lignes, couverture, colonnes", i)

out.write("\n=== FIN ===\n"); out.close(); con.close()
print("Termine :", OUT)
