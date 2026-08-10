"""
cw_decoder.py
-------------
Décodeur CW à partir de l'audio de la carte son.

Version améliorée par rapport au premier prototype, sur trois points qui
faisaient le plus défaut en pratique :

1. Sélectivité en fréquence : le calcul de Goertzel se fait maintenant sur
   une fenêtre d'analyse de ~20 ms (au lieu de 10 ms), ce qui réduit de
   moitié la largeur de bande du filtre effectif (~50 Hz au lieu de
   ~100 Hz) — le décodeur est donc moins sensible au bruit et aux
   signaux voisins hors de la fréquence du ton CW réglée.
2. Hystérésis : un seuil plus haut pour détecter le début d'une tonalité
   et un seuil plus bas pour détecter sa fin, ce qui évite les
   "hachures" (basculements parasites) autour du seuil.
3. Estimation de vitesse plus stable : la durée d'un point n'est mise à
   jour qu'à partir des éléments effectivement classés comme points (pas
   mélangée avec les traits), et le bruit de fond n'est réévalué que
   pendant les silences (pas pendant qu'une tonalité est présente) — ce
   qui évite que le seuil ne dérive pendant un CW un peu long.

C'est un décodeur prototype qui reste perfectible (pas de filtrage passe-
bande en amont, pas de gestion fine du QRM), mais ces trois changements
couvrent les causes les plus courantes d'un mauvais décodage en pratique.
"""

import math
import time

import numpy as np
import sounddevice as sd

MORSE_TABLE = {
    '.-': 'A', '-...': 'B', '-.-.': 'C', '-..': 'D', '.': 'E',
    '..-.': 'F', '--.': 'G', '....': 'H', '..': 'I', '.---': 'J',
    '-.-': 'K', '.-..': 'L', '--': 'M', '-.': 'N', '---': 'O',
    '.--.': 'P', '--.-': 'Q', '.-.': 'R', '...': 'S', '-': 'T',
    '..-': 'U', '...-': 'V', '.--': 'W', '-..-': 'X', '-.--': 'Y',
    '--..': 'Z',
    '-----': '0', '.----': '1', '..---': '2', '...--': '3', '....-': '4',
    '.....': '5', '-....': '6', '--...': '7', '---..': '8', '----.': '9',
}


def goertzel_power(samples, sample_rate, target_freq):
    """Retourne l'énergie du signal à target_freq sur ce bloc d'échantillons."""
    n = len(samples)
    k = int(0.5 + (n * target_freq) / sample_rate)
    w = 2 * math.pi * k / n
    cw = 2 * math.cos(w)
    q0 = q1 = q2 = 0.0
    for sample in samples:
        q0 = cw * q1 - q2 + sample
        q2 = q1
        q1 = q0
    return q1 * q1 + q2 * q2 - q1 * q2 * cw


class CWDecoder:
    def __init__(self, sample_rate=8000, tone_freq=700, audio_device=None,
                 silence_timeout=3.0, on_char=None, on_word_end=None, on_silence=None,
                 analysis_window_ms=8, hop_ms=4, on_debug=None, startup_settle_ms=15,
                 gain=1.0, auto_gain=False, agc_target=0.4,
                 mark_threshold_mult=3.5, space_threshold_mult=2.0,
                 amplitude_smoothing_alpha=0.0, debounce_hops=1,
                 tone_tolerance_hz=0, tone_freq_steps=1, on_tick=None):
        self.on_tick = on_tick  # rappel périodique (~1x/s), indépendant du bruit ambiant
        self._last_tick_time = 0.0
        self.sample_rate = sample_rate
        self.tone_freq = tone_freq
        self.audio_device = audio_device
        self.silence_timeout = silence_timeout

        # Tolérance de fréquence : plutôt que d'écouter une seule fréquence
        # exacte, on peut scanner une petite plage autour du réglage et
        # retenir la plus forte réponse — utile si le ton reçu n'est pas
        # exactement à la fréquence configurée (autre correspondant, léger
        # décalage d'accord...). DÉSACTIVÉ PAR DÉFAUT (tone_freq_steps=1) :
        # les tests montrent qu'même une tolérance modeste dégrade nettement
        # la réception d'un signal faible (le "maximum" pris sur plusieurs
        # fréquences laisse passer davantage de bruit). À n'activer que si
        # le ton reçu ne correspond vraiment pas au réglage, pas par défaut.
        if tone_freq_steps > 1:
            half = tone_tolerance_hz / 2
            self._scan_freqs = [
                tone_freq - half + i * (tone_tolerance_hz / (tone_freq_steps - 1))
                for i in range(tone_freq_steps)
            ]
        else:
            self._scan_freqs = [tone_freq]
        self.gain = gain  # multiplicateur manuel ; ignoré/écrasé si auto_gain est actif
        self._stream = None  # référence au flux audio, utilisée par flush_input_buffer()

        # Gain automatique (AGC) : ajuste `self.gain` en continu pour ramener
        # le niveau brut reçu vers `agc_target`. Attaque rapide (le niveau
        # vient de monter, ex: signal fort) / relâchement lent (le niveau
        # redescend), comme un AGC de récepteur classique. À noter : le
        # décodeur utilise déjà des seuils RELATIFS au bruit de fond, donc
        # l'AGC améliore surtout le confort (pas besoin de retoucher le
        # volume manuellement) plus que la fiabilité du décodage en soi.
        self.auto_gain = auto_gain
        self.agc_target = agc_target
        self._agc_peak_estimate = 1e-6

        # Fenêtre d'analyse (sélectivité en fréquence) glissée par petits pas
        # (résolution temporelle) : les deux ne sont plus couplés comme dans
        # la première version. Une fenêtre plus large améliore la
        # sélectivité mais "étale" les durées mesurées — 8 ms est le
        # compromis retenu après tests sur signal simulé aux vitesses CW
        # courantes (15-25 mots/minute).
        self.analysis_window = max(int(sample_rate * analysis_window_ms / 1000), 8)
        self.hop_size = max(int(sample_rate * hop_ms / 1000), 1)
        self._ring = np.zeros(self.analysis_window, dtype=np.float64)

        # Nombre de hops nécessaires pour que le buffer d'analyse soit
        # entièrement composé de nouveaux échantillons (et non plus en partie
        # de zéros de départ) — tant que ce n'est pas le cas, une mesure
        # d'amplitude serait faussée (sous-estimée).
        self._warmup_hops_needed = -(-self.analysis_window // self.hop_size)  # arrondi au sup.
        self._hops_seen = 0

        # Calibration initiale du bruit de fond : on observe plusieurs mesures
        # (pas une seule !) juste après le préchauffage, et on retient leur
        # MÉDIANE comme estimation de départ (pas le minimum : le minimum
        # d'une série de mesures est structurellement biaisé vers le bas —
        # un artefact statistique classique — et rendrait le seuil bien trop
        # sensible au moindre bruit). Se caler sur une seule mesure est
        # risqué : si l'audio démarre alors qu'une tonalité est déjà présente
        # (le correspondant est déjà en train d'émettre au moment du clic sur
        # "Démarrer"), cette unique mesure calibrerait le bruit de fond sur la
        # tonalité elle-même, rendant tout signal — surtout un signal faible —
        # impossible à distinguer ensuite. La médiane sur une fenêtre résiste
        # aux deux problèmes à la fois.
        self._calibration_hops_needed = 25  # ~100 ms à 4 ms/hop
        self._calibration_samples = []

        self.noise_floor = None
        self.mark_threshold_mult = mark_threshold_mult   # seuil de déclenchement (début de tonalité)
        self.space_threshold_mult = space_threshold_mult  # seuil de relâchement (fin de tonalité), hystérésis
        self.letter_gap_mult = 1.8       # espace > 1.8x la durée d'un point : fin de lettre
        self.word_gap_mult = 4.5         # espace > 4.5x la durée d'un point : fin de mot

        # Lissage de l'amplitude (moyenne mobile) + confirmation sur plusieurs
        # mesures consécutives avant de valider un changement d'état : ça
        # rejette une bonne partie des impulsions de bruit/QRM (clics brefs).
        # DÉSACTIVÉ PAR DÉFAUT (alpha=0.0, debounce=1) : les tests montrent
        # que ça dégrade nettement la réception des signaux faibles (la
        # confirmation multi-hops retarde justement la détection d'un
        # élément faible qui ne dépasse le seuil que brièvement). C'est un
        # vrai compromis, pas un réglage à activer systématiquement — à
        # n'activer (voir le réglage "Réduction du bruit impulsif" dans
        # l'interface) que si le signal est plutôt correct mais parasité
        # par des clics/QRM, pas s'il est simplement faible.
        self.amplitude_smoothing_alpha = amplitude_smoothing_alpha
        self._smoothed_amplitude = None
        self.debounce_hops = debounce_hops
        self._pending_state = None
        self._pending_count = 0

        # Sensibilité automatique : au lieu d'un seuil fixe (multiple constant
        # du bruit de fond), on suit l'amplitude des tops effectivement reçus
        # et on place le seuil à mi-chemin (en échelle log) entre le bruit de
        # fond et ce niveau — s'adapte donc tout seul à un signal fort ou
        # faible. Tant qu'aucun top n'a encore été confirmé récemment (ou
        # après une longue inactivité), on repart en mode "recherche" avec un
        # seuil sensible, le temps qu'un signal soit à nouveau détecté.
        self.auto_sensitivity = False
        self._auto_peak_amplitude = None
        self._auto_last_mark_time = 0.0
        self._auto_search_mark_mult = 2.3
        self._auto_search_space_mult = 1.4
        self._auto_min_mark_mult = 1.6
        self._auto_max_mark_mult = 4.0
        self._auto_fraction = 0.4  # position du seuil entre bruit et crête (0=bruit, 1=crête)
        self._auto_reset_after_s = 5.0  # inactivité avant retour en mode recherche

        # Beaucoup de postes/relais produisent un bref artefact (clic, sursaut
        # d'AGC) juste au moment où l'autre station commence à émettre. On
        # ignore volontairement ce court instant après un long silence
        # (début probable d'une nouvelle transmission), avant de commencer à
        # mesurer le premier élément — ça évite qu'un clic ne soit compté
        # comme un élément Morse à part entière ou n'allonge le premier point.
        self.startup_settle_s = startup_settle_ms / 1000
        self._settle_until = 0.0

        # Sourdine temporaire après une émission de NOTRE part (voir
        # mute_for()) : un simple vidage du tampon audio juste après avoir
        # coupé le PTT ne suffit pas toujours, car un résidu de notre propre
        # signal peut encore arriver avec un léger délai (relais, pilote
        # audio). Pendant cette fenêtre, tout l'audio reçu est ignoré.
        self._mute_until = 0.0

        self.tone_state = False
        self.state_start = time.time()

        self.dot_len = 0.08  # estimation initiale (~15 mots/minute), s'affine ensuite
        self.symbol_buffer = ""
        self.last_amplitude = 0.0

        self.on_char = on_char or (lambda c: None)
        self.on_word_end = on_word_end or (lambda: None)
        self.on_silence = on_silence or (lambda: None)
        self.on_debug = on_debug  # None par défaut ; sinon appelé avec (kind, duration_ms, dot_len_ms, symbol)
        self._silence_since = time.time()

    def _flush_symbol(self):
        if self.symbol_buffer:
            letter = MORSE_TABLE.get(self.symbol_buffer, "")

            if not letter and len(self.symbol_buffer) > 1:
                # Motif invalide : très souvent causé par un élément parasite
                # (clic de relais, sursaut d'AGC...) juste avant le vrai
                # premier caractère d'une transmission. Si retirer ce tout
                # premier symbole donne un motif valide, on l'utilise.
                stripped = self.symbol_buffer[1:]
                candidate = MORSE_TABLE.get(stripped, "")
                if candidate:
                    letter = candidate

            if letter:
                self.on_char(letter)
            else:
                # Motif non reconnu : on l'affiche quand même entre crochets
                # plutôt que de le faire disparaître silencieusement. Voir
                # apparaître beaucoup de motifs de ce type (et leur forme)
                # est le meilleur indice pour diagnostiquer un problème de
                # réglage (fréquence du ton, niveau audio, seuils).
                self.on_char(f"[{self.symbol_buffer}]")
            self.symbol_buffer = ""

    # Durée minimale plausible d'un point, même à vitesse CW très élevée
    # (~80 mots/minute). En dessous, c'est un clic/glitch de bruit, pas un
    # vrai élément Morse — on l'ignore complètement plutôt que de le
    # compter comme point, pour ne pas corrompre l'estimation de vitesse.
    MIN_PLAUSIBLE_MARK_S = 0.015

    def _handle_mark_end(self, duration, amplitude):
        """Une tonalité vient de se terminer : classe point ou trait."""
        if duration < self.MIN_PLAUSIBLE_MARK_S:
            # Glitch de bruit trop bref pour être un vrai élément : on
            # l'ignore entièrement (ni point, ni trait, aucun effet sur
            # symbol_buffer ni sur dot_len). Particulièrement utile en
            # sensibilité "Très sensible", où le seuil bas laisse parfois
            # passer de courtes impulsions de bruit ambiant.
            if self.on_debug:
                self.on_debug("mark", duration * 1000, self.dot_len * 1000, "(ignoré, trop bref)")
            return

        self.last_amplitude = amplitude

        if self.auto_sensitivity:
            if self._auto_peak_amplitude is None:
                self._auto_peak_amplitude = amplitude
            else:
                self._auto_peak_amplitude = 0.7 * self._auto_peak_amplitude + 0.3 * amplitude
            self._auto_last_mark_time = time.time()

        dot_len_before = self.dot_len
        if duration < self.dot_len * 1.8:
            symbol = "."
            self.symbol_buffer += symbol
            # On ne recale la durée du point qu'à partir des éléments
            # effectivement classés comme points : plus stable que de
            # mélanger points et traits dans la même moyenne. Le facteur de
            # lissage est volontairement réactif (0.5) pour converger vite
            # vers la bonne vitesse dès les premiers caractères, même si
            # l'estimation initiale (~15 mots/minute) est très éloignée de
            # la vitesse réelle de l'opérateur.
            self.dot_len = 0.5 * self.dot_len + 0.5 * duration
            # Garde-fou : quelle que soit la mise à jour, la vitesse estimée
            # reste dans une plage plausible (entre ~4 et ~80 mots/minute).
            # Ça évite qu'une poignée de glitches très brefs (qui auraient
            # échappé au filtre ci-dessus) ne fasse dériver l'estimation
            # jusqu'à rendre le décodage incohérent.
            self.dot_len = min(max(self.dot_len, self.MIN_PLAUSIBLE_MARK_S), 0.3)
        else:
            symbol = "-"
            self.symbol_buffer += symbol

        if self.on_debug:
            self.on_debug("mark", duration * 1000, dot_len_before * 1000, symbol)

    def _handle_space_end(self, duration):
        """Un silence vient de se terminer : espace intra-lettre, inter-lettre ou inter-mot."""
        if self.on_debug:
            self.on_debug("space", duration * 1000, self.dot_len * 1000, "")
        if duration > self.dot_len * self.word_gap_mult:
            self._flush_symbol()
            self.on_word_end()
        elif duration > self.dot_len * self.letter_gap_mult:
            self._flush_symbol()
        # sinon : espace intra-caractère (~1 unité), rien à faire

    def _update_auto_gain(self, hop_samples):
        """Ajuste self.gain automatiquement pour ramener le niveau brut vers agc_target."""
        raw_peak = float(np.max(np.abs(hop_samples))) if len(hop_samples) else 0.0

        if raw_peak > self._agc_peak_estimate:
            # Attaque rapide : le niveau vient de monter (signal fort qui arrive).
            self._agc_peak_estimate = 0.7 * self._agc_peak_estimate + 0.3 * raw_peak
        else:
            # Relâchement lent : le niveau redescend (fin de signal, silence).
            self._agc_peak_estimate = 0.995 * self._agc_peak_estimate + 0.005 * raw_peak

        self._agc_peak_estimate = max(self._agc_peak_estimate, 1e-6)
        self.gain = min(max(self.agc_target / self._agc_peak_estimate, 0.1), 20.0)

    def _process_hop(self, hop_samples):
        if time.time() < self._mute_until:
            # Sourdine post-émission active : on ignore complètement ce
            # hop (ni ring, ni bruit de fond, ni décision) pour ne rien
            # laisser passer d'un éventuel résidu de notre propre signal.
            return

        if self.on_tick is not None:
            # Volontairement indépendant de tone_state : dans un environnement
            # bruyant (surtout en sensibilité "Sensible"/"Très sensible"), le
            # décodeur peut capter du bruit ambiant en quasi continu, et
            # attendre un instant "calme" pouvait retarder ce contrôle de
            # plusieurs minutes. Ce simple contrôle périodique ne perturbe
            # rien même s'il tombe en pleine tonalité.
            now_tick = time.time()
            if now_tick - self._last_tick_time >= 1.0:
                self._last_tick_time = now_tick
                self.on_tick(now_tick)

        if self.auto_gain:
            self._update_auto_gain(hop_samples)

        if self.gain != 1.0:
            hop_samples = hop_samples * self.gain

        n = len(hop_samples)
        # buffer circulaire : on décale et on insère les nouveaux échantillons
        self._ring[:-n] = self._ring[n:]
        self._ring[-n:] = hop_samples

        if self._hops_seen < self._warmup_hops_needed:
            # Le buffer d'analyse contient encore une partie des zéros de
            # départ : une mesure d'amplitude serait sous-estimée. On ne
            # prend aucune décision tant qu'il n'est pas entièrement composé
            # de vrais échantillons.
            self._hops_seen += 1
            return

        now = time.time()

        if now < self._settle_until:
            # Fenêtre de stabilisation en cours (juste après un long silence) :
            # on ignore volontairement ce court instant pour ne pas laisser un
            # artefact de début de transmission (clic, sursaut d'AGC) fausser
            # la mesure du premier élément qui suit.
            return

        # On scanne toute la plage de tolérance et on retient la plus forte
        # réponse — tolère un ton reçu qui ne correspond pas exactement à la
        # fréquence réglée.
        amplitude = 0.0
        for freq in self._scan_freqs:
            power = goertzel_power(self._ring, self.sample_rate, freq)
            amp = math.sqrt(power) / len(self._ring)
            if amp > amplitude:
                amplitude = amp

        if self.noise_floor is None:
            # Phase de calibration : on observe plusieurs mesures et on
            # retient leur MÉDIANE, plutôt que de se fier à une seule mesure
            # ou au minimum (voir le commentaire détaillé dans __init__).
            self._calibration_samples.append(amplitude)
            self._hops_seen += 1
            if self._hops_seen - self._warmup_hops_needed < self._calibration_hops_needed:
                return
            sorted_samples = sorted(self._calibration_samples)
            median = sorted_samples[len(sorted_samples) // 2]
            self.noise_floor = max(median, 1e-9)
            self._calibration_samples = None  # libère la mémoire, plus utile ensuite

        # Le bruit de fond n'est réévalué QUE pendant les silences, pour ne
        # pas dériver pendant une tonalité un peu longue.
        elif not self.tone_state:
            self.noise_floor = 0.98 * self.noise_floor + 0.02 * amplitude

        # Lissage : on prend la décision sur une moyenne mobile de l'amplitude
        # plutôt que sur la mesure brute d'un seul hop, pour amortir les
        # sursauts brefs de bruit/QRM. Le bruit de fond, lui, continue d'être
        # évalué sur la mesure brute (voir ci-dessus).
        if self._smoothed_amplitude is None:
            self._smoothed_amplitude = amplitude
        else:
            self._smoothed_amplitude = (self.amplitude_smoothing_alpha * self._smoothed_amplitude
                                         + (1 - self.amplitude_smoothing_alpha) * amplitude)
        decision_amplitude = self._smoothed_amplitude

        if self.auto_sensitivity:
            mark_mult, space_mult = self._compute_auto_thresholds(now)
        else:
            mark_mult, space_mult = self.mark_threshold_mult, self.space_threshold_mult

        want_mark = decision_amplitude > self.noise_floor * mark_mult
        want_space = decision_amplitude < self.noise_floor * space_mult

        if not self.tone_state and want_mark:
            confirmed = self._confirm_pending("mark")
            if confirmed:
                duration = now - self.state_start
                self._handle_space_end(duration)

                is_transmission_start = duration > self.dot_len * self.word_gap_mult
                if is_transmission_start and self.startup_settle_s > 0:
                    if self.on_debug:
                        self.on_debug("settle", self.startup_settle_s * 1000,
                                       self.dot_len * 1000, "")
                    self._settle_until = now + self.startup_settle_s
                    self.state_start = now
                    self._silence_since = now
                    return

                self.tone_state = True
                self.state_start = now
                self._silence_since = now

        elif self.tone_state and want_space:
            confirmed = self._confirm_pending("space")
            if confirmed:
                duration = now - self.state_start
                self._handle_mark_end(duration, amplitude)
                self.tone_state = False
                self.state_start = now
                self._silence_since = now
        else:
            self._pending_state = None
            self._pending_count = 0

        if not self.tone_state and (now - self._silence_since) > self.silence_timeout:
            self._flush_symbol()
            self.on_silence()
            self._silence_since = now

    def _compute_auto_thresholds(self, now):
        """
        Calcule le seuil de déclenchement/relâchement en mode "sensibilité
        automatique" : placé à mi-chemin (en échelle log, plus adapté à des
        rapports signal/bruit très variés) entre le bruit de fond et
        l'amplitude des tops récemment confirmés. Si aucun top n'a été
        confirmé récemment (début d'écoute, ou longue inactivité), on repart
        en mode "recherche" avec un seuil sensible fixe, le temps qu'un
        signal soit à nouveau détecté et qu'on puisse recalibrer dessus.
        """
        no_recent_mark = (self._auto_peak_amplitude is None
                           or (now - self._auto_last_mark_time) > self._auto_reset_after_s)
        if no_recent_mark:
            self._auto_peak_amplitude = None
            return self._auto_search_mark_mult, self._auto_search_space_mult

        ratio = max(self._auto_peak_amplitude / self.noise_floor, 1.01)
        # Position à mi-chemin en échelle log entre 1x (bruit) et le ratio
        # crête/bruit observé, plutôt qu'une simple moyenne linéaire — plus
        # stable quand le rapport signal/bruit est très grand (signal fort).
        log_mult = self._auto_fraction * math.log(ratio)
        mark_mult = min(max(math.exp(log_mult), self._auto_min_mark_mult), self._auto_max_mark_mult)
        space_mult = max(mark_mult * 0.6, 1.05)
        return mark_mult, space_mult

    def _confirm_pending(self, candidate_state):
        """
        Exige plusieurs mesures consécutives d'accord (`debounce_hops`) avant
        de confirmer un changement d'état — rejette ainsi une impulsion de
        bruit isolée qui ne durerait qu'un seul hop.
        """
        if self.debounce_hops <= 1:
            return True
        if self._pending_state == candidate_state:
            self._pending_count += 1
        else:
            self._pending_state = candidate_state
            self._pending_count = 1
        if self._pending_count >= self.debounce_hops:
            self._pending_state = None
            self._pending_count = 0
            return True
        return False

    def estimate_rst(self, rig_smeter=None):
        """
        Estimation d'un rapport RST. Si `rig_smeter` est fourni, c'est la
        valeur BRUTE Kenwood du S-mètre (échelle 0-30, où 20 = S9) lue via
        la commande CAT 'SM0;' — la même échelle que celle affichée sur
        l'écran du poste. On l'utilise en priorité pour le "S", bien plus
        fiable qu'une estimation audio locale.
        """
        if rig_smeter is not None:
            # 0-20 -> S1-S9 (à peu près linéaire), 20-30 -> toujours "9"
            # dans le rapport RST (le "+dB au-dessus de S9" ne se code pas
            # dans le chiffre unique du RST, comme c'est l'usage).
            # La lisibilité (R) est fixée à 5 : seul le "S" reflète la
            # vraie mesure du S-mètre du poste.
            strength = min(9, max(1, round(rig_smeter * 9 / 20)))
            return f"5{strength}9"

        if self.noise_floor <= 0:
            return "599"
        snr = self.last_amplitude / (self.noise_floor + 1e-9)
        strength = min(9, max(1, int(3 + math.log2(max(snr, 1)))))
        readability = 5 if snr > 2 else max(1, int(strength / 2))
        return f"{readability}{strength}9"

    def measure_audio(self, duration=3.0):
        """
        Échantillonne l'audio pendant `duration` secondes et retourne des
        niveaux utiles au diagnostic :
        - peak_raw : amplitude brute maximale du signal audio (tous sons confondus)
        - peak_tone : amplitude maximale mesurée précisément à la fréquence
          du ton CW réglée (via Goertzel)

        Si peak_raw est très faible : mauvais périphérique choisi, ou
        volume d'entrée coupé/trop bas.
        Si peak_tone est nettement plus faible que peak_raw pendant que
        vous faites entendre un CW au poste : la fréquence du ton réglée
        ne correspond probablement pas à ce que vous entendez au casque.
        """
        peak_raw = 0.0
        peak_tone = 0.0
        ring = np.zeros(self.analysis_window)

        with sd.InputStream(channels=1, samplerate=self.sample_rate,
                             blocksize=self.hop_size, device=self.audio_device) as stream:
            n_hops = max(int(duration * self.sample_rate / self.hop_size), 1)
            for _ in range(n_hops):
                block, _ = stream.read(self.hop_size)
                hop = block[:, 0]
                if self.gain != 1.0:
                    hop = hop * self.gain
                peak_raw = max(peak_raw, float(np.max(np.abs(hop))))

                ring[:-len(hop)] = ring[len(hop):]
                ring[-len(hop):] = hop
                power = goertzel_power(ring, self.sample_rate, self.tone_freq)
                amp = math.sqrt(power) / len(ring)
                peak_tone = max(peak_tone, amp)

        return {"peak_raw": peak_raw, "peak_tone": peak_tone}

    def mute_for(self, seconds: float):
        """
        Ignore tout l'audio reçu pendant les `seconds` secondes qui suivent.
        À appeler juste après une émission CW (en plus de flush_input_buffer)
        pour couvrir le petit délai avec lequel un résidu de notre propre
        signal peut encore arriver (relais, pilote audio).
        """
        self._mute_until = time.time() + seconds

    def set_tone_tolerance(self, tolerance_hz: float, steps: int):
        """Recalcule en direct la plage de fréquences scannées autour de tone_freq."""
        if steps > 1:
            half = tolerance_hz / 2
            self._scan_freqs = [
                self.tone_freq - half + i * (tolerance_hz / (steps - 1))
                for i in range(steps)
            ]
        else:
            self._scan_freqs = [self.tone_freq]

    def flush_input_buffer(self):
        """
        Vide l'audio accumulé dans le tampon d'entrée (sans le traiter).
        À appeler juste après avoir émis en CW : pendant qu'on transmet, la
        carte son continue de capturer de l'audio en arrière-plan (souvent
        un peu de notre propre signal qui boucle vers l'entrée micro/ligne).
        Sans ce vidage, ce résidu serait décodé au retour en écoute et
        pourrait être confondu avec une réponse du correspondant.
        """
        stream = self._stream
        if stream is None:
            return
        try:
            available = stream.read_available
            if available > 0:
                stream.read(available)
        except Exception:
            pass

    def run_forever(self, stop_event=None):
        """
        Boucle d'écoute. Si `stop_event` (threading.Event) est fourni, la
        boucle s'arrête proprement dès qu'il est activé (utilisé par
        l'interface graphique pour le bouton "Arrêter").
        """
        with sd.InputStream(channels=1, samplerate=self.sample_rate,
                             blocksize=self.hop_size, device=self.audio_device) as stream:
            self._stream = stream
            try:
                while stop_event is None or not stop_event.is_set():
                    block, _ = stream.read(self.hop_size)
                    self._process_hop(block[:, 0])
            finally:
                self._stream = None
