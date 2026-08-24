#!/usr/bin/env bash
# Ajoute la commande vocale "ecoute la musique" a pidog-voice.
#   ./install/integrer_pidog_voice.sh
set -euo pipefail

VOICE="${PIDOG_VOICE:-$HOME/script/pidog-voice}"
CFG="$VOICE/commandes.json"
ECOUTE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/ecoute.py"

[ -f "$CFG" ] || { echo "pidog-voice introuvable : $CFG"; exit 1; }
[ -f "$ECOUTE" ] || { echo "ecoute.py introuvable : $ECOUTE"; exit 1; }

cp "$CFG" "$CFG.avant-ecoute"
echo "sauvegarde : $CFG.avant-ecoute"

python3 - "$CFG" "$ECOUTE" <<'PY'
import collections, json, sys
chemin, ecoute = sys.argv[1], sys.argv[2]
with open(chemin, encoding="utf-8") as f:
    cfg = json.load(f, object_pairs_hook=collections.OrderedDict)

if "ecoute" in cfg["commandes"]:
    print("remplace la commande 'ecoute' existante")

cfg["commandes"]["ecoute"] = collections.OrderedDict([
    ("intention", "ecouter la musique ambiante et bouger la tete et la queue "
                  "en rythme ; reagir a la musique qui joue autour"),
    ("phrases", [
        "{nom} ecoute la musique",
        "{nom} ecoute",
        "{nom} danse sur la musique",
    ]),
    # Whisper deforme souvent 'ecoute' : on couvre les transcriptions vues et
    # probables, pour que le LLM reconnaisse l'intention meme mal transcrite.
    ("exemples", [
        "ecoute la musique",
        "sympa la musique",
        "ecoute ca",
        "tu entends la musique",
        "bouge sur la musique",
        "{nom} ecoute la zik",
    ]),
    ("deplace", False),
    ("reponse", "J'ecoute la musique."),
    # 'sequence' est la cle lue par config.py (surtout PAS 'etapes').
    ("sequence", [{"externe": ecoute}]),
])
with open(chemin, "w", encoding="utf-8") as f:
    json.dump(cfg, f, ensure_ascii=False, indent=2)
print("commande 'ecoute' ajoutee")
PY

if [ -f "$VOICE/outils/verifier_config.py" ]; then
  echo "== verification de pidog-voice =="
  python3 "$VOICE/outils/verifier_config.py" || {
    echo "!! configuration invalide, restauration"
    mv "$CFG.avant-ecoute" "$CFG"; exit 1; }
fi

cat <<'FIN'

== integre ==

Dites : "PiDog, ecoute la musique."

L'ecoute vocale se suspend pendant qu'il ecoute la musique (il partagerait
sinon le micro et le GPIO), et repart toute seule a la fin. Il s'arrete au bout
de duree_max_s (5 min par defaut) ou sur "PiDog, stop" si vous relancez d'abord
l'ecoute — sinon coupez le processus.
FIN
