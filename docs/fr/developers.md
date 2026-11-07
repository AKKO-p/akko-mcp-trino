# Développeurs

[English](../en/developers.md) · **Français**

## Architecture

Chaque module de `akko_mcp_trino/` a une seule responsabilité et ses tests, dans `tests/` :

| Module | Responsabilité |
|---|---|
| `config.py` | lire l'environnement, des valeurs par défaut neutres |
| `auth.py` | la vérification JWT avec le JWKS, `Principal` |
| `identity.py` | l'appelant vérifié dans une ContextVar |
| `middleware.py` | la garde : token, identifiant de requête, gardes et documents des extensions |
| `extensions.py` | les points d'extension et leur chargement |
| `transport_security.py` | le filtre de l'en-tête Host (`MCP_ALLOWED_HOSTS`) |
| `audit.py` | la ligne d'audit, l'identifiant de requête, l'enregistrement et le budget de temps d'un appel |
| `sql_guard.py` | la validation des identifiants, la décision de lecture seule sur l'arbre |
| `tools.py`, `plugins.py` | les neuf tools et le `ToolContext`, le chargement des plugins de tools |
| `trino_client.py` | la connexion à Trino, les modes d'identité (`jwt`, `impersonation`, `oauth2`, `service` dans le cœur) et les budgets de temps |
| `token_exchange.py` | OAuth 2.0 Token Exchange (RFC 8693) : un token pour Trino en échange de celui de l'appelant |
| `health.py`, `metrics.py` | `/health`, `/ready` et `/metrics` ; les compteurs Prometheus |
| `server.py`, `app.py`, `__main__.py` | l'assemblage, le transport, le point d'entrée et `--check` |
| `context/` | les fournisseurs de contexte : protocole, types, chaîne, filtre par les droits de Trino, le fournisseur `file` |
| `testing/` | les contrats que passe chaque fournisseur de contexte, seul et derrière le filtre (`FakeTrino`), pour les tests propres des fournisseurs |

`tests/test_core_alone.py` liste ce que contient le cœur (tools, modules, réglages, champs d'audit)
et échoue le jour où quelque chose est ajouté sans être listé.

## Développement

```bash
pip install --require-hashes -r requirements/build.txt -r requirements/dev.txt && pip install --no-deps --no-build-isolation -e .
ruff check akko_mcp_trino tests examples && ruff format akko_mcp_trino tests examples
ruff check akko_mcp_trino --select D100,D101,D102,D103,D105,D107   # chaque nom public documenté
mypy akko_mcp_trino           # le paquet livre py.typed et passe la vérification de types
pip-audit --disable-pip --require-hashes -r requirements/image.txt -r requirements/dev.txt -r requirements/release.txt   # aucune vulnérabilité connue dans les verrous
bash scripts/lock.sh check    # les verrous suivent leurs sources (CONTRIBUTING.fr.md, « Dépendances et verrous »)
bash lint-vendor-neutral.sh   # échoue si le paquet importe quoi que ce soit de propre à un produit
pytest                        # une couverture des lignes ou des branches sous 100 % fait échouer l'exécution
integration/run.sh            # en vrai : Keycloak, Trino et trois serveurs dans Docker, puis supprimés
python -m build --no-isolation && twine check dist/*
akko-mcp-trino --check        # la configuration effective, sans secret ; code 2 si le serveur ne démarrerait pas
```

La documentation est testée elle aussi : chaque variable qu'elle liste est lue par le code, et les
valeurs par défaut qu'elle donne sont celles du code. [CONTRIBUTING.fr.md](../../CONTRIBUTING.fr.md)
donne les règles de base et les verrous.

## Plugins de tools

Un produit construit sur ce serveur peut ajouter ses propres tools sans le forker. Un paquet
enregistre une fonction d'enregistrement `(mcp, client) -> None`, ou
`(mcp, client, tools) -> None`, dans le groupe d'entrée `akko_mcp_trino.tools`, et l'administrateur
la nomme dans `MCP_TOOL_PLUGINS` :

```toml
[project.entry-points."akko_mcp_trino.tools"]
my-tools = "my_package.tools:register"
```

```python
from akko_mcp_trino.identity import current_subject


def register(mcp, client):
    @mcp.tool()
    def my_tool(text: str) -> str:
        return client.query("SELECT ...", user=current_subject())
```

Rien n'est chargé qui ne soit nommé, et un nom inconnu empêche le démarrage. Les tools ajoutés ainsi
passent par le même client, donc par le même mode d'identité que les tools intégrés, et chaque tool
qu'une fonction d'enregistrement ajoute par `mcp.tool()` ou `mcp.add_tool()` laisse la même ligne
d'audit, sans rien changer au plugin. `build_server(extra_tool_registrars=...)` fait la même chose
depuis Python.

Une fonction d'enregistrement à trois arguments reçoit le `ToolContext` :
`tools.tool(title, governed=..., open_world=...)` enregistre un tool avec ses annotations MCP,
`tools.run(sql)` interroge Trino par le mode d'identité, `tools.note(...)` enregistre ce qu'a fait
l'appel (le SQL lancé, les tables, les lignes, si elles ont été coupées),
`tools.context.tables(refs)` lit la description courte, tirée du catalogue, des tables que Trino
prouve à l'appelant (`tools.context` vaut None sans fournisseur), et `tools.add_describer(fn)`
ajoute à ce que rend `describe_table` :

```python
import json


def register(mcp, client, tools):
    @tools.tool("Ask the data", governed=True, idempotent=False)
    def ask_data(question: str) -> str:
        answer = my_service.ask(question)  # runs its SQL through tools.run
        tools.note(sql=answer["sql"], tables=answer["tables"], rows=len(answer["rows"]))
        return json.dumps(answer)
```

## Extensions

Un plugin de tools ajoute des tools. Une extension peut ajouter davantage, autour du cœur et
jamais à sa place. Un paquet enregistre une fabrique `(config, env) -> Extension` dans le groupe
d'entrée `akko_mcp_trino.extensions`, et l'administrateur la nomme dans `MCP_EXTENSIONS` (séparées
par des virgules, dans l'ordre) :

```toml
[project.entry-points."akko_mcp_trino.extensions"]
my-extension = "my_package.extension:create"
```

```python
from akko_mcp_trino.extensions import Extension, Refusal


class MyExtension(Extension):
    name = "my-extension"

    def after_auth(self, admission):
        if admission.principal is not None and admission.principal.subject.startswith("svc-"):
            return Refusal("service_accounts_refused", status=403)
        return None


def create(config, env):
    return MyExtension()
```

Rien n'est chargé qui ne soit nommé. Un nom inconnu empêche le démarrage, de même qu'une fabrique
qui lève `ValueError` sur un réglage qu'elle ne peut pas servir ; `--check` signale les deux.
`akko_mcp_trino.extensions.Extension` est la classe de base : chaque point d'accroche ne fait rien,
donc une extension ne redéfinit que ce qu'elle ajoute.

| Point d'accroche | Ce qu'il ajoute |
|---|---|
| `check_lines()` | des lignes qu'affiche `--check`, jamais un secret |
| `identity_modes()` | des modes d'identité par nom de `TRINO_IDENTITY_MODE`, chacun un `IdentityMode` (`describe`, `connection(user)`, `ping`) ; un mode nommé comme un mode du cœur (`jwt`, `impersonation`, `oauth2`, `service`) le remplace |
| `audiences()` | des audiences de token acceptées en plus de `MCP_OIDC_AUDIENCE` et `MCP_RESOURCE_URL` ; jamais celle de Trino |
| `before_auth(admission)` | un refus avant tout examen du token, ou `None` |
| `after_auth(admission)` | un refus une fois que le cœur a vérifié le token (ou n'en a trouvé aucun), ou `None` |
| `public_documents()` | des documents JSON servis sans token, par chemin de requête ; ils passent avant ceux du cœur |
| `challenge(refusal)` | la valeur `WWW-Authenticate` d'un refus ; elle passe avant celle du cœur |
| `routes()` | des routes Starlette ajoutées à l'application servie, derrière la garde d'identité |
| `wrap_audit(audit)` | la destination d'audit où écrit le serveur, à partir de celle où il écrirait sinon |
| `client_tags()` | les étiquettes client d'une requête lancée maintenant (`X-Trino-Client-Tags`) |
| `describe_table_doc()` | un texte ajouté à la description de `describe_table` |
| `tool_registrars()` | des fonctions d'enregistrement de tools, appelées comme celles d'un plugin, avant celles des plugins |

Une garde répond un `Refusal(reason, status=401, error="", headers={}, message="")` : la raison va
dans `X-Reason` et dans le corps, `error` est le code RFC 6750 que peut porter un challenge,
`headers` sont ajoutés à la réponse (`Retry-After`), et `message`, quand il est posé, dit au client
dans le corps quoi faire. Elle lit une `Admission` : les en-têtes de la requête, le token que la
requête a présenté, le `principal` vérifié (None avant l'authentification ou sans token), `stdio`
(vrai pour une admission en stdio) ; `admission.bind(var, value)` pose une variable de contexte pour
la durée de la requête, remise à zéro ensuite. En stdio, un processus est admis une fois, au
démarrage, sauf avec `TRINO_IDENTITY_MODE=oauth2` : les gardes tournent alors à chaque requête qui
n'est pas de la découverte (voir
[Connexion navigateur native](identity.md#connexion-navigateur-native-oauth2-en-stdio-uniquement)),
jamais au démarrage ni pour la découverte, et toujours après que Trino a nommé l'utilisateur :
`before_auth` voit donc déjà ce `principal`, et une liaison dure une requête. Une garde qui compte
les sessions, ou qui refuse au démarrage pour empêcher le serveur de servir, doit en tenir compte.

La règle est simple : **une extension peut seulement refuser, jamais élargir.** Ses gardes tournent autour de
la vérification du token par le cœur, jamais à sa place ; le mode strict refuse toujours une
requête sans identité vérifiée, quoi que réponde une garde, et la garde de lecture seule sur le SQL
s'applique à chaque requête. Un mode d'identité décide de qui est le serveur pour Trino, requête par
requête (`akko_mcp_trino.trino_client.resolve_identity` choisit le mode que nomme
`TRINO_IDENTITY_MODE` parmi ceux du cœur et des extensions, et refuse un mode inconnu : il n'y a pas
de repli sur `service`). Un mode qui envoie à Trino un token pour l'appelant doit en obtenir un
pour Trino, jamais transmettre celui de l'appelant : `JwtIdentity.trino_token(bearer)` est le point
d'accroche. Un plugin qui appelle son propre backend pour l'appelant lit le bearer vérifié avec
`akko_mcp_trino.identity.current_bearer()`, et doit l'échanger de la même façon plutôt que de le
transmettre. Trino décide de ce que cette identité peut lire.

## Écrire un fournisseur de contexte

Un fournisseur est une classe qui implémente le protocole `akko_mcp_trino.context.ContextProvider` :
`name`, `context_api` (à fixer à `akko_mcp_trino.context.CONTEXT_API` ; le serveur refuse une autre
version), `maps(catalog)`, `served_sets()`, `describe(refs, deadline=…)`, `terms(keys, deadline=…)`,
`check_lines()`, `aclose()` et `credentials_stamp()`. Il peut ajouter une méthode facultative
`reset()`, que le serveur appelle sur `SIGHUP`, depuis son gestionnaire de signal, pour qu'il
oublie ce qu'il gardait de son catalogue : une fonction ordinaire, jamais `async def` (rien ne
l'attend), qui rend `None` tout de suite, ne bloque jamais, n'envoie aucune requête et ne lève
rien. Une méthode qui lève est écrite au journal avec le nom du fournisseur et le type de
l'erreur, jamais son texte, et le contrat vérifie le reste quand `reset()` existe. Son paquet enregistre une fabrique
`(env) -> fournisseur` dans le groupe d'entrée `akko_mcp_trino.context`, par exemple
`mycatalog = "my_package.provider:from_env"` sous `[project.entry-points."akko_mcp_trino.context"]`.

La fabrique lit les variables du fournisseur dans `env`, lève `ValueError` quand elle ne peut pas
les servir, et ne joint pas son catalogue. Un secret se lit avec
`akko_mcp_trino.context.secret_setting(env, name)`, pour que `<NOM>_FILE` marche aussi. Un fournisseur
suit ces règles :

- il ne sert que les ensembles que l'administrateur autorise par leur nom exact, une liste par
  genre (glossaire, classification, propriété, propriétaire), rien par défaut, et `served_sets()`
  dit ce qu'il sert ;
- il rend les noms comme Trino les liste, en minuscules : les tables, les clés de colonnes, le
  grain et les jointures. Le cœur compare les noms exactement : un nom dans une autre casse est
  perdu ;
- il ne rend que les types du cœur (`TableContext`, `ColumnContext`, `Term`...), jamais de données :
  ni valeur, ni échantillon ;
- il respecte l'échéance reçue, lève `ProviderRefused` quand son compte est refusé et
  `ProviderUnavailable` quand le catalogue est en panne, en retard ou rend ce qu'il ne sait pas
  lire. Le texte de ses erreurs reste dans le serveur ; ses journaux et `check_lines()` ne
  contiennent jamais son secret, ni un nom de schéma, de table ou de colonne ;
- il n'importe ni `akko_mcp_trino.trino_client` ni `akko_mcp_trino.identity`, ne reçoit jamais le
  client Trino ni l'identité de l'appelant, et n'écrit jamais dans son catalogue.

`akko_mcp_trino.testing` publie le contrat qui vérifie ces règles. On l'installe dans
l'environnement de test du fournisseur avec `pip install "akko-mcp-trino[contract]"`, qui apporte
`pytest` par une plage de versions, sans l'épingler. Il ne demande ni réseau, ni catalogue, ni Trino
: le fournisseur envoie ses requêtes à un transport qui répond avec les réponses enregistrées du
catalogue, ou, exprès, en retard, par un 401, un 503, un corps qui n'est pas du JSON ou un 500 qui
contient le secret. On sous-classe `ContextContract` dans les tests du fournisseur et on lui donne
une fixture `harness` qui rend un `ContractHarness` : la fabrique `build(env, transport)`, la
fonction `replay` qui rejoue les réponses enregistrées, les réglages `env`, le `secret` qu'ils
contiennent, et les `ContractNames` (catalogue mappé et non mappé, table décrite, table partitionnée
et sa colonne de partition, entité supprimée, table inconnue, nom piégé, colonne à terme, glossaire
hors liste). Les réglages servent le glossaire du terme, et le propriétaire quand le catalogue en
connaît un. Le secret est cherché tel quel, échappé en JSON, encodé dans une URL, écrit par Python
et en base64 : en choisir un qui contient un guillemet droit, une barre oblique inverse et un
caractère non ASCII. Les journaux sont lus sur le logger racine : un logger qui ne propage pas
échappe au contrôle. La fabrique du point d'entrée reste dans le module de la classe du fournisseur,
que le contrôle des imports charge seul. Un exemple complet :

```python
import pytest
from akko_mcp_trino.context import TableRef
from akko_mcp_trino.testing import ContextContract, ContractHarness, ContractNames

from my_package.provider import MyProvider
from tests.recordings import replay  # answers an httpx.Request from the recorded answers

# A quote, a backslash and an accent, so that a secret escaped or encoded is still found.
SECRET = 'a-"secret\\é-to-find'

NAMES = ContractNames(
    catalog="warehouse",                  # a Trino catalog the provider maps
    unmapped_catalog="lakeside",          # one it does not map
    described=TableRef("warehouse", "ledger", "invoices"),
    partitioned=TableRef("warehouse", "ledger", "payments"),
    partition_column="booking_month",
    dropped=TableRef("warehouse", "ledger", "refunds"),     # marked deleted in the catalogue
    unknown=TableRef("warehouse", "ledger", "ghosts"),      # not in the catalogue
    trap=TableRef("warehouse", "o'brien.été", "l'année fiscale.v2"),
    term_column="invoice_total",          # a column of `described` with a served term
    foreign_glossary="personnel",         # a glossary outside the allow list
)


class TestMyProviderContract(ContextContract):
    @pytest.fixture
    def harness(self):
        return ContractHarness(
            build=lambda env, transport: MyProvider(env, transport=transport),
            replay=replay,
            env={
                "MY_CATALOGUE_TOKEN": SECRET,
                "MY_CATALOGUE_GLOSSARIES": "finance",
                "MY_CATALOGUE_SERVE_OWNER": "true",
            },
            secret=SECRET,
            names=NAMES,
        )
```

Le contrat vérifie que le contexte revient avec ses colonnes de partition, qu'une table inconnue et
une entité supprimée sont omises, qu'un catalogue non mappé ne coûte aucune requête, que rien hors
de `served_sets()` n'est rendu et que ces ensembles suivent les réglages (sans ses listes
autorisées, un fournisseur décrit toujours la table et ne sert aucun ensemble ; le propriétaire,
quand le fournisseur en déclare un, n'est servi que si son réglage le demande, et un fournisseur
dont le catalogue ne connaît pas de propriétaire n'en déclare pas et saute ce contrôle), qu'aucun
type ne porte de valeur, qu'un catalogue lent rend la main avant l'échéance plus
`ContextContract.REPLY_MARGIN_SECONDS` (0,5 s) sans laisser de requête ouverte, qu'un 401 lève
`ProviderRefused` sans être renvoyé, qu'un délai, un 503 ou un corps illisible lèvent
`ProviderUnavailable`, que ni les journaux (`httpx` à `WARNING`, comme le serveur le règle), ni
`check_lines()`, relu après les erreurs du catalogue, ni une exception ne contiennent le secret, que
journaux et `check_lines()` ne contiennent aucun nom d'objet, que le module du fournisseur ne charge
rien du côté Trino du cœur, qu'une fabrique qui refuse un réglage mal formé n'en met pas le secret
dans son message (chaque réglage qui le contient est rendu entouré d'espaces, puis de guillemets),
qu'un nom piégé est trouvé sous son nom exact, et que tous les noms sont en minuscules. Un
fournisseur qui n'envoie aucune requête, comme `file`, saute les vérifications qui en demandent une,
avec `no HTTP`. La vérification de `snapshot` arrive avec le préchauffage en 0.8.x et est sautée en
0.8.

`ContextContract.DEADLINE_SECONDS` (5 s) est l'échéance que les vérifications donnent à un appel qui
doit réussir, et `SLOW_DEADLINE_SECONDS` (1 s) celle qu'elles donnent à un catalogue qui ne répond
pas. Ce sont des attributs de classe : les relever dans la sous-classe quand la CI est lente, en
gardant le second bien en dessous de 30 s, le temps que met le catalogue lent à répondre.

Le filtre appartient au cœur, et il est prouvé sur chaque fournisseur : `GateContract` construit le
serveur tel qu'il tourne, avec le fournisseur et son catalogue enregistré, dans les modes d'identité
`jwt` et `impersonation` (le second avec `MCP_CONTEXT_REQUIRE_SELECT=true`), et met `FakeTrino` à la
place de Trino. `FakeTrino` répond aux requêtes du cœur selon ce que voit chaque utilisateur
(`Visible` : les tables, les colonnes que Trino liste de chacune, et les tables qu'il liste mais
dont il refuse la lecture) et enregistre chaque requête avec son utilisateur, son token pour Trino,
son texte et ses étiquettes client (`Statement`). Chaque vérification tourne deux fois : avec un
Trino qui répond un objet caché comme absent (un contrôle d'accès qui filtre les métadonnées), puis
avec un Trino qui le refuse (`PERMISSION_DENIED`, comme des règles ;
`FakeTrino(..., existing=...)`). Un appelant voit toutes les tables du harnais sauf un objet : une
table, puis une colonne de la table décrite, puis la cible d'une de ses jointures. Le contrat
vérifie que rien de cet objet, ni son nom ni aucun texte que le catalogue lui donne à lui seul,
n'atteint une réponse, un statut de `context_sources`, une ligne d'audit, une ligne de journal ou
une métrique, chaque canal contrôlé à part ; que chaque requête part sous l'identité de l'appelant,
avec son token en `jwt`, et ne nomme que ce que Trino lui a listé, celles de l'agent aussi ce que
l'agent a écrit, et que la seule requête qui nomme une table cachée est le `DESCRIBE` que l'agent en
a demandé ; qu'une table cachée et une table absente (`names.unknown`) reçoivent la même chose du
cœur (clés, types et statuts hors l'erreur de Trino, ligne d'audit hors son code d'erreur,
requêtes) ; qu'avec `MCP_CONTEXT_REQUIRE_SELECT=true`, une table que Trino liste mais dont il refuse
la lecture reçoit ses colonnes, aucun contexte et `not_checked`. Un appelant qui voit tout reçoit
les trois objets, pour que les vérifications testent quelque chose. On l'ajoute à la classe
ci-dessus, avec une fixture `gate` :

```python
from akko_mcp_trino.testing import GateContract, GateHarness


class TestMyProviderContract(ContextContract, GateContract):
    # the harness fixture above, then:
    @pytest.fixture
    def gate(self, harness):
        return GateHarness(
            contract=harness,
            hidden_table=TableRef("warehouse", "ledger", "payroll"),         # described
            hidden_column="discount_code",                                   # of `described`
            hidden_join_target=TableRef("warehouse", "ledger", "payments"),  # joined from it
        )
```

Les trois objets sont dans le catalogue enregistré, écrits comme Trino les liste et comme un agent
nomme une table (lettres, chiffres, `_` et `-`), longs de quatre caractères au moins, et leurs noms ne
font partie d'aucun nom ni d'aucun texte que l'appelant a le droit de voir : le contrat les cherche,
avec chaque texte que le catalogue leur donne à eux seuls, dans tout ce que l'appelant reçoit.
`tests/context/test_gate_contract_file.py` le lance sur `file`.

Un catalogue qui n'a pas de notion de jointure laisse `hidden_join_target` de côté (`None`) : le
scénario de la jointure sort de chaque vérification,
`test_a_hidden_join_target_is_checked_when_the_catalogue_gives_joins` est sautée et dit pourquoi, et
le contrat échoue si le fournisseur rend quand même une jointure pour une table qu'il décrit (« the
provider gives joins: set hidden_join_target ») : un fournisseur qui a des jointures ne peut pas les
laisser sans contrôle. Avec une cible, cette vérification joue le scénario de la jointure seul.
