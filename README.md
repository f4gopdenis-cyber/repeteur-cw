# Répéteur CW HF — Kenwood TS-990S

Une seule application avec interface graphique : un onglet **Réglages**
(indicatif, **port COM et baud rate du poste**, audio, vitesse CW...) et un
onglet **Répéteur** (Démarrer/Arrêter, suivi en direct). Écoute le trafic
reçu par le TS-990S, décode l'indicatif appelant, et répond
automatiquement avec un rapport (RST), le locator et le QTH de la
station.

L'application parle **directement** au port série du TS-990S (protocole
CAT ASCII Kenwood) — **aucun logiciel intermédiaire requis**, ni Hamlib,
ni OmniRig. Juste le pilote USB du poste.

## Ce qu'il faut installer une seule fois, avant de commencer

1. **Pilote USB Kenwood** pour le TS-990S (le poste doit apparaître comme
   port COM dans le Gestionnaire de périphériques Windows — notez le
   numéro, par exemple `COM5`)
2. **Python 3.11+** depuis https://www.python.org/downloads/ (cochez
   "Add python.exe to PATH"), mais uniquement pour fabriquer l'exécutable
   à l'étape suivante — vous n'en aurez plus besoin ensuite

## Fabriquer l'application (une seule fois)

Double-cliquez sur **`build_exe.bat`**. Il installe les dépendances dans un
environnement temporaire et fabrique **`RepeteurCW.exe`** dans ce dossier.
Ça prend une à deux minutes.

Une fois `RepeteurCW.exe` créé, vous pouvez :
- le déplacer où vous voulez (il est autonome)
- supprimer les dossiers `build\`, `dist\` et `build_venv\` si vous le
  souhaitez — ils ne servent qu'à la fabrication

## Utilisation au quotidien

**Un seul programme à lancer : `RepeteurCW.exe`.**

- Onglet **Réglages** :
  - indicatif, locator, QTH
  - **Port COM** : liste déroulante avec bouton **Actualiser** (détecte
    automatiquement les ports disponibles)
  - **Baud rate** : doit correspondre au réglage du menu **7-00** (port
    COM) ou **7-01** (port USB virtuel) du TS-990S — souvent 115200 en USB
  - périphérique audio d'entrée (liste déroulante), fréquence du ton CW,
    vitesse CW
  - bouton "Enregistrer les réglages"
- Onglet **Répéteur** : bouton **Démarrer** (ouvre le port série, vérifie
  que le poste répond, puis lance l'écoute), zone de suivi en direct du
  texte décodé et des réponses envoyées, bouton **Arrêter**.

Les réglages sont sauvegardés dans `settings.json` à côté de l'exécutable
et rechargés automatiquement au prochain lancement.

## Comment ça marche (pour comprendre le code)

```
TS-990S --(audio RX, câble USB)--> décodeur Goertzel (cw_decoder.py)
TS-990S <--(CAT, câble USB/série)--> rig_control.py
                                        ├─ FA;/MD;  lecture fréquence/mode
                                        ├─ TX;/RX;  PTT
                                        ├─ SM0;     S-mètre réel (RST fiable)
                                        └─ KY ...;  émission CW réelle
app.py relie le tout dans une interface graphique (Tkinter) et gère le
cycle de vie de la connexion série + du décodeur dans un thread séparé.
```

Le TS-990S envoie le CW directement via sa commande CAT native `KY` —
pas besoin de matériel externe ni d'interface de manipulation. La
connexion série directe permet aussi de vraiment interroger `KY;` et
d'attendre la confirmation `KY0;` (buffer disponible) avant chaque bloc
de texte — plus fiable qu'un simple calcul de durée.

## Limites connues de ce prototype

- Le décodeur CW reste un algorithme de Goertzel simple avec seuils
  adaptatifs. Il fonctionne bien sur un signal propre en test, mais un
  vrai trafic HF avec QRM/fading demandera d'affiner les seuils, voire de
  passer à un décodeur plus robuste.
- Le "S" du RST envoyé s'appuie sur le S-mètre réel du poste (lu via
  `SM0;`) — bien plus fiable qu'une estimation purement audio — mais le
  mapping vers l'échelle S reste approximatif et à recaler chez vous.
- Le déclenchement de la réponse (indicatif détecté dans le texte décodé)
  est simpliste. Vous voudrez sans doute affiner la logique de
  reconnaissance d'appel une fois le décodeur validé.
- **Un seul programme à la fois peut utiliser le port série** : fermez
  tout autre logiciel de contrôle CAT (WSJT-X, un logger, OmniRig s'il
  tourne encore...) avant de cliquer sur Démarrer ici.

## Dépannage

- **"ERREUR de connexion série"** : le port COM choisi est peut-être déjà
  utilisé par un autre logiciel, ou ne correspond pas au TS-990S. Fermez
  les autres logiciels CAT et vérifiez le port dans le Gestionnaire de
  périphériques.
- **"Port ouvert mais le poste ne répond pas"** : le baud rate choisi
  dans l'application ne correspond pas à celui réglé sur le poste (menu
  7-00/7-01). Essayez les valeurs courantes (115200 en USB, ou celle
  affichée dans le menu du TS-990S).
- **Pas de son détecté** : vérifiez dans les paramètres Windows du son que
  le niveau d'entrée du périphérique audio du TS-990S n'est pas coupé, et
  que vous avez choisi le bon périphérique dans l'onglet Réglages.

## Point réglementaire à vérifier

Un dispositif qui émet automatiquement en HF sans intervention d'un
opérateur peut relever d'un régime particulier (station à fonctionnement
automatique/balise) selon la réglementation de votre pays (en France :
ARCEP/ANFR). Vérifiez ce point avant une mise en service réelle sur
l'antenne. Vous pouvez tester toute l'application sans risque sur charge
fictive ou sans antenne branchée.
