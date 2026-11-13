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
> sur Apache Atlas 2.1, la version de référence. Kerberos (SPNEGO) est prouvé devant un vrai KDC,
> avec des tickets de courte durée renouvelés depuis le keytab. Le jeton Knox n'est testé que
> contre un faux Atlas : il n'est pas encore prouvé derrière une vraie passerelle Knox.

## Installation

```bash
pip install "akko-mcp-trino[atlas]"     # le cœur avec ce fournisseur et Kerberos
export MCP_CONTEXT_PROVIDERS=atlas
```

Sous Linux, l'extra `atlas` compile les liaisons Kerberos (`gssapi`, `krb5`) depuis les sources :
il lui faut un compilateur C et les en-têtes MIT Kerberos (`libkrb5-dev` sous Debian et Ubuntu,
`krb5-devel` sous Red Hat). Avec l'authentification Basic ou un jeton Knox, installer sans eux :
`pip install akko-mcp-trino akko-mcp-trino-atlas`.

Seul : `pip install akko-mcp-trino-atlas` (Basic et Knox), ou
`pip install "akko-mcp-trino-atlas[kerberos]"` pour Kerberos, qui apporte `pyspnego` et ses
liaisons GSSAPI (bibliothèques MIT Kerberos sur le système).

Le fournisseur s'enregistre sous le groupe de points d'entrée `akko_mcp_trino.context`, sous le nom
`atlas`. Rien n'est chargé tant que `MCP_CONTEXT_PROVIDERS` ne le nomme pas ; l'image de conteneur
d'akko-mcp-trino l'embarque, inactif, avec son extra `kerberos` et les bibliothèques MIT Kerberos.

## Réglages

Le serveur refuse de démarrer quand le fournisseur n'a pas d'adresse, pas de moyen de
s'authentifier ou pas d'`ATLAS_CLUSTERS`, ou quand un réglage est mal formé. Aucun message ne
cite une adresse, un proxy ni un secret.

| Variable | Rôle | Défaut |
|---|---|---|
| `ATLAS_URL` | adresse d'Atlas, `http` ou `https` ; plusieurs, séparées par des virgules, pour un Atlas en haute disponibilité. Jamais avec des identifiants dedans. Un mot de passe ou un jeton Knox sur `http` refuse de démarrer (`ATLAS_PASSWORD or ATLAS_KNOX_TOKEN (or its file) is set but an address of ATLAS_URL is not https`), comme le serveur refuse `TRINO_PASSWORD` sans https. Kerberos sur `http` démarre, avec un avertissement au démarrage et dans `--check` : le compte est authentifié, mais rien n'authentifie Atlas ni ne protège ses réponses et son cookie de session, qui pourrait être rejoué au nom du compte jusqu'à son expiration. | obligatoire |
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
| `ATLAS_TIMEOUT_SECONDS` | délai d'une requête à Atlas ; avec plusieurs adresses, voir plus bas son rapport à `MCP_CONTEXT_TIMEOUT_SECONDS` | `5` |
| `ATLAS_CONNECT_TIMEOUT_SECONDS` | délai pour ouvrir une connexion à une adresse, inférieur à `ATLAS_TIMEOUT_SECONDS`, pour qu'une adresse muette laisse la place à la suivante dans le temps de l'appel | `1` |
| `ATLAS_MAX_CONCURRENT_REQUESTS` | lectures de termes de glossaire envoyées à la fois, de 1 à 32 ; avec Kerberos, aussi les négociations menées à la fois | `4` |
| `ATLAS_MAX_RESPONSE_BYTES` | taille maximale du corps d'une réponse d'Atlas une fois décodé, au moins `1048576` ; la borne vaut par lecture, pas par appel, et un appel peut tenir plusieurs lectures à la fois (lectures par lot, puis jusqu'à `ATLAS_MAX_CONCURRENT_REQUESTS` lectures de termes) ; la lecture s'arrête au-delà, avec ou sans `Content-Length`, et l'appel est `unavailable` pour lui seul : aucune autre adresse n'est essayée et aucune pause de réessai ne s'ouvre. Un avertissement par appel conseille de baisser `MCP_CONTEXT_BATCH` | `67108864` |

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
comprise. Une table dont le schéma ou le nom contient un point n'est jamais demandée et ne reçoit
aucun contexte : le schéma `a` avec la table `b.c` et le schéma `a.b` avec la table `c` seraient
la même entité.

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
| une connexion refusée, ou aucune ouverte en `ATLAS_CONNECT_TIMEOUT_SECONDS` | essaie l'adresse suivante d'`ATLAS_URL`, et commence par elle aux appels suivants ; puis se dit indisponible |
| pas de réponse à temps de l'adresse par laquelle l'appel a commencé : un délai de lecture ou d'écriture dépassé, ou l'échéance de l'appel lui-même atteinte pendant qu'il l'attend | essaie l'adresse suivante d'`ATLAS_URL` s'il reste du temps à l'appel, commence par elle aux appels suivants, et l'écrit une fois au journal (plus bas) ; puis se dit indisponible |
| une autre erreur de transport (une connexion fermée sous l'appel, une erreur du proxy), un corps qui ne se décode pas, ou un 5xx (son corps non lu) | essaie l'adresse suivante d'`ATLAS_URL`, puis se dit indisponible ; les appels suivants commencent là où ils commençaient |
| une redirection vers une adresse d'`ATLAS_URL` (le serveur passif d'Atlas HA) | la suit, et commence par cette adresse aux appels suivants |
| une redirection ailleurs (une connexion Knox sur un autre hôte) | se dit refusé tout de suite, sans essayer d'autre adresse, pour qu'aucune boucle d'essais ne verrouille le compte |
| 401, ou un 403 autre que celui d'en dessous | se dit refusé tout de suite ; le cookie de session est oublié |
| 403 avec `ATLAS-403-00-001` (Atlas refuse au compte une partie des objets demandés) | ne sert rien de cette lecture (tout le lot de tables, ou ce terme), reste ouvert, et écrit une fois pour l'appel `atlas refused some objects to the account; an account with no read grant serves nothing` ; les tables de ce lot reçoivent le statut `not_found`, exprès, le même qu'une table qu'Atlas ne connaît pas, pour qu'un agent ne puisse pas distinguer un objet refusé d'un objet absent |
| un 200 qui n'est pas du JSON (une page de connexion Knox), un JSON illisible, un autre statut | se dit indisponible |
| un corps dans un codage de contenu autre qu'aucun, `identity`, `gzip` ou `deflate`, ou dans plusieurs (le client demande `Accept-Encoding: gzip, deflate`) | n'en lit rien, et se dit indisponible pour cet appel seul, comme ci-dessous ; écrit une fois pour l'appel `atlas answered in a content coding other than gzip or deflate` |
| un corps plus long qu'`ATLAS_MAX_RESPONSE_BYTES` une fois décodé | cesse de le lire, et se dit indisponible pour cet appel seul : aucune autre adresse n'est essayée, les appels suivants commencent là où ils commençaient, aucune pause de réessai ne s'ouvre ; écrit une fois pour l'appel `atlas answered more than ATLAS_MAX_RESPONSE_BYTES for one read; lower MCP_CONTEXT_BATCH` |
| 404 avec `ATLAS-404-00-009` à une lecture de tables (aucune instance) | ne sait rien de ces tables |
| 404 avec `ATLAS-404-00-005` à une lecture de terme (aucun GUID) | écarte ce terme |
| 400 avec `ATLAS-400-00-014` (aucun type) | ne sait rien sous ce type, et s'en souvient jusqu'au `SIGHUP` |
| tout autre 400 ou 404 (une page, un autre code, un code de l'autre lecture, un mauvais chemin derrière Knox) | se dit indisponible |

Une panne que signale Atlas, ou aucune adresse qui répond, écrit au journal `atlas unreachable`,
sans adresse, utilisateur ni nom d'objet ; une lecture coupée par l'échéance de l'appel n'écrit
que la ligne d'adresse plus bas, quand l'adresse était en retard. Un refus est écrit par le
serveur, `atlas refused the account`. Ni le journal ni une
erreur ne portent le texte d'une réponse ou le secret.

**Un serveur qui accepte la connexion puis ne répond plus** (figé) est laissé : quand l'adresse
par laquelle un appel a commencé ne répond pas à temps, les appels suivants commencent par
l'adresse suivante d'`ATLAS_URL`. Le fournisseur l'écrit une fois au journal, par sa position,
jamais son adresse : `atlas address 2 of 3 in ATLAS_URL did not answer in time` (`--check`
affiche les adresses dans cet ordre). La ligne n'est pas redite pour cette adresse tant qu'elle
n'a pas répondu ; les appels qui l'attendent ensemble écrivent une ligne à eux tous, et deux
adresses bloquées écrivent deux lignes. Seule l'échéance de l'appel lui-même compte, et seulement
contre une adresse qui a tenu l'attente au moins `ATLAS_CONNECT_TIMEOUT_SECONDS`, ou qui n'a pas
répondu depuis `ATLAS_TIMEOUT_SECONDS` avant le début de l'attente (une adresse bloquée est donc
laissée même sous une échéance plus courte que le délai de connexion). Une attente plus courte
coupée par l'échéance sur une adresse qui venait de répondre (le temps est parti sur une autre
adresse, ou sur une lecture précédente du même appel d'outil), ou une lecture annulée pour une
autre raison (une autre lecture du même appel refusée, le client parti, un arrêt), ne laisse
aucune adresse et n'écrit rien. L'appel qui rencontre l'adresse bloquée reçoit encore son
contexte ou non selon deux réglages :

- avec `ATLAS_TIMEOUT_SECONDS` inférieur à `MCP_CONTEXT_TIMEOUT_SECONDS` d'au moins le temps que
  l'adresse suivante met à répondre, négociation Kerberos comprise, la requête à l'adresse
  bloquée expire d'abord, et l'adresse suivante répond dans le même appel ; avec une marge plus
  courte, ce que cet appel lit après l'adresse bloquée est perdu, et les appels suivants
  commencent par l'adresse suivante, sauf si l'échéance a aussi coupé sur elle une attente d'au
  moins `ATLAS_CONNECT_TIMEOUT_SECONDS`, ou une attente alors qu'elle n'avait pas répondu depuis
  `ATLAS_TIMEOUT_SECONDS` ;
- avec des valeurs égales (les défauts, 5 s chacun), l'échéance de l'appel vient d'abord :
  l'appel qui rencontre l'adresse bloquée est perdu (`unavailable`), et les appels suivants
  commencent par l'adresse suivante.

Derrière la redirection d'un serveur passif, le serveur bloqué est l'actif : le serveur passif
continue de lui envoyer les appels, et le fournisseur n'a pas de meilleure adresse par laquelle
commencer. Chaque appel est perdu, et la ligne donne une fois la position du serveur actif.

Avec une seule adresse, il n'y a pas d'autre endroit où aller : chaque appel l'attend jusqu'à
`ATLAS_TIMEOUT_SECONDS` ou son échéance, au premier des deux termes. La ligne est écrite tout de
même, `atlas address 1 of 1 in ATLAS_URL did not answer in time`, une fois tant que l'adresse n'a
pas répondu.

## Kerberos (SPNEGO)

Avec `ATLAS_AUTH=kerberos`, le fournisseur répond au `WWW-Authenticate: Negotiate` d'Atlas par un
jeton de `pyspnego`. Chaque négociation obtient un ticket neuf depuis le keytab, gardé dans un cache
en mémoire (`MEMORY:`) : un ticket expiré n'est jamais utilisé et aucun processus à part ne le
renouvelle ; c'est une requête au KDC. Une fois qu'Atlas a ouvert une session (`ATLASSESSIONID`),
aucune nouvelle négociation n'a lieu tant qu'Atlas n'en demande pas, et une redirection du serveur
passif d'Atlas renégocie avec l'actif. L'authentification mutuelle d'Atlas est laissée à TLS
(`https` et `ATLAS_CA_BUNDLE`).

Une négociation tourne sur un fil d'exécution propre au fournisseur, jamais sur la boucle qui sert
le contexte de chaque appel : un KDC lent ne retient jamais les autres lectures. Un KDC qui n'a
pas répondu à l'échéance de l'appel (un port 88 filtré, des essais d'un KDC à l'autre) rend cet
appel `unavailable` à son échéance ; cela seul n'écarte pas le fournisseur comme en panne,
puisque l'échéance a coupé l'appel, donc l'appel suivant renégocie. Au plus
`ATLAS_MAX_CONCURRENT_REQUESTS` négociations tournent à la fois ; une négociation qui attend encore
un fil quand son appel est coupé ne commence jamais, et une négociation en cours s'achève quand le
KDC répond, son résultat écarté. Quand chacun de ces fils est tenu par une négociation dont
l'appel a été coupé, ce qui est pris pour un KDC qui ne répond plus, l'appel suivant ne l'attend
pas : il est `unavailable` tout de suite, avant son échéance, et le fournisseur est réessayé
comme une panne, après `MCP_CONTEXT_RETRY_AFTER_SECONDS`. Tant qu'un fil est libre, un KDC lent
qui répond encore est attendu, jusqu'à l'échéance de l'appel. L'attente du KDC n'est pas une attente d'Atlas :
l'adresse a répondu à son défi, donc la ligne d'adresse plus haut ne la nomme jamais pour cette
attente, et les appels suivants commencent toujours par la même adresse d'`ATLAS_URL`. Une
négociation en cours retient aussi le processus quand il s'arrête : Python attend son fil, donc
un arrêt pendant un KDC bloqué dure jusqu'à ce que krb5 abandonne, dans le délai de grâce du pod.

Une négociation qui échoue est un refus du compte, comme un 401, sans essayer d'autre adresse : un
keytab que le KDC refuse (un mot de passe changé dans l'annuaire, un mauvais principal, un keytab
inutilisable) n'est plus essayé pendant `MCP_CONTEXT_REFUSED_RETRY_SECONDS`, pour que le
fournisseur ne verrouille jamais le compte par des échecs de pré-authentification. Seul un KDC
introuvable ou injoignable rend le fournisseur indisponible : quand le KDC le dit avant
l'échéance de l'appel, le fournisseur est réessayé comme une panne, après
`MCP_CONTEXT_RETRY_AFTER_SECONDS`.
`ATLAS_KEYTAB` doit être un fichier que le serveur peut lire au démarrage ; un keytab remplacé (son
heure ou sa taille change) lève le refus, comme `SIGHUP`.

Ce qu'il faut au conteneur :

- l'extra `kerberos` (`ATLAS_AUTH=kerberos` sans lui refuse de démarrer) ;
- un `krb5.conf` (`/etc/krb5.conf`, ou `KRB5_CONFIG`) qui nomme le royaume et son KDC, avec
  `rdns = false`, car une adresse derrière un répartiteur de charge ou un service Kubernetes n'a
  pas de nom inverse qui corresponde au principal de service d'Atlas (`ATLAS_SPN` le nomme) ;
- une route vers le KDC (port 88, en UDP et en TCP, ou `udp_preference_limit = 1` dans
  `krb5.conf`) depuis le pod : les règles réseau de Kubernetes et d'OpenShift doivent la laisser
  passer ;
- une horloge à l'heure du KDC (cinq minutes d'écart au plus, par défaut) ;
- le keytab monté en lecture seule, comme un secret, jamais dans l'image.

`--check` affiche le principal, jamais le keytab.

## Le compte avec lequel le fournisseur lit

1. **Lecture seule.** Le compte doit lire les entités, les glossaires et leurs termes, rien
   d'autre : aucune écriture, aucune administration. Sur un Atlas à utilisateurs propres, un
   utilisateur du rôle `DATA_SCIENTIST` suffit : les réponses de référence garnies
   (`atlas-2.1-seed`) ont été lues avec un tel compte. Sur un Atlas gouverné par Ranger, une règle
   du service `atlas` qui ne fait que lire. **Le compte doit lire chaque table qu'il sert**, sous
   les clusters rattachés, et chaque terme des glossaires autorisés : Atlas refuse toute une
   lecture par lot dès qu'une de ses tables est interdite (403 `ATLAS-403-00-001`), et ce lot n'a
   alors aucun contexte. Un compte sans aucun droit de lecture reçoit cette réponse à chaque
   lecture : le fournisseur ne sert alors rien, et son journal le dit (`atlas refused some objects
   to the account; an account with no read grant serves nothing`, une fois par appel). `--check` ne
   le voit pas forcément : il lit la version, et sous chaque type un nom qu'aucune table ne porte,
   jamais un objet qui pourrait être refusé au compte.
2. **Il voit ce qui est servi à tous.** Le fournisseur sert ce que lit ce compte, dans la limite
   des listes blanches, à chaque appelant qui voit l'objet dans Trino. Ne lui donnez que les
   glossaires, classifications et métadonnées métier que toute personne qui voit la table peut
   lire.
3. **Un refus l'arrête, sans boucle d'essais.** Un compte refusé (401, 403, ou une redirection
   vers une page de connexion sur un autre hôte) n'est plus sollicité pendant
   `MCP_CONTEXT_REFUSED_RETRY_SECONDS` (3600 par défaut, un réglage du serveur), ou jusqu'au
   changement de son fichier secret, ou jusqu'à `SIGHUP`, pour que le fournisseur ne le
   verrouille jamais dans un annuaire qui compte les échecs de connexion.

**Droits.** Le contexte suit les droits de Trino : un appelant ne le reçoit que pour les tables et
les colonnes que Trino lui liste. Le fournisseur lit Atlas avec ce compte technique en lecture
seule. Les descriptions suivent ce que Trino liste à l'appelant. Avec
`MCP_CONTEXT_REQUIRE_SELECT=true`, `describe_table` exige en plus que Trino valide un SELECT par
l'appelant des colonnes qu'il liste, tandis que `describe_schema` continue de servir les
descriptions des tables que Trino liste. Il ne sert les termes de glossaire, les classifications et
les métadonnées métier que si leur glossaire, leur classification ou leur ensemble est nommé dans
`ATLAS_GLOSSARIES`, `ATLAS_CLASSIFICATIONS` ou `ATLAS_BUSINESS_METADATA`, fermées par défaut (vide
veut dire aucun), et le propriétaire seulement si `ATLAS_OWNER_ATTRIBUTE` est posé. Un glossaire ou
une classification restreints dans Atlas ne sont donc jamais servis, sauf si l'administrateur les
nomme. La lecture d'Atlas sous l'identité de l'appelant n'est pas dans cette version.

## Vérifier la configuration

`akko-mcp-trino --check` affiche une ligne par réglage : les adresses (jamais avec des
identifiants), l'authentification (l'utilisateur, ou le principal et le service pour Kerberos,
jamais un secret ni le chemin du keytab), chaque catalogue et son cluster, les types de table, les
délais, les bornes de la requête d'une lecture par lot et des lectures de termes en parallèle, les
ensembles servis, les attributs du propriétaire et du statut des termes, et si des autorités de
certification et un proxy sont posés (jamais l'adresse du proxy). Il demande ensuite sa version à
Atlas (`GET /api/atlas/admin/version`) et affiche `atlas: answers, version <version>`, ou une
ligne qui commence par `UNREACHABLE:` quand Atlas refuse le compte (une négociation Kerberos
refusée comprise) ou ne répond pas ; `--check --strict` sort alors avec le code 2. Enfin, il
demande à chaque type de table un nom qu'aucune table ne porte, et affiche les types qu'Atlas ne
connaît pas : s'il n'en connaît aucun (`atlas: knows none of the table types`), le fournisseur ne
sert rien, et écrit cette ligne au journal une fois quand il le découvre.

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
