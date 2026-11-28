# Politique de versions

[English](../en/versioning.md) · **Français**

## Numéros de version

Le serveur suit le [versionnage sémantique](https://semver.org/lang/fr/). Sa version est
`MAJEURE.MINEURE.CORRECTIVE` ; les deux paquets (`akko-mcp-trino`, `akko-mcp-trino-atlas`), les
images et l'`appVersion` de la chart portent la même. La chart a sa propre version, relevée à
chaque changement de la chart.

Tant que la version majeure est 0 :

- une release **corrective** (0.8.5 vers 0.8.6) corrige ou ajoute, et ne casse jamais rien : un
  réglage, un tool, un champ de réponse ou une valeur par défaut garde son sens ;
- une release **mineure** (0.7 vers 0.8) peut casser. Chaque rupture est listée dans
  [CHANGELOG.md](../../CHANGELOG.md) sous la release, et [MIGRATING.md](../../MIGRATING.md) (en
  anglais) dit quoi changer.

Ce qui compte comme une rupture : un réglage retiré, renommé ou qui change de sens, une valeur par
défaut qui ouvre ou ferme l'accès autrement, un tool retiré ou renommé, un champ retiré de la
réponse d'un tool, un champ de la ligne d'audit ou une métrique retirés ou renommés, une valeur de
la chart retirée ou qui change de sens. Un nouveau réglage, tool, champ ou une nouvelle métrique ne
sont pas des ruptures, ni le refus d'une requête déjà hors du contrat documenté.

## Durée de prise en charge

- La dernière version mineure reçoit tous les correctifs, dans une nouvelle release corrective.
- La version mineure précédente reçoit les correctifs de sécurité pendant trois mois après la
  sortie de la mineure suivante.
- [SECURITY.fr.md](../../SECURITY.fr.md#versions-prises-en-charge) liste les versions prises en
  charge aujourd'hui.

## Rythme

Une release corrective sort quand un correctif ou un ajout est prêt ; un correctif de sécurité sort
aussitôt. Une version mineure sort quand une rupture est nécessaire, annoncée dans le changelog de
la release précédente quand c'est possible. Un tag n'est jamais déplacé et une release n'est jamais
remplacée : une erreur se corrige par une nouvelle release.
