# akko-mcp-trino-atlas

Fournisseur de contexte Apache Atlas pour
[akko-mcp-trino](https://github.com/AKKO-p/akko-mcp-trino). Il lit ce qu'Atlas sait des tables que
Trino liste pour l'appelant (descriptions, classifications, termes de glossaire, métadonnées métier)
et le donne à `describe_table` et `describe_schema`, à travers le filtre du serveur par les droits
de Trino. Il lit Atlas avec un compte à lui, par l'API REST v2 seulement, et n'y écrit jamais.

*Version anglaise : [README.md](README.md).*

> **État.** Les tables, les colonnes et les termes de glossaire se lisent, avec les listes
> blanches et la haute disponibilité, par authentification Basic, jeton Knox ou Kerberos
> (SPNEGO). Le fournisseur passe le contrat de contexte du serveur sur des réponses enregistrées
> sur Apache Atlas 2.1, la version de référence. Kerberos n'est testé que contre une doublure
> de sa bibliothèque, et le jeton Knox que contre un faux Atlas : ni l'un ni l'autre n'est encore
> prouvé devant un vrai KDC ou une vraie passerelle Knox.

## Installation

```bash
pip install "akko-mcp-trino[atlas]"     # le cœur avec ce fournisseur et Kerberos
export MCP_CONTEXT_PROVIDERS=atlas
```

Seul : `pip install akko-mcp-trino-atlas` (Basic et Knox), ou
`pip install "akko-mcp-trino-atlas[kerberos]"` pour Kerberos, qui apporte `pyspnego` et ses
liaisons GSSAPI (bibliothèques MIT Kerberos sur le système).

Le fournisseur s'enregistre sous le groupe de points d'entrée `akko_mcp_trino.context`, sous le
nom `atlas`. Rien n'est chargé tant que `MCP_CONTEXT_PROVIDERS` ne le nomme pas ; l'image de
conteneur d'akko-mcp-trino l'embarque, inactif, sans les bibliothèques Kerberos pour l'instant.

## Réglages

Le serveur refuse de démarrer quand le fournisseur n'a pas d'adresse, pas de moyen de
s'authentifier ou pas d'`ATLAS_CLUSTERS`, ou quand un réglage est mal formé. Aucun message ne
cite une adresse, un proxy ni un secret.

| Variable | Rôle | Défaut |
|---|---|---|
| `ATLAS_URL` | adresse d'Atlas, `http` ou `https` ; plusieurs, séparées par des virgules, pour un Atlas en haute disponibilité. Jamais avec des identifiants dedans. | obligatoire |
| `ATLAS_AUTH` | `basic` (un utilisateur et un mot de passe, ou un jeton Knox) ou `kerberos` | `basic` |
| `ATLAS_USER`, `ATLAS_PASSWORD` ou `ATLAS_PASSWORD_FILE` | authentification Basic, avec un compte en lecture seule ; le fichier passe en premier | vide |
| `ATLAS_KNOX_TOKEN` ou `ATLAS_KNOX_TOKEN_FILE` | un jeton Knox, envoyé en `Authorization: Bearer`, pour un Atlas derrière une topologie Knox avec le rôle `ATLAS` ; pas avec `ATLAS_USER` | vide |
| `ATLAS_PRINCIPAL`, `ATLAS_KEYTAB` | Kerberos (SPNEGO), avec `ATLAS_AUTH=kerberos` : le principal du compte et le chemin de son keytab. Le chemin n'est jamais affiché. | vide |
| `ATLAS_SPN` | avec `ATLAS_AUTH=kerberos`, le nom de service sous lequel tourne Atlas, `<service>@<hôte>`, pris pour chaque adresse d'`ATLAS_URL` : à poser quand toutes les adresses partagent un principal (le nom d'un répartiteur de charge ou d'un service) ; avec un principal par serveur en haute disponibilité, le laisser vide | `HTTP@<hôte de chaque adresse>` |
| `ATLAS_CLUSTERS` | objet JSON, du catalogue Trino vers le cluster Atlas : `{"lake": "primary"}`. Les catalogues sont repliés en minuscules, comme Trino les liste ; deux qui donnent le même nom sont refusés. Un catalogue absent n'est pas couvert et rien n'est demandé à son sujet. | obligatoire |
| `ATLAS_TABLE_TYPES` | types d'entités sous lesquels une table est cherchée, dans l'ordre ; le premier qui la contient l'emporte | `hive_table,iceberg_table` |
| `ATLAS_MAX_QUERY_CHARS` | longueur maximale de la chaîne de requête d'une lecture par lot ; un lot de tables est découpé en dessous | `6000` |
| `ATLAS_GLOSSARIES` | glossaires dont les termes peuvent être servis, par nom exact (le suffixe du nom qualifié d'un terme, `<terme>@<glossaire>`) | vide : aucun |
| `ATLAS_CLASSIFICATIONS` | classifications qui peuvent être servies, par nom exact | vide : aucune |
| `ATLAS_BUSINESS_METADATA` | ensembles de métadonnées métier qui peuvent être servis, par nom exact | vide : aucun |
| `ATLAS_OWNER_ATTRIBUTE` | l'attribut de métadonnée métier qui porte le propriétaire d'une table, `<ensemble>.<attribut>` | vide : pas de propriétaire |
| `ATLAS_TERM_STATUS_ATTRIBUTE` | l'attribut du terme (dans `additionalAttributes`) qui porte son statut | vide |
| `ATLAS_CA_BUNDLE` | autorités de certification contre lesquelles vérifier le certificat d'Atlas | celles du système |
| `ATLAS_PROXY` | proxy `http` ou `https` pour joindre Atlas ; les réglages de proxy de l'environnement ne sont jamais lus | aucun |
| `ATLAS_TIMEOUT_SECONDS` | délai d'une requête à Atlas | `5` |
| `ATLAS_MAX_CONCURRENT_REQUESTS` | lectures de termes de glossaire envoyées à la fois, de 1 à 32 | `4` |

Un secret donné par fichier (`*_FILE`) est relu quand le fichier change.

**Sans listes blanches, seules les descriptions sont servies.** Les trois listes sont fermées
par défaut : vides, elles ne servent aucun terme de glossaire, aucune classification et aucune
métadonnée métier, et `*` est un nom comme un autre, jamais un joker. Chaque ensemble se nomme.
Le serveur retire encore tout ce qui en sort.

## Lecture des tables

Une table Trino `catalogue.schéma.table` est l'entité Atlas dont le nom qualifié est
`schéma.table@<cluster du catalogue>`, lue par
`GET /api/atlas/v2/entity/bulk/uniqueAttribute/type/{type}` pour tout le lot que demande le
serveur, sous chaque type d'`ATLAS_TABLE_TYPES`. Seuls les noms que Trino a listés sont
demandés ; rien n'est cherché. Le schéma et la table sont les noms de Trino, en minuscules ;
le cluster est envoyé tel qu'`ATLAS_CLUSTERS` l'écrit, car Atlas compare un nom qualifié casse
comprise.

Un type dont Atlas dit qu'il n'existe pas (Atlas 2.4 répond 400 avec `ATLAS-400-00-014` pour
`iceberg_table` sans le modèle Iceberg) ne donne rien et n'est plus demandé. **Un
administrateur qui ajoute un type de table dans Atlas envoie `SIGHUP` au serveur**, et le
fournisseur le demande de nouveau ; un changement du fichier du secret aussi.

- La description est celle du responsable (`userDescription`), sinon `description`, sinon le
  commentaire du métastore. Les colonnes viennent de `columns` et de `partitionKeys`.
- Une entité qui n'est pas `ACTIVE` est ignorée, de même qu'une colonne, une clé de partition
  ou un terme de glossaire dont la relation ou la cible n'est pas `ACTIVE`, et qu'une
  classification propagée depuis une entité qui ne l'est pas.
- Chaque nom rendu est replié en minuscules, comme Trino le liste. Quand deux colonnes d'Atlas
  donnent le même nom Trino, celle déjà écrite en minuscules l'emporte, sinon la première dans
  l'ordre des points de code, quel que soit l'ordre de la réponse d'Atlas ; entre types, le
  premier type d'`ATLAS_TABLE_TYPES` qui contient la table l'emporte.
- Une valeur de métadonnée métier est servie en texte, une liste jointe par des virgules.

## Lecture des termes de glossaire

Un terme se lit par son GUID, `GET /api/atlas/v2/glossary/term/{guid}`, une fois par clé
distincte que demande le serveur. La première lecture part seule, pour qu'un compte refusé ne
coûte qu'une requête et que les suivantes réutilisent sa session ; les autres partent au plus
`ATLAS_MAX_CONCURRENT_REQUESTS` à la fois. L'échéance de l'appel les couvre toutes : dépassée,
le fournisseur se dit indisponible pour l'appel et n'en rend aucune partie.
`GET /api/atlas/v2/glossary/terms/{guid}/assignedEntities` n'est jamais appelé.

- Le glossaire est le suffixe du nom qualifié du terme (`<terme>@<glossaire>`). Un terme hors
  d'`ATLAS_GLOSSARIES`, ou dont le nom qualifié ne nomme aucun glossaire, n'est jamais servi.
- La définition est `shortDescription`, sinon `longDescription`. Le statut est
  `additionalAttributes.<ATLAS_TERM_STATUS_ATTRIBUTE>` quand ce réglage est posé, jamais le
  statut d'un rattachement.
- Les termes liés viennent de six relations du terme, `synonyms`, `preferredTerms`,
  `preferredToTerms`, `seeAlso`, `isA` et `replacedBy` (servies en `synonym`, `preferred`,
  `preferred_to`, `see_also`, `is_a`, `replaced_by`), et les catégories par leur nom, le tout
  tiré de la réponse du terme lui-même, sans autre requête. Un terme lié d'un glossaire hors de
  la liste blanche, ou sans nom qualifié, est écarté sans être lu. Les autres relations
  (`classifies`, `replacementTerms`, `antonyms`, `translationTerms`, `validValues`...) ne sont
  pas lues. Le serveur ne nomme un terme lié que s'il est rattaché à un objet que l'appelant voit.
- Un terme qu'Atlas n'a pas (404 avec `ATLAS-404-00-005`) est écarté.

## Réseau, haute disponibilité et erreurs

Le client ne lit rien dans l'environnement (pas de proxy implicite), envoie `X-XSRF-HEADER` et
un `User-Agent` à lui, et garde le cookie de session `ATLASSESSIONID`.

| Atlas répond | Le fournisseur |
|---|---|
| une connexion refusée, un délai dépassé ou un 5xx | essaie l'adresse suivante d'`ATLAS_URL`, puis se dit indisponible |
| une redirection vers une adresse d'`ATLAS_URL` (le serveur passif d'Atlas HA) | la suit |
| une redirection ailleurs (une connexion Knox sur un autre hôte) | se dit refusé tout de suite, sans essayer d'autre adresse, pour qu'aucune boucle d'essais ne verrouille le compte |
| 401 ou 403 | se dit refusé tout de suite ; le cookie de session est oublié |
| un 200 qui n'est pas du JSON (une page de connexion Knox), un JSON illisible, un autre statut | se dit indisponible |
| 404 avec `ATLAS-404-00-009` à une lecture de tables (aucune instance) | ne sait rien de ces tables |
| 404 avec `ATLAS-404-00-005` à une lecture de terme (aucun GUID) | écarte ce terme |
| 400 avec `ATLAS-400-00-014` (aucun type) | ne sait rien sous ce type, et s'en souvient jusqu'au `SIGHUP` |
| tout autre 400 ou 404 (une page, un autre code, un code de l'autre lecture, un mauvais chemin derrière Knox) | se dit indisponible |

Indisponible, il écrit au journal `atlas unreachable`, sans adresse, utilisateur ni nom
d'objet ; un refus est écrit par le serveur, `atlas refused the account`. Ni le journal ni une
erreur ne portent le texte d'une réponse ou le secret.

## Kerberos (SPNEGO)

Avec `ATLAS_AUTH=kerberos`, le fournisseur répond au `WWW-Authenticate: Negotiate` d'Atlas par un
jeton de `pyspnego`. Chaque négociation obtient un ticket neuf depuis le keytab, gardé dans un cache
en mémoire (`MEMORY:`) : un ticket expiré n'est jamais utilisé et aucun processus à part ne le
renouvelle ; c'est une requête au KDC. Une fois qu'Atlas a ouvert une session (`ATLASSESSIONID`),
aucune nouvelle négociation n'a lieu tant qu'Atlas n'en demande pas, et une redirection du serveur
passif d'Atlas renégocie avec l'actif. Une négociation qui échoue rend le fournisseur indisponible
pour l'appel, sans essayer d'autre adresse. L'authentification mutuelle d'Atlas est laissée à TLS
(`https` et `ATLAS_CA_BUNDLE`).

Ce qu'il faut au conteneur :

- l'extra `kerberos` (`ATLAS_AUTH=kerberos` sans lui refuse de démarrer) ;
- un `krb5.conf` (`/etc/krb5.conf`, ou `KRB5_CONFIG`) qui nomme le royaume et son KDC, avec
  `rdns = false`, car une adresse derrière un répartiteur de charge ou un service Kubernetes n'a
  pas de nom inverse qui corresponde au principal de service d'Atlas (`ATLAS_SPN` le nomme) ;
- une route vers le KDC (port 88) depuis le pod : les règles réseau de Kubernetes et d'OpenShift
  doivent la laisser passer ;
- une horloge à l'heure du KDC (cinq minutes d'écart au plus, par défaut) ;
- le keytab monté en lecture seule, comme un secret, jamais dans l'image.

`--check` affiche le principal, jamais le keytab.

## Le compte avec lequel le fournisseur lit

1. **Lecture seule.** Le compte doit lire les entités, les glossaires et leurs termes, rien
   d'autre : aucune écriture, aucune administration. Sur un Atlas à utilisateurs propres, un
   utilisateur du rôle `DATA_SCIENTIST` suffit : les réponses enregistrées du contrat ont été lues
   avec un tel compte. Sur un Atlas gouverné par Ranger, une règle du service `atlas` qui ne fait
   que lire.
2. **Il voit ce qui est servi à tous.** Le fournisseur sert ce que lit ce compte, dans la limite
   des listes blanches, à chaque appelant qui voit l'objet dans Trino. Ne lui donnez que les
   glossaires, classifications et métadonnées métier que toute personne qui voit la table peut
   lire.
3. **Un refus l'arrête, sans boucle d'essais.** Un compte refusé (401, 403, ou une redirection
   vers une page de connexion sur un autre hôte) n'est plus sollicité pendant
   `MCP_CONTEXT_REFUSED_RETRY_SECONDS` (3600 par défaut, un réglage du serveur), ou jusqu'au
   changement de son fichier secret, ou jusqu'à `SIGHUP`, pour que le fournisseur ne le
   verrouille jamais dans un annuaire qui compte les échecs de connexion.

## Vérifier la configuration

`akko-mcp-trino --check` affiche les adresses (jamais avec des identifiants), l'authentification,
le principal pour Kerberos, les catalogues, les ensembles servis et la borne des lectures en
parallèle. Il demande ensuite sa version à Atlas (`GET /api/atlas/admin/version`) et affiche
`atlas: answers, version <version>`, ou une ligne qui commence par `UNREACHABLE:` quand Atlas
refuse le compte ou ne répond pas ; `--check --strict` sort alors avec le code 2.

## Versions d'Atlas

| Atlas | Rôle |
|---|---|
| Apache Atlas 2.1 | référence |
| Apache Atlas 2.4 | compatibilité |

Les tests rejouent des réponses enregistrées, dans `tests/recordings/` :

- `atlas-2.1-seed` : une version d'Atlas 2.1, garnie d'un catalogue neutre qui tient chaque rôle
  des contrats du serveur, et lu par un compte qui ne peut que lire ;
- `atlas-2.1` : un vrai catalogue de la même version, rendu neutre ;
- `atlas-2.4` : Apache Atlas 2.4.0, construit depuis les sources officielles.

Le contrat de contexte du serveur (`ContextContract`) et son contrat du filtre (`GateContract`, le
filtre du serveur par les droits de Trino prouvé de bout en bout sur le fournisseur) tournent sur
`atlas-2.1-seed` et sur `atlas-2.1` ; une vérification qui demande un rôle absent du vrai catalogue
est sautée et dit pourquoi. Atlas n'a pas de notion de jointure : le scénario de la jointure est
sauté, et le contrat échouerait si le fournisseur en rendait une. Chaque nom et chaque texte d'un
vrai catalogue a été remplacé par un nom neutre, structure gardée, par
`tests/recordings/anonymize.py` ; sa docstring dit comment enregistrer et anonymiser une nouvelle
version.

## Licence

Licence Apache 2.0, comme akko-mcp-trino.
