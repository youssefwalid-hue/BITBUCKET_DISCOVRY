# Découverte des capacités Bitbucket Data Center — `bitbucket_discovery.py` v2.0

Outil de lancement du pilote **Bitbucket Repository Intelligence** (spécification v1.3, sections III.1, III.5, III.6, III.8 et V.1).
Il confirme la version de l'instance, inventorie les dépôts du périmètre pilote, sonde les 25 lignes du catalogue de collecte, mesure l'activité des dépôts et produit un tableau de bord HTML, un JSON, cinq exports CSV et un rapport Markdown, avec une synthèse et des propositions d'actions.

Le mode d'emploi complet (installation, configuration, lecture du rapport, dépannage) est dans `GUIDE_UTILISATEUR_discovery.md` (et sa version Word en charte BPCE).

## Nouveautés de la version 2.0

- **Aucune dépendance externe** : client HTTP sur `urllib` (bibliothèque standard), compatible avec les postes sans accès à `pip`.
- **Rapport HTML autonome** : tableau de bord (KPI, graphiques), synthèse propositionnelle, catalogue avec détail des sondes, inventaire des projets, inventaire des dépôts filtrable, journal d'exécution.
- **Inventaire complet** des dépôts du périmètre (dénominateur D-06) et détails d'activité par dépôt (`--details sample|all|none`).
- **Cinq exports CSV** (projets, dépôts, capacités, volumétrie, appels), séparateur « ; », UTF-8 avec BOM pour Excel.
- **Arrêt anticipé** explicite (jeton refusé, instance injoignable, périmètre vide) avec rapports produits et code de retour 3.

## Garanties

- **Lecture seule par construction** : le client HTTP ne construit que des requêtes `GET` et le vérifie à chaque appel ; le JSON atteste `calls_non_get = 0`.
- **Mode non nominatif** (D-05) : aucun payload conservé ; seuls les noms de champs, les comptages, les statuts HTTP et les identifiants techniques (clé de projet, slug, SHA, numéro de PR) sont écrits. Le nom du groupe échantillon n'est pas conservé. Les projets personnels sont comptés, jamais inventoriés.
- **Aucun secret dans les sorties ni dans les journaux** ; les paramètres de requête ne sont pas journalisés.
- **Robustesse** : pagination par curseur (`start` / `limit` / `isLastPage` / `nextPageStart`), reprise bornée avec backoff et `Retry-After` sur 429 et 5xx, budget d'appels, délai entre appels, redirections non suivies.

## Prérequis

- Python 3.8 ou plus, bibliothèque standard uniquement.
- Le jeton d'accès HTTP du compte de service (D-02), fourni par la variable `BITBUCKET_TOKEN` ou par `--token-file` (fichier en mode 600). Authentification basique possible (`--basic-user` + `BITBUCKET_PASSWORD`).
- Le bundle de certificats de l'entreprise si l'instance est servie avec une autorité interne (`--ca-bundle`). Proxys via `HTTPS_PROXY` / `NO_PROXY` (`--no-proxy` pour les ignorer).

## Lancement

```bash
export BITBUCKET_TOKEN='<jeton du compte de service>'
python bitbucket_discovery.py \
  --base-url https://bitbucket.exemple.local \
  --projects BPCEIT,BPCECIB \
  --declared-version 9.4.17 --declared-build 900417 \
  --ca-bundle /etc/ssl/certs/entreprise.pem \
  --out ./discovery_out
```

Les clés de projet exactes de BPCE IT et BPCE CIB sont à renseigner dans `--projects`. Si elles ne sont pas connues, `--project-name-regex 'BPCE ?(IT|CIB)'` sélectionne les projets par leur nom ; sans l'un ni l'autre, tous les projets visibles sont pris en compte (note dans le rapport). En cas de clé inexacte, l'outil s'arrête et liste les clés visibles.

| Option | Effet |
|---|---|
| `--profile base` | n'exécute pas les familles à droits étendus (API-14 à API-19, API-21) ; défaut `extended` (D-02) |
| `--details sample\|all\|none` | détails par dépôt (branches, tags, commits, PR, dernier commit) : 25 dépôts (défaut), tous, aucun |
| `--sample-repos N` | nombre de dépôts détaillés en mode `sample` (défaut 25) |
| `--sample-repo CLE/slug` | impose le dépôt échantillon des sondes |
| `--max-pages N` | plafond de pages par liste (100 éléments par page) ; au-delà, comptage marqué « + » (défaut 50) |
| `--max-calls N` | budget total d'appels (défaut 5 000) ; au-delà, sondes restantes NOT_REQUESTED |
| `--delay S` / `--timeout S` / `--max-retries N` | pause entre appels (0,1 s), délai par appel (30 s), reprises sur 429 et 5xx (3) |
| `--no-volumetry` | sondes seules |
| `--dry-run` | liste les appels prévus sans exécuter aucune requête |
| `--verbose` | un message par appel (chemin et statut, sans paramètre ni secret) |

Codes de retour : 0 terminé · 1 appel non GET détecté (ne doit jamais arriver) · 2 paramètres ou secret absent · 3 arrêt anticipé (rapports produits).

Durée indicative : une soixantaine d'appels pour les sondes ; une page par tranche de 100 dépôts et par projet pour l'inventaire ; environ six appels par dépôt détaillé.

## Sorties

Huit fichiers horodatés dans `--out` :

- `discovery_<date>.html` — rapport autonome : bandeau d'attestation, tuiles KPI, graphiques (états du catalogue, dépôts par projet, activité), instance et périmètre, **synthèse et propositions** (Acquis / Information / À traiter / Bloquant), catalogue API avec sondes dépliables, inventaire des projets, inventaire des dépôts filtrable, exécution, lecture des états.
- `discovery_<date>.json` — la source de vérité : `run`, `instance`, `perimeter`, `samples`, `sample_notes`, `capabilities`, `summary`, `volumetry`, `repositories`, `calls`, `propositions`.
- `discovery_<date>.md` — synthèse, catalogue, volumétrie, propositions et notes : la pièce à joindre au compte rendu.
- `projets_<date>.csv`, `depots_<date>.csv`, `capacites_<date>.csv`, `volumetrie_<date>.csv`, `appels_<date>.csv` — exports tabulaires (voir le guide, tableau « Exports CSV »).

## Lecture des états de collecte (spécification III.5)

| État | Signification dans la découverte | Suite à donner |
|---|---|---|
| AVAILABLE | première page lue avec des enregistrements | contrat observé exploitable : reporter `keys_observed` dans `L10_catalogue_api.yaml` |
| EMPTY | collecte autorisée, aucun résultat sur l'échantillon | normal pour forks, hooks, builds, rapports Insights ; ne prouve rien au-delà de l'échantillon |
| PARTIAL | sondes d'une même ligne dans des états différents | lire le détail des sondes |
| UNAUTHENTICATED | 401 | jeton absent, invalide ou expiré ; arrêt après l'étape 1 |
| FORBIDDEN | 403 | droits du compte à revoir pour cette famille ; aucune élévation automatique |
| UNSUPPORTED | 405, 410 ou 501 établi | famille indisponible sur cette version : ligne à retirer du socle |
| ERROR | 404, 5xx, 429 persistant, redirection ou erreur technique | **un 404 ne vaut pas UNSUPPORTED** : vérifier la route contre la documentation 9.4 et l'échantillon |
| NOT_REQUESTED | profil, échantillon indisponible, budget épuisé ou arrêt anticipé | relancer avec `--profile extended`, un autre `--sample-repo` ou `--max-calls` plus élevé |

Les sondes marquées « variante » (`branches/default`, `build-status` historique) sont informatives : leur résultat n'entre pas dans l'état de la ligne.

## Ce que l'on fait des résultats

1. `instance.version_status` renseigne H-01 : CONFIRMED clôt l'hypothèse ; CONFLICTING impose de corriger la version déclarée avant toute validation du catalogue.
2. Chaque ligne AVAILABLE ou EMPTY devient `compatibility_checked: true` dans le catalogue ; ses `keys_observed` servent à valider les correspondances de champs (III.6).
3. Chaque ligne ERROR ou UNSUPPORTED est traitée avant le lot 1 si elle est P0, et peut rester ouverte si elle est P1 ou P2.
4. `volumetry.denominator_candidate` fournit la valeur du dénominateur par défaut (D-06) et les ordres de grandeur de H-02 ; une mesure tronquée (`capped`) est relancée avec `--max-pages` plus élevé.
5. Les tranches d'activité, les dépôts archivés et les forks orientent l'échantillon de contrôle (≥ 50 dépôts, V.5).
6. Le rapport HTML et le JSON sont archivés avec la date d'exécution : ils constituent la preuve de la découverte des capacités exigée par la séquence de démarrage.

## Test hors ligne

Un simulateur minimal permet de vérifier l'outil sans accès à l'instance :

```bash
python mock_bitbucket_server.py --port 8089 --token test-token
BITBUCKET_TOKEN=test-token python bitbucket_discovery.py --base-url http://127.0.0.1:8089 --projects BPCEIT,BPCECIB --delay 0 --out ./out_test
```

Le simulateur reproduit la pagination Data Center, un dépôt vide, un dépôt archivé, un fork, des dates de dernier commit variées, des 404 sur `readme` et `license`, un 403 sur les permissions de groupe au niveau projet et un 429 avec `Retry-After` sur les affectations globales ; il refuse toute méthode autre que GET. Le dossier `exemples/` contient les sorties de ce test (HTML, JSON, Markdown, CSV) et une capture du tableau de bord.

## Limites

- Les sondes lisent une première page : elles établissent l'accès et le contrat, pas l'exhaustivité ; l'inventaire et les détails, eux, paginent jusqu'au plafond configuré.
- Les familles P2 (tokens, clés, secret scanning, audit, Jira, plugins) ne sont pas sondées : leurs endpoints restent à documenter avant activation (III.6).
- Un échantillon unique peut masquer des différences entre projets (hooks hérités, restrictions) ; relancer avec `--sample-repo` sur un dépôt de chaque périmètre si besoin.
- L'activité est mesurée sur la branche par défaut (dernier commit) ; les dépôts détaillés en mode `sample` ne représentent pas le périmètre entier.
