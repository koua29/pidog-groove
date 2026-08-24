#!/usr/bin/env python3
"""
Detection de tempo sur un flux micro, en calcul pur.

Aucune dependance materielle et aucune bibliotheque d'analyse : une enveloppe
d'energie et une autocorrelation suffisent, et c'est ce qui permet de tout
verifier sur signaux synthetiques avant d'approcher le robot.

Le probleme central de ce projet est mesure et connu : les servos en mouvement
portent le bruit a 1513 RMS contre 35 au silence, soit 42 fois plus fort que la
musique. Un chien qui bouge pour ecouter s'entend donc surtout lui-meme. La
parade est ici : on sait exactement QUAND on commande un geste, donc on masque
ces trames-la (voir le parametre `masque`).
"""
import math

BPM_MIN = 60.0
BPM_MAX = 180.0

try:
    import numpy as _np
except ImportError:                      # le module reste utilisable sans numpy
    _np = None


def rms(bloc):
    """Energie d'un bloc d'echantillons entiers signes."""
    if not bloc:
        return 0.0
    return math.sqrt(sum(x * x for x in bloc) / len(bloc))


class FluxSpectral:
    """Detecteur d'attaques par flux spectral, alimente trame par trame.

    Mesure sur un enregistrement reel du micro du robot : a confiance egale de
    methode, le flux double la nettete du pic par rapport a un simple RMS large
    bande (1,24 contre 0,77). Le RMS confond une nappe de synthe qui monte avec
    un coup de percussion ; le flux ne retient que ce qui APPARAIT dans le
    spectre.

    Sans numpy on retombe sur le RMS, qui marche aussi une fois les harmoniques
    sommees — simplement avec moins de marge.
    """

    def __init__(self, taille_fenetre=1024):
        self.n = taille_fenetre
        self.dispo = _np is not None
        if self.dispo:
            self.tampon = _np.zeros(taille_fenetre, dtype=_np.float32)
            self.fenetrage = _np.hanning(taille_fenetre).astype(_np.float32)
            self.precedent = None

    def pousser(self, echantillons):
        """Renvoie la valeur d'attaque de cette trame."""
        if not self.dispo:
            return rms(echantillons)
        bloc = _np.asarray(echantillons, dtype=_np.float32)
        k = min(len(bloc), self.n)
        self.tampon = _np.roll(self.tampon, -k)
        self.tampon[-k:] = bloc[-k:]
        spectre = _np.abs(_np.fft.rfft(self.tampon * self.fenetrage))
        if self.precedent is None:
            self.precedent = spectre
            return 0.0
        flux = float(_np.maximum(spectre - self.precedent, 0).sum())
        self.precedent = spectre
        return flux


def enveloppe(niveaux, lissage=8):
    """Enveloppe d'attaque a partir d'une suite de niveaux RMS.

    On ne garde que les HAUSSES d'energie par rapport a la moyenne glissante :
    c'est ce qui distingue un coup de grosse caisse d'un fond sonore continu.
    Une simple derivee suffirait, mais elle reagit au moindre souffle ; la
    moyenne glissante fixe une reference locale.
    """
    n = len(niveaux)
    out = [0.0] * n
    for i in range(n):
        a = max(0, i - lissage)
        moyenne = sum(niveaux[a:i + 1]) / (i - a + 1)
        out[i] = max(0.0, niveaux[i] - moyenne)
    return out


def _normaliser(v):
    m = max(v) if v else 0.0
    return [x / m for x in v] if m > 0 else list(v)


def autocorrelation(env, decalage_min, decalage_max):
    """Autocorrelation normalisee, pour les decalages utiles seulement."""
    n = len(env)
    moyenne = sum(env) / n if n else 0.0
    centre = [x - moyenne for x in env]
    base = sum(x * x for x in centre) or 1.0
    out = {}
    for d in range(decalage_min, min(decalage_max, n - 1) + 1):
        s = sum(centre[i] * centre[i + d] for i in range(n - d))
        out[d] = s / base
    return out


def tempo_et_phase(niveaux, periode_trame_s, masque=None,
                   bpm_min=BPM_MIN, bpm_max=BPM_MAX):
    """(bpm, phase_s, confiance) a partir d'une suite de niveaux RMS.

    phase_s : instant du premier temps, compte depuis le DEBUT de la fenetre.
    confiance : hauteur du pic d'autocorrelation rapportee au bruit de fond.
                En dessous de ~0.15 il n'y a pas de pulsation exploitable.

    masque : liste de booleens de meme longueur, True = trame a ignorer (le
             robot bougeait). Les trames masquees sont ramenees a la moyenne
             locale plutot que supprimees : retirer des trames deformerait
             l'axe du temps, et donc le tempo lui-meme.
    """
    if len(niveaux) < 8:
        return None, None, 0.0

    niveaux = list(niveaux)
    if masque:
        propres = [v for v, m in zip(niveaux, masque) if not m]
        if len(propres) < len(niveaux) * 0.35:
            return None, None, 0.0        # trop peu d'ecoute reelle
        moyenne = sum(propres) / len(propres)
        niveaux = [moyenne if m else v for v, m in zip(niveaux, masque)]

    env = _normaliser(enveloppe(niveaux))
    if max(env) <= 0:
        return None, None, 0.0

    d_min = max(1, int(round(60.0 / bpm_max / periode_trame_s)))
    d_max = int(round(60.0 / bpm_min / periode_trame_s))
    if d_max <= d_min or d_max >= len(env):
        return None, None, 0.0

    # On calcule l'autocorrelation BIEN AU-DELA de la plage utile, parce que le
    # score d'une periode candidate additionne ses multiples (voir plus bas).
    corr = autocorrelation(env, d_min, min(d_max * 4, len(env) - 2))
    if not corr:
        return None, None, 0.0

    def score(periode):
        """Somme ponderee de l'autocorrelation a p, 2p, 3p, 4p.

        Deux corrections, chacune tiree d'un echec mesure :

        1. SOMMER LES HARMONIQUES. Sur un enregistrement reel a 120 BPM, le pic
           brut le plus haut tombait a 60 BPM — le demi-tempo, piege classique
           de l'autocorrelation : un temps sur deux correle aussi bien que
           chaque temps. Une vraie periode se distingue parce que ses multiples
           correlent aussi, ce qu'une subdivision ne fait pas.

        2. ACCEPTER DES PERIODES FRACTIONNAIRES. A 160 BPM la periode vaut 37,5
           trames de 10 ms. En entiers, 2 x 37 = 74 alors que le vrai pic est a
           75 : l'erreur d'arrondi s'accumule a chaque harmonique et le score
           s'effondre, tandis que celui de 75 (le demi-tempo) tombe juste
           partout. On evalue donc des periodes flottantes et on arrondit
           SEULEMENT au moment de lire l'autocorrelation.
        """
        s = 0.0
        for k, poids in enumerate((1.0, 0.7, 0.5, 0.35), start=1):
            v = corr.get(int(round(periode * k)))
            if v is None:
                break
            s += poids * v
        return s

    pas = 0.25
    candidats = [d_min + i * pas
                 for i in range(int((d_max - d_min) / pas) + 1)]
    candidats = [p for p in candidats if int(round(p)) in corr]
    if not candidats:
        return None, None, 0.0
    meilleur = max(candidats, key=score)

    # Sommer les harmoniques cree le defaut inverse : le score d'une periode
    # DOUBLE additionne les memes pics, donc il s'en approche. On redescend donc
    # vers le tempo rapide chaque fois qu'un diviseur marque presque aussi fort.
    # Musicalement c'est aussi le bon choix : mieux vaut bouger sur chaque temps
    # que sur un temps sur deux.
    for _ in range(3):
        pic = score(meilleur)
        remplacant = None
        for k in (2, 3):
            d = meilleur / k
            if d >= d_min and int(round(d)) in corr and score(d) >= pic * 0.85:
                if remplacant is None or score(d) > score(remplacant):
                    remplacant = d
        if remplacant is None:
            break
        meilleur = remplacant

    pic = score(meilleur)
    fond = sum(score(p) for p in candidats) / len(candidats)
    confiance = max(0.0, pic - fond)

    bpm = 60.0 / (meilleur * periode_trame_s)

    # Phase : on essaie chaque decalage possible et on garde celui dont les
    # temps tombent sur le plus d'energie d'attaque.
    entier = max(1, int(round(meilleur)))
    meilleure_phase, meilleur_score = 0, -1.0
    for phase in range(entier):
        cumul = 0.0
        i = phase
        while i < len(env):
            cumul += env[int(i)]
            i += meilleur
        if cumul > meilleur_score:
            meilleur_score, meilleure_phase = cumul, phase

    return bpm, meilleure_phase * periode_trame_s, confiance
