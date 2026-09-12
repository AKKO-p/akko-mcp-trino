#!/usr/bin/env bash
# Garde-fou open-source : le cœur générique (core/) ne doit contenir AUCUN couplage
# fonctionnel AKKO — ni import croisé vers akko/, ni valeur vendor/AKKO en dur. Ainsi
# core/ reste extractible en repo open source sans détricotage.
# () Les commentaires expliquant la frontière
# ("la couche AKKO injecte…") sont autorisés ; seuls les couplages réels sont interdits.
set -euo pipefail
cd "$(dirname "$0")"

fail=0

# 1. Import croisé vers la couche akko/ (la violation structurelle).
if grep -rnE --include=*.py --exclude-dir=__pycache__ "^\s*(from|import)\s+akko(\.|\s|$)" core/ 2>/dev/null; then
  echo "❌ core/ importe la couche akko/ — le cœur doit être autonome." >&2
  fail=1
fi

# 2. Valeurs vendor/AKKO codées en dur (défauts, FQDN, fonctions spécifiques).
#    Ces tokens ne doivent JAMAIS apparaître dans core/ (ils vivent dans akko/).
FORBIDDEN='akko-trino|akko_ai|akko-ai\.com|harbor\.akko|iceberg|ollama|mcp-trino'
if grep -rniE --include=*.py --exclude-dir=__pycache__ "$FORBIDDEN" core/ 2>/dev/null; then
  echo "❌ core/ contient une valeur AKKO/vendor en dur — déplace-la dans akko/defaults.py." >&2
  fail=1
fi

if [ "$fail" -eq 0 ]; then
  echo "✅ core/ est vendor-neutre (aucun couplage AKKO) — prêt pour l'open-source."
fi
exit "$fail"
