#!/usr/bin/env python3
"""
PiDog ecoute la musique ambiante et bouge la tete et la queue en rythme.

    python3 ecoute.py                 # jusqu'a duree_max_s, Ctrl-C pour sortir
    python3 ecoute.py --duree 60
    python3 ecoute.py --sans-robot    # ecoute et affiche le tempo, sans bouger

Il ne joue AUCUN son et ne bouge PAS les pattes. C'est volontaire : la lecture
audio et les mouvements de pattes sont ce qui a fait echouer le projet
precedent (lecteurs audio incompatibles, chutes du robot).

Principe : ECOUTER d'abord, immobile, pour verrouiller tempo et phase — puis
PREDIRE les temps suivants et bouger dessus, en masquant du micro les instants
ou l'on bouge. Sans cela le robot verrouille sur le rythme de ses propres
servos : mesure sur ce robot, 1513 RMS en mouvement contre 35 au silence.
Le test `test_sans_masque_le_tempo_est_perdu` en fait la demonstration.
"""
import argparse
import json
import random
import os
import signal
import struct
import subprocess
import sys
import threading
import time

ICI = os.path.dirname(os.path.abspath(__file__))

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


sys.path.insert(0, ICI)

import rythme
import servo


def journal(msg):
    print(f"{time.strftime('%H:%M:%S')} {msg}", flush=True)


def charger(chemin=None):
    with open(chemin or os.path.join(ICI, "config.json"), encoding="utf-8") as f:
        return json.load(f)


# --------------------------------------------------------------------- micro
class Oreille:
    """Capture continue du micro, transformee en niveaux d'energie.

    On ne garde jamais l'audio : seulement un niveau RMS toutes les 10 ms, plus
    un drapeau disant si le robot bougeait a cet instant. C'est tout ce dont
    l'autocorrelation a besoin, et cela tient dans quelques kilo-octets.
    """

    def __init__(self, cfg):
        m = cfg["micro"]
        self.peripherique = m["peripherique"]
        self.sr = int(m["frequence_hz"])
        self.trame_ms = int(m["trame_ms"])
        self.trame = self.sr * self.trame_ms // 1000
        self.periode = self.trame_ms / 1000.0
        self.taille = int(float(cfg["ecoute"]["fenetre_s"]) / self.periode)
        self.niveaux, self.masque = [], []
        self._verrou = threading.Lock()
        self._masque_jusqu_a = 0.0
        self.proc = None
        self.arret = threading.Event()

    def masquer_pendant(self, duree_s):
        """A appeler AVANT de commander un geste."""
        self._masque_jusqu_a = max(self._masque_jusqu_a,
                                   time.monotonic() + duree_s)

    def demarrer(self):
        self.proc = subprocess.Popen(
            ["arecord", "-D", self.peripherique, "-f", "S16_LE",
             "-r", str(self.sr), "-c", "1", "-t", "raw", "-q"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        threading.Thread(target=self._boucle, daemon=True).start()

    def _boucle(self):
        octets = self.trame * 2
        while not self.arret.is_set():
            bloc = self.proc.stdout.read(octets)
            if not bloc or len(bloc) < octets:
                break
            ech = struct.unpack(f"<{self.trame}h", bloc)
            niveau = rythme.rms(ech)
            pollue = time.monotonic() < self._masque_jusqu_a
            with self._verrou:
                self.niveaux.append(niveau)
                self.masque.append(pollue)
                if len(self.niveaux) > self.taille:
                    del self.niveaux[:len(self.niveaux) - self.taille]
                    del self.masque[:len(self.masque) - self.taille]

    def fenetre(self):
        with self._verrou:
            return list(self.niveaux), list(self.masque)

    def pleine(self):
        with self._verrou:
            return len(self.niveaux) >= self.taille

    def stop(self):
        self.arret.set()
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()


# ------------------------------------------------------------------ mouvement
class Danseur:
    """Tete et queue seulement, sur une grille de temps PREDITE."""

    def __init__(self, dog, cfg):
        self.dog = dog
        m = cfg["mouvements"]
        self.pas = max(1, int(m["temps_par_geste"]))
        self.fraction = float(m["fraction_attaque"])
        self.ampl = (float(m["amplitude_tete_deg"]),
                     float(m.get("amplitude_roulis_deg", 25)),
                     float(m["tangage_deg"]))
        self.ampl_queue = float(m["amplitude_queue_deg"])
        self.variation = float(m.get("variation_queue", 0.35))
        self.marge = float(m["marge_masque_ms"]) / 1000.0
        self.figures = m.get("figures") or [{"nom": "gauche", "tete": [1, 0, 0]},
                                            {"nom": "droite", "tete": [-1, 0, 0]}]
        self.ecart_min = float(m.get("ecart_minimal", 0.6))
        self.rng = random.Random()
        self.precedente = None
        self.compte = 0

    def duree_geste(self, periode_s):
        creneau = periode_s * self.pas
        return max(0.05, min(1.0, creneau * self.fraction))

    def _choisir(self):
        """Tire une figure au sort, en evitant la monotonie ET l'invisible.

        Deux filtres : jamais la meme figure deux fois de suite, et jamais une
        figure trop proche de la precedente. Sans le second, le tirage produit
        regulierement deux positions voisines a la suite — le robot fait alors
        un geste imperceptible sur un temps, ce qui se lit comme un raté.
        """
        precedent = self.precedente["tete"] if self.precedente else None
        candidats = []
        for f in self.figures:
            if self.precedente is not None and f["nom"] == self.precedente["nom"]:
                continue
            if precedent is not None:
                ecart = max(abs(a - b) for a, b in zip(f["tete"], precedent))
                if ecart < self.ecart_min:
                    continue
            candidats.append(f)
        if not candidats:
            candidats = [f for f in self.figures
                         if self.precedente is None or f["nom"] != self.precedente["nom"]]
        if not candidats:
            candidats = self.figures
        poids = [max(1, int(f.get("poids", 1))) for f in candidats]
        return self.rng.choices(candidats, weights=poids, k=1)[0]

    def geste(self, periode_s, oreille):
        """Un geste sur le temps. Renvoie la duree pendant laquelle le micro
        sera pollue, pour que l'appelant sache quoi masquer."""
        duree = self.duree_geste(periode_s)
        vitesse = servo.vitesse_pour_duree(duree)
        pollution = servo.duree_pour_vitesse(vitesse) + self.marge
        oreille.masquer_pendant(pollution)

        figure = self._choisir()
        self.precedente = figure
        cible = [round(f * a, 1) for f, a in zip(figure["tete"], self.ampl)]

        # La queue bat a chaque geste en alternant les cotes : c'est elle qui
        # porte la pulsation, la tete se contentant de varier. Un peu d'aleatoire
        # sur l'amplitude evite l'effet metronome.
        cote = 1 if self.compte % 2 == 0 else -1
        facteur = 1.0 + self.rng.uniform(-self.variation, self.variation)
        queue = round(cote * self.ampl_queue * facteur, 1)

        self.compte += 1
        if self.dog is None:
            return pollution
        try:
            self.dog.head_move([cible], immediately=True, speed=vitesse)
            self.dog.tail_move([[queue]], immediately=True,
                               speed=min(100, vitesse + 10))
        except Exception as e:
            journal(f"[geste] {type(e).__name__}: {e}")
        return pollution

    def repos(self):
        if self.dog is None:
            return
        try:
            self.dog.head_move([[0, 0, 0]], immediately=True, speed=45)
            self.dog.tail_move([[0]], immediately=True, speed=45)
        except Exception:
            pass


# ------------------------------------------------- cohabitation avec pidog-voice
def ecoute_vocale_active():
    return subprocess.run(["pgrep", "-f", "[p]idog_ears"],
                          capture_output=True).returncode == 0


def suspendre_ecoute_vocale():
    """Deux Pidog() se disputent le GPIO, et deux arecord se disputent le micro.

    Arret par SIGTERM puis SIGINT, JAMAIS kill -9 : un arret force laisse des
    poignees GPIO ouvertes, d'ou des 'GPIO busy' et une initialisation de
    Pidog() qui demarre parfois sans ses fils d'execution — constate toute la
    journee du 24/08, et c'est ce qui a fait piloter un robot muet pendant une
    heure sans que rien ne le signale.
    """
    if not ecoute_vocale_active():
        return False
    journal("[voice] ecoute vocale suspendue")
    for sig in ("-TERM", "-INT"):
        subprocess.run(["pkill", sig, "-f", "[e]ars_loop"], capture_output=True)
        subprocess.run(["pkill", sig, "-f", "[p]idog_ears"], capture_output=True)
        for _ in range(16):
            if not ecoute_vocale_active():
                return True
            time.sleep(0.5)
    journal("[voice] elle resiste — on continue, le micro sera partage")
    return True


def relancer_ecoute_vocale():
    sup = os.path.expanduser("~/script/pidog-voice/pi/ears_loop.sh")
    if not os.path.exists(sup):
        journal("[voice] superviseur introuvable, ecoute non relancee")
        return
    journal("[voice] ecoute vocale relancee")
    subprocess.Popen(["setsid", "nohup", sup],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     stdin=subprocess.DEVNULL, start_new_session=True)


# ------------------------------------------------------------------- materiel
def preparer_robot(cfg):
    """(dog, message). None si le materiel n'est pas utilisable.

    Deux verifications que l'echec du projet precedent a rendues obligatoires :
    la remise a zero du microcontroleur, et surtout le CONTROLE DES FILS. Pidog()
    peut rendre la main avec un seul fil d'execution et aucun moteur pilotable,
    sans lever la moindre erreur — on croit alors piloter un robot alors qu'on
    remplit une file que personne ne depile.
    """
    try:
        from robot_hat.utils import reset_mcu
        reset_mcu()
        time.sleep(1.5)
    except Exception as e:
        journal(f"[robot] remise a zero impossible : {type(e).__name__}: {e}")

    from pidog import Pidog
    dog = Pidog()
    time.sleep(1.5)

    fils = {t.name for t in threading.enumerate()}
    manquants = {"head_thread", "tail_thread"} - fils
    if manquants:
        try:
            dog.close()
        except Exception:
            pass
        return None, (f"les fils {', '.join(sorted(manquants))} ne tournent pas : "
                      f"les ordres ne seraient jamais executes. Coupez "
                      f"l'interrupteur du robot dix secondes et rallumez.")
    return dog, "fils d'execution verifies"


def verifier_batterie(minimum_pct):
    if minimum_pct <= 0:
        return True, "controle desactive"
    try:
        from robot_hat.device import get_battery_voltage
    except Exception:
        return True, "tension illisible, controle ignore"
    lues = []
    for _ in range(20):
        try:
            v = get_battery_voltage()
        except Exception:
            v = 0
        if v >= 5.0:
            lues.append(v)
        time.sleep(0.05)
    if len(lues) < 3:
        # L'ADC est muet par fenetres entieres sur ce robot. Prevenir, pas
        # bloquer : les decrochages constates l'ont ete avec une mesure BASSE.
        return True, "tension illisible, controle ignore"
    import statistics
    v = statistics.median(lues)
    pct = max(0.0, min(100.0, (v - 6.4) / 2.0 * 100))
    if pct < minimum_pct:
        return False, f"batterie a {v:.2f} V ({pct:.0f} %), minimum {minimum_pct} %"
    return True, f"{v:.2f} V ({pct:.0f} %)"


# ----------------------------------------------------------------- programme
class Ecouteur:
    def __init__(self, cfg, dog):
        self.cfg = cfg
        self.oreille = Oreille(cfg)
        self.danseur = Danseur(dog, cfg)
        self.arret = threading.Event()
        e = cfg["ecoute"]
        self.confiance_min = float(e["confiance_min"])
        self.reestimation = float(e["reestimation_s"])
        self.patience = float(e["patience_s"])
        self.bpm_min, self.bpm_max = float(e["bpm_min"]), float(e["bpm_max"])

    def _estimer(self):
        niveaux, masque = self.oreille.fenetre()
        return rythme.tempo_et_phase(niveaux, self.oreille.periode, masque,
                                     self.bpm_min, self.bpm_max)

    def tourner(self, duree_max):
        self.oreille.demarrer()
        journal(f"[ecoute] remplissage de {self.cfg['ecoute']['fenetre_s']} s...")
        debut = time.monotonic()
        while not self.oreille.pleine() and not self.arret.is_set():
            if time.monotonic() - debut > 20:
                journal("[ecoute] le micro ne fournit rien — verifiez plug:mic")
                return False
            time.sleep(0.2)

        prochain_temps = None
        periode = None
        derniere_estimation = 0.0
        derniere_musique = time.monotonic()
        gestes = 0

        while not self.arret.is_set():
            maintenant = time.monotonic()
            if maintenant - debut > duree_max:
                journal("[ecoute] duree maximale atteinte")
                break

            if maintenant - derniere_estimation >= self.reestimation:
                derniere_estimation = maintenant
                bpm, phase, conf = self._estimer()
                if bpm and conf >= self.confiance_min:
                    nouvelle = 60.0 / bpm
                    # La phase est comptee depuis le DEBUT de la fenetre, donc
                    # depuis (maintenant - fenetre). On la ramene dans le futur.
                    ancre = maintenant - self.oreille.periode * len(
                        self.oreille.niveaux) + phase
                    while ancre < maintenant:
                        ancre += nouvelle
                    if periode is None:
                        journal(f"[ecoute] rythme accroche : {bpm:.0f} BPM "
                                f"(confiance {conf:.2f})")
                    elif abs(60.0 / nouvelle - 60.0 / periode) > 3:
                        journal(f"[ecoute] tempo revu : {bpm:.0f} BPM "
                                f"(confiance {conf:.2f})")
                    periode, prochain_temps = nouvelle, ancre
                    derniere_musique = maintenant
                elif periode is not None and maintenant - derniere_musique > self.patience:
                    journal("[ecoute] plus de rythme — retour a l'ecoute immobile")
                    periode = prochain_temps = None
                    self.danseur.repos()

            if periode and prochain_temps:
                # L'attente porte sur l'instant de LANCEMENT, pas sur le temps
                # lui-meme. Un servo ne se teleporte pas : il faut partir une
                # duree de mouvement a l'avance pour ARRIVER sur le temps.
                # Premiere version : on n'examinait un temps qu'a moins de
                # 0,4 s, alors que le depart tombe 0,6 s avant lui — chaque
                # geste etait donc juge en retard et saute. Resultat mesure :
                # rythme correctement accroche, et zero geste joue.
                duree = self.danseur.duree_geste(periode)
                vitesse = servo.vitesse_pour_duree(duree)
                anticipation = servo.duree_pour_vitesse(vitesse)
                depart = prochain_temps - anticipation
                reste = depart - time.monotonic()
                if reste > 0.3:
                    time.sleep(0.1)     # on repasse par la re-estimation
                    continue
                if reste > 0:
                    if self.arret.wait(reste):
                        break
                elif reste < -0.15:
                    # En retard : on saute. Rattraper empile les ordres et le
                    # robot s'agite de plus en plus loin du tempo.
                    prochain_temps += periode * self.danseur.pas
                    continue
                self.danseur.geste(periode, self.oreille)
                gestes += 1
                prochain_temps += periode * self.danseur.pas
            else:
                time.sleep(0.15)

        journal(f"[ecoute] {gestes} gestes joues")
        return True

    def stop(self):
        self.arret.set()
        self.oreille.stop()
        self.danseur.repos()


def main():
    p = argparse.ArgumentParser(
        description="PiDog bouge la tete et la queue au rythme de la musique.")
    p.add_argument("--duree", type=float, help="duree maximale, en secondes")
    p.add_argument("--sans-robot", action="store_true",
                   help="ecoute et affiche le tempo, sans bouger")
    p.add_argument("--config", default=None)
    args = p.parse_args()

    cfg = charger(args.config)
    duree_max = args.duree or float(cfg["securite"]["duree_max_s"])

    dog = None
    reprendre = False
    if not args.sans_robot:
        ok, msg = verifier_batterie(
            float(cfg["securite"]["batterie_minimale_pourcent"]))
        journal(f"[batterie] {msg}")
        if not ok:
            return 1
        reprendre = suspendre_ecoute_vocale()
        if reprendre:
            time.sleep(2)     # le noyau met un instant a rendre les lignes GPIO
        dog, msg = preparer_robot(cfg)
        journal(f"[robot] {msg}")
        if dog is None:
            if reprendre:
                relancer_ecoute_vocale()
            return 1

    ecouteur = Ecouteur(cfg, dog)

    def sortir(*_):
        journal("[ecoute] interruption")
        ecouteur.stop()
    signal.signal(signal.SIGINT, sortir)
    signal.signal(signal.SIGTERM, sortir)

    try:
        ecouteur.tourner(duree_max)
    finally:
        ecouteur.stop()
        if dog is not None:
            time.sleep(0.8)
            try:
                dog.close()
            except Exception:
                pass
        if reprendre:
            time.sleep(1)
            relancer_ecoute_vocale()
    return 0


if __name__ == "__main__":
    sys.exit(main())
