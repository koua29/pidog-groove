#!/usr/bin/env python3
"""
Modele de temps des servos, MESURE sur ce robot le 24/08/2026.

robot_hat annonce duree_ms = -9.9 * vitesse + 1000, mais sa boucle interne vise
10 ms par pas et en met ~13,2 (time.sleep deborde, plus le surcout Python et
l'ecriture I2C). Mesures, mediane de 3 essais par vitesse :

    vitesse 90 : annonce 109 ms, reel 130 ms  (10 pas -> 13,0 ms/pas)
    vitesse 75 : annonce 258 ms, reel 322 ms  (25 pas -> 12,9)
    vitesse 65 : annonce 356 ms, reel 455 ms  (35 pas -> 13,0)
    vitesse 50 : annonce 505 ms, reel 674 ms  (50 pas -> 13,5)
    vitesse 40 : annonce 604 ms, reel 808 ms  (60 pas -> 13,5)

Modeliser le PAS ramene l'erreur a 2 %. Un facteur global se trompait de 12 %
aux extremes, parce que le nombre de pas est tronque par int().

Corollaire utile ici : un mouvement ne peut pas durer plus d'une seconde
(vitesse 0), et surtout on connait sa duree A L'AVANCE — c'est ce qui permet de
savoir exactement quelles trames micro seront polluees par les servos.
"""
PENTE_MS = -9.9
ORIGINE_MS = 1000.0
PAS_MS = 10.0
PAS_REEL_MS = 13.2

DPS_TETE = 300.0
DPS_QUEUE = 500.0


def vitesse_pour_duree(duree_s, pas_reel_ms=PAS_REEL_MS):
    """Vitesse (0-100) pour que le mouvement dure REELLEMENT duree_s."""
    pas = max(1.0, float(duree_s) * 1000.0 / max(1.0, pas_reel_ms))
    ms = max(PAS_MS, pas * PAS_MS)
    return int(max(0, min(100, round((ms - ORIGINE_MS) / PENTE_MS))))


def duree_pour_vitesse(vitesse, delta_degres=0.0, max_dps=DPS_TETE,
                       pas_reel_ms=PAS_REEL_MS):
    """Duree REELLE d'un mouvement, plafond de vitesse angulaire compris."""
    ms = PENTE_MS * float(vitesse) + ORIGINE_MS
    if delta_degres:
        ms = max(ms, abs(delta_degres) / max_dps * 1000.0)
    return int(ms / PAS_MS) * pas_reel_ms / 1000.0
