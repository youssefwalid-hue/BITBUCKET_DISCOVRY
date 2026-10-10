#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
bitbucket_discovery.py — Découverte des capacités et inventaire Bitbucket Data Center (lecture seule)
Version 2.0 — bibliothèque standard uniquement (urllib), exports CSV, rapport HTML avec tableau de bord.

Référence : Spécification & cadrage v1.3 — Bitbucket Repository Intelligence
  * III.1  séquence de démarrage et registre de capacités
  * III.5  états de collecte (AVAILABLE, EMPTY, PARTIAL, UNAUTHENTICATED, FORBIDDEN, UNSUPPORTED, ERROR, NOT_REQUESTED)
  * III.6  catalogue initial des collectes API (API-01 à API-25)
  * III.8  sécurité et données personnelles (mode non nominatif)
  * V.1    volumétrie et hypothèses de dimensionnement ; D-06 dénominateur

Ce que fait le script
  1. lit application-properties (API-01) et compare la version observée à la version déclarée ;
  2. résout le périmètre pilote (clés de projet ou filtre sur le nom) et inventorie tous ses dépôts ;
  3. choisit un échantillon (dépôt, commit, PR, fichier, groupe, rapport Code Insights) ;
  4. sonde chaque ligne du catalogue API-01 à API-25 par des appels GET minimaux ;
  5. mesure la volumétrie : dépôts par projet, puis branches / tags / commits / PR / dernier commit par dépôt (échantillon ou tous) ;
  6. écrit : JSON complet, cinq CSV (projets, dépôts, capacités, volumétrie, appels), rapport Markdown et rapport HTML autonome.

Ce que le script ne fait jamais
  * aucune méthode HTTP autre que GET : le client ne sait construire que des requêtes GET ;
  * aucun stockage de contenu de fichier, de diff, d'identité ou de payload : seuls les noms de champs, les comptages,
    les statuts HTTP et les identifiants techniques (clé de projet, slug, SHA, numéro de PR) sont conservés ;
  * aucun secret dans les sorties ni dans les journaux.

Prérequis : Python 3.8 ou plus. Aucune dépendance externe.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import html
import json
import logging
import os
import re
import socket
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

TOOL_VERSION = "2.0.0"
RULES_VERSION = "catalogue v1.3 — API-01..API-25"
STATUSES = ("AVAILABLE", "EMPTY", "PARTIAL", "UNAUTHENTICATED", "FORBIDDEN", "UNSUPPORTED", "ERROR", "NOT_REQUESTED")

A = "/rest/api/latest"
BP = "/rest/branch-permissions/latest"
DR = "/rest/default-reviewers/latest"
RB = "/rest/required-builds/latest"
INS = "/rest/insights/latest"

log = logging.getLogger("discovery")


# ============================================================================================
# Client HTTP strictement GET (urllib)
# ============================================================================================
class ProbeResult:
    def __init__(self) -> None:
        self.status_code: Optional[int] = None
        self.json: Any = None
        self.is_json: bool = False
        self.bytes_read: int = 0
        self.content_type: str = ""
        self.duration_ms: int = 0
        self.attempts: int = 0
        self.error: Optional[str] = None
        self.retry_after_seen: bool = False


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Les redirections ne sont jamais suivies : un 3xx est rapporté tel quel (SSO, proxy, URL de base)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: N802
        return None


class ReadOnlyClient:
    """Le seul verbe disponible est GET. Aucune méthode post/put/delete/patch n'existe dans cette classe."""

    def __init__(self, base_url: str, token: Optional[str], basic: Optional[Tuple[str, str]], ca_bundle: Optional[str],
                 insecure: bool, no_proxy: bool, timeout: int, delay: float, max_retries: int, call_budget: int, dry_run: bool) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.delay = delay
        self.max_retries = max_retries
        self.call_budget = call_budget
        self.dry_run = dry_run
        self.calls: List[Dict[str, Any]] = []
        self.budget_exhausted = False
        self.headers = {"Accept": "application/json", "User-Agent": "bitbucket-discovery/%s (read-only; GET only)" % TOOL_VERSION,
                        "X-Atlassian-Token": "no-check"}
        if token:
            self.headers["Authorization"] = "Bearer " + token
        elif basic:
            import base64
            self.headers["Authorization"] = "Basic " + base64.b64encode(("%s:%s" % basic).encode("utf-8")).decode("ascii")
        if insecure:
            ctx = ssl._create_unverified_context()  # noqa: SLF001 — demandé explicitement par --insecure
        else:
            ctx = ssl.create_default_context(cafile=ca_bundle) if ca_bundle else ssl.create_default_context()
        handlers: List[Any] = [_NoRedirect(), urllib.request.HTTPSHandler(context=ctx)]
        if no_proxy:
            handlers.append(urllib.request.ProxyHandler({}))
        self.opener = urllib.request.build_opener(*handlers)

    def get(self, path: str, params: Optional[Dict[str, Any]] = None, raw: bool = False, max_bytes: int = 65536) -> ProbeResult:
        res = ProbeResult()
        if self.budget_exhausted or len(self.calls) >= self.call_budget:
            self.budget_exhausted = True
            res.error = "budget d'appels épuisé (%d)" % self.call_budget
            return res
        url = self.base_url + path
        if params:
            url += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        if self.dry_run:
            self.calls.append({"ts": _now(), "method": "GET", "path": path, "status": None, "ms": 0, "attempt": 0, "dry_run": True})
            res.error = "dry-run"
            return res
        backoff = 1.0
        for attempt in range(1, self.max_retries + 2):
            res.attempts = attempt
            t0 = time.time()
            req = urllib.request.Request(url, headers=self.headers, method="GET")
            assert req.get_method() == "GET"
            code: Optional[int] = None
            body = b""
            ctype = ""
            retry_after = None
            try:
                resp = self.opener.open(req, timeout=self.timeout)
                try:
                    code = resp.getcode()
                    ctype = resp.headers.get("Content-Type", "") or ""
                    body = resp.read(max_bytes)
                finally:
                    resp.close()
            except urllib.error.HTTPError as e:
                code = e.code
                ctype = (e.headers.get("Content-Type", "") if e.headers else "") or ""
                retry_after = e.headers.get("Retry-After") if e.headers else None
                try:
                    body = e.read(max_bytes)
                except Exception:  # noqa: BLE001
                    body = b""
                finally:
                    try:
                        e.close()
                    except Exception:  # noqa: BLE001
                        pass
            except urllib.error.URLError as e:
                reason = e.reason
                if isinstance(reason, ssl.SSLError) or "CERTIFICATE" in str(reason).upper():
                    res.error = "erreur TLS : %s" % _short(str(reason))
                    self._record(path, None, t0, attempt)
                    return res
                res.error = "erreur de connexion : %s" % _short(str(reason))
                self._record(path, None, t0, attempt)
                if attempt <= self.max_retries:
                    time.sleep(backoff); backoff = min(backoff * 2, 30)
                    continue
                return res
            except (socket.timeout, TimeoutError):
                res.error = "délai dépassé (%ds)" % self.timeout
                self._record(path, None, t0, attempt)
                if attempt <= self.max_retries:
                    time.sleep(backoff); backoff = min(backoff * 2, 30)
                    continue
                return res
            except Exception as e:  # noqa: BLE001
                res.error = "erreur : %s" % _short(str(e))
                self._record(path, None, t0, attempt)
                return res
            res.status_code = code
            res.content_type = ctype
            if code == 429 or (code is not None and 500 <= code < 600):
                wait = backoff
                if retry_after:
                    res.retry_after_seen = True
                    try:
                        wait = min(float(retry_after), 60.0)
                    except ValueError:
                        pass
                self._record(path, code, t0, attempt)
                if attempt <= self.max_retries:
                    log.warning("HTTP %s sur %s — reprise dans %.1fs (tentative %d)", code, path, wait, attempt)
                    time.sleep(wait); backoff = min(backoff * 2, 30)
                    continue
                res.error = "HTTP %s persistant après %d tentatives" % (code, attempt)
                return res
            res.bytes_read = len(body)
            res.duration_ms = int((time.time() - t0) * 1000)
            self._record(path, code, t0, attempt)
            if not raw and "json" in ctype.lower() and body:
                try:
                    res.json = json.loads(body.decode("utf-8", errors="replace"))
                    res.is_json = True
                except ValueError:
                    res.json = None
            if self.delay:
                time.sleep(self.delay)
            return res
        return res

    def _record(self, path: str, status: Optional[int], t0: float, attempt: int) -> None:
        self.calls.append({"ts": _now(), "method": "GET", "path": path, "status": status, "ms": int((time.time() - t0) * 1000), "attempt": attempt})


# ============================================================================================
# Utilitaires
# ============================================================================================
def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _short(s: str, n: int = 160) -> str:
    s = re.sub(r"\s+", " ", s)
    return s if len(s) <= n else s[:n] + "…"


def ok(res: ProbeResult) -> bool:
    return bool(res.status_code and 200 <= res.status_code < 300 and isinstance(res.json, dict))


def keys_observed(obj: Any, depth: int = 1) -> List[str]:
    out: List[str] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.append(k)
            if depth > 0 and isinstance(v, dict):
                out.extend("%s.%s" % (k, sk) for sk in v.keys())
    return sorted(out)


def classify(res: ProbeResult) -> Tuple[str, str, Dict[str, Any]]:
    d: Dict[str, Any] = {"response_shape": None, "records_first_page": None, "pagination": None, "keys_observed": []}
    if res.error == "dry-run":
        return "NOT_REQUESTED", "dry-run : appel non exécuté", d
    if res.error and res.error.startswith("budget d'appels épuisé"):
        return "NOT_REQUESTED", res.error + " : appel non exécuté, relancer avec --max-calls plus élevé", d
    if res.error and res.status_code is None:
        return "ERROR", res.error, d
    sc = res.status_code or 0
    if sc == 401:
        return "UNAUTHENTICATED", "HTTP 401 : authentification absente, invalide ou expirée", d
    if sc == 403:
        return "FORBIDDEN", "HTTP 403 : accès refusé pour ce compte sur cette ressource", d
    if sc == 404:
        return "ERROR", "HTTP 404 : ressource inexistante, masquée ou route indisponible — ne vaut pas UNSUPPORTED, à vérifier contre la documentation 9.4", d
    if sc in (405, 410, 501):
        return "UNSUPPORTED", "HTTP %d : méthode ou fonctionnalité non prise en charge sur cette instance" % sc, d
    if 300 <= sc < 400:
        return "ERROR", "HTTP %d : redirection inattendue (SSO, proxy ou URL de base incorrecte)" % sc, d
    if sc == 429 or sc >= 500 or res.error:
        return "ERROR", res.error or ("HTTP %d" % sc), d
    if 200 <= sc < 300:
        j = res.json
        if isinstance(j, dict) and "values" in j and ("isLastPage" in j or "size" in j):
            vals = j.get("values") or []
            d["response_shape"] = "paged"
            d["records_first_page"] = len(vals)
            d["pagination"] = {"size": j.get("size"), "limit": j.get("limit"), "isLastPage": j.get("isLastPage"), "nextPageStart": j.get("nextPageStart"), "start": j.get("start")}
            d["keys_observed"] = keys_observed(vals[0]) if vals and isinstance(vals[0], dict) else []
            return ("EMPTY" if not vals else "AVAILABLE"), ("page vide" if not vals else "première page lue"), d
        if res.is_json:
            d["response_shape"] = "unit" if isinstance(j, dict) else "list"
            if isinstance(j, list):
                d["records_first_page"] = len(j)
                d["keys_observed"] = keys_observed(j[0]) if j and isinstance(j[0], dict) else []
                return ("EMPTY" if not j else "AVAILABLE"), "liste JSON", d
            d["keys_observed"] = keys_observed(j)
            return ("EMPTY" if not j else "AVAILABLE"), "réponse unitaire JSON", d
        d["response_shape"] = "raw"
        return ("AVAILABLE" if res.bytes_read > 0 else "EMPTY"), "contenu brut (%d octets lus, non conservés)" % res.bytes_read, d
    return "ERROR", "HTTP %d non interprété" % sc, d


def aggregate(statuses: List[str]) -> str:
    if not statuses or all(s == "NOT_REQUESTED" for s in statuses):
        return "NOT_REQUESTED"
    real = [s for s in statuses if s != "NOT_REQUESTED"]
    for s in ("UNAUTHENTICATED", "FORBIDDEN", "UNSUPPORTED", "ERROR"):
        if s in real:
            return s if all(x == s for x in real) else "PARTIAL"
    if all(s == "EMPTY" for s in real):
        return "EMPTY"
    if all(s in ("AVAILABLE", "EMPTY") for s in real):
        return "AVAILABLE" if len(real) == len(statuses) else "PARTIAL"
    return "PARTIAL"


# ============================================================================================
# Catalogue (III.6) — une ligne = une ou plusieurs sondes GET
# ============================================================================================
def catalogue() -> List[Dict[str, Any]]:
    P = A + "/projects/{project}"
    R = P + "/repos/{repo}"
    return [
        {"id": "API-01", "purpose": "Version et build de l'instance", "priority": "P0", "admin": False, "probes": [{"name": "application-properties", "path": A + "/application-properties", "needs": []}]},
        {"id": "API-02", "purpose": "Projets accessibles", "priority": "P0", "admin": False, "probes": [{"name": "projects", "path": A + "/projects", "params": {"limit": 1}, "needs": []}]},
        {"id": "API-03", "purpose": "Repositories d'un projet", "priority": "P0", "admin": False, "probes": [{"name": "repos", "path": P + "/repos", "params": {"limit": 1}, "needs": ["project"]}]},
        {"id": "API-04", "purpose": "Identité, état et métadonnées du repository", "priority": "P0", "admin": False, "probes": [{"name": "repository", "path": R, "needs": ["project", "repo"]}]},
        {"id": "API-05", "purpose": "Branche par défaut", "priority": "P0", "admin": False, "probes": [{"name": "default-branch", "path": R + "/default-branch", "needs": ["project", "repo"]}, {"name": "branches/default (variante)", "path": R + "/branches/default", "needs": ["project", "repo"], "optional": True}]},
        {"id": "API-06", "purpose": "Forks et relations", "priority": "P1", "admin": False, "probes": [{"name": "forks", "path": R + "/forks", "params": {"limit": 1}, "needs": ["project", "repo"]}, {"name": "related", "path": R + "/related", "params": {"limit": 1}, "needs": ["project", "repo"]}]},
        {"id": "API-07", "purpose": "README et licence détectée", "priority": "P1", "admin": False, "probes": [{"name": "readme", "path": R + "/readme", "needs": ["project", "repo"]}, {"name": "license", "path": R + "/license", "needs": ["project", "repo"]}]},
        {"id": "API-08", "purpose": "Branches et tags", "priority": "P0", "admin": False, "probes": [{"name": "branches", "path": R + "/branches", "params": {"limit": 1}, "needs": ["project", "repo"]}, {"name": "tags", "path": R + "/tags", "params": {"limit": 1}, "needs": ["project", "repo"]}]},
        {"id": "API-09", "purpose": "Chemins, arborescence et contenu ciblé", "priority": "P0", "admin": False, "probes": [{"name": "files", "path": R + "/files", "params": {"limit": 1}, "needs": ["project", "repo"]}, {"name": "browse (racine)", "path": R + "/browse", "params": {"limit": 1}, "needs": ["project", "repo"]}, {"name": "raw (un fichier)", "path": R + "/raw/{path}", "needs": ["project", "repo", "path"], "raw": True, "max_bytes": 4096}]},
        {"id": "API-10", "purpose": "Commits et identités Git", "priority": "P0", "admin": False, "probes": [{"name": "commits", "path": R + "/commits", "params": {"limit": 1}, "needs": ["project", "repo"]}, {"name": "commit", "path": R + "/commits/{commit}", "needs": ["project", "repo", "commit"]}]},
        {"id": "API-11", "purpose": "Changements et diffs d'un commit", "priority": "P1", "admin": False, "probes": [{"name": "changes", "path": R + "/commits/{commit}/changes", "params": {"limit": 1}, "needs": ["project", "repo", "commit"]}, {"name": "diff", "path": R + "/commits/{commit}/diff", "needs": ["project", "repo", "commit"], "raw": True, "max_bytes": 4096}]},
        {"id": "API-12", "purpose": "PR et participants déclarés", "priority": "P0", "admin": False, "probes": [{"name": "pull-requests", "path": R + "/pull-requests", "params": {"state": "ALL", "limit": 1}, "needs": ["project", "repo"]}, {"name": "pull-request", "path": R + "/pull-requests/{pr}", "needs": ["project", "repo", "pr"]}]},
        {"id": "API-13", "purpose": "Actions et commentaires de PR", "priority": "P1", "admin": False, "probes": [{"name": "activities", "path": R + "/pull-requests/{pr}/activities", "params": {"limit": 1}, "needs": ["project", "repo", "pr"]}]},
        {"id": "API-14", "purpose": "Droits directs du repository", "priority": "P1", "admin": True, "probes": [{"name": "permissions/users", "path": R + "/permissions/users", "params": {"limit": 1}, "needs": ["project", "repo"]}, {"name": "permissions/groups", "path": R + "/permissions/groups", "params": {"limit": 1}, "needs": ["project", "repo"]}]},
        {"id": "API-15", "purpose": "Recherche de droits directs/implicites", "priority": "P1", "admin": True, "probes": [{"name": "permissions/search", "path": R + "/permissions/search", "params": {"limit": 1}, "needs": ["project", "repo"]}]},
        {"id": "API-16", "purpose": "Affectations au niveau projet", "priority": "P1", "admin": True, "probes": [{"name": "project permissions/users", "path": P + "/permissions/users", "params": {"limit": 1}, "needs": ["project"]}, {"name": "project permissions/groups", "path": P + "/permissions/groups", "params": {"limit": 1}, "needs": ["project"]}]},
        {"id": "API-17", "purpose": "Groupes et appartenance consultable", "priority": "P1", "admin": True, "probes": [{"name": "groups", "path": A + "/groups", "params": {"limit": 1}, "needs": []}, {"name": "admin/groups/more-members", "path": A + "/admin/groups/more-members", "params": {"context": "{group}", "limit": 1}, "needs": ["group"]}]},
        {"id": "API-18", "purpose": "Affectations globales", "priority": "P2", "admin": True, "probes": [{"name": "admin/permissions/users", "path": A + "/admin/permissions/users", "params": {"limit": 1}, "needs": []}, {"name": "admin/permissions/groups", "path": A + "/admin/permissions/groups", "params": {"limit": 1}, "needs": []}]},
        {"id": "API-19", "purpose": "Restrictions de branches", "priority": "P1", "admin": True, "probes": [{"name": "repo restrictions", "path": BP + "/projects/{project}/repos/{repo}/restrictions", "params": {"limit": 1}, "needs": ["project", "repo"]}, {"name": "project restrictions", "path": BP + "/projects/{project}/restrictions", "params": {"limit": 1}, "needs": ["project"]}]},
        {"id": "API-20", "purpose": "Conditions de reviewers par défaut", "priority": "P1", "admin": False, "probes": [{"name": "default-reviewers conditions", "path": DR + "/projects/{project}/repos/{repo}/conditions", "needs": ["project", "repo"]}]},
        {"id": "API-21", "purpose": "Hooks et webhooks", "priority": "P1", "admin": True, "probes": [{"name": "repo settings/hooks", "path": R + "/settings/hooks", "params": {"limit": 1}, "needs": ["project", "repo"]}, {"name": "repo webhooks", "path": R + "/webhooks", "params": {"limit": 1}, "needs": ["project", "repo"]}, {"name": "project settings/hooks", "path": P + "/settings/hooks", "params": {"limit": 1}, "needs": ["project"]}, {"name": "project webhooks", "path": P + "/webhooks", "params": {"limit": 1}, "needs": ["project"]}]},
        {"id": "API-22", "purpose": "Builds requis pour le merge", "priority": "P1", "admin": False, "probes": [{"name": "required-builds conditions", "path": RB + "/projects/{project}/repos/{repo}/conditions", "params": {"limit": 1}, "needs": ["project", "repo"]}]},
        {"id": "API-23", "purpose": "Builds et déploiements liés au commit", "priority": "P1", "admin": False, "probes": [{"name": "commit builds", "path": R + "/commits/{commit}/builds", "params": {"limit": 1}, "needs": ["project", "repo", "commit"]}, {"name": "commit deployments", "path": R + "/commits/{commit}/deployments", "params": {"limit": 1}, "needs": ["project", "repo", "commit"]}, {"name": "build-status (variante historique)", "path": "/rest/build-status/latest/commits/{commit}", "params": {"limit": 1}, "needs": ["commit"], "optional": True}]},
        {"id": "API-24", "purpose": "Rapports Code Insights", "priority": "P1", "admin": False, "probes": [{"name": "insights reports", "path": INS + "/projects/{project}/repos/{repo}/commits/{commit}/reports", "params": {"limit": 1}, "needs": ["project", "repo", "commit"]}]},
        {"id": "API-25", "purpose": "Détail de rapport et annotations", "priority": "P1", "admin": False, "probes": [{"name": "insights report", "path": INS + "/projects/{project}/repos/{repo}/commits/{commit}/reports/{report}", "needs": ["project", "repo", "commit", "report"]}, {"name": "insights annotations", "path": INS + "/projects/{project}/repos/{repo}/commits/{commit}/reports/{report}/annotations", "needs": ["project", "repo", "commit", "report"]}]},
    ]


# ============================================================================================
# Découverte
# ============================================================================================
class Discovery:
    def __init__(self, client: ReadOnlyClient, args: argparse.Namespace) -> None:
        self.c = client
        self.args = args
        self.samples: Dict[str, Optional[str]] = {k: None for k in ("project", "repo", "commit", "pr", "path", "group", "report")}
        self.sample_notes: List[str] = []
        self.perimeter_projects: List[Dict[str, Any]] = []
        self.perimeter_mode = "all"
        self.projects_visible: List[Dict[str, Any]] = []
        self.repos: List[Dict[str, Any]] = []          # inventaire des dépôts du périmètre
        self.project_rows: List[Dict[str, Any]] = []

    # ---------------------------------------------------------------- pagination ---------
    def paged(self, path: str, params: Optional[Dict[str, Any]] = None, limit: int = 100, max_pages: int = 100) -> Tuple[List[Any], bool, Optional[ProbeResult]]:
        values: List[Any] = []
        start = 0
        pages = 0
        last: Optional[ProbeResult] = None
        while True:
            p = dict(params or {}); p.update({"limit": limit, "start": start})
            res = self.c.get(path, params=p)
            last = res
            if not ok(res):
                return values, True, res
            page = res.json
            values.extend(page.get("values") or [])
            pages += 1
            if page.get("isLastPage", True) or page.get("nextPageStart") is None:
                return values, False, res
            if pages >= max_pages:
                return values, True, res
            start = page["nextPageStart"]

    # ---------------------------------------------------------------- instance (API-01) ---
    def instance(self) -> Dict[str, Any]:
        res = self.c.get(A + "/application-properties")
        status, diag, _ = classify(res)
        out: Dict[str, Any] = {"collection_status": status, "diagnostic": diag, "http_status": res.status_code, "version_observed": None, "build_number": None,
                               "build_date": None, "display_name": None, "version_declared": self.args.declared_version, "build_declared": self.args.declared_build,
                               "version_status": "UNKNOWN"}
        if status == "AVAILABLE" and isinstance(res.json, dict):
            j = res.json
            out.update({"version_observed": j.get("version"), "build_number": j.get("buildNumber"), "build_date": j.get("buildDate"), "display_name": j.get("displayName")})
            if self.args.declared_version:
                same_v = str(j.get("version")) == str(self.args.declared_version)
                same_b = (not self.args.declared_build) or str(j.get("buildNumber")) == str(self.args.declared_build)
                out["version_status"] = "CONFIRMED" if (same_v and same_b) else "CONFLICTING"
            else:
                out["version_status"] = "CONFIRMED"
        return out

    # ---------------------------------------------------------------- périmètre (API-02) ---
    def resolve_perimeter(self) -> Dict[str, Any]:
        projects, capped, res = self.paged(A + "/projects", limit=100, max_pages=200)
        status, diag, _ = classify(res) if res else ("ERROR", "aucune réponse", {})
        self.projects_visible = [p for p in projects if isinstance(p, dict)]
        info: Dict[str, Any] = {"collection_status": status, "diagnostic": diag, "projects_visible": len(self.projects_visible), "capped": capped,
                                "personal_projects_visible": sum(1 for p in self.projects_visible if p.get("type") == "PERSONAL")}
        keys = [k.strip().upper() for k in (self.args.projects or "").split(",") if k.strip()]
        if keys:
            self.perimeter_mode = "projects"
            found = {p.get("key", "").upper(): p for p in self.projects_visible}
            self.perimeter_projects = [found[k] for k in keys if k in found]
            missing = [k for k in keys if k not in found]
            if missing:
                self.sample_notes.append("clés de projet introuvables dans la liste visible : %s" % ", ".join(missing))
        elif self.args.project_name_regex:
            self.perimeter_mode = "regex"
            rx = re.compile(self.args.project_name_regex, re.I)
            self.perimeter_projects = [p for p in self.projects_visible if rx.search(p.get("name", "") or "") or rx.search(p.get("key", "") or "")]
        else:
            self.perimeter_mode = "all"
            self.perimeter_projects = list(self.projects_visible)
            self.sample_notes.append("aucun périmètre fourni : tous les projets visibles sont pris en compte")
        info["perimeter_mode"] = self.perimeter_mode
        info["perimeter_projects"] = [{"key": p.get("key"), "name": p.get("name"), "type": p.get("type"), "public": p.get("public")} for p in self.perimeter_projects]
        # clés des projets non personnels visibles (identifiants techniques, non nominatifs) : aide au choix du périmètre
        info["visible_project_keys"] = sorted(str(p.get("key")) for p in self.projects_visible if p.get("type") != "PERSONAL" and p.get("key"))[:200]
        info["missing_project_keys"] = missing if keys else []
        return info

    # ---------------------------------------------------------------- inventaire (API-03) ---
    def inventory(self) -> None:
        a = self.args
        for p in self.perimeter_projects:
            key = p.get("key")
            repos, capped, res = self.paged(A + "/projects/%s/repos" % key, limit=100, max_pages=a.max_pages)
            status, _, _ = classify(res) if res else ("ERROR", "", {})
            rows = []
            for r in repos:
                if not isinstance(r, dict):
                    continue
                origin = r.get("origin") if isinstance(r.get("origin"), dict) else None
                rows.append({"project_key": key, "project_name": p.get("name"), "project_type": p.get("type"), "repo_id": r.get("id"), "slug": r.get("slug"), "name": r.get("name"),
                             "state": r.get("state"), "archived": r.get("archived"), "public": r.get("public"), "forkable": r.get("forkable"),
                             "is_fork": bool(origin), "origin": ("%s/%s" % ((origin.get("project") or {}).get("key"), origin.get("slug"))) if origin else None,
                             "scm": r.get("scmId"), "details": None})
            self.repos.extend(rows)
            self.project_rows.append({"key": key, "name": p.get("name"), "type": p.get("type"), "public": p.get("public"), "repositories": len(rows), "capped": capped,
                                      "collection_status": status, "forks": sum(1 for r in rows if r["is_fork"]), "archived": sum(1 for r in rows if r["archived"] is True),
                                      "public_repos": sum(1 for r in rows if r["public"] is True)})

    # ---------------------------------------------------------------- échantillon --------
    def resolve_samples(self) -> None:
        a = self.args
        if a.sample_repo and "/" in a.sample_repo:
            pk, slug = a.sample_repo.split("/", 1)
            self.samples["project"], self.samples["repo"] = pk, slug
            self.sample_notes.append("dépôt échantillon imposé : %s/%s" % (pk, slug))
        candidates: List[Tuple[str, str]] = []
        if self.samples["repo"]:
            candidates.append((self.samples["project"], self.samples["repo"]))
        candidates.extend((r["project_key"], r["slug"]) for r in self.repos[:12] if r.get("slug"))
        if not self.repos:
            # inventaire non exécuté (--no-volumetry) : première page de dépôts des premiers projets du périmètre
            for p in self.perimeter_projects[:3]:
                res = self.c.get(A + "/projects/%s/repos" % p.get("key"), params={"limit": 10})
                if ok(res):
                    candidates.extend((p.get("key"), r.get("slug")) for r in res.json.get("values", []) if isinstance(r, dict) and r.get("slug"))
        for pk, slug in candidates:
            res = self.c.get(A + "/projects/%s/repos/%s/commits" % (pk, slug), params={"limit": 1})
            if ok(res) and res.json.get("values"):
                self.samples["project"], self.samples["repo"] = pk, slug
                self.samples["commit"] = res.json["values"][0].get("id")
                break
        if not self.samples["project"] and candidates:
            self.samples["project"], self.samples["repo"] = candidates[0]
            self.sample_notes.append("aucun dépôt avec commit parmi les %d premiers candidats : sondes liées au commit non exécutées" % len(candidates))
        if not self.samples["project"] and self.perimeter_projects:
            self.samples["project"] = self.perimeter_projects[0].get("key")
            self.sample_notes.append("aucun dépôt trouvé : sondes liées au dépôt non exécutées")
        pk, slug = self.samples["project"], self.samples["repo"]
        if pk and slug:
            res = self.c.get(A + "/projects/%s/repos/%s/pull-requests" % (pk, slug), params={"state": "ALL", "limit": 1})
            if ok(res) and res.json.get("values"):
                self.samples["pr"] = str(res.json["values"][0].get("id"))
            else:
                self.sample_notes.append("aucune pull request sur le dépôt échantillon : API-12 (détail) et API-13 non exécutées")
            res = self.c.get(A + "/projects/%s/repos/%s/files" % (pk, slug), params={"limit": 1})
            if ok(res) and res.json.get("values"):
                v = res.json["values"][0]
                self.samples["path"] = v if isinstance(v, str) else ((v.get("path") or {}).get("toString") if isinstance(v, dict) else None)
            if self.samples["commit"]:
                res = self.c.get(INS + "/projects/%s/repos/%s/commits/%s/reports" % (pk, slug, self.samples["commit"]), params={"limit": 1})
                if ok(res) and res.json.get("values"):
                    self.samples["report"] = res.json["values"][0].get("key")
                else:
                    self.sample_notes.append("aucun rapport Code Insights sur le commit échantillon : API-25 non exécutée")
        if a.profile == "extended":
            res = self.c.get(A + "/groups", params={"limit": 1})
            if ok(res) and res.json.get("values"):
                v = res.json["values"][0]
                self.samples["group"] = v if isinstance(v, str) else (v.get("name") if isinstance(v, dict) else None)

    # ---------------------------------------------------------------- sondes -------------
    def probe_catalogue(self, skip_reason: Optional[str] = None) -> List[Dict[str, Any]]:
        """Sonde chaque ligne du catalogue. Avec skip_reason (arrêt anticipé), aucune sonde n'est exécutée :
        chaque ligne est NOT_REQUESTED avec ce diagnostic, sauf API-01 qui reflète l'état réellement observé."""
        out: List[Dict[str, Any]] = []
        for line in catalogue():
            entry: Dict[str, Any] = {"collection_id": line["id"], "purpose": line["purpose"], "priority": line["priority"], "admin_class": line["admin"], "probes": [], "collection_status": None, "to_verify": False}
            if skip_reason and line["id"] != "API-01":
                entry["collection_status"] = "NOT_REQUESTED"; entry["diagnostic"] = skip_reason; entry["compatibility_checked"] = False
                out.append(entry); continue
            if line["admin"] and self.args.profile == "base":
                entry["collection_status"] = "NOT_REQUESTED"; entry["diagnostic"] = "profil BASE : famille nécessitant des droits étendus non demandée"; entry["compatibility_checked"] = False
                out.append(entry); continue
            statuses: List[str] = []
            for pr in line["probes"]:
                pe: Dict[str, Any] = {"name": pr["name"], "endpoint_template": pr["path"], "optional": pr.get("optional", False)}
                missing = [n for n in pr.get("needs", []) if not self.samples.get(n)]
                if missing:
                    pe.update({"collection_status": "NOT_REQUESTED", "diagnostic": "échantillon indisponible : %s" % ", ".join(missing), "http_status": None})
                    entry["probes"].append(pe)
                    if not pr.get("optional"):
                        statuses.append("NOT_REQUESTED")
                    continue
                path = pr["path"]; params = dict(pr.get("params") or {})
                for k, v in list(self.samples.items()):
                    if v is not None:
                        path = path.replace("{%s}" % k, urllib.parse.quote(str(v), safe="/") if k == "path" else str(v))
                        for pk_, pv in params.items():
                            if isinstance(pv, str):
                                params[pk_] = pv.replace("{%s}" % k, str(v))
                res = self.c.get(path, params=params or None, raw=pr.get("raw", False), max_bytes=pr.get("max_bytes", 65536))
                status, diag, d = classify(res)
                pe.update({"collection_status": status, "diagnostic": diag, "http_status": res.status_code, "duration_ms": res.duration_ms, "attempts": res.attempts, "bytes_read": res.bytes_read, "content_type": res.content_type[:60]})
                pe.update(d)
                entry["probes"].append(pe)
                if not pr.get("optional"):
                    statuses.append(status)
                    if status in ("ERROR", "UNSUPPORTED"):
                        entry["to_verify"] = True
            entry["collection_status"] = aggregate(statuses)
            entry["compatibility_checked"] = entry["collection_status"] in ("AVAILABLE", "EMPTY")
            out.append(entry)
        return out

    # ---------------------------------------------------------------- détails par dépôt ---
    def repo_details(self) -> int:
        a = self.args
        if a.details == "none":
            return 0
        targets = self.repos if a.details == "all" else self.repos[: a.sample_repos]
        n = 0
        for r in targets:
            if self.c.budget_exhausted:
                self.sample_notes.append("budget d'appels épuisé pendant les détails par dépôt : %d dépôts détaillés" % n)
                break
            r["details"] = self._one_repo_details(r["project_key"], r["slug"])
            n += 1
        return n

    def _one_repo_details(self, pk: str, slug: str) -> Dict[str, Any]:
        a = self.args
        base = A + "/projects/%s/repos/%s" % (pk, slug)
        out: Dict[str, Any] = {}
        res = self.c.get(base + "/default-branch")
        out["default_branch"] = (res.json.get("displayId") if ok(res) else None)
        for name, path, params, max_pages in (("branches", base + "/branches", None, a.max_pages), ("tags", base + "/tags", None, a.max_pages),
                                              ("commits", base + "/commits", None, min(a.max_pages, 10)), ("pull_requests", base + "/pull-requests", {"state": "ALL"}, min(a.max_pages, 10))):
            vals, capped, res = self.paged(path, params=params, limit=100, max_pages=max_pages)
            status, _, _ = classify(res) if res else ("ERROR", "", {})
            out[name] = len(vals); out[name + "_capped"] = capped; out[name + "_status"] = status
            if name == "commits":
                ts = (vals[0].get("authorTimestamp") or vals[0].get("committerTimestamp")) if vals and isinstance(vals[0], dict) else None
                if isinstance(ts, (int, float)):
                    d = dt.datetime.fromtimestamp(ts / 1000, dt.timezone.utc)
                    out["last_commit_utc"] = d.strftime("%Y-%m-%d")
                    out["days_since_last_commit"] = (dt.datetime.now(dt.timezone.utc) - d).days
                else:
                    out["last_commit_utc"] = None; out["days_since_last_commit"] = None
        out["activity_bucket"] = activity_bucket(out)
        return out


def activity_bucket(d: Optional[Dict[str, Any]]) -> str:
    if not d or d.get("commits_status") not in ("AVAILABLE", "EMPTY"):
        return "non mesuré"
    if not d.get("commits"):
        return "sans commit"
    days = d.get("days_since_last_commit")
    if days is None:
        return "non mesuré"
    if days <= 90:
        return "≤ 90 j"
    if days <= 365:
        return "91–365 j"
    return "> 365 j"


# ============================================================================================
# Propositions (règles simples sur les résultats)
# ============================================================================================
def propositions(doc: Dict[str, Any]) -> List[Dict[str, str]]:
    props: List[Dict[str, str]] = []
    run = doc["run"]; inst = doc["instance"]; caps = doc["capabilities"]; vol = doc.get("volumetry") or {}; per = doc.get("perimeter") or {}
    if run.get("dry_run"):
        return [{"level": "info", "title": "Mode dry-run : aucun appel exécuté", "text": "%d appel(s) GET prévus (voir le journal des appels). Relancer sans --dry-run pour exécuter la découverte ; ce mode ne valide ni la connectivité ni le jeton." % run["calls_total"]}]
    if run.get("stopped_after_step") == 2:
        visible = per.get("visible_project_keys") or []
        missing = per.get("missing_project_keys") or []
        return [{"level": "critical", "title": "Aucun projet retenu : découverte interrompue après l'étape 2", "text": "%sClés de projet visibles par le compte (hors projets personnels) : %s. Relancer avec --projects ou --project-name-regex ; %s projet(s) personnel(s) visible(s) sont hors périmètre (D-01)."
                 % (("Clés introuvables : %s. " % ", ".join(missing)) if missing else "", (", ".join(visible[:40]) + (" …" if len(visible) > 40 else "")) if visible else "aucune — le compte ne voit aucun projet (droits à revoir, D-02)", per.get("personal_projects_visible", 0))}]
    if run.get("stopped_after_step"):
        # arrêt anticipé : instance injoignable ou jeton refusé → une seule proposition, pas de cascade
        st = inst.get("collection_status")
        if st == "UNAUTHENTICATED":
            return [{"level": "critical", "title": "Jeton refusé (HTTP 401) : découverte interrompue après l'étape 1", "text": "Le compte de service n'est pas authentifié. Vérifier BITBUCKET_TOKEN ou --token-file (jeton expiré, révoqué ou mal copié), ou l'option --basic-user. Aucune autre sonde n'a été exécutée."}]
        if st == "FORBIDDEN":
            return [{"level": "critical", "title": "Accès refusé (HTTP 403) sur API-01 : découverte interrompue après l'étape 1", "text": "Le compte est authentifié mais ne peut pas lire l'instance. Revue des droits du compte de service (D-02) avant toute relance."}]
        return [{"level": "critical", "title": "Instance injoignable : découverte interrompue après l'étape 1", "text": "%s. Vérifier l'URL de base (--base-url), le chemin réseau (proxy : HTTPS_PROXY / --no-proxy), le certificat (--ca-bundle) et le délai (--timeout)." % (inst.get("diagnostic") or "aucune réponse")}]
    if inst.get("version_status") == "CONFLICTING":
        props.append({"level": "critical", "title": "Version déclarée à corriger (H-01)", "text": "Observée %s (build %s) ; déclarée %s (build %s). Mettre à jour le registre et valider le catalogue contre la documentation de la version observée." % (inst.get("version_observed"), inst.get("build_number"), inst.get("version_declared"), inst.get("build_declared"))})
    elif inst.get("version_status") == "UNKNOWN":
        props.append({"level": "critical", "title": "API-01 inaccessible", "text": "Vérifier l'URL de base, le jeton et le chemin réseau (proxy, TLS) avant toute autre exploitation : %s." % inst.get("diagnostic")})
    else:
        props.append({"level": "good", "title": "Version confirmée (H-01 clôturée)", "text": "Bitbucket Data Center %s, build %s : la documentation REST 9.4 est la référence de validation du catalogue." % (inst.get("version_observed"), inst.get("build_number"))})
    p0_bad = [c for c in caps if c["priority"] == "P0" and c["collection_status"] not in ("AVAILABLE", "EMPTY")]
    if p0_bad:
        props.append({"level": "critical", "title": "%d ligne(s) P0 non disponibles — bloquantes pour le lot 1" % len(p0_bad), "text": " ; ".join("%s %s (%s)" % (c["collection_id"], c["collection_status"], c.get("diagnostic") or "voir les sondes") for c in p0_bad)})
    unauth = [c["collection_id"] for c in caps if c["collection_status"] == "UNAUTHENTICATED" or any(p.get("collection_status") == "UNAUTHENTICATED" for p in c.get("probes", []))]
    if unauth:
        props.append({"level": "critical", "title": "Authentification refusée sur %d famille(s)" % len(unauth), "text": "%s : jeton absent, expiré ou révoqué en cours d'exécution ; vérifier BITBUCKET_TOKEN et relancer." % ", ".join(unauth)})
    forb = [c["collection_id"] for c in caps if c["collection_status"] == "FORBIDDEN" or any(p.get("collection_status") == "FORBIDDEN" for p in c.get("probes", []))]
    if forb:
        props.append({"level": "warning", "title": "Droits insuffisants sur %d famille(s)" % len(forb), "text": "Revue des droits du compte de service pour : %s. Aucune élévation automatique : décision D-02." % ", ".join(forb)})
    unsup = [c["collection_id"] for c in caps if c["collection_status"] == "UNSUPPORTED"]
    if unsup:
        props.append({"level": "warning", "title": "Familles non prises en charge", "text": "%s : retirer ces lignes du socle ou les reporter dans les familles conditionnelles." % ", ".join(unsup)})
    err404 = [c["collection_id"] for c in caps if c["collection_status"] in ("ERROR", "PARTIAL") and any(p.get("http_status") == 404 and not p.get("optional") for p in c.get("probes", []))]
    if err404:
        props.append({"level": "warning", "title": "Routes en 404 à vérifier (%d)" % len(err404), "text": "%s : confronter chaque route à la documentation 9.4 ; si la route n'existe pas, classer UNSUPPORTED ; si l'échantillon est inadapté (dépôt sans README, commit sans build…), relancer avec --sample-repo sur un autre dépôt." % ", ".join(err404)})
    nr = [c["collection_id"] for c in caps if c["collection_status"] == "NOT_REQUESTED" and c.get("diagnostic", "").startswith("profil") is False and any("échantillon" in (p.get("diagnostic") or "") for p in c.get("probes", []))]
    if nr:
        props.append({"level": "info", "title": "Sondes non exécutées faute d'échantillon", "text": "%s : relancer avec un dépôt disposant de pull requests ou de rapports Code Insights (--sample-repo CLE/slug)." % ", ".join(nr)})
    if vol:
        n = vol.get("repositories_in_perimeter", 0)
        extra = ""
        arch = sum(p.get("archived", 0) for p in vol.get("projects", [])); forks = sum(p.get("forks", 0) for p in vol.get("projects", []))
        if arch or forks:
            extra = " Dont %d archivé(s) et %d fork(s) : décider s'ils restent dans le dénominateur ou sortent du périmètre de scan." % (arch, forks)
        props.append({"level": "info" if not vol.get("capped") else "warning", "title": "Dénominateur candidat D-06 : %d dépôts%s" % (n, " (liste tronquée)" if vol.get("capped") else ""),
                      "text": ("Liste complète des dépôts des projets du périmètre." if not vol.get("capped") else "Relancer avec --max-pages plus élevé pour obtenir la liste complète.") + extra})
        buckets = vol.get("activity_buckets") or {}
        old = buckets.get("> 365 j", 0); nocommit = buckets.get("sans commit", 0); measured = sum(v for k, v in buckets.items() if k != "non mesuré")
        if measured:
            props.append({"level": "info", "title": "Activité sur l'échantillon détaillé (%d dépôts)" % measured, "text": "%d sans commit depuis plus d'un an, %d sans aucun commit : candidats DORMANT / ARCHIVED à confirmer par le moteur, jamais supprimés." % (old, nocommit)})
        props.append({"level": "info", "title": "Échantillon de contrôle à constituer (V.5)", "text": "Au moins 50 dépôts étiquetés à la main, répartis sur les %d projets du périmètre, incluant monorepos, POC, benchmarks, dépôts archivés et forks." % len(vol.get("projects", []))})
    if per.get("personal_projects_visible"):
        props.append({"level": "info", "title": "%d projet(s) personnel(s) visible(s)" % per["personal_projects_visible"], "text": "Hors périmètre pilote (D-01) ; à inventorier séparément si la gouvernance le demande."})
    if doc["run"].get("budget_exhausted"):
        props.append({"level": "warning", "title": "Budget d'appels épuisé", "text": "Relancer avec --max-calls plus élevé ou --details sample pour limiter la volumétrie par dépôt."})
    return props


# ============================================================================================
# Sorties : CSV, Markdown, HTML
# ============================================================================================
def write_csvs(doc: Dict[str, Any], out_dir: str, stamp: str) -> List[str]:
    paths = []

    def w(name: str, header: List[str], rows: List[List[Any]]) -> None:
        p = os.path.join(out_dir, "%s_%s.csv" % (name, stamp))
        with open(p, "w", encoding="utf-8-sig", newline="") as fh:
            cw = csv.writer(fh, delimiter=";")
            cw.writerow(header)
            for r in rows:
                cw.writerow(["" if v is None else v for v in r])
        paths.append(p)

    vol = doc.get("volumetry") or {}
    w("projets", ["cle", "nom", "type", "public", "depots", "liste_tronquee", "etat_collecte", "forks", "archives", "depots_publics"],
      [[p["key"], p["name"], p["type"], p["public"], p["repositories"], p["capped"], p["collection_status"], p["forks"], p["archived"], p["public_repos"]] for p in vol.get("projects", [])])
    rows = []
    for r in doc.get("repositories", []):
        d = r.get("details") or {}
        rows.append([r["project_key"], r["project_name"], r["project_type"], r["repo_id"], r["slug"], r["name"], r["state"], r["archived"], r["public"], r["forkable"], r["is_fork"], r["origin"], r["scm"],
                     d.get("default_branch"), d.get("branches"), d.get("branches_capped"), d.get("tags"), d.get("tags_capped"), d.get("commits"), d.get("commits_capped"),
                     d.get("pull_requests"), d.get("pull_requests_capped"), d.get("last_commit_utc"), d.get("days_since_last_commit"), d.get("activity_bucket") if d else ""])
    w("depots", ["projet_cle", "projet_nom", "projet_type", "depot_id", "slug", "nom", "etat", "archive", "public", "forkable", "fork", "origine", "scm", "branche_defaut", "branches", "branches_tronque",
                 "tags", "tags_tronque", "commits", "commits_tronque", "pull_requests", "pull_requests_tronque", "dernier_commit_utc", "jours_depuis_dernier_commit", "activite"], rows)
    rows = []
    for c in doc["capabilities"]:
        if not c.get("probes"):
            rows.append([c["collection_id"], c["purpose"], c["priority"], c["collection_status"], c.get("to_verify"), c.get("compatibility_checked"), "", "", "", "", "", "", "", c.get("diagnostic")])
        for p in c.get("probes", []):
            rows.append([c["collection_id"], c["purpose"], c["priority"], c["collection_status"], c.get("to_verify"), c.get("compatibility_checked"), p["name"], p["endpoint_template"], p.get("optional"),
                         p.get("http_status"), p.get("collection_status"), p.get("response_shape"), p.get("records_first_page"), p.get("diagnostic")])
    w("capacites", ["ligne", "objet", "priorite", "etat_ligne", "a_verifier", "contrat_verifie", "sonde", "endpoint", "variante", "http", "etat_sonde", "forme", "enregistrements_page1", "diagnostic"], rows)
    rows = []
    for r in doc.get("repositories", []):
        d = r.get("details")
        if d:
            rows.append([r["project_key"], r["slug"], d.get("default_branch"), d.get("branches"), d.get("branches_capped"), d.get("tags"), d.get("commits"), d.get("commits_capped"), d.get("pull_requests"), d.get("last_commit_utc"), d.get("days_since_last_commit"), d.get("activity_bucket")])
    w("volumetrie", ["projet", "depot", "branche_defaut", "branches", "branches_tronque", "tags", "commits", "commits_tronque", "pull_requests", "dernier_commit_utc", "jours", "activite"], rows)
    w("appels", ["horodatage", "methode", "chemin", "http", "ms", "tentative"], [[c["ts"], c["method"], c["path"], c["status"], c["ms"], c.get("attempt")] for c in doc["calls"]])
    return paths


def markdown_report(doc: Dict[str, Any]) -> str:
    run, inst, caps, vol = doc["run"], doc["instance"], doc["capabilities"], doc.get("volumetry") or {}
    lines = ["# Découverte des capacités — Bitbucket Data Center", "",
             "Exécution : %s · hôte : %s · profil : %s · périmètre : %s (%s)" % (run["timestamp_utc"], run["base_url_host"], run["profile"], run["perimeter"]["mode"], ", ".join(run["perimeter"]["project_keys"]) or "—"),
             "Lecture seule : %d appels, tous GET (appels non GET : %d). Mode non nominatif : aucune identité conservée." % (run["calls_total"], run["calls_non_get"]), "",
             "## Synthèse", ""] + ["- %s" % t for t in synthesis_text(doc)] + ["",
             "## Instance (API-01)", "", "| Élément | Valeur |", "|---|---|",
             "| Version observée | %s (build %s) |" % (inst.get("version_observed") or "—", inst.get("build_number") or "—"),
             "| Version déclarée | %s (build %s) |" % (inst.get("version_declared") or "—", inst.get("build_declared") or "—"),
             "| Statut de la version | **%s** |" % inst.get("version_status"), "| État de collecte | %s — %s |" % (inst.get("collection_status"), inst.get("diagnostic")), "",
             "## Catalogue (API-01 à API-25)", "", "| ID | Prio. | État | Sondes | HTTP | Forme | Diagnostic |", "|---|---|---|---|---|---|---|"]
    for c in caps:
        probes = c.get("probes") or []
        http = ", ".join(str(p.get("http_status")) for p in probes if p.get("http_status") is not None) or "—"
        shapes = ", ".join(sorted({p.get("response_shape") for p in probes if p.get("response_shape")})) or "—"
        diag = c.get("diagnostic") or "; ".join("%s: %s" % (p["name"], p.get("diagnostic")) for p in probes if p.get("collection_status") not in ("AVAILABLE", "EMPTY") and not p.get("optional"))
        lines.append("| %s | %s | **%s**%s | %d | %s | %s | %s |" % (c["collection_id"], c["priority"], c["collection_status"], " ⚠" if c.get("to_verify") else "", len(probes), http, shapes, (diag or "—").replace("|", "/")))
    s = doc["summary"]
    lines += ["", "Synthèse : " + ", ".join("%s %d" % (k, v) for k, v in s["by_status"].items()), "P0 disponibles : %d / %d · lignes à vérifier : %d" % (s["p0_available"], s["p0_total"], s["to_verify"]), ""]
    if vol:
        lines += ["## Volumétrie du périmètre", "", "| Projet | Dépôts | Forks | Archivés | État |", "|---|---|---|---|---|"]
        for p in vol.get("projects", []):
            lines.append("| %s (%s) | %d%s | %d | %d | %s |" % (p["key"], p.get("name"), p["repositories"], " (tronqué)" if p["capped"] else "", p["forks"], p["archived"], p["collection_status"]))
        lines += ["", "Dénominateur candidat (D-06) : **%d dépôts**%s. Dépôts détaillés : %d. Activité : %s" % (vol["repositories_in_perimeter"], "" if not vol["capped"] else " — liste tronquée", vol.get("sample_size", 0), ", ".join("%s %d" % (k, v) for k, v in (vol.get("activity_buckets") or {}).items())), ""]
    lines += ["## Propositions", ""] + ["- **%s** — %s" % (p["title"], p["text"]) for p in doc.get("propositions", [])] + [""]
    if doc.get("sample_notes"):
        lines += ["## Notes d'échantillonnage", ""] + ["- " + n for n in doc["sample_notes"]] + [""]
    return "\n".join(lines)


# ---------------------------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------------------------
STATUS_COLOR = {"AVAILABLE": "#00A193", "EMPTY": "#00A193", "PARTIAL": "#C98F00", "UNAUTHENTICATED": "#55579E", "FORBIDDEN": "#55579E", "UNSUPPORTED": "#55579E", "ERROR": "#DB4B4B", "NOT_REQUESTED": "#8C8C8C"}
STATUS_ICON = {"AVAILABLE": "✓", "EMPTY": "○", "PARTIAL": "◐", "UNAUTHENTICATED": "⊖", "FORBIDDEN": "⊘", "UNSUPPORTED": "⊗", "ERROR": "✖", "NOT_REQUESTED": "–"}
LEVEL_COLOR = {"good": "#00A193", "info": "#55579E", "warning": "#C98F00", "critical": "#DB4B4B"}
LEVEL_LABEL = {"good": "Acquis", "info": "Information", "warning": "À traiter", "critical": "Bloquant"}


def _bar_chart(items: List[Tuple[str, float, str]], label_w: int = 150, unit: str = "", tips: Optional[Dict[str, str]] = None) -> str:
    """Barres horizontales en HTML (responsives, lisibles à toute largeur) : libellé · barre fine à bout arrondi · valeur.
    Les textes portent la couleur du texte, jamais celle de la série ; l'info-bulle apparaît au survol."""
    if not items:
        return '<p class="muted">Aucune donnée.</p>'
    vmax = max(v for _, v, _ in items) or 1
    total = sum(v for _, v, _ in items) or 1
    rows = []
    for label, v, color in items:
        pct = 100.0 * v / vmax
        tip = (tips or {}).get(label) or ("%s : %d%s (%.0f %% du total)" % (label, v, unit, 100.0 * v / total))
        rows.append('<div class="bar-row" tabindex="0"><div class="bar-label" style="width:%dpx">%s</div><div class="bar-track"><div class="bar-fill" style="width:%.1f%%;background:%s"></div></div>'
                    '<div class="bar-val">%d%s</div><div class="bar-tip">%s</div></div>' % (label_w, html.escape(label), max(pct, 0.8), color, v, html.escape(unit), html.escape(tip)))
    return '<div class="bars" role="img" aria-label="graphique à barres">%s</div>' % "".join(rows)


def _kpi(label: str, value: str, sub: str = "", color: str = "#581D74") -> str:
    return '<div class="kpi"><div class="kpi-label">%s</div><div class="kpi-value" style="color:%s">%s</div><div class="kpi-sub">%s</div></div>' % (html.escape(label), color, html.escape(value), html.escape(sub))


def _badge(status: str) -> str:
    return '<span class="badge" style="background:%s">%s %s</span>' % (STATUS_COLOR.get(status, "#8C8C8C"), STATUS_ICON.get(status, ""), html.escape(status))


HTML_CSS = """
:root{--purple:#581D74;--teal:#00A193;--red:#DB4B4B;--lav:#9176B4;--indigo:#55579E;--magenta:#B34A8C;--dark:#2B0E39;--ink:#222;--muted:#6b6b6b;--line:#e6e2ec;--tint:#f4f1f8;--surface:#fff}
*{box-sizing:border-box}body{margin:0;font-family:Arial,Helvetica,sans-serif;color:var(--ink);background:#faf9fc;font-size:14px;line-height:1.45}
header{background:var(--purple);color:#fff;padding:22px 32px}header h1{margin:0 0 4px;font-size:22px;font-weight:700}header .meta{font-size:13px;opacity:.9}
header .attest{display:inline-block;margin-top:10px;background:rgba(255,255,255,.14);border-radius:999px;padding:4px 12px;font-size:12px}
main{max-width:1240px;margin:0 auto;padding:20px 24px 40px}
nav.toc{display:flex;flex-wrap:wrap;gap:8px;margin:0 0 18px}nav.toc a{text-decoration:none;color:var(--purple);background:var(--tint);border-radius:999px;padding:5px 12px;font-size:12.5px}
h2{font-size:17px;color:var(--purple);margin:26px 0 10px;padding-bottom:4px;border-bottom:1px solid var(--line)}h3{font-size:14px;margin:14px 0 6px;color:var(--dark)}
.kpis{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}.kpi{background:var(--surface);border:1px solid var(--line);border-radius:10px;padding:12px 14px}
.kpi-label{font-size:12px;color:var(--muted)}.kpi-value{font-size:30px;font-weight:700;line-height:1.15;margin-top:2px}.kpi-sub{font-size:11.5px;color:var(--muted);margin-top:2px}
.grid2{display:grid;grid-template-columns:repeat(2,1fr);gap:14px}.grid3{display:grid;grid-template-columns:repeat(3,1fr);gap:14px}.card{background:var(--surface);border:1px solid var(--line);border-radius:10px;padding:14px 16px}
.card h3{margin-top:0}.card .legend{margin-top:10px}
.bars{display:flex;flex-direction:column;gap:9px;margin-top:6px}.bar-row{display:flex;align-items:center;gap:8px;position:relative;outline:none}
.bar-label{flex:none;font-size:12.5px;color:var(--ink);text-align:right;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.bar-track{flex:1;height:14px;background:var(--tint);border-radius:0 4px 4px 0}.bar-fill{height:100%;border-radius:0 4px 4px 0;min-width:2px;transition:opacity .15s}
.bar-val{flex:none;min-width:32px;font-size:12.5px;color:var(--ink);font-variant-numeric:tabular-nums}
.bar-tip{display:none;position:absolute;left:0;bottom:calc(100% + 4px);background:var(--dark);color:#fff;font-size:12px;padding:5px 9px;border-radius:6px;white-space:nowrap;z-index:5;box-shadow:0 2px 8px rgba(0,0,0,.2)}
.bar-row:hover .bar-tip,.bar-row:focus .bar-tip{display:block}.bar-row:hover .bar-fill,.bar-row:focus .bar-fill{opacity:.8}
.synth{background:var(--surface);border:1px solid var(--line);border-left:5px solid var(--purple);border-radius:8px;padding:12px 16px;margin:0 0 10px;font-size:14px}.synth b{color:var(--purple)}
table{width:100%;border-collapse:collapse;background:var(--surface);font-size:12.5px}th{background:var(--purple);color:#fff;text-align:left;padding:7px 8px;font-weight:700;position:sticky;top:0}
td{padding:6px 8px;border-bottom:1px solid var(--line);vertical-align:top}tbody tr:nth-child(even) td{background:#f8f6fb}tbody tr:hover td{background:#efe9f5}td.num,th.num{text-align:right;font-variant-numeric:tabular-nums}
.card table th{position:static}.kv td{overflow-wrap:anywhere}.kv td:first-child{color:var(--muted);width:38%}
.badge{display:inline-block;color:#fff;border-radius:999px;padding:2px 9px;font-size:11px;font-weight:700;white-space:nowrap}.muted{color:var(--muted)}.small{font-size:12px}
details summary{cursor:pointer;color:var(--purple)}.probe{font-size:12px;color:var(--muted);margin:4px 0 0 14px}.probe code{color:var(--dark)}
.prop{border-left:4px solid var(--indigo);background:var(--surface);border-radius:8px;padding:10px 14px;margin:8px 0;border:1px solid var(--line);border-left-width:5px}.prop b{display:block;margin-bottom:2px}
.prop .lvl{font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.3px}
.toolbar{display:flex;gap:10px;align-items:center;margin:8px 0 10px;flex-wrap:wrap}.toolbar input,.toolbar select{font:inherit;padding:6px 9px;border:1px solid var(--line);border-radius:6px;min-width:220px}
.toolbar button{font:inherit;padding:6px 12px;border:1px solid var(--purple);color:var(--purple);background:#fff;border-radius:6px;cursor:pointer}.toolbar .count{color:var(--muted);font-size:12.5px}
.tablewrap{overflow-x:auto;border:1px solid var(--line);border-radius:8px}footer{margin-top:30px;color:var(--muted);font-size:12px;text-align:center}
code{font-family:Consolas,Menlo,monospace;font-size:12px;background:var(--tint);padding:1px 4px;border-radius:3px}.legend{font-size:12px;color:var(--muted);margin-top:6px}
@media (max-width:1000px){.kpis{grid-template-columns:repeat(2,1fr)}.grid3{grid-template-columns:1fr}.grid2{grid-template-columns:1fr}}
@media (max-width:560px){.kpis{grid-template-columns:1fr}main{padding:16px 16px 32px}header{padding:18px 16px}}
@media print{.tablewrap{overflow:visible}header,th,.badge,.bar-fill,.prop{-webkit-print-color-adjust:exact;print-color-adjust:exact}nav.toc,.toolbar{display:none}}
"""

HTML_JS = """
(function(){
  var data = JSON.parse(document.getElementById('repo-data').textContent);
  var rows = data.repositories || [];
  var tbody = document.getElementById('repo-body');
  var q = document.getElementById('repo-filter'), proj = document.getElementById('repo-project'), act = document.getElementById('repo-activity');
  var more = document.getElementById('repo-more'), count = document.getElementById('repo-count');
  var PAGE = 100, shown = 0, filtered = rows;
  function cell(tr, txt, cls){ var td = document.createElement('td'); if (cls) td.className = cls; td.textContent = (txt === null || txt === undefined) ? '' : String(txt); tr.appendChild(td); }
  function fmt(d, k){ if (!d) return ''; var v = d[k]; if (v === null || v === undefined) return ''; return d[k + '_capped'] ? (v + '+') : v; }
  function render(reset){ if (reset){ tbody.textContent = ''; shown = 0; }
    var end = Math.min(filtered.length, shown + PAGE);
    for (var i = shown; i < end; i++){ var r = filtered[i], d = r.details; var tr = document.createElement('tr');
      cell(tr, r.project_key); cell(tr, r.slug); cell(tr, r.name); cell(tr, r.state); cell(tr, r.archived ? 'oui' : ''); cell(tr, r.is_fork ? (r.origin || 'oui') : '');
      cell(tr, d ? (d.default_branch || '') : ''); cell(tr, fmt(d, 'branches'), 'num'); cell(tr, fmt(d, 'tags'), 'num'); cell(tr, fmt(d, 'commits'), 'num'); cell(tr, fmt(d, 'pull_requests'), 'num');
      cell(tr, d ? (d.last_commit_utc || '') : ''); cell(tr, d ? (d.activity_bucket || '') : ''); tbody.appendChild(tr); }
    shown = end; count.textContent = shown + ' / ' + filtered.length + ' dépôts affichés (' + rows.length + ' au total)'; more.style.display = shown < filtered.length ? '' : 'none'; }
  function apply(){ var s = (q.value || '').toLowerCase(), p = proj.value, a = act.value;
    filtered = rows.filter(function(r){ if (p && r.project_key !== p) return false; if (a && (!(r.details) || r.details.activity_bucket !== a)) return false;
      if (!s) return true; return ((r.slug || '') + ' ' + (r.name || '') + ' ' + (r.project_key || '') + ' ' + (r.origin || '')).toLowerCase().indexOf(s) >= 0; });
    render(true); }
  q.addEventListener('input', apply); proj.addEventListener('change', apply); act.addEventListener('change', apply); more.addEventListener('click', function(){ render(false); });
  render(true);
})();
"""


def synthesis_text(doc: Dict[str, Any]) -> List[str]:
    """Synthèse en quelques phrases, calculée à partir des résultats (texte brut, réutilisé en Markdown et en HTML)."""
    run, inst, s, vol, per = doc["run"], doc["instance"], doc["summary"], doc.get("volumetry") or {}, doc.get("perimeter") or {}
    bs = s.get("by_status", {})
    out = []
    vstat = inst.get("version_status") or "UNKNOWN"
    if run.get("dry_run"):
        return ["Mode dry-run : %d appel(s) GET prévus, aucun exécuté ; ce mode ne valide ni la connectivité ni le jeton." % run["calls_total"],
                "Verdict : relancer sans --dry-run pour exécuter la découverte."]
    if run.get("stopped_after_step") == 2:
        return ["Instance Bitbucket Data Center %s (build %s) lue, mais aucun projet retenu dans le périmètre (%s visibles) : découverte interrompue après l'étape 2." % (inst.get("version_observed"), inst.get("build_number"), per.get("projects_visible", "—")),
                "Exécution : %d appel(s) GET, %d écriture." % (run["calls_total"], run["calls_non_get"]),
                "Verdict : corriger --projects (clés exactes) ou --project-name-regex puis relancer ; aucun résultat de catalogue n'est exploitable."]
    if run.get("stopped_after_step"):
        return ["Instance : API-01 en état %s (%s) ; découverte interrompue après l'étape 1, aucune autre sonde exécutée." % (inst.get("collection_status"), inst.get("diagnostic")),
                "Exécution : %d appel(s) GET, %d écriture." % (run["calls_total"], run["calls_non_get"]),
                "Verdict : corriger l'accès (jeton, URL de base, proxy, certificat) puis relancer ; aucun résultat de catalogue n'est exploitable."]
    if vstat == "CONFIRMED":
        out.append("Instance Bitbucket Data Center %s (build %s), version confirmée par rapport à la déclaration (H-01 clôturée)." % (inst.get("version_observed"), inst.get("build_number")))
    elif vstat == "CONFLICTING":
        out.append("Instance Bitbucket Data Center %s (build %s) alors que la version déclarée est %s : écart à corriger avant validation du catalogue (H-01)." % (inst.get("version_observed"), inst.get("build_number"), inst.get("version_declared")))
    else:
        out.append("Version de l'instance non observable (API-01 : %s) : H-01 reste ouverte." % inst.get("collection_status"))
    out.append("Socle P0 : %d lignes disponibles sur %d ; %d ligne(s) à vérifier (ERROR ou UNSUPPORTED), %d en droits insuffisants (FORBIDDEN), %d non exécutée(s) (NOT_REQUESTED)."
               % (s["p0_available"], s["p0_total"], s["to_verify"], bs.get("FORBIDDEN", 0), bs.get("NOT_REQUESTED", 0)))
    projects = vol.get("projects", [])
    n_repos = vol.get("repositories_in_perimeter", 0)
    buckets = vol.get("activity_buckets") or {}
    out.append("Périmètre : %d projet(s) retenu(s) sur %s visibles, %d%s dépôt(s) inventorié(s) — dénominateur candidat D-06 — dont %d archivé(s) et %d fork(s)."
               % (len(projects), per.get("projects_visible", "—"), n_repos, "+" if vol.get("capped") else "", sum(p["archived"] for p in projects), sum(p["forks"] for p in projects)))
    if vol.get("sample_size"):
        out.append("Sur les %d dépôt(s) détaillé(s) : %d actif(s) (commit ≤ 90 j), %d à activité modérée (91–365 j), %d sans commit depuis plus d'un an, %d sans aucun commit."
                   % (vol["sample_size"], buckets.get("≤ 90 j", 0), buckets.get("91–365 j", 0), buckets.get("> 365 j", 0), buckets.get("sans commit", 0)))
    out.append("Exécution : %d appels GET, %d écriture, budget %s, mode non nominatif." % (run["calls_total"], run["calls_non_get"], "épuisé" if run.get("budget_exhausted") else "non atteint"))
    critical = any(p["level"] == "critical" for p in doc.get("propositions", []))
    if critical:
        out.append("Verdict : des points bloquants (ci-dessous) sont à lever avant le démarrage du lot 1.")
    elif s["p0_available"] == s["p0_total"]:
        out.append("Verdict : le lot 1 peut démarrer sur ce périmètre, sous réserve des points « à traiter » ci-dessous.")
    else:
        out.append("Verdict : le socle P0 est incomplet ; les lignes manquantes sont à traiter avant le lot 1.")
    return out


_LEADS = ("Socle P0", "Périmètre", "Exécution", "Verdict", "Instance", "Sur les")


def _lead(sentence: str) -> str:
    """Met en gras le mot-clé de tête d'une phrase de synthèse (« Socle P0 : », « Verdict : »…)."""
    for lead in _LEADS:
        if sentence.startswith(lead + " : "):
            return "<b>%s :</b> %s" % (html.escape(lead), html.escape(sentence[len(lead) + 3:]))
    return html.escape(sentence)


def html_report(doc: Dict[str, Any]) -> str:
    run, inst, caps, vol, per = doc["run"], doc["instance"], doc["capabilities"], doc.get("volumetry") or {}, doc.get("perimeter") or {}
    s = doc["summary"]
    e = html.escape
    vstat = inst.get("version_status") or "UNKNOWN"
    vcolor = {"CONFIRMED": "#00A193", "CONFLICTING": "#DB4B4B", "UNKNOWN": "#8C8C8C"}[vstat]
    projects = vol.get("projects", [])
    n_repos = vol.get("repositories_in_perimeter", 0)
    kpis = "".join([
        _kpi("Version de l'instance", str(inst.get("version_observed") or "inconnue"), "déclarée %s · %s" % (inst.get("version_declared") or "—", vstat), vcolor),
        _kpi("Lignes P0 disponibles", "%d / %d" % (s["p0_available"], s["p0_total"]), "socle du MVP", "#00A193" if s["p0_available"] == s["p0_total"] else "#DB4B4B"),
        _kpi("Lignes à vérifier", str(s["to_verify"]), "ERROR ou UNSUPPORTED sur une sonde", "#C98F00" if s["to_verify"] else "#00A193"),
        _kpi("Projets du périmètre", str(len(projects)), "%s visibles · mode %s" % (per.get("projects_visible", "—"), per.get("perimeter_mode", "—"))),
        _kpi("Dépôts inventoriés", "%d%s" % (n_repos, "+" if vol.get("capped") else ""), "dénominateur candidat D-06"),
        _kpi("Archivés · forks", "%d · %d" % (sum(p["archived"] for p in projects), sum(p["forks"] for p in projects)), "dans le périmètre", "#55579E"),
        _kpi("Dépôts détaillés", str(vol.get("sample_size", 0)), "branches, tags, commits, PR"),
        _kpi("Appels GET", str(run["calls_total"]), "non GET : %d · lecture seule" % run["calls_non_get"], "#00A193" if run["calls_non_get"] == 0 else "#DB4B4B"),
    ])
    by_status = [(st, s["by_status"].get(st, 0), STATUS_COLOR[st]) for st in STATUSES if s["by_status"].get(st, 0)]
    chart_status = _bar_chart([("%s %s" % (STATUS_ICON[st], st), v, c) for st, v, c in by_status])
    top_proj = sorted(projects, key=lambda p: -p["repositories"])[:20]
    chart_proj = _bar_chart([(p["key"], p["repositories"], "#581D74") for p in top_proj], label_w=150)
    buckets = vol.get("activity_buckets") or {}
    order = ["≤ 90 j", "91–365 j", "> 365 j", "sans commit", "non mesuré"]
    ramp = {"≤ 90 j": "#2E0F3E", "91–365 j": "#581D74", "> 365 j": "#8A5BA8", "sans commit": "#B9A3CC", "non mesuré": "#8C8C8C"}
    chart_act = _bar_chart([(k, buckets.get(k, 0), ramp[k]) for k in order if buckets.get(k, 0)], label_w=120)
    # capabilities table
    cap_rows = []
    for c in caps:
        probes = c.get("probes") or []
        det = "".join('<div class="probe">%s <code>%s</code> → %s%s%s</div>' % (e(p["name"]), e(p["endpoint_template"]), e(str(p.get("http_status") if p.get("http_status") is not None else "—")),
                      " · " + e(p.get("collection_status") or ""), (" · " + e(p.get("diagnostic") or "")) if p.get("collection_status") not in ("AVAILABLE",) else "") for p in probes)
        shapes = ", ".join(sorted({p.get("response_shape") for p in probes if p.get("response_shape")})) or "—"
        cap_rows.append("<tr><td><b>%s</b></td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td><details><summary>%d sonde(s)%s</summary>%s</details></td></tr>"
                        % (e(c["collection_id"]), e(c["purpose"]), e(c["priority"]), _badge(c["collection_status"]), "⚠" if c.get("to_verify") else "", e(shapes), len(probes), " · " + e(c.get("diagnostic")) if c.get("diagnostic") else "", det))
    proj_rows = "".join("<tr><td><b>%s</b></td><td>%s</td><td>%s</td><td class=\"num\">%d%s</td><td class=\"num\">%d</td><td class=\"num\">%d</td><td class=\"num\">%d</td><td>%s</td></tr>"
                        % (e(str(p["key"])), e(str(p.get("name") or "")), e(str(p.get("type") or "")), p["repositories"], "+" if p["capped"] else "", p["forks"], p["archived"], p["public_repos"], _badge(p["collection_status"])) for p in projects)
    props = "".join('<div class="prop" style="border-left-color:%s"><span class="lvl" style="color:%s">%s</span><b>%s</b>%s</div>' % (LEVEL_COLOR[p["level"]], LEVEL_COLOR[p["level"]], LEVEL_LABEL[p["level"]], e(p["title"]), e(p["text"])) for p in doc.get("propositions", []))
    project_options = "".join('<option value="%s">%s</option>' % (e(str(p["key"])), e("%s — %s" % (p["key"], p.get("name") or ""))) for p in projects)
    repo_data = json.dumps({"repositories": [{k: v for k, v in r.items()} for r in doc.get("repositories", [])]}, ensure_ascii=False).replace("</", "<\\/")
    calls_by_status: Dict[str, int] = {}
    for c in doc["calls"]:
        k = str(c.get("status") if c.get("status") is not None else "erreur")
        calls_by_status[k] = calls_by_status.get(k, 0) + 1
    samples = doc.get("samples") or {}
    notes = "".join("<li>%s</li>" % e(n) for n in doc.get("sample_notes", []))
    return """<!doctype html><html lang="fr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Découverte Bitbucket Data Center — %(host)s</title><style>%(css)s</style></head><body>
<header><h1>Découverte des capacités et inventaire — Bitbucket Data Center</h1>
<div class="meta">%(ts)s · hôte %(host)s · profil %(profile)s · périmètre %(pmode)s (%(pkeys)s) · outil v%(tool)s · %(rules)s</div>
<span class="attest">Lecture seule : %(calls)d appels GET · 0 écriture · mode non nominatif (aucune identité conservée)</span></header>
<main>
<nav class="toc"><a href="#kpis">Tableau de bord</a><a href="#props">Propositions</a><a href="#caps">Catalogue API</a><a href="#projects">Inventaire des projets</a><a href="#repos">Inventaire des dépôts</a><a href="#run">Exécution</a><a href="#lecture">Lecture des états</a></nav>
<h2 id="kpis">Tableau de bord</h2><div class="kpis">%(kpis)s</div>
<div class="grid3" style="margin-top:14px">
<div class="card"><h3>Lignes du catalogue par état de collecte</h3>%(chart_status)s<div class="legend">Une ligne = une famille de collecte (API-01 à API-25) ; l'état agrège ses sondes non optionnelles.</div></div>
<div class="card"><h3>Dépôts par projet du périmètre</h3>%(chart_proj)s<div class="legend">Comptage via API-03 ; « + » signale une liste tronquée par le plafond de pages.</div></div>
<div class="card"><h3>Activité des dépôts détaillés (dernier commit)</h3>%(chart_act)s<div class="legend">%(sample_size)d dépôt(s) détaillé(s) sur %(n_repos)d ; les dates de commit ne sont pas des données nominatives.</div></div></div>
<div class="grid2" style="margin-top:14px">
<div class="card"><h3>Instance (API-01)</h3><table class="kv"><tbody>
<tr><td>Version observée</td><td><b>%(vobs)s</b> (build %(bobs)s)</td></tr><tr><td>Version déclarée</td><td>%(vdec)s (build %(bdec)s)</td></tr>
<tr><td>Statut de la version</td><td><span class="badge" style="background:%(vcolor)s">%(vstat)s</span></td></tr><tr><td>État de collecte</td><td>%(vcs)s — %(vdiag)s</td></tr></tbody></table></div>
<div class="card"><h3>Périmètre et échantillon</h3><table class="kv"><tbody>
<tr><td>Mode de sélection</td><td>%(pmode)s</td></tr><tr><td>Projets</td><td>%(p_visible)s visibles · <b>%(p_kept)d retenus</b> · %(p_personal)s personnel(s) visible(s)</td></tr>
<tr><td>Dépôts</td><td><b>%(n_repos)d</b> inventoriés · %(n_detailed)d détaillés (mode %(details)s)</td></tr>
<tr><td>Échantillon des sondes</td><td class="small">%(samples)s</td></tr></tbody></table></div></div>
<h2 id="props">Synthèse et propositions</h2>%(synth)s%(props)s
<h2 id="caps">Catalogue API — état de collecte par ligne</h2>
<div class="tablewrap"><table><thead><tr><th>ID</th><th>Donnée recherchée</th><th>Prio.</th><th>État</th><th></th><th>Forme</th><th>Sondes et diagnostics</th></tr></thead><tbody>%(cap_rows)s</tbody></table></div>
<p class="legend">⚠ : au moins une sonde non optionnelle en ERROR ou UNSUPPORTED ; un HTTP 404 ne vaut pas UNSUPPORTED et se vérifie contre la documentation 9.4.</p>
<h2 id="projects">Inventaire des projets du périmètre</h2>
<div class="tablewrap"><table><thead><tr><th>Clé</th><th>Nom</th><th>Type</th><th class="num">Dépôts</th><th class="num">Forks</th><th class="num">Archivés</th><th class="num">Publics</th><th>État</th></tr></thead><tbody>%(proj_rows)s</tbody></table></div>
<h2 id="repos">Inventaire des dépôts</h2>
<div class="toolbar"><input id="repo-filter" type="search" placeholder="Filtrer : slug, nom, projet, origine…"><select id="repo-project"><option value="">Tous les projets</option>%(project_options)s</select>
<select id="repo-activity"><option value="">Toute activité</option><option>≤ 90 j</option><option>91–365 j</option><option>&gt; 365 j</option><option>sans commit</option><option>non mesuré</option></select><span class="count" id="repo-count"></span></div>
<div class="tablewrap"><table><thead><tr><th>Projet</th><th>Slug</th><th>Nom</th><th>État</th><th>Archivé</th><th>Fork de</th><th>Branche par défaut</th><th class="num">Branches</th><th class="num">Tags</th><th class="num">Commits</th><th class="num">PR</th><th>Dernier commit</th><th>Activité</th></tr></thead><tbody id="repo-body"></tbody></table></div>
<div class="toolbar"><button id="repo-more">Afficher 100 dépôts de plus</button><span class="small muted">Colonnes de détail renseignées pour les dépôts détaillés (--details) ; « + » = compte tronqué. Export complet : depots_*.csv</span></div>
<h2 id="run">Exécution</h2><div class="grid2"><div class="card"><h3>Appels HTTP par statut</h3><table><tbody>%(calls_rows)s</tbody></table></div>
<div class="card"><h3>Notes d'échantillonnage</h3><ul class="small">%(notes)s</ul><p class="small muted">Budget : %(budget)s · durée %(duration)ss · délai entre appels %(delay)ss</p></div></div>
<h2 id="lecture">Lecture des états de collecte</h2>
<div class="tablewrap"><table><thead><tr><th>État</th><th>Signification</th><th>Suite à donner</th></tr></thead><tbody>
<tr><td>%(b_av)s</td><td>première page lue avec des enregistrements</td><td>contrat observé exploitable : reporter les champs dans le catalogue</td></tr>
<tr><td>%(b_em)s</td><td>collecte autorisée, aucun résultat sur l'échantillon</td><td>normal pour forks, hooks, builds, rapports ; ne prouve rien au-delà de l'échantillon</td></tr>
<tr><td>%(b_pa)s</td><td>sondes d'une même ligne dans des états différents</td><td>lire le détail des sondes</td></tr>
<tr><td>%(b_un)s</td><td>HTTP 401</td><td>jeton absent, invalide ou expiré</td></tr>
<tr><td>%(b_fo)s</td><td>HTTP 403</td><td>revue des droits du compte ; aucune élévation automatique</td></tr>
<tr><td>%(b_us)s</td><td>HTTP 405, 410 ou 501</td><td>famille indisponible sur cette version</td></tr>
<tr><td>%(b_er)s</td><td>404, 5xx, 429 persistant, redirection ou erreur technique</td><td>un 404 ne vaut pas UNSUPPORTED : vérifier la route et l'échantillon</td></tr>
<tr><td>%(b_nr)s</td><td>famille exclue par le profil, échantillon indisponible, budget épuisé ou arrêt anticipé</td><td>relancer avec --profile extended, un autre --sample-repo ou --max-calls plus élevé</td></tr></tbody></table></div>
</main><footer>Généré par bitbucket_discovery.py v%(tool)s · Bitbucket Repository Intelligence · usage interne</footer>
<script id="repo-data" type="application/json">%(repo_data)s</script><script>%(js)s</script></body></html>""" % {
        "host": e(run["base_url_host"]), "css": HTML_CSS, "js": HTML_JS, "ts": e(run["timestamp_utc"]), "profile": e(run["profile"]), "pmode": e(run["perimeter"]["mode"]),
        "pkeys": e(", ".join(run["perimeter"]["project_keys"]) or "—"), "tool": e(TOOL_VERSION), "rules": e(RULES_VERSION), "calls": run["calls_total"], "kpis": kpis,
        "chart_status": chart_status, "chart_proj": chart_proj, "chart_act": chart_act, "sample_size": vol.get("sample_size", 0), "n_repos": n_repos,
        "p_visible": e(str(per.get("projects_visible", "—"))), "p_kept": len(projects), "p_personal": e(str(per.get("personal_projects_visible", 0))), "n_detailed": vol.get("sample_size", 0), "details": e(str(run.get("details", "—"))),
        "synth": '<div class="synth">%s</div>' % " ".join(_lead(t) for t in synthesis_text(doc)),
        "vobs": e(str(inst.get("version_observed") or "—")), "bobs": e(str(inst.get("build_number") or "—")), "vdec": e(str(inst.get("version_declared") or "—")), "bdec": e(str(inst.get("build_declared") or "—")),
        "vcolor": vcolor, "vstat": e(vstat), "vcs": e(str(inst.get("collection_status"))), "vdiag": e(str(inst.get("diagnostic"))),
        "samples": e(" · ".join("%s = %s" % (k, v) for k, v in samples.items() if v) or "—"), "props": props or '<p class="muted">Aucune proposition.</p>', "cap_rows": "".join(cap_rows), "proj_rows": proj_rows,
        "project_options": project_options, "repo_data": repo_data, "calls_rows": "".join("<tr><td>HTTP %s</td><td class=\"num\">%d</td></tr>" % (e(k), v) for k, v in sorted(calls_by_status.items())),
        "notes": notes or "<li>—</li>", "budget": "épuisé" if run.get("budget_exhausted") else "non atteint", "duration": run["duration_s"], "delay": run.get("delay", 0),
        "b_av": _badge("AVAILABLE"), "b_em": _badge("EMPTY"), "b_pa": _badge("PARTIAL"), "b_un": _badge("UNAUTHENTICATED"), "b_fo": _badge("FORBIDDEN"), "b_us": _badge("UNSUPPORTED"), "b_er": _badge("ERROR"), "b_nr": _badge("NOT_REQUESTED")}


# ============================================================================================
# Programme principal
# ============================================================================================
def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Découverte des capacités et inventaire Bitbucket Data Center — lecture seule ; sorties JSON, CSV, Markdown et HTML.")
    p.add_argument("--base-url", required=True, help="URL de base de l'instance, ex. https://bitbucket.exemple.local")
    p.add_argument("--projects", help="Clés de projet du périmètre pilote, séparées par des virgules (ex. BPCEIT,BPCECIB)")
    p.add_argument("--project-name-regex", help="Sinon : expression régulière sur le nom ou la clé des projets (ex. 'BPCE ?(IT|CIB)')")
    p.add_argument("--sample-repo", help="Dépôt échantillon imposé : CLE_PROJET/slug")
    p.add_argument("--profile", choices=("base", "extended"), default="extended", help="base = sans les familles à droits étendus ; extended = toutes (défaut)")
    p.add_argument("--details", choices=("sample", "all", "none"), default="sample", help="détails par dépôt (branches, tags, commits, PR, dernier commit) : échantillon (défaut), tous, aucun")
    p.add_argument("--sample-repos", type=int, default=25, help="Nombre de dépôts détaillés en mode sample (défaut 25)")
    p.add_argument("--declared-version", default="9.4.17", help="Version déclarée par l'exploitation (H-01)")
    p.add_argument("--declared-build", default="900417", help="Numéro de build déclaré")
    p.add_argument("--token-file", help="Fichier contenant le jeton d'accès HTTP (sinon variable BITBUCKET_TOKEN)")
    p.add_argument("--basic-user", help="Authentification basique : utilisateur (mot de passe dans BITBUCKET_PASSWORD)")
    p.add_argument("--ca-bundle", help="Chemin du bundle de certificats de l'entreprise (PEM)")
    p.add_argument("--insecure", action="store_true", help="Désactive la vérification TLS (à éviter)")
    p.add_argument("--no-proxy", action="store_true", help="Ignore HTTPS_PROXY / HTTP_PROXY")
    p.add_argument("--timeout", type=int, default=30)
    p.add_argument("--delay", type=float, default=0.1, help="Pause entre deux appels, en secondes")
    p.add_argument("--max-retries", type=int, default=3)
    p.add_argument("--max-calls", type=int, default=5000, help="Budget total d'appels")
    p.add_argument("--max-pages", type=int, default=50, help="Pages maximales par liste ; au-delà : tronqué")
    p.add_argument("--no-volumetry", action="store_true", help="Pas d'inventaire ni de détails (sondes seules)")
    p.add_argument("--dry-run", action="store_true", help="Liste les appels prévus sans exécuter aucune requête")
    p.add_argument("--out", default="./discovery_out", help="Dossier de sortie")
    p.add_argument("--verbose", action="store_true")
    return p.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    token = None
    if args.token_file:
        with open(args.token_file, "r", encoding="utf-8") as fh:
            token = fh.read().strip()
    else:
        token = os.environ.get("BITBUCKET_TOKEN")
    basic = (args.basic_user, os.environ.get("BITBUCKET_PASSWORD", "")) if (not token and args.basic_user) else None
    if not token and not basic and not args.dry_run:
        log.error("Aucun secret fourni : BITBUCKET_TOKEN, --token-file ou --basic-user + BITBUCKET_PASSWORD")
        return 2
    if args.insecure:
        log.warning("Vérification TLS désactivée")
    client = ReadOnlyClient(args.base_url, token, basic, args.ca_bundle, args.insecure, args.no_proxy, args.timeout, args.delay, args.max_retries, args.max_calls, args.dry_run)
    t0 = time.time()
    disc = Discovery(client, args)

    log.info("1/6 instance (API-01)")
    instance = disc.instance()
    log.info("version observée : %s (build %s) → %s", instance.get("version_observed"), instance.get("build_number"), instance.get("version_status"))
    stopped_after_step: Optional[int] = None
    volumetry: Optional[Dict[str, Any]] = None
    perimeter: Dict[str, Any] = {}
    if not args.dry_run and instance.get("collection_status") not in ("AVAILABLE", "EMPTY"):
        # arrêt anticipé : sans instance lisible (401, 403, 404 sur l'URL de base, TLS, proxy…), les étapes suivantes n'ont pas de sens
        stopped_after_step = 1
        reason = "arrêt anticipé après l'étape 1 : API-01 %s (%s)" % (instance.get("collection_status"), instance.get("diagnostic"))
        log.error("API-01 %s — %s ; découverte interrompue, les rapports sont tout de même produits", instance.get("collection_status"), instance.get("diagnostic"))
        capabilities = disc.probe_catalogue(skip_reason=reason)
    else:
        log.info("2/6 périmètre (API-02)")
        perimeter = disc.resolve_perimeter()
        log.info("projets visibles : %s · retenus : %d (%s)", perimeter.get("projects_visible"), len(perimeter["perimeter_projects"]), disc.perimeter_mode)
    if stopped_after_step is None and not args.dry_run and not perimeter.get("perimeter_projects"):
        stopped_after_step = 2
        log.error("aucun projet retenu (clés introuvables : %s ; clés visibles hors projets personnels : %s) ; découverte interrompue, les rapports sont tout de même produits",
                  ", ".join(perimeter.get("missing_project_keys") or []) or "—", ", ".join(perimeter.get("visible_project_keys") or []) or "aucune")
        capabilities = disc.probe_catalogue(skip_reason="arrêt anticipé après l'étape 2 : aucun projet retenu dans le périmètre")
    if stopped_after_step is None:
        if not args.no_volumetry and not args.dry_run:
            log.info("3/6 inventaire des dépôts (API-03)")
            disc.inventory()
            log.info("dépôts inventoriés : %d", len(disc.repos))
        log.info("4/6 échantillon")
        disc.resolve_samples()
        log.info("échantillon : %s", {k: v for k, v in disc.samples.items() if v})
        log.info("5/6 sondes du catalogue")
        capabilities = disc.probe_catalogue()
    if not args.no_volumetry and not args.dry_run and stopped_after_step is None:
        log.info("6/6 détails par dépôt (%s)", args.details)
        n = disc.repo_details()
        buckets: Dict[str, int] = {}
        for r in disc.repos:
            if r.get("details"):
                b = r["details"].get("activity_bucket") or "non mesuré"
                buckets[b] = buckets.get(b, 0) + 1
        volumetry = {"projects": disc.project_rows, "repositories_in_perimeter": len(disc.repos), "capped": any(p["capped"] for p in disc.project_rows),
                     "denominator_candidate": {"definition": "dépôts des projets du périmètre pilote listés par API-02/API-03 avec le compte de service (D-06)", "value": len(disc.repos), "complete": not any(p["capped"] for p in disc.project_rows)},
                     "sample_size": n, "details_mode": args.details, "activity_buckets": buckets}

    by_status = {st: sum(1 for c in capabilities if c["collection_status"] == st) for st in STATUSES}
    by_status = {k: v for k, v in by_status.items() if v}
    summary = {"by_status": by_status, "p0_total": sum(1 for c in capabilities if c["priority"] == "P0"),
               "p0_available": sum(1 for c in capabilities if c["priority"] == "P0" and c["collection_status"] in ("AVAILABLE", "EMPTY")), "to_verify": sum(1 for c in capabilities if c.get("to_verify"))}
    non_get = sum(1 for c in client.calls if c.get("method") != "GET")
    host = re.sub(r"^https?://", "", args.base_url).split("/")[0]
    doc: Dict[str, Any] = {
        "run": {"timestamp_utc": _now(), "tool_version": TOOL_VERSION, "rules_version": RULES_VERSION, "base_url_host": host, "profile": args.profile,
                "perimeter": {"mode": disc.perimeter_mode, "project_keys": [p.get("key") for p in disc.perimeter_projects]}, "declared_version": args.declared_version, "declared_build": args.declared_build,
                "read_only": True, "non_nominative_mode": True, "dry_run": args.dry_run, "details": args.details, "calls_total": len(client.calls), "calls_non_get": non_get,
                "budget_exhausted": client.budget_exhausted, "duration_s": round(time.time() - t0, 1), "delay": args.delay, "stopped_after_step": stopped_after_step},
        "instance": instance, "perimeter": perimeter,
        # le nom du groupe échantillon n'est pas conservé (un nom de groupe peut désigner une personne) ; seule sa présence est attestée
        "samples": dict(disc.samples, group=("1 groupe (nom non conservé)" if disc.samples.get("group") else None)), "sample_notes": disc.sample_notes, "capabilities": capabilities, "summary": summary,
        "volumetry": volumetry, "repositories": disc.repos, "calls": client.calls}
    doc["propositions"] = propositions(doc)
    os.makedirs(args.out, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    jpath = os.path.join(args.out, "discovery_%s.json" % stamp)
    with open(jpath, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, ensure_ascii=False, indent=2)
    mpath = os.path.join(args.out, "discovery_%s.md" % stamp)
    with open(mpath, "w", encoding="utf-8") as fh:
        fh.write(markdown_report(doc))
    hpath = os.path.join(args.out, "discovery_%s.html" % stamp)
    with open(hpath, "w", encoding="utf-8") as fh:
        fh.write(html_report(doc))
    csvs = write_csvs(doc, args.out, stamp)
    log.info("terminé : %d appels GET, %d appel(s) non GET — JSON %s · HTML %s · MD %s · CSV %d fichiers", len(client.calls), non_get, jpath, hpath, mpath, len(csvs))
    log.info("états : %s · P0 disponibles %d/%d · à vérifier %d", by_status, summary["p0_available"], summary["p0_total"], summary["to_verify"])
    # codes de retour : 0 = terminé ; 1 = appel non GET détecté (ne doit jamais arriver) ; 2 = paramètres ; 3 = arrêt anticipé (instance injoignable ou jeton refusé)
    if non_get:
        return 1
    return 3 if stopped_after_step else 0


if __name__ == "__main__":
    sys.exit(main())
