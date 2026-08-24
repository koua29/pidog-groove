#!/usr/bin/env python3
"""Controle la configuration et la chaine de capture, sans toucher aux servos.

    python3 outils/verifier.py
"""
import json
import os
import struct
import subprocess
import sys

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ---------------------------------------------------------------------------
# PIEGE MAJEUR, paye cher le 24/08/2026 : robot_hat appelle `i2cdetect` pour
# scanner le bus I2C. Si /usr/sbin n'est pas dans le PATH — c'est le cas d'une
# session SSH ordinaire — la commande est introuvable, le scan renvoie une
# liste VIDE, et la bibliotheque ecrit ensuite dans le vide SANS LA MOINDRE
# ERREUR. Les fils d'execution tournent, les positions internes avancent, les
# compteurs de gestes sont parfaits, et aucun servo ne bouge.
#     sans /usr/sbin : I2C().scan() == []
#     avec           : I2C().scan() == [21, 54, 116]
# On ne compte donc pas sur l'environnement de l'appelant : on le corrige ici.
# ---------------------------------------------------------------------------
if "/usr/sbin" not in os.environ.get("PATH", "").split(":"):
    os.environ["PATH"] = os.environ.get("PATH", "") + ":/usr/sbin"


sys.path.insert(0, RACINE)
import rythme
import servo


def main():
    cfg = json.load(open(os.path.join(RACINE, "config.json"), encoding="utf-8"))
    print("configuration :")
    e, m = cfg["ecoute"], cfg["mouvements"]
    print(f"  fenetre d'analyse   {e['fenetre_s']} s")
    print(f"  plage de tempo      {e['bpm_min']}-{e['bpm_max']} BPM")
    print(f"  confiance minimale  {e['confiance_min']}")
    print(f"  un geste tous les   {m['temps_par_geste']} temps")

    for bpm in (80, 100, 120, 140):
        periode = 60.0 / bpm
        creneau = periode * int(m["temps_par_geste"])
        duree = max(0.05, min(1.0, creneau * float(m["fraction_attaque"])))
        v = servo.vitesse_pour_duree(duree)
        reelle = servo.duree_pour_vitesse(v)
        silence = creneau - reelle
        etat = "ok" if silence > 0.25 else "!! trop peu de silence pour ecouter"
        print(f"  a {bpm:3} BPM : geste {reelle*1000:4.0f} ms sur un creneau de "
              f"{creneau*1000:4.0f} ms -> {silence*1000:4.0f} ms d'ecoute  {etat}")

    print("\nmicro :")
    mic = cfg["micro"]
    sr, trame_ms = int(mic["frequence_hz"]), int(mic["trame_ms"])
    trame = sr * trame_ms // 1000
    try:
        p = subprocess.Popen(
            ["arecord", "-D", mic["peripherique"], "-f", "S16_LE",
             "-r", str(sr), "-c", "1", "-t", "raw", "-q"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        niveaux = []
        for _ in range(200):                      # 2 s
            bloc = p.stdout.read(trame * 2)
            if not bloc or len(bloc) < trame * 2:
                break
            niveaux.append(rythme.rms(struct.unpack(f"<{trame}h", bloc)))
        p.terminate()
    except FileNotFoundError:
        print("  arecord absent : sudo apt install alsa-utils")
        return 1
    if not niveaux:
        print(f"  aucun son capte sur {mic['peripherique']}")
        return 1
    moyen = sum(niveaux) / len(niveaux)
    print(f"  {len(niveaux)} trames captees | niveau moyen {moyen:.0f}, "
          f"pic {max(niveaux):.0f}")
    if moyen < 5:
        print("  tres silencieux — le micro capte-t-il quelque chose ?")
    bpm, phase, conf = rythme.tempo_et_phase(niveaux, trame_ms / 1000.0)
    if bpm and conf >= float(e["confiance_min"]):
        print(f"  pulsation detectee : {bpm:.0f} BPM (confiance {conf:.2f})")
    else:
        print(f"  pas de pulsation nette (confiance {conf:.2f}) — "
              f"normal si rien ne joue")
    return 0


if __name__ == "__main__":
    sys.exit(main())
