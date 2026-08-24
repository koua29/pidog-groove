#!/usr/bin/env python3
"""Tests de la detection de tempo, sur signaux fabriques. Aucun materiel.

    python3 tests/test_rythme.py
"""
import math
import os
import random
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import rythme

TRAME = 0.010          # 10 ms par trame, comme a la capture


def battements(bpm, duree_s, trame=TRAME, force=1.0, fond=0.05,
               graine=1, largeur=2):
    """Suite de niveaux RMS simulant une musique a pulsation marquee."""
    rng = random.Random(graine)
    n = int(duree_s / trame)
    pas = 60.0 / bpm / trame
    niveaux = [fond * rng.random() for _ in range(n)]
    k = 0
    while True:
        i = int(round(k * pas))
        if i >= n:
            break
        for j in range(largeur):
            if i + j < n:
                niveaux[i + j] += force * (1.0 - j / (largeur + 1))
        k += 1
    return niveaux


class Detection(unittest.TestCase):
    def test_tempos_courants(self):
        for bpm in (72, 90, 100, 120, 140, 160):
            niveaux = battements(bpm, 8.0)
            trouve, phase, conf = rythme.tempo_et_phase(niveaux, TRAME)
            self.assertIsNotNone(trouve, f"{bpm} BPM non detecte")
            self.assertLess(abs(trouve - bpm) / bpm, 0.06,
                            f"attendu {bpm}, trouve {trouve:.1f}")
            self.assertGreater(conf, 0.15)

    def test_phase_retrouvee(self):
        """Le premier temps doit tomber la ou on l'a mis."""
        bpm = 120
        niveaux = battements(bpm, 8.0)
        decalage = 7                      # trames de silence ajoutees devant
        niveaux = [0.01] * decalage + niveaux
        _, phase, _ = rythme.tempo_et_phase(niveaux, TRAME)
        periode = 60.0 / bpm
        ecart = abs((phase - decalage * TRAME) % periode)
        ecart = min(ecart, periode - ecart)
        self.assertLess(ecart, 0.035, f"phase decalee de {ecart*1000:.0f} ms")

    def test_silence_rejete(self):
        _, _, conf = rythme.tempo_et_phase([0.001] * 800, TRAME)
        self.assertLess(conf, 0.15)

    def test_bruit_sans_pulsation_rejete(self):
        rng = random.Random(3)
        niveaux = [rng.random() for _ in range(800)]
        bpm, _, conf = rythme.tempo_et_phase(niveaux, TRAME)
        self.assertLess(conf, 0.15, f"a cru voir {bpm} BPM dans du bruit blanc")

    def test_pas_de_demi_tempo(self):
        """Piege classique : l'autocorrelation culmine aussi sur la mesure.
        Un 120 BPM ne doit pas etre rendu comme 60."""
        for bpm in (120, 140, 160):
            trouve, _, _ = rythme.tempo_et_phase(battements(bpm, 10.0), TRAME)
            self.assertGreater(trouve, bpm * 0.75,
                               f"{bpm} BPM rendu comme {trouve:.0f} (demi-tempo)")


class BruitDesServos(unittest.TestCase):
    """Le vrai probleme du projet : le robot s'entend lui-meme.
    Mesure sur ce robot : 1513 RMS en mouvement contre 35 au silence."""

    def _pollue(self, niveaux, debuts, duree_trames, force=42.0):
        masque = [False] * len(niveaux)
        for d in debuts:
            for i in range(d, min(d + duree_trames, len(niveaux))):
                niveaux[i] = force
                masque[i] = True
        return niveaux, masque

    def test_sans_masque_le_tempo_est_perdu(self):
        """Demonstration du probleme : sans masquer ses propres gestes, le
        robot verrouille sur SON rythme a lui, pas sur celui de la musique."""
        niveaux = battements(120, 10.0)
        pollue, _ = self._pollue(list(niveaux), range(0, 1000, 71), 6)
        bpm, _, _ = rythme.tempo_et_phase(pollue, TRAME)
        self.assertIsNotNone(bpm)
        # 71 trames = 710 ms = ~85 BPM : c'est le rythme des servos.
        self.assertLess(abs(bpm - 84.5), 8,
                        "sans masque on devrait justement capter les servos")

    def test_avec_masque_le_tempo_est_retrouve(self):
        niveaux = battements(120, 10.0)
        pollue, masque = self._pollue(list(niveaux), range(0, 1000, 71), 6)
        bpm, _, conf = rythme.tempo_et_phase(pollue, TRAME, masque=masque)
        self.assertIsNotNone(bpm)
        self.assertLess(abs(bpm - 120) / 120, 0.08,
                        f"masque, on devrait retrouver 120, trouve {bpm:.0f}")
        self.assertGreater(conf, 0.10)

    def test_trop_de_masque_refuse(self):
        """Si le robot a bouge presque tout le temps, il n'a rien entendu :
        mieux vaut ne rien affirmer que verrouiller sur du vide."""
        niveaux = battements(120, 6.0)
        masque = [i % 10 < 8 for i in range(len(niveaux))]   # 80 % masque
        bpm, _, conf = rythme.tempo_et_phase(niveaux, TRAME, masque=masque)
        self.assertIsNone(bpm)
        self.assertEqual(conf, 0.0)


class Robustesse(unittest.TestCase):
    def test_fenetre_trop_courte(self):
        self.assertEqual(rythme.tempo_et_phase([0.5] * 4, TRAME), (None, None, 0.0))

    def test_bornes_respectees(self):
        """Rien en dehors de la plage demandee, meme si le signal y pousse."""
        bpm, _, _ = rythme.tempo_et_phase(battements(200, 8.0), TRAME,
                                          bpm_min=60, bpm_max=180)
        if bpm is not None:
            self.assertLessEqual(bpm, 180.5)
            self.assertGreaterEqual(bpm, 59.5)

    def test_enveloppe_ignore_un_fond_continu(self):
        """Un bruit fort mais constant ne doit produire aucune attaque."""
        env = rythme.enveloppe([0.8] * 100)
        self.assertLess(max(env), 0.05)


if __name__ == "__main__":
    unittest.main(verbosity=2)
