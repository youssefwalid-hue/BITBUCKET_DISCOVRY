# -*- coding: utf-8 -*-
r"""sonde_apitools.py — test de lecture des façades apitools pour un code CIA témoin (bibliothèque standard, Python 3.9+, lecture seule).

Pour chaque opération d'une liste blanche FIXE de GET sans effet de bord, la sonde envoie une requête et note : code HTTP,
durée, taille, type de contenu et la FORME de la réponse (noms des champs, sans les valeurs). Rien n'est écrit ailleurs que
dans le fichier JSON de sortie ; aucun secret n'y figure ; les corps de réponse ne sont conservés que pour les façades
demandées par --contenu (par défaut : refa, données d'organisation non nominatives).

  py sonde_apitools.py --cia NXW53 --out D:\bri\in\sonde_apitools_NXW53.json
  py sonde_apitools.py --cia NXW53 --url https://<hôte apitools> --contenu refa,bitbucket --ca-bundle D:\certs\bpce.pem

Jeton : variable d'environnement APITOOLS_TOKEN (sinon saisie masquée). Jamais en ligne de commande, jamais dans la sortie.
"""
import argparse
import datetime
import getpass
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

VERSION = "1.0"
DEFAULT_URL = "https://apitools.mycloud.intrabpce.fr"
TIMEOUT = 30

# Liste blanche : (façade, chemin relatif à /api/v1, description). {cia} est remplacé ; {cia_tech}, {key} et {repo} viennent
# des réponses précédentes (enchaînement). Aucune opération d'écriture, aucun GET à effet de bord (nutriscore, reset_permission,
# licences_cleaning, applyPermissionTemplate, sync, stats…) n'est constructible ici.
TESTS = [
    ("bitbucket", "bitbucket/health", "disponibilité de la façade Bitbucket"),
    ("refa", "refa/{cia}/info", "informations complètes du CIA (CIA techniques, score DICP, entité, établissement, équipe ops, statut, parent)"),
    ("refa", "refa/{cia}/dicp", "score DICP"),
    ("refa", "refa/{cia}/cia_tech", "CIA techniques associés"),
    ("refa", "refa/privatif_etab", "table préfixe CIA (bigramme) → code contributeur (trigramme)"),
    ("refa", "refa/code_etab", "codes établissement par entité"),
    ("bitbucket", "bitbucket/projects/{cia}", "projets Bitbucket rattachés au CIA selon apitools"),
    ("bitbucket", "bitbucket/project/{key}/habilitations", "habilitations du premier projet renvoyé"),
    ("bitbucket", "bitbucket/project/{key}/repos", "dépôts du premier projet renvoyé"),
    ("bitbucket", "bitbucket/project/{key}/{repo}/forks", "forks du premier dépôt"),
    ("bitbucket", "bitbucket/project/{key}/repos/{repo}/branches?order_by=MODIFICATION&limit=1", "branche la plus récemment modifiée"),
    ("bitbucket", "bitbucket/project/{key}/cf-activity", "activité Code Frequency du projet"),
    ("bitbucket", "bitbucket/awesome-graphs-api-commits?limit=1", "Awesome Graphs : commits (1 élément)"),
    ("bitbucket", "bitbucket/awesome-graphs-api-pull-requests?limit=1", "Awesome Graphs : pull requests (1 élément)"),
    ("checkmarx", "Checkmarx/teams/{cia}", "équipe Checkmarx du CIA (SAST)"),
    ("sonarqube", "Sonarqube/projects?project={cia}", "projets SonarQube filtrés sur le CIA"),
    ("jenkins", "jenkins/folder/{cia}", "dossier Jenkins du CIA"),
    ("jenkins", "jenkins/url/{cia}", "instance Jenkins du CIA"),
    ("jenkins", "jenkins/inventory/{cia}/items", "jobs du dossier racine du CIA"),
    ("jira", "jira/projects/search?cia={cia}", "projet Jira du CIA"),
    ("xld", "xld/{cia_tech}/url", "URLs XLD du CIA technique"),
    ("xld", "xld/{cia_tech}/applications?elt_type=deployed_application&editor={editor}&page_size=1&page=1", "applications déployées XLD (1 élément)"),
    ("xld", "xld/{cia_tech}/environments?editor={editor}&page_size=1&page=1", "environnements XLD (1 élément)"),
    ("artifactory", "artifactory/health", "connectivité Artifactory"),
    ("artifactory", "artifactory/xray/index/{repo_art}", "indexation Xray du dépôt Artifactory nommé par --repo-artifactory"),
]


def shape(o, depth=0):
    """Forme d'une réponse JSON : noms de champs et types, jamais les valeurs."""
    if isinstance(o, dict):
        if depth >= 3:
            return "{…%d clés}" % len(o)
        return {k: shape(v, depth + 1) for k, v in list(o.items())[:60]}
    if isinstance(o, list):
        return ["%d éléments" % len(o), shape(o[0], depth + 1) if o else None]
    if o is None:
        return "null"
    return type(o).__name__


class Sonde(object):
    def __init__(self, base, token, ctx):
        self.base = base.rstrip("/") + "/api/v1/"
        self.token = token
        self.ctx = ctx
        self.opener = urllib.request.build_opener(_NoRedirect(), urllib.request.HTTPSHandler(context=ctx))

    def get(self, path):
        url = self.base + path
        req = urllib.request.Request(url, method="GET", headers={"Authorization": "Bearer " + self.token, "Accept": "application/json", "User-Agent": "bri-sonde/" + VERSION})
        t0 = time.time()
        res = {"url": url.replace(urllib.parse.quote(self.token), "<jeton>"), "status": None, "ms": None, "bytes": 0, "content_type": None, "shape": None, "error": None, "body": None}
        try:
            with self.opener.open(req, timeout=TIMEOUT) as r:
                data = r.read(2 * 1024 * 1024)
                res["status"] = r.status
                res["content_type"] = (r.headers.get("Content-Type") or "").split(";")[0]
        except urllib.error.HTTPError as e:
            data = e.read(64 * 1024) if hasattr(e, "read") else b""
            res["status"] = e.code
            res["content_type"] = (e.headers.get("Content-Type") or "").split(";")[0] if e.headers else None
        except Exception as e:  # noqa: BLE001 — réseau, TLS, délai
            data = b""
            res["error"] = type(e).__name__ + ": " + str(e)[:200]
        res["ms"] = int((time.time() - t0) * 1000)
        res["bytes"] = len(data)
        js = None
        if data:
            try:
                js = json.loads(data.decode("utf-8", "replace"))
                res["shape"] = shape(js)
            except ValueError:
                res["shape"] = "non JSON (%s)" % (res["content_type"] or "?")
                if res["status"] and res["status"] >= 400:
                    res["error"] = data.decode("utf-8", "replace")[:200]
        return res, js


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def main(argv=None):
    ap = argparse.ArgumentParser(description="Sonde de lecture apitools (GET sans effet de bord) pour un code CIA témoin — bibliothèque standard, rien n'est stocké hors du JSON de sortie.")
    ap.add_argument("--cia", required=True, help="code CIA témoin (ex. NXW53)")
    ap.add_argument("--url", default=os.environ.get("APITOOLS_URL") or DEFAULT_URL, help="hôte apitools (défaut : %(default)s, ou APITOOLS_URL)")
    ap.add_argument("--out", default=None, help="fichier JSON de sortie (défaut : sonde_apitools_<CIA>_<date>.json dans le dossier courant)")
    ap.add_argument("--contenu", default="refa", help="façades dont le corps JSON est conservé dans la sortie (liste séparée par des virgules, 'aucune' pour rien ; défaut : refa)")
    ap.add_argument("--repo-artifactory", default=None, help="clé d'un dépôt Artifactory pour le test Xray (sinon test sauté)")
    ap.add_argument("--ca-bundle", default=os.environ.get("BRI_CA_BUNDLE"), help="bundle de certificats PEM (sinon magasin système)")
    ap.add_argument("--insecure", action="store_true", help="ne pas vérifier le certificat TLS (déconseillé)")
    a = ap.parse_args(argv)

    token = os.environ.get("APITOOLS_TOKEN") or getpass.getpass("jeton apitools (saisie masquée) : ")
    if not token:
        print("aucun jeton : définir $env:APITOOLS_TOKEN", file=sys.stderr)
        return 2
    cia = a.cia.strip().upper()
    ctx = ssl.create_default_context(cafile=a.ca_bundle) if a.ca_bundle else ssl.create_default_context()
    if a.insecure:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    keep = set(x.strip().lower() for x in a.contenu.split(",") if x.strip()) - {"aucune", "none"}
    s = Sonde(a.url, token, ctx)
    vars_ = {"cia": cia, "editor": "BI" if cia[:2] in ("BI", "BF", "SI", "CE", "IB", "C3") else "NX", "key": None, "repo": None, "cia_tech": None, "repo_art": a.repo_artifactory}
    out = {"sonde": VERSION, "date_utc": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "hote": a.url, "cia": cia, "tests": [], "resume": {}}
    print("sonde apitools %s · hôte %s · CIA %s · %d tests (GET sans effet de bord, lecture seule)" % (VERSION, a.url, cia, len(TESTS)))
    for facade, path, desc in TESTS:
        needed = [v for v in ("key", "repo", "cia_tech", "repo_art") if "{" + v + "}" in path]
        if any(vars_.get(v) is None for v in needed):
            out["tests"].append({"facade": facade, "operation": path, "description": desc, "status": None, "skipped": "prérequis absent : " + ", ".join(v for v in needed if vars_.get(v) is None)})
            print("  %-80s  sauté (%s)" % (path, ", ".join(v for v in needed if vars_.get(v) is None)))
            continue
        p = path
        for k, v in vars_.items():
            if v is not None:
                p = p.replace("{" + k + "}", urllib.parse.quote(str(v), safe=""))
        res, js = s.get(p)
        row = {"facade": facade, "operation": path, "description": desc}
        row.update(res)
        if facade not in keep or (res["status"] or 0) >= 400:
            row.pop("body", None)
        else:
            row["body"] = js
        out["tests"].append(row)
        print("  %-80s  %s  %5d ms  %7d o" % (p[:80], res["status"] if res["status"] is not None else ("ERR " + (res["error"] or "")[:40]), res["ms"] or 0, res["bytes"]))
        # enchaînement : premier projet / dépôt / CIA technique
        if res["status"] == 200 and isinstance(js, (list, dict)):
            if path.startswith("bitbucket/projects/") and vars_["key"] is None:
                items = js if isinstance(js, list) else (js.get("values") or js.get("projects") or js.get("items") or [])
                for it in items:
                    k = it.get("key") if isinstance(it, dict) else None
                    if k:
                        vars_["key"] = k
                        break
            elif path.endswith("/repos") and vars_["repo"] is None:
                items = js if isinstance(js, list) else (js.get("values") or js.get("repositories") or js.get("items") or [])
                for it in items:
                    sl = it.get("slug") if isinstance(it, dict) else None
                    if sl:
                        vars_["repo"] = sl
                        break
            elif path.endswith("/cia_tech") and vars_["cia_tech"] is None:
                cands = js if isinstance(js, list) else (js.get("cia_tech") or js.get("values") or js.get("items") or [js])
                for it in cands:
                    v = it if isinstance(it, str) else (it.get("cia_tech") or it.get("code") if isinstance(it, dict) else None)
                    if v:
                        vars_["cia_tech"] = v
                        break
    codes = {}
    for t in out["tests"]:
        k = str(t.get("status")) if t.get("status") is not None else ("sauté" if t.get("skipped") else "erreur")
        codes[k] = codes.get(k, 0) + 1
    out["resume"] = {"par_code": codes, "cle_projet": vars_["key"], "depot": vars_["repo"], "cia_tech": vars_["cia_tech"],
                     "lisibles": [t["operation"] for t in out["tests"] if t.get("status") == 200],
                     "refusees_403": [t["operation"] for t in out["tests"] if t.get("status") == 403],
                     "absentes_404": [t["operation"] for t in out["tests"] if t.get("status") == 404]}
    path_out = a.out or ("sonde_apitools_%s_%s.json" % (cia, datetime.datetime.now().strftime("%Y%m%d-%H%M")))
    with open(path_out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print("\nrésumé : %s · projet témoin %s · dépôt %s · CIA technique %s" % (", ".join("%s × %d" % kv for kv in sorted(codes.items())), vars_["key"], vars_["repo"], vars_["cia_tech"]))
    print("sortie : %s (aucun secret ; corps conservés pour : %s)" % (path_out, ", ".join(sorted(keep)) or "aucune façade"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
