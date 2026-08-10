"""
rig_control.py
--------------
Contrôle DIRECT du Kenwood TS-990S en CAT, via le port série (protocole
ASCII Kenwood), sans passer par un logiciel intermédiaire (ni Hamlib, ni
OmniRig). Cette application ouvre elle-même le port COM du poste.

Prérequis : uniquement le pilote USB Kenwood du TS-990S (le poste doit
apparaître comme port COM dans le Gestionnaire de périphériques Windows).
"""

import time
import serial

KY_CHUNK_LEN = 24  # la commande KY Kenwood transmet des blocs fixes de 24 caractères
PROSIGN_MAP = {
    "[BT]": "[", "[AR]": "_", "[AS]": "<", "[HH]": "#",
    "[SK]": ">", "[KN]": "]", "[BK]": "\\", "[SN]": "%",
}


class RigCTL:
    def __init__(self, port: str, baud: int = 115200, timeout: float = 1.0):
        self.port = port
        self.baud = baud
        self.timeout = timeout
        self.ser = None

    def connect(self):
        self.ser = serial.Serial(self.port, self.baud, timeout=self.timeout)
        try:
            self.ser.reset_input_buffer()
        except Exception:
            pass

    def close(self):
        if self.ser:
            try:
                self.ser.close()
            except Exception:
                pass
            self.ser = None

    def _send(self, cmd: str, expect_reply: bool = True, timeout: float = None) -> str:
        if not self.ser:
            raise ConnectionError("Port série non ouvert.")

        try:
            self.ser.reset_input_buffer()
        except Exception:
            pass

        self.ser.write(cmd.encode("ascii"))

        if not expect_reply:
            return ""

        old_timeout = self.ser.timeout
        if timeout is not None:
            self.ser.timeout = timeout
        try:
            raw = self.ser.read_until(b";")
        finally:
            self.ser.timeout = old_timeout

        return raw.decode("ascii", errors="replace")

    def get_freq(self):
        reply = self._send("FA;")
        digits = "".join(ch for ch in reply if ch.isdigit())
        return int(digits) if digits else None

    def get_mode(self):
        reply = self._send("MD;")
        return reply.strip() or None

    def set_ptt(self, on: bool):
        self._send("TX;" if on else "RX;", expect_reply=False)

    def get_smeter(self):
        """
        Lit le S-mètre réel du poste (canal principal) via la commande Kenwood
        'SM0;'. Réponse typique : 'SM0xxx;' où xxx est une valeur brute 0-30
        sur l'échelle Kenwood (0 = S0, 20 = S9, 30 = S9+60dB) — c'est la
        même échelle que celle affichée sur l'écran du poste.
        Retourne cette valeur brute (0-30), ou None si le poste ne répond pas.
        """
        try:
            reply = self._send("SM0;", timeout=0.5)
        except Exception:
            return None
        digits = "".join(ch for ch in reply if ch.isdigit())
        if not digits:
            return None
        try:
            return int(digits[-3:])
        except ValueError:
            return None

    @staticmethod
    def _apply_prosigns(text: str) -> str:
        for tag, char in PROSIGN_MAP.items():
            text = text.replace(tag, char)
        return text

    @staticmethod
    def _pad_chunk(chunk: str) -> str:
        chunk = chunk[:KY_CHUNK_LEN]
        return chunk.ljust(KY_CHUNK_LEN, " ")

    def send_cw(self, text: str, wpm: int = 20, poll_interval: float = 0.05, max_wait: float = 2.0,
                on_chunk_sent=None):
        """
        Envoie du texte en CW via la commande native Kenwood 'KY'. Contrairement
        à une liaison passant par OmniRig, l'accès série direct permet ici de
        vraiment interroger 'KY;' et attendre la réponse 'KY0;' (buffer
        disponible) avant chaque bloc — plus fiable qu'une simple temporisation.

        Pour un espacement supplémentaire (entre lettres ou entre mots), voir
        `RepeaterEngine._send_cw_text` qui coupe réellement le PTT pendant la
        pause — le poste fusionnant plusieurs espaces consécutifs en un seul
        silence standard, insérer des espaces supplémentaires dans le texte
        n'a aucun effet perceptible.

        `on_chunk_sent(chunk, is_space)`, si fourni, est appelé après chaque
        envoi — utile pour tracer précisément ce qui est réellement envoyé
        (diagnostic).
        """
        text = self._apply_prosigns(text.upper())
        chunks = [text[i:i + KY_CHUNK_LEN] for i in range(0, len(text), KY_CHUNK_LEN)] or [""]

        for chunk in chunks:
            waited = 0.0
            while waited < max_wait:
                status = self._send("KY;", timeout=0.3)
                if "KY0" in status:
                    break
                time.sleep(poll_interval)
                waited += poll_interval

            padded = self._pad_chunk(chunk)
            self._send(f"KY {padded};", expect_reply=False)

            if on_chunk_sent:
                on_chunk_sent(chunk, chunk == " ")

    def wait_cw_done(self, poll_interval: float = 0.1, max_wait: float = 30.0):
        waited = 0.0
        while waited < max_wait:
            status = self._send("KY;", timeout=0.3)
            if "KY0" in status:
                return True
            time.sleep(poll_interval)
            waited += poll_interval
        return False
