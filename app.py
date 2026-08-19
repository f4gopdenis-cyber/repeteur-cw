"""
app.py
------
Application unique (interface graphique) pour le répéteur CW HF Kenwood
TS-990S. Tous les réglages se font dans l'onglet "Réglages" — y compris le
port COM et le baud rate du poste, réglés directement ici — et le
démarrage/arrêt et le suivi en direct se font dans l'onglet "Répéteur".

Le poste est piloté DIRECTEMENT en CAT via le port série (voir
rig_control.py) : aucun logiciel intermédiaire requis (ni Hamlib, ni
OmniRig), seulement le pilote USB Kenwood du TS-990S.

Ce fichier est destiné à être compilé en un seul exécutable Windows via
PyInstaller (voir build_exe.bat).
"""

import json
import queue
import random
import string
import sys
import threading
import time
import traceback
import urllib.request
import tkinter as tk
from pathlib import Path
from tkinter import colorchooser, messagebox, scrolledtext, ttk

from cw_decoder import CWDecoder
from rig_control import RigCTL

try:
    import serial.tools.list_ports as list_ports
except ImportError:
    list_ports = None

try:
    import sounddevice as sd
except ImportError:
    sd = None


# --- Emplacement des réglages : à côté de l'exécutable (ou du script) ---
if getattr(sys, "frozen", False):
    BASE_DIR = Path(sys.executable).parent
else:
    BASE_DIR = Path(__file__).parent

SETTINGS_PATH = BASE_DIR / "settings.json"

DEFAULT_SETTINGS = {
    "my_call": "F0XXX",
    "my_locator": "JN18SO",
    "my_qth": "FRANCE",
    "my_name": "OP",
    "my_rig": "TS-990S",
    "my_power": "100",
    "my_antenna": "DIPOLE",
    "com_port": "",
    "baud": "115200",
    "audio_device": "",       # index sous forme de texte, ou vide = défaut
    "rx_gain": "1.0",         # gain logiciel appliqué au signal reçu (curseur "Volume")
    "sensitivity": "normal",  # "normal" (3.5x/2.0x), "sensible" (2.5x/1.5x), "tres_sensible" (1.8x/1.2x)
    "auto_gain": False,       # True = gain automatique (AGC), le curseur devient indicatif seulement
    "tone_freq": "700",
    "cw_speed": "20",
    "char_spacing_extra": "0",  # nombre d'espaces CW supplémentaires insérés entre les lettres à l'émission (0-5)
    "word_gap_ms": "0",   # silence supplémentaire entre les mots à l'émission, PTT réellement coupé (0-2000ms)
    "debug_mode": False,      # affiche les durées mesurées (point/trait/espace) dans le journal
    "qso_mode": True,         # False = simple rapport, True = QSO scripté en plusieurs échanges
    "qrm_reduction": False,   # lissage + confirmation multi-mesures (aide contre le QRM, mais réduit la portée sur signal faible)
    "freq_tolerance": False,  # tolère un ton reçu différent du réglage (utile si décalé, mais réduit la portée sur signal faible)
    "weather_enabled": False,   # diffuse automatiquement température/pression toutes les weather_interval_h heures
    "weather_interval_h": "2",
    "weather_template": "WX RPT TEMP {temp} C PRESS {pressure} HPA {call} K",
    "theme_color": "#1d4666",   # couleur du bandeau et des titres de section (personnalisable)
    "theme_bg_color": "#eef1f5",  # couleur de fond de l'application (personnalisable)
}

# Script du QSO : chaque étape est envoyée quand le correspondant termine son
# tour de parole (silence détecté), dans l'ordre. Personnalisable ici si vous
# voulez changer la formulation ; les champs entre accolades sont remplacés
# automatiquement par vos réglages.
QSO_STEPS = [
    "UR RST {rst} {rst} NAME {name} {name} QTH {qth} HW? {call} K",
    "RIG HR {rig} PWR {power}W ANT {antenna} TNX FER RPRT {name} FB QSO HW? {call} K",
    "TNX FER NICE QSO {name} 73 GL CUL {call} SK",
]

# Exercice de copie au son : le correspondant envoie EXC (chiffres), EXN
# (lettres) ou EXM (mots) pour démarrer, le programme envoie un groupe/mot,
# attend que le correspondant renvoie ce qu'il a copié, n'émet rien tant que
# ce n'est pas correct (attend un AGN explicite pour répéter), ou confirme
# par "RR" puis passe au suivant. 10 au total.
EXERCISE_TRIGGER_DIGITS = "EXC"
EXERCISE_TRIGGER_LETTERS = "EXN"
EXERCISE_TRIGGER_WORDS = "EXM"
EXERCISE_REPEAT_REQUEST = "AGN"  # le correspondant demande de répéter le groupe/mot
EXERCISE_GROUP_LENGTH = 5
EXERCISE_TOTAL_ROUNDS = 10
EXERCISE_PAUSE_BEFORE_NEXT_S = 2.5  # pause après le RR, avant d'envoyer le suivant

# Liste de mots pour l'exercice EXM (vocabulaire courant en radioamateur).
# Volontairement sans "AGN"/"RR"/"EXC"/"EXN"/"EXM" pour éviter toute ambiguïté
# avec les mots de contrôle. Modifiable ici si vous voulez d'autres mots.
EXERCISE_WORD_LIST = [
    "RADIO", "ANTENNA", "SIGNAL", "POWER", "WEATHER", "STATION", "OPERATOR",
    "FREQUENCY", "MORSE", "CONTACT", "RECEIVE", "TRANSMIT", "LICENSE",
    "CALLSIGN", "DISTANCE", "BATTERY", "CIRCUIT", "VOLTAGE", "CURRENT",
    "MAGNET", "COMPUTER", "KEYBOARD", "MONITOR", "SPEAKER", "HEADSET",
    "MICROPHONE", "CABLE", "CONNECTOR", "SWITCH", "BUTTON", "HANDLE",
    "BRACKET", "TOWER", "MOUNTAIN", "VALLEY", "RIVER", "FOREST", "GARDEN",
    "KITCHEN", "WINDOW", "TABLE", "CHAIR", "MIRROR", "CLOCK", "VILLAGE",
]

# Sensibilité de détection : seuil de déclenchement / relâchement de tonalité,
# en multiple du bruit de fond mesuré. Plus bas = détecte des signaux plus
# faibles, mais plus sensible aussi au bruit ambiant (plus de faux positifs).
SENSITIVITY_LEVELS = {
    "normal": (3.5, 2.0),
    "sensible": (2.5, 1.5),
    "tres_sensible": (1.8, 1.2),
}
SENSITIVITY_LABELS = {
    "normal": "Normal",
    "sensible": "Sensible (signaux faibles)",
    "tres_sensible": "Très sensible (signaux très faibles, plus de faux positifs)",
}


def maidenhead_to_latlon(locator: str):
    """Convertit un locator Maidenhead (ex: JN18SO) en (latitude, longitude)."""
    locator = locator.strip().upper()
    a = ord('A')
    lon = (ord(locator[0]) - a) * 20 - 180
    lat = (ord(locator[1]) - a) * 10 - 90
    lon += int(locator[2]) * 2
    lat += int(locator[3]) * 1
    if len(locator) >= 6:
        lon += (ord(locator[4]) - a) * (2 / 24)
        lat += (ord(locator[5]) - a) * (1 / 24)
        lon += (2 / 24) / 2
        lat += (1 / 24) / 2
    else:
        lon += 1
        lat += 0.5
    return lat, lon


def fetch_weather(lat: float, lon: float, timeout: float = 8.0):
    """
    Récupère la température (°C) et la pression (hPa) actuelles via Open-Meteo
    (service gratuit, sans clé API). Retourne (temp_c, pressure_hpa), ou lève
    une exception en cas d'échec réseau.
    """
    url = (
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={lat:.4f}&longitude={lon:.4f}"
        "&current=temperature_2m,pressure_msl"
    )
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    current = data.get("current", {})
    return current.get("temperature_2m"), current.get("pressure_msl")


def format_temp_for_cw(temp_c) -> str:
    """Formate une température pour l'envoi CW (pas de signe '-', on écrit MINUS)."""
    if temp_c is None:
        return "NA"
    value = round(temp_c)
    if value < 0:
        return f"MINUS {abs(value)}"
    return str(value)


def load_settings():
    if SETTINGS_PATH.exists():
        try:
            data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
            merged = dict(DEFAULT_SETTINGS)
            merged.update(data)
            return merged
        except (json.JSONDecodeError, OSError):
            pass
    return dict(DEFAULT_SETTINGS)


def save_settings(settings: dict):
    SETTINGS_PATH.write_text(json.dumps(settings, indent=2, ensure_ascii=False), encoding="utf-8")


class RepeaterEngine:
    """Gère le cycle de vie de la connexion série + du décodeur, dans un thread séparé."""

    def __init__(self, settings: dict, log_callback):
        self.settings = settings
        self.log = log_callback
        self.stop_event = threading.Event()
        self.thread = None
        self.rig = None
        self.decoder = None
        self.received_buffer = []

        # État du QSO scripté (voir _on_silence / _send_qso_step)
        self.qso_active = False
        self.qso_step = 0
        self.qso_empty_silences = 0
        self._current_rst = "599"

        # État de l'exercice de copie (groupes de 5) — voir _on_silence /
        # _start_exercise / _send_exercise_group
        self.exercise_active = False
        self.exercise_kind = None  # "digits" ou "letters"
        self.exercise_round = 0
        self.exercise_current_group = ""
        self.exercise_empty_silences = 0

        # Balise météo automatique (voir _on_silence_impl / _send_weather_report)
        self._last_weather_time = 0.0
        self._weather_test_requested = threading.Event()

    def is_running(self):
        return self.thread is not None and self.thread.is_alive()

    def set_gain(self, gain: float):
        """Applique immédiatement un nouveau gain au décodeur en cours d'écoute, si actif."""
        if self.decoder is not None:
            self.decoder.gain = gain

    def set_auto_gain(self, enabled: bool):
        """Active/désactive l'AGC en direct sur le décodeur en cours d'écoute, si actif."""
        if self.decoder is not None:
            self.decoder.auto_gain = enabled

    def set_sensitivity(self, key: str):
        """Applique immédiatement un nouveau niveau de sensibilité, si le décodeur tourne."""
        if self.decoder is not None:
            mark_mult, space_mult = SENSITIVITY_LEVELS.get(key, SENSITIVITY_LEVELS["normal"])
            self.decoder.mark_threshold_mult = mark_mult
            self.decoder.space_threshold_mult = space_mult

    def set_qrm_reduction(self, enabled: bool):
        """Active/désactive la réduction de bruit impulsif en direct, si le décodeur tourne."""
        if self.decoder is not None:
            self.decoder.amplitude_smoothing_alpha = 0.25 if enabled else 0.0
            self.decoder.debounce_hops = 2 if enabled else 1

    def set_freq_tolerance(self, enabled: bool):
        """Active/désactive la tolérance de fréquence élargie en direct, si le décodeur tourne."""
        if self.decoder is not None:
            tol = 150 if enabled else 0
            steps = 7 if enabled else 1
            self.decoder.set_tone_tolerance(tol, steps)

    def request_weather_test(self):
        """
        Demande l'émission d'un rapport météo dès que possible, en toute
        sécurité : le drapeau est simplement posé ici (thread-safe), et
        c'est le thread unique du répéteur qui l'exécutera à son prochain
        passage (quelques secondes maximum) — jamais depuis un thread
        séparé, pour ne jamais faire se chevaucher deux accès au port série.
        """
        self._weather_test_requested.set()

    def start(self):
        if self.is_running():
            return
        self.stop_event.clear()
        self.qso_active = False
        self.qso_step = 0
        self.qso_empty_silences = 0
        self.exercise_active = False
        self.exercise_kind = None
        self.exercise_round = 0
        self.exercise_current_group = ""
        self.exercise_empty_silences = 0
        self._last_weather_time = time.time()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.rig:
            self.rig.close()
            self.rig = None

    def _build_simple_reply(self, rst: str) -> str:
        s = self.settings
        return (f"DE {s['my_call']} UR RST {rst} NAME {s['my_name']} "
                f"QTH {s['my_locator']} {s['my_qth']} K")

    def _send_qso_step(self):
        s = self.settings
        idx = self.qso_step - 1  # qso_step est 1-indexé côté log utilisateur

        if idx == 0:
            smeter = self.rig.get_smeter()
            self._current_rst = self.decoder.estimate_rst(smeter)

        template = QSO_STEPS[idx]
        message = template.format(
            call=s["my_call"],
            name=s["my_name"],
            qth=f"{s['my_locator']} {s['my_qth']}",
            rig=s["my_rig"],
            power=s["my_power"],
            antenna=s["my_antenna"],
            rst=self._current_rst,
        )

        self.log(f"[QSO étape {self.qso_step}/{len(QSO_STEPS)}] {message}")
        self._send_cw_text(message)

    # ------------------------------------------------------------- Émission
    def _send_cw_text(self, text):
        """
        Point unique d'émission CW : bascule le PTT, envoie le texte, puis
        revient en réception. Vide ensuite le tampon audio accumulé pendant
        l'émission, ET ignore encore l'audio pendant une courte fenêtre
        supplémentaire — sinon un résidu de notre propre signal (qui boucle
        souvent un peu vers l'entrée micro/ligne, parfois avec un léger
        délai de relais) pourrait être décodé au retour en écoute et
        confondu avec une réponse du correspondant, provoquant une relance
        automatique en boucle.

        Pour tout espacement supplémentaire (entre lettres et/ou entre mots),
        le PTT est réellement coupé pendant la pause — silence garanti à
        100%, puisqu'il n'y a alors plus aucune porteuse du tout. Une
        première version insérait des espaces supplémentaires dans le texte,
        mais le poste fusionne en réalité plusieurs espaces consécutifs en un
        seul silence standard, ce qui n'avait donc aucun effet audible, ni
        pour les mots ni pour les lettres.

        Attention : si l'espacement entre lettres est activé, le PTT est
        coupé/rétabli à CHAQUE lettre — le relais TX/RX du poste va donc
        cliqueter à chaque lettre. C'est le prix à payer pour un silence
        garanti ; à utiliser avec modération (valeurs faibles) si le bruit
        du relais vous gêne.
        """
        try:
            char_spacing_count = int(float(self.settings.get("char_spacing_extra", "0")))
        except (ValueError, TypeError):
            char_spacing_count = 0
        char_gap_s = char_spacing_count * 0.2  # 200 ms de silence réel par cran (0-5 -> 0-1000ms)
        try:
            word_gap_s = float(self.settings.get("word_gap_ms", "0")) / 1000
        except (ValueError, TypeError):
            word_gap_s = 0.0

        # Court délai après CHAQUE réactivation du PTT, avant de reprendre
        # l'émission : sur certains postes, le relais TX/RX a besoin d'un
        # court instant pour se stabiliser, sans quoi le tout début du
        # caractère suivant peut être partiellement coupé (de façon
        # intermittente). 40 ms est une valeur prudente, inaudible en tant
        # que telle mais suffisante pour laisser le relais se stabiliser.
        PTT_SETTLE_S = 0.04

        debug_on = bool(self.settings.get("debug_mode", False))
        wpm = int(self.settings["cw_speed"])
        words = [w for w in text.split(" ") if w != ""]

        def flush_decoder():
            # Vide le tampon audio à CHAQUE bascule de PTT — pas seulement à
            # la toute fin du message. Avec un espacement lettres/mots actif,
            # une émission comme le rapport météo peut durer plusieurs
            # dizaines de secondes avec de nombreuses bascules PTT ; ne vider
            # le tampon qu'à la toute fin laissait le résidu de plusieurs
            # bascules s'accumuler, et le décodeur finissait par "rattraper"
            # tout ce résidu d'un coup au retour en écoute, le confondant
            # avec une réponse du correspondant.
            if self.decoder is not None:
                self.decoder.flush_input_buffer()

        def key_up():
            self.rig.set_ptt(True)
            time.sleep(PTT_SETTLE_S)
            flush_decoder()

        def key_down():
            self.rig.set_ptt(False)
            flush_decoder()

        def send_word(word):
            if char_gap_s > 0 and len(word) > 1:
                key_up()
                for j, letter in enumerate(word):
                    self.rig.send_cw(letter, wpm=wpm)
                    self.rig.wait_cw_done()
                    if debug_on:
                        self.log(f"\n  [debug TX] lettre envoyée : {letter!r}")
                    if j < len(word) - 1:
                        key_down()
                        if debug_on:
                            self.log(f"\n  [debug TX] silence réel entre lettres : {char_gap_s * 1000:.0f} ms")
                        time.sleep(char_gap_s)
                        key_up()
            else:
                key_up()
                self.rig.send_cw(word, wpm=wpm)
                self.rig.wait_cw_done()
                if debug_on:
                    self.log(f"\n  [debug TX] mot envoyé : {word!r}")

        for i, word in enumerate(words):
            send_word(word)

            if i < len(words) - 1:
                # Espace naturel entre les mots (le poste l'encode lui-même)
                self.rig.send_cw(" ", wpm=wpm)
                self.rig.wait_cw_done()

                if word_gap_s > 0:
                    key_down()
                    if debug_on:
                        self.log(f"\n  [debug TX] silence réel entre mots : {word_gap_s * 1000:.0f} ms")
                    time.sleep(word_gap_s)
                    key_up()

        self.rig.set_ptt(False)

        if self.decoder is not None:
            self.decoder.flush_input_buffer()
            self.decoder.mute_for(0.5)

    # ------------------------------------------------------------- Météo
    def _send_weather_report(self):
        """Récupère la météo actuelle (via le locator réglé) et l'émet en CW."""
        try:
            lat, lon = maidenhead_to_latlon(self.settings.get("my_locator", "JN18SO"))
            temp_c, pressure_hpa = fetch_weather(lat, lon)
        except Exception as exc:
            self.log(f"\n[météo] Impossible de récupérer la météo : {exc}")
            return

        if temp_c is None or pressure_hpa is None:
            self.log("\n[météo] Données météo indisponibles pour le moment.")
            return

        s = self.settings
        placeholders = {
            "temp": format_temp_for_cw(temp_c),
            "pressure": str(round(pressure_hpa)),
            "call": s["my_call"],
            "name": s.get("my_name", ""),
            "locator": s.get("my_locator", ""),
            "qth": s.get("my_qth", ""),
        }
        template = s.get("weather_template", DEFAULT_SETTINGS["weather_template"])
        try:
            message = template.format(**placeholders)
        except (KeyError, ValueError) as exc:
            self.log(f"\n[météo] Modèle de message invalide ({exc}) — vérifiez les {{variables}} "
                     "dans l'onglet Réglages. Utilisation du modèle par défaut pour cette fois.")
            message = DEFAULT_SETTINGS["weather_template"].format(**placeholders)

        self.log(f"\n[météo] {message}")
        self._send_cw_text(message)

    # ------------------------------------------------------------- Exercice
    def _generate_exercise_group(self):
        if self.exercise_kind == "words":
            return random.choice(EXERCISE_WORD_LIST)
        alphabet = string.digits if self.exercise_kind == "digits" else string.ascii_uppercase
        return "".join(random.choice(alphabet) for _ in range(EXERCISE_GROUP_LENGTH))

    def _start_exercise(self, kind):
        self.exercise_active = True
        self.exercise_kind = kind
        self.exercise_round = 1
        self.exercise_empty_silences = 0
        self.exercise_current_group = self._generate_exercise_group()

        label = {"digits": "chiffres", "letters": "lettres", "words": "mots"}[kind]
        self.log(f"\n[exercice] Début — groupes de {label}, "
                 f"{EXERCISE_TOTAL_ROUNDS} groupes au total.")
        self.log(f"[exercice] Groupe {self.exercise_round}/{EXERCISE_TOTAL_ROUNDS} : "
                 f"{self.exercise_current_group}")
        self._send_cw_text(self.exercise_current_group)

    def _handle_exercise_reply(self, text):
        cleaned = "".join(ch for ch in text.upper() if ch.isalnum())

        if self.exercise_current_group in cleaned:
            self.log(f"[exercice] Copié correctement : {self.exercise_current_group} -> RR")
            self._send_cw_text("RR")

            if self.exercise_round >= EXERCISE_TOTAL_ROUNDS:
                self.log("[exercice] Terminé (10 groupes copiés). Retour à l'écoute.")
                self.exercise_active = False
                self.exercise_kind = None
                self.exercise_round = 0
                self.exercise_current_group = ""
                return

            time.sleep(EXERCISE_PAUSE_BEFORE_NEXT_S)
            self.exercise_round += 1
            self.exercise_current_group = self._generate_exercise_group()
            self.log(f"[exercice] Groupe {self.exercise_round}/{EXERCISE_TOTAL_ROUNDS} : "
                     f"{self.exercise_current_group}")
            self._send_cw_text(self.exercise_current_group)
        elif EXERCISE_REPEAT_REQUEST in cleaned:
            self.log(f"[exercice] Le correspondant demande de répéter (AGN) "
                     f"-> renvoi de {self.exercise_current_group}")
            self._send_cw_text(self.exercise_current_group)
        else:
            # Pas correct (ou pas compris) : on n'émet rien et on attend soit
            # une nouvelle tentative, soit un AGN explicite pour renvoyer le
            # groupe. On ne répète plus automatiquement.
            self.log(f"[exercice] Pas encore correct — en attente "
                     f"(envoyez AGN pour un rappel du groupe).")

    def _on_char(self, c):
        self.received_buffer.append(c)
        self.log(c, newline=False)

    def _on_word_end(self):
        self.received_buffer.append(" ")
        self.log(" ", newline=False)

    def _on_debug(self, kind, duration_ms, dot_len_ms, symbol):
        if kind == "mark":
            self.log(f"\n  [debug] tonalité {duration_ms:.0f} ms "
                     f"(point de référence ~{dot_len_ms:.0f} ms) -> classée '{symbol}'")
        elif kind == "settle":
            self.log(f"\n  [debug] début de transmission détecté — "
                     f"{duration_ms:.0f} ms ignorés (anti-artefact)")
        else:
            self.log(f"\n  [debug] silence {duration_ms:.0f} ms "
                     f"(point de référence ~{dot_len_ms:.0f} ms)")

    def _on_tick(self, now):
        """
        Filet de sécurité : comme pour _on_silence, une erreur ici ne doit
        jamais interrompre l'écoute elle-même.
        """
        try:
            self._on_tick_impl(now)
        except Exception:
            self.log("\n[ERREUR] Un problème est survenu dans la vérification périodique, "
                     "mais l'écoute continue :")
            self.log(traceback.format_exc())

    def _on_tick_impl(self, now):
        """
        Rappel périodique (~1x/s), déclenché indépendamment de toute
        détection de silence — donc fiable même dans un environnement
        bruyant où le décodeur redémarre sans cesse son compte à rebours de
        silence à cause de faux déclenchements sur le bruit ambiant.
        """
        if self.qso_active or self.exercise_active:
            return

        test_requested = self._weather_test_requested.is_set()
        if test_requested:
            self._weather_test_requested.clear()

        due_for_schedule = False
        if self.settings.get("weather_enabled", False):
            try:
                interval_h = float(self.settings.get("weather_interval_h", "2"))
            except (ValueError, TypeError):
                interval_h = 2.0
            due_for_schedule = now - self._last_weather_time >= interval_h * 3600

        if test_requested or due_for_schedule:
            self._send_weather_report()
            self._last_weather_time = now

    def _on_silence(self):
        """
        Filet de sécurité : quoi qu'il arrive dans la logique QSO/exercice
        (bug, exception inattendue...), l'écoute elle-même ne doit JAMAIS
        s'interrompre. Toute erreur est journalisée mais n'empêche pas de
        continuer à écouter la suite.
        """
        try:
            self._on_silence_impl()
        except Exception:
            self.log("\n[ERREUR] Un problème est survenu en traitant la dernière "
                     "réception, mais l'écoute continue :")
            self.log(traceback.format_exc())

    def _on_silence_impl(self):
        text = "".join(self.received_buffer).strip()
        self.received_buffer.clear()

        # --- Exercice de copie (prioritaire, indépendant du mode QSO) ---
        if self.exercise_active:
            if not text:
                self.exercise_empty_silences += 1
                if self.exercise_empty_silences >= 3:
                    self.log("\n[exercice] Pas de réponse, on abandonne "
                             "et on repasse en écoute.")
                    self.exercise_active = False
                    self.exercise_kind = None
                    self.exercise_round = 0
                    self.exercise_current_group = ""
                    self.exercise_empty_silences = 0
                return
            self.log(f"\n[reçu] {text}")
            self.exercise_empty_silences = 0
            self._handle_exercise_reply(text)
            return

        if text:
            # On retire les espaces avant de comparer : le décodeur peut
            # parfois insérer un espace parasite en plein milieu d'un mot
            # déclencheur (ex: "EX C" au lieu de "EXC").
            upper_text = text.upper().replace(" ", "")
            if EXERCISE_TRIGGER_DIGITS in upper_text:
                self.log(f"\n[reçu] {text}")
                self._start_exercise("digits")
                return
            if EXERCISE_TRIGGER_LETTERS in upper_text:
                self.log(f"\n[reçu] {text}")
                self._start_exercise("letters")
                return
            if EXERCISE_TRIGGER_WORDS in upper_text:
                self.log(f"\n[reçu] {text}")
                self._start_exercise("words")
                return

        if not self.settings.get("qso_mode", True):
            # Mode simple : un seul rapport envoyé, comme avant.
            if not text:
                return
            self.log(f"\n[reçu] {text}")
            if self.settings["my_call"].strip().upper() in text.upper().replace(" ", ""):
                smeter = self.rig.get_smeter()
                rst = self.decoder.estimate_rst(smeter)
                reply = self._build_simple_reply(rst)
                self.log(f"[réponse] {reply}")
                self._send_cw_text(reply)
            return

        # --- Mode QSO scripté ---
        if not text:
            if self.qso_active:
                self.qso_empty_silences += 1
                if self.qso_empty_silences >= 3:
                    self.log("\n[QSO] Pas de réponse du correspondant, on abandonne "
                             "et on repasse en écoute.")
                    self.qso_active = False
                    self.qso_step = 0
                    self.qso_empty_silences = 0
            return

        self.log(f"\n[reçu] {text}")
        self.qso_empty_silences = 0

        if not self.qso_active:
            # Pas de QSO en cours : on ne démarre que si notre indicatif est
            # reconnu dans le texte décodé (quelqu'un nous appelle). On
            # retire les espaces avant de comparer : le décodeur peut parfois
            # insérer un espace parasite en plein milieu de l'indicatif (ex:
            # "F4 GOP" au lieu de "F4GOP"), ce qui ne doit pas empêcher la
            # reconnaissance.
            if self.settings["my_call"].strip().upper() in text.upper().replace(" ", ""):
                self.qso_active = True
                self.qso_step = 1
                self._send_qso_step()
            return

        # QSO en cours : chaque transmission reçue du correspondant est prise
        # comme la fin de son tour de parole, et on enchaîne sur l'étape
        # suivante du script — sans chercher à comprendre ce qu'il a dit.
        self.qso_step += 1
        if self.qso_step > len(QSO_STEPS):
            self.qso_active = False
            self.qso_step = 0
            self.log("\n[QSO] Terminé, retour à l'écoute.")
            return

        self._send_qso_step()

    def _run(self):
        s = self.settings

        if not s["com_port"]:
            self.log("\nERREUR : aucun port COM renseigné. Vérifiez l'onglet Réglages.")
            return

        try:
            baud = int(s["baud"])
        except (ValueError, TypeError):
            baud = 115200

        self.log(f"Connexion au TS-990S sur {s['com_port']} ({baud} bauds)...")
        self.rig = RigCTL(port=s["com_port"], baud=baud)
        try:
            self.rig.connect()
        except Exception as exc:
            self.log(f"\nERREUR de connexion série : {exc}")
            self.log("Vérifiez le port COM, le baud rate, et qu'aucun autre logiciel "
                      "(WSJT-X, un logger...) n'utilise déjà ce port.")
            self.rig = None
            return

        freq = self.rig.get_freq()
        if freq is None:
            self.log("Port ouvert mais le poste ne répond pas aux commandes CAT.")
            self.log("Vérifiez le baud rate réglé sur le poste (menu 7-00/7-01 du TS-990S).")
            self.stop()
            return

        self.log(f"Connecté — fréquence lue : {freq} Hz")

        audio_device = None
        if s["audio_device"]:
            try:
                audio_device = int(s["audio_device"])
            except ValueError:
                audio_device = None

        try:
            gain = float(s.get("rx_gain", "1.0"))
        except (ValueError, TypeError):
            gain = 1.0

        mark_mult, space_mult = SENSITIVITY_LEVELS.get(s.get("sensitivity", "normal"),
                                                        SENSITIVITY_LEVELS["normal"])

        qrm_reduction = bool(s.get("qrm_reduction", False))
        smoothing_alpha = 0.25 if qrm_reduction else 0.0
        debounce_hops = 2 if qrm_reduction else 1

        freq_tolerance = bool(s.get("freq_tolerance", False))
        tone_tolerance_hz = 150 if freq_tolerance else 0
        tone_freq_steps = 7 if freq_tolerance else 1

        self.decoder = CWDecoder(
            tone_freq=float(s["tone_freq"]),
            audio_device=audio_device,
            on_char=self._on_char,
            on_word_end=self._on_word_end,
            on_silence=self._on_silence,
            on_debug=self._on_debug if s.get("debug_mode") else None,
            gain=gain,
            auto_gain=bool(s.get("auto_gain", False)),
            mark_threshold_mult=mark_mult,
            space_threshold_mult=space_mult,
            amplitude_smoothing_alpha=smoothing_alpha,
            debounce_hops=debounce_hops,
            tone_tolerance_hz=tone_tolerance_hz,
            tone_freq_steps=tone_freq_steps,
            on_tick=self._on_tick,
        )

        self.log(f"Répéteur actif — indicatif {s['my_call']}, locator {s['my_locator']}")
        self.log("En écoute...\n")

        try:
            self.decoder.run_forever(stop_event=self.stop_event)
        except Exception:
            self.log("\nERREUR inattendue pendant l'écoute — le répéteur vient de s'arrêter :")
            self.log(traceback.format_exc())
        finally:
            self.stop()
            self.log("\nRépéteur arrêté.")


class App(tk.Tk):
    # Palette de couleurs de l'application
    COLOR_BG = "#eef1f5"
    COLOR_ACCENT = "#2c5f8a"
    COLOR_ACCENT_DARK = "#1d4666"
    COLOR_START = "#2e8b57"
    COLOR_START_HOVER = "#256f45"
    COLOR_STOP = "#b5462f"
    COLOR_STOP_HOVER = "#93361f"
    COLOR_LOG_BG = "#0f1a12"
    COLOR_LOG_FG = "#7fe0a0"
    COLOR_STATUS_IDLE = "#6b7280"
    COLOR_STATUS_RUNNING = "#2e8b57"
    COLOR_STATUS_ERROR = "#b5462f"

    def __init__(self):
        super().__init__()
        self.title("Répéteur CW HF — F4GOP")
        self.geometry("760x620")
        self.minsize(680, 560)

        self.settings = load_settings()
        self.COLOR_BG = self.settings.get("theme_bg_color", self.COLOR_BG)
        self.configure(bg=self.COLOR_BG)
        self._setup_style()

        self.log_queue = queue.Queue()
        self.engine = RepeaterEngine(self.settings, self._queue_log)

        header = ttk.Frame(self, style="Header.TFrame")
        header.pack(fill="x")
        ttk.Label(
            header, text="📡 Répéteur CW HF — F4GOP",
            style="HeaderTitle.TLabel",
        ).pack(side="left", padx=16, pady=12)

        notebook = ttk.Notebook(self)
        notebook.pack(fill="both", expand=True, padx=10, pady=(6, 10))

        self.settings_tab = ttk.Frame(notebook, style="TFrame")
        self.repeater_tab = ttk.Frame(notebook, style="TFrame")
        notebook.add(self.settings_tab, text="  Réglages  ")
        notebook.add(self.repeater_tab, text="  Répéteur  ")

        self._build_settings_tab()
        self._build_repeater_tab()

        self.after(100, self._poll_log_queue)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _setup_style(self):
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        style.configure("TFrame", background=self.COLOR_BG)
        style.configure("TLabelframe", background=self.COLOR_BG, borderwidth=1)
        style.configure("TLabel", background=self.COLOR_BG, font=("Segoe UI", 9))
        style.configure("TCheckbutton", background=self.COLOR_BG, font=("Segoe UI", 9))
        style.configure("TNotebook", background=self.COLOR_BG, borderwidth=0)
        style.configure("TNotebook.Tab", font=("Segoe UI", 9, "bold"), padding=(14, 6))

        style.configure("TButton", font=("Segoe UI", 9), padding=6)

        style.configure("Start.TButton", font=("Segoe UI", 10, "bold"),
                         padding=8, foreground="white", background=self.COLOR_START)
        style.map("Start.TButton",
                  background=[("active", self.COLOR_START_HOVER), ("disabled", "#9aa5a1")])

        style.configure("Stop.TButton", font=("Segoe UI", 10, "bold"),
                         padding=8, foreground="white", background=self.COLOR_STOP)
        style.map("Stop.TButton",
                  background=[("active", self.COLOR_STOP_HOVER), ("disabled", "#c9aaa0")])

        style.configure("Status.TLabel", font=("Segoe UI", 10, "bold"),
                         foreground=self.COLOR_STATUS_IDLE)

        accent = self.settings.get("theme_color", self.COLOR_ACCENT_DARK)
        self._apply_theme_colors(accent)

    def _apply_theme_colors(self, accent_color: str):
        """
        Applique la couleur d'accent (bandeau + titres de section) partout où
        elle est utilisée. Comme les widgets ttk référencent ces styles par
        nom, les reconfigurer ici met à jour tous les widgets déjà affichés,
        sans avoir besoin de reconstruire l'interface.
        """
        style = ttk.Style(self)
        style.configure("TLabelframe.Label", background=self.COLOR_BG,
                         foreground=accent_color, font=("Segoe UI", 10, "bold"))
        style.configure("Header.TFrame", background=accent_color)
        style.configure("HeaderTitle.TLabel", background=accent_color,
                         foreground="white", font=("Segoe UI", 13, "bold"))

    def _apply_bg_color(self, bg_color: str):
        """Applique la couleur de fond partout où elle est utilisée (widgets déjà affichés inclus)."""
        self.COLOR_BG = bg_color
        self.configure(bg=bg_color)

        style = ttk.Style(self)
        style.configure("TFrame", background=bg_color)
        style.configure("TLabelframe", background=bg_color, borderwidth=1)
        style.configure("TLabelframe.Label", background=bg_color)
        style.configure("TLabel", background=bg_color)
        style.configure("TCheckbutton", background=bg_color)
        style.configure("TNotebook", background=bg_color)

        if hasattr(self, "_settings_canvas"):
            self._settings_canvas.configure(bg=bg_color)

    # ---------------------------------------------------------------- Réglages
    def _build_settings_tab(self):
        outer = self.settings_tab

        # Zone défilante : au cas où la fenêtre soit redimensionnée petite
        canvas = tk.Canvas(outer, bg=self.COLOR_BG, highlightthickness=0)
        self._settings_canvas = canvas
        scrollbar = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        frame = ttk.Frame(canvas, style="TFrame")
        frame.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=frame, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        def _on_mousewheel(event):
            canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

        canvas.bind_all("<MouseWheel>", _on_mousewheel)

        pad = {"padx": 8, "pady": 5}

        self.vars = {
            "my_call": tk.StringVar(value=self.settings["my_call"]),
            "my_locator": tk.StringVar(value=self.settings["my_locator"]),
            "my_qth": tk.StringVar(value=self.settings["my_qth"]),
            "my_name": tk.StringVar(value=self.settings.get("my_name", "OP")),
            "my_rig": tk.StringVar(value=self.settings.get("my_rig", "TS-990S")),
            "my_power": tk.StringVar(value=self.settings.get("my_power", "100")),
            "my_antenna": tk.StringVar(value=self.settings.get("my_antenna", "DIPOLE")),
            "com_port": tk.StringVar(value=self.settings["com_port"]),
            "baud": tk.StringVar(value=self.settings["baud"]),
            "audio_device": tk.StringVar(value=self.settings["audio_device"]),
            "rx_gain": tk.DoubleVar(value=float(self.settings.get("rx_gain", "1.0"))),
            "sensitivity": tk.StringVar(
                value=self.settings.get("sensitivity", "normal")
                if self.settings.get("sensitivity") in SENSITIVITY_LEVELS else "normal"),
            "char_spacing_extra": tk.DoubleVar(
                value=float(self.settings.get("char_spacing_extra", "0"))),
            "word_gap_ms": tk.DoubleVar(
                value=float(self.settings.get("word_gap_ms", "0"))),
            "auto_gain": tk.BooleanVar(value=self.settings.get("auto_gain", False)),
            "tone_freq": tk.StringVar(value=self.settings["tone_freq"]),
            "cw_speed": tk.StringVar(value=self.settings["cw_speed"]),
            "debug_mode": tk.BooleanVar(value=self.settings.get("debug_mode", False)),
            "qso_mode": tk.BooleanVar(value=self.settings.get("qso_mode", True)),
            "qrm_reduction": tk.BooleanVar(value=self.settings.get("qrm_reduction", False)),
            "freq_tolerance": tk.BooleanVar(value=self.settings.get("freq_tolerance", False)),
            "weather_enabled": tk.BooleanVar(value=self.settings.get("weather_enabled", False)),
            "weather_interval_h": tk.StringVar(value=self.settings.get("weather_interval_h", "2")),
            "weather_template": tk.StringVar(
                value=self.settings.get("weather_template", DEFAULT_SETTINGS["weather_template"])),
        }

        def add_entry(parent, r, label, key, width=25):
            ttk.Label(parent, text=label).grid(row=r, column=0, sticky="w", **pad)
            ttk.Entry(parent, textvariable=self.vars[key], width=width).grid(
                row=r, column=1, sticky="w", **pad)

        # --- Apparence ---
        appearance = ttk.Labelframe(frame, text="  Apparence  ", padding=10)
        appearance.pack(fill="x", padx=12, pady=(12, 8))
        ttk.Label(appearance, text="Couleur du bandeau et des titres :").grid(
            row=0, column=0, sticky="w", **pad)
        self.color_swatch = tk.Label(
            appearance, text="   ", background=self.settings.get("theme_color", self.COLOR_ACCENT_DARK),
            relief="solid", borderwidth=1, width=4)
        self.color_swatch.grid(row=0, column=1, sticky="w", padx=8, pady=4)
        ttk.Button(appearance, text="Choisir la couleur...", command=self._on_choose_color).grid(
            row=0, column=2, sticky="w", **pad)

        ttk.Label(appearance, text="Couleur de fond :").grid(
            row=1, column=0, sticky="w", **pad)
        self.bg_color_swatch = tk.Label(
            appearance, text="   ", background=self.settings.get("theme_bg_color", self.COLOR_BG),
            relief="solid", borderwidth=1, width=4)
        self.bg_color_swatch.grid(row=1, column=1, sticky="w", padx=8, pady=4)
        ttk.Button(appearance, text="Choisir la couleur...", command=self._on_choose_bg_color).grid(
            row=1, column=2, sticky="w", **pad)

        # --- Identité de la station ---
        identity = ttk.Labelframe(frame, text="  Identité de la station  ", padding=10)
        identity.pack(fill="x", padx=12, pady=(12, 8))
        for i, (label, key) in enumerate([
            ("Indicatif :", "my_call"),
            ("Locator (Maidenhead) :", "my_locator"),
            ("QTH (texte libre) :", "my_qth"),
            ("Prénom (envoyé dans le QSO) :", "my_name"),
            ("Poste (RIG) :", "my_rig"),
            ("Puissance (W) :", "my_power"),
            ("Antenne :", "my_antenna"),
        ]):
            add_entry(identity, i, label, key)

        # --- Liaison CAT ---
        cat = ttk.Labelframe(frame, text="  Liaison CAT (TS-990S) — connexion série directe  ",
                             padding=10)
        cat.pack(fill="x", padx=12, pady=8)

        ttk.Label(cat, text="Port COM :").grid(row=0, column=0, sticky="w", **pad)
        self.com_combo = ttk.Combobox(cat, textvariable=self.vars["com_port"], width=30)
        self.com_combo.grid(row=0, column=1, sticky="w", **pad)
        ttk.Button(cat, text="Actualiser", command=self._refresh_com_ports).grid(
            row=0, column=2, sticky="w", **pad)

        ttk.Label(cat, text="Baud rate :").grid(row=1, column=0, sticky="w", **pad)
        baud_combo = ttk.Combobox(cat, textvariable=self.vars["baud"], width=12,
                                   values=["4800", "9600", "19200", "38400", "57600", "115200"])
        baud_combo.grid(row=1, column=1, sticky="w", **pad)

        ttk.Label(
            cat, text="(doit correspondre au réglage du menu 7-00 / 7-01 du TS-990S)",
            foreground="#6b7280", font=("Segoe UI", 8, "italic"),
        ).grid(row=2, column=0, columnspan=3, sticky="w", padx=8)

        # --- Audio et CW ---
        audio = ttk.Labelframe(frame, text="  Audio et CW  ", padding=10)
        audio.pack(fill="x", padx=12, pady=8)

        ttk.Label(audio, text="Périphérique audio d'entrée :").grid(
            row=0, column=0, sticky="w", **pad)
        self.audio_combo = ttk.Combobox(audio, textvariable=self.vars["audio_device"], width=38)
        self.audio_combo.grid(row=0, column=1, sticky="w", **pad)
        ttk.Button(audio, text="Actualiser", command=self._refresh_audio_devices).grid(
            row=0, column=2, sticky="w", **pad)

        ttk.Label(audio, text="Volume réception (gain) :").grid(
            row=1, column=0, sticky="w", **pad)
        gain_row = ttk.Frame(audio, style="TFrame")
        gain_row.grid(row=1, column=1, columnspan=2, sticky="w")
        gain_scale = ttk.Scale(
            gain_row, from_=0.1, to=5.0, orient="horizontal", length=200,
            variable=self.vars["rx_gain"], command=self._on_gain_change,
        )
        gain_scale.pack(side="left", padx=(8, 8))
        self.gain_label = ttk.Label(gain_row, text=f"{self.vars['rx_gain'].get():.1f}x", width=6)
        self.gain_label.pack(side="left")
        self.gain_scale = gain_scale

        ttk.Checkbutton(
            audio, text="Gain automatique (AGC) — le curseur ci-dessus devient indicatif",
            variable=self.vars["auto_gain"], command=self._on_auto_gain_toggle,
        ).grid(row=2, column=0, columnspan=3, sticky="w", **pad)

        ttk.Label(audio, text="Sensibilité de détection :").grid(
            row=3, column=0, sticky="w", **pad)
        # Variable séparée pour le TEXTE AFFICHÉ dans la liste déroulante :
        # self.vars["sensitivity"] doit rester la clé interne ("normal",
        # "sensible", ...), pas le libellé affiché — les mélanger via la
        # même variable corromprait la clé stockée dans les réglages.
        self._sensitivity_display_var = tk.StringVar(
            value=SENSITIVITY_LABELS.get(self.vars["sensitivity"].get(), SENSITIVITY_LABELS["normal"]))
        sensitivity_combo = ttk.Combobox(
            audio, textvariable=self._sensitivity_display_var, width=45, state="readonly",
            values=list(SENSITIVITY_LABELS.values()),
        )
        sensitivity_combo.grid(row=3, column=1, columnspan=2, sticky="w", **pad)
        sensitivity_combo.bind("<<ComboboxSelected>>", self._on_sensitivity_change)
        self._sensitivity_combo = sensitivity_combo

        add_entry(audio, 4, "Fréquence du ton CW (Hz) :", "tone_freq")
        add_entry(audio, 5, "Vitesse CW (WPM) :", "cw_speed")

        ttk.Label(audio, text="Espaces suppl. entre les lettres (émission) :").grid(
            row=6, column=0, sticky="w", **pad)
        spacing_row = ttk.Frame(audio, style="TFrame")
        spacing_row.grid(row=6, column=1, columnspan=2, sticky="w")
        spacing_scale = ttk.Scale(
            spacing_row, from_=0, to=5, orient="horizontal", length=200,
            variable=self.vars["char_spacing_extra"], command=self._on_char_spacing_change,
        )
        spacing_scale.pack(side="left", padx=(8, 8))
        self.spacing_label = ttk.Label(
            spacing_row, text=f"{int(self.vars['char_spacing_extra'].get())}", width=8)
        self.spacing_label.pack(side="left")

        ttk.Label(audio, text="Silence suppl. entre les mots (PTT coupé) :").grid(
            row=7, column=0, sticky="w", **pad)
        word_spacing_row = ttk.Frame(audio, style="TFrame")
        word_spacing_row.grid(row=7, column=1, columnspan=2, sticky="w")
        word_spacing_scale = ttk.Scale(
            word_spacing_row, from_=0, to=2000, orient="horizontal", length=200,
            variable=self.vars["word_gap_ms"], command=self._on_word_spacing_change,
        )
        word_spacing_scale.pack(side="left", padx=(8, 8))
        self.word_spacing_label = ttk.Label(
            word_spacing_row, text=f"{int(self.vars['word_gap_ms'].get())} ms", width=8)
        self.word_spacing_label.pack(side="left")

        test_row = ttk.Frame(audio, style="TFrame")
        test_row.grid(row=8, column=0, columnspan=3, sticky="w", pady=(4, 0))
        self.test_audio_btn = ttk.Button(test_row, text="🔊 Tester l'audio (3 s)",
                                          command=self._on_test_audio)
        self.test_audio_btn.pack(side="left", padx=8)
        self.test_audio_result = ttk.Label(test_row, text="")
        self.test_audio_result.pack(side="left", padx=8)

        # --- Options ---
        options = ttk.Labelframe(frame, text="  Options  ", padding=10)
        options.pack(fill="x", padx=12, pady=8)

        ttk.Checkbutton(
            options, text="Mode diagnostic (afficher les durées mesurées point/trait/espace)",
            variable=self.vars["debug_mode"],
        ).pack(anchor="w", pady=2)

        ttk.Checkbutton(
            options, text="QSO scripté en 3 échanges (sinon, un simple rapport RST est envoyé)",
            variable=self.vars["qso_mode"],
        ).pack(anchor="w", pady=2)

        ttk.Checkbutton(
            options,
            text="Réduction du bruit impulsif / QRM (réduit aussi la portée sur signal faible)",
            variable=self.vars["qrm_reduction"], command=self._on_qrm_reduction_toggle,
        ).pack(anchor="w", pady=2)

        ttk.Checkbutton(
            options,
            text="Tolérance de fréquence élargie (ton reçu différent du réglage, mais réduit aussi la portée sur signal faible)",
            variable=self.vars["freq_tolerance"], command=self._on_freq_tolerance_toggle,
        ).pack(anchor="w", pady=2)

        weather_row = ttk.Frame(options, style="TFrame")
        weather_row.pack(anchor="w", fill="x", pady=2)
        ttk.Checkbutton(
            weather_row, text="Diffuser automatiquement la météo (température/pression) toutes les",
            variable=self.vars["weather_enabled"], command=self._on_weather_enabled_toggle,
        ).pack(side="left")
        ttk.Entry(weather_row, textvariable=self.vars["weather_interval_h"], width=4).pack(
            side="left", padx=4)
        ttk.Label(weather_row, text="heures").pack(side="left", padx=(0, 8))
        ttk.Button(weather_row, text="Tester maintenant", command=self._on_test_weather).pack(
            side="left", padx=8)

        weather_template_row = ttk.Frame(options, style="TFrame")
        weather_template_row.pack(anchor="w", fill="x", pady=(2, 2))
        ttk.Label(weather_template_row, text="Message météo :").pack(side="left")
        ttk.Entry(weather_template_row, textvariable=self.vars["weather_template"], width=55).pack(
            side="left", padx=8)

        ttk.Label(
            options,
            text="Variables disponibles : {temp} {pressure} {call} {name} {locator} {qth}",
            foreground="#6b7280", font=("Segoe UI", 8, "italic"),
        ).pack(anchor="w", padx=(0, 0))

        ttk.Button(frame, text="💾  Enregistrer les réglages",
                   command=self._save_settings).pack(anchor="w", padx=12, pady=(4, 16))

        self._refresh_com_ports()
        self._refresh_audio_devices()
        if self.vars["auto_gain"].get():
            self.gain_scale.config(state="disabled")

    def _on_choose_color(self):
        current = self.settings.get("theme_color", self.COLOR_ACCENT_DARK)
        result = colorchooser.askcolor(color=current, title="Choisir la couleur du thème")
        color_hex = result[1]  # (rgb_tuple, "#rrggbb") ou (None, None) si annulé
        if not color_hex:
            return
        self.settings["theme_color"] = color_hex
        self.color_swatch.config(background=color_hex)
        self._apply_theme_colors(color_hex)

    def _on_choose_bg_color(self):
        current = self.settings.get("theme_bg_color", self.COLOR_BG)
        result = colorchooser.askcolor(color=current, title="Choisir la couleur de fond")
        color_hex = result[1]
        if not color_hex:
            return
        self.settings["theme_bg_color"] = color_hex
        self.bg_color_swatch.config(background=color_hex)
        self._apply_bg_color(color_hex)

    def _on_test_audio(self):
        """
        Échantillonne 3 secondes d'audio avec le périphérique et la
        fréquence de ton actuellement réglés, pour vérifier AVANT de
        démarrer le répéteur que le bon micro/entrée est choisi et que le
        niveau est correct. Faites entendre un CW au poste pendant le test
        pour un diagnostic utile.
        """
        audio_value = self.vars["audio_device"].get()
        if ":" in audio_value:
            audio_value = audio_value.split(":")[0].strip()
        audio_device = int(audio_value) if audio_value else None

        try:
            tone_freq = float(self.vars["tone_freq"].get())
        except ValueError:
            tone_freq = 700.0

        gain = self.vars["rx_gain"].get()

        self.test_audio_btn.config(state="disabled")
        self.test_audio_result.config(text="Test en cours (3 s)... faites entendre un CW au poste.")

        def worker():
            try:
                decoder = CWDecoder(tone_freq=tone_freq, audio_device=audio_device, gain=gain)
                levels = decoder.measure_audio(duration=3.0)
                self.after(0, lambda: self._show_audio_test_result(levels, error=None))
            except Exception as exc:
                self.after(0, lambda: self._show_audio_test_result(None, error=exc))

        threading.Thread(target=worker, daemon=True).start()

    def _show_audio_test_result(self, levels, error):
        self.test_audio_btn.config(state="normal")
        if error is not None:
            self.test_audio_result.config(text=f"Erreur : {error}")
            return

        peak_raw = levels["peak_raw"]
        peak_tone = levels["peak_tone"]
        if peak_raw < 0.005:
            verdict = "Niveau quasi nul — mauvais périphérique choisi, ou volume coupé."
        elif peak_tone < peak_raw * 0.3:
            verdict = ("Du son arrive, mais peu à la fréquence de ton réglée — "
                       "vérifiez qu'elle correspond au ton entendu au casque.")
        else:
            verdict = "Niveau correct et cohérent avec la fréquence de ton réglée."

        self.test_audio_result.config(
            text=f"Brut max: {peak_raw:.3f}  |  Au ton réglé: {peak_tone:.3f}  —  {verdict}")

    def _refresh_com_ports(self):
        if list_ports is None:
            self.com_combo["values"] = []
            return
        ports = list(list_ports.comports())
        values = [p.device for p in ports]
        descriptions = {p.device: p.description for p in ports}
        self.com_combo["values"] = [f"{d} — {descriptions[d]}" for d in values] or values

    def _refresh_audio_devices(self):
        if sd is None:
            self.audio_combo["values"] = []
            return
        devices = sd.query_devices()
        values = [f"{i}: {d['name']} (in={d['max_input_channels']})" for i, d in enumerate(devices)]
        self.audio_combo["values"] = values

    def _on_gain_change(self, value):
        gain = float(value)
        self.gain_label.config(text=f"{gain:.1f}x")
        self.settings["rx_gain"] = str(round(gain, 2))
        # Applique immédiatement si le répéteur est déjà en train d'écouter,
        # sans avoir besoin de redémarrer.
        self.engine.set_gain(gain)

    def _on_auto_gain_toggle(self):
        enabled = self.vars["auto_gain"].get()
        self.settings["auto_gain"] = enabled
        self.gain_scale.config(state="disabled" if enabled else "normal")
        self.engine.set_auto_gain(enabled)

    def _on_sensitivity_change(self, event=None):
        label = self._sensitivity_combo.get()
        key = next((k for k, v in SENSITIVITY_LABELS.items() if v == label), "normal")
        self.vars["sensitivity"].set(key)
        self.settings["sensitivity"] = key
        self.engine.set_sensitivity(key)

    def _on_qrm_reduction_toggle(self):
        enabled = self.vars["qrm_reduction"].get()
        self.settings["qrm_reduction"] = enabled
        self.engine.set_qrm_reduction(enabled)

    def _on_freq_tolerance_toggle(self):
        enabled = self.vars["freq_tolerance"].get()
        self.settings["freq_tolerance"] = enabled
        self.engine.set_freq_tolerance(enabled)

    def _on_weather_enabled_toggle(self):
        self.settings["weather_enabled"] = self.vars["weather_enabled"].get()
        self.settings["weather_interval_h"] = self.vars["weather_interval_h"].get()

    def _on_test_weather(self):
        if not self.engine.is_running():
            messagebox.showwarning(
                "Répéteur arrêté",
                "Démarrez d'abord le répéteur (onglet Répéteur) pour tester la météo "
                "— la connexion au poste est nécessaire pour émettre.")
            return
        self.settings["weather_interval_h"] = self.vars["weather_interval_h"].get()
        self.settings["weather_template"] = self.vars["weather_template"].get()
        # On ne lance plus l'émission depuis un thread séparé (ça pouvait faire
        # se chevaucher deux accès simultanés au port série avec le thread
        # principal du répéteur, corrompant parfois le message envoyé). On
        # pose juste une demande ; le thread unique du répéteur l'exécutera
        # à son prochain passage (au plus tard au bout de la durée de
        # silence réglée pour le décodeur, quelques secondes typiquement).
        self.engine.request_weather_test()
        self._queue_log("\n[météo] Test demandé — émission dans quelques secondes...")

    def _on_char_spacing_change(self, value):
        n = int(float(value))
        self.spacing_label.config(text=f"{n}")
        self.settings["char_spacing_extra"] = str(n)

    def _on_word_spacing_change(self, value):
        ms = int(float(value))
        self.word_spacing_label.config(text=f"{ms} ms")
        self.settings["word_gap_ms"] = str(ms)

    def _save_settings(self):
        com_value = self.vars["com_port"].get()
        if "—" in com_value:
            com_value = com_value.split("—")[0].strip()

        audio_value = self.vars["audio_device"].get()
        if ":" in audio_value:
            audio_value = audio_value.split(":")[0].strip()

        for key, var in self.vars.items():
            self.settings[key] = var.get()
        self.settings["com_port"] = com_value
        self.settings["audio_device"] = audio_value
        self.settings["my_call"] = self.settings["my_call"].strip().upper()
        self.vars["my_call"].set(self.settings["my_call"])

        save_settings(self.settings)
        messagebox.showinfo("Réglages", "Réglages enregistrés.")

    # ---------------------------------------------------------------- Répéteur
    def _build_repeater_tab(self):
        frame = self.repeater_tab

        btn_frame = ttk.Frame(frame, style="TFrame")
        btn_frame.pack(fill="x", padx=12, pady=12)

        self.start_btn = ttk.Button(btn_frame, text="▶  Démarrer", command=self._on_start,
                                     style="Start.TButton")
        self.start_btn.pack(side="left", padx=(0, 6))

        self.stop_btn = ttk.Button(btn_frame, text="■  Arrêter", command=self._on_stop,
                                    state="disabled", style="Stop.TButton")
        self.stop_btn.pack(side="left", padx=6)

        self.status_label = ttk.Label(btn_frame, text="●  Arrêté", style="Status.TLabel")
        self.status_label.configure(foreground=self.COLOR_STATUS_IDLE)
        self.status_label.pack(side="left", padx=16)

        log_container = ttk.Frame(frame, style="TFrame")
        log_container.pack(fill="both", expand=True, padx=12, pady=(0, 12))

        self.log_widget = scrolledtext.ScrolledText(
            log_container, state="disabled", wrap="word",
            bg=self.COLOR_LOG_BG, fg=self.COLOR_LOG_FG,
            insertbackground=self.COLOR_LOG_FG,
            font=("Consolas", 10), borderwidth=0, highlightthickness=1,
            highlightbackground="#33443a", highlightcolor="#33443a",
            padx=10, pady=8,
        )
        self.log_widget.pack(fill="both", expand=True)

    def _on_start(self):
        self._save_settings()

        if not self.settings["com_port"]:
            messagebox.showwarning("Réglages incomplets",
                                    "Choisissez d'abord un port COM dans l'onglet Réglages.")
            return

        self.engine.settings = self.settings
        self.engine.start()
        self.start_btn.config(state="disabled")
        self.stop_btn.config(state="normal")
        self.status_label.config(text="●  En fonctionnement", foreground=self.COLOR_STATUS_RUNNING)

    def _on_stop(self):
        self.engine.stop()
        self.start_btn.config(state="normal")
        self.stop_btn.config(state="disabled")
        self.status_label.config(text="●  Arrêté", foreground=self.COLOR_STATUS_IDLE)

    def _queue_log(self, message, newline=True):
        self.log_queue.put(message + ("\n" if newline else ""))

    def _poll_log_queue(self):
        while not self.log_queue.empty():
            msg = self.log_queue.get_nowait()
            self.log_widget.configure(state="normal")
            self.log_widget.insert("end", msg)
            self.log_widget.see("end")
            self.log_widget.configure(state="disabled")

        # Si le moteur s'est arrêté tout seul (erreur inattendue) alors que
        # l'interface pensait encore être "en fonctionnement", on
        # resynchronise les boutons plutôt que de laisser l'affichage
        # bloqué sur un état qui ne correspond plus à la réalité.
        if self.stop_btn["state"] == "normal" and not self.engine.is_running():
            self.start_btn.config(state="normal")
            self.stop_btn.config(state="disabled")
            self.status_label.config(text="●  Arrêté (erreur)", foreground=self.COLOR_STATUS_ERROR)

        self.after(100, self._poll_log_queue)

    def _on_close(self):
        if self.engine.is_running():
            self.engine.stop()
        self.destroy()


if __name__ == "__main__":
    App().mainloop()
