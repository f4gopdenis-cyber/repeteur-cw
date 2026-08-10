# Répéteur CW HF — Kenwood TS-990S

Application Windows (interface graphique) qui écoute le trafic CW reçu par
un Kenwood TS-990S, décode l'indicatif appelant, et répond automatiquement
en CW — rapport RST, QSO scripté, exercices de copie au son, balise météo
automatique, et plus. Aucun logiciel intermédiaire requis (ni Hamlib, ni
OmniRig) : l'application parle directement au port série du poste.

> ⚠️ **Écrit et testé spécifiquement pour le Kenwood TS-990S** (commande CAT
> native `KY` pour l'émission CW). Une adaptation serait nécessaire pour un
> autre poste — voir [Adapter à un autre poste](#adapter-à-un-autre-poste).

## Fonctionnalités

- **Décodage CW** par algorithme de Goertzel, avec plusieurs niveaux de
  sensibilité (Normal / Sensible / Très sensible), réduction optionnelle du
  bruit impulsif (QRM), tolérance de fréquence élargie, gain manuel et
  automatique (AGC)
- **Réponse automatique** : soit un simple rapport RST + QTH, soit un QSO
  scripté en 3 échanges (RST/nom/QTH → poste/puissance/antenne → salutations)
- **Exercices de copie au son** : le correspondant envoie `EXC` (chiffres),
  `EXN` (lettres) ou `EXM` (mots) — le programme envoie 10 groupes, confirme
  par `RR` si correct, répète sur demande (`AGN`)
- **Balise météo automatique** : diffuse température/pression (via
  [Open-Meteo](https://open-meteo.com), gratuit, sans clé API) à intervalle
  réglable, message personnalisable
- **Espacement Farnsworth réglable** à l'émission (entre lettres et entre
  mots), via coupure réelle du PTT — utile pour l'entraînement de
  correspondants débutants
- **Mode diagnostic** : affiche les durées mesurées (point/trait/espace) en
  réception, et le détail de ce qui est réellement envoyé en émission
- **Interface personnalisable** : couleur du thème et du fond au choix

## Prérequis

1. **Pilote USB Kenwood** pour le TS-990S (le poste doit apparaître comme
   port COM dans le Gestionnaire de périphériques Windows)
2. **Python 3.11+** ([python.org](https://www.python.org/downloads/)) —
   cochez *"Add python.exe to PATH"* à l'installation. Uniquement nécessaire
   pour fabriquer l'exécutable ; pas requis ensuite pour l'utiliser.

## Installation

```bash
git clone https://github.com/<votre-compte>/<nom-du-depot>.git
cd <nom-du-depot>
```

Double-cliquez sur **`build_exe.bat`**. Il crée un environnement Python
temporaire, installe les dépendances, et fabrique **`RepeteurCW.exe`** dans
ce dossier (1-2 minutes).

Une fois l'exécutable créé, vous pouvez :
- le déplacer où vous voulez (autonome, pas d'installation)
- supprimer `build\`, `dist\` et `build_venv\` (uniquement utiles à la
  fabrication)

## Utilisation

**Un seul programme à lancer : `RepeteurCW.exe`.**

- **Onglet Réglages** : identité de la station (indicatif, locator, QTH,
  nom, poste, puissance, antenne), port COM et baud rate (menu 7-00/7-01 du
  TS-990S), audio (périphérique, gain, sensibilité), options (QSO scripté,
  QRM, tolérance de fréquence, balise météo), apparence
- **Onglet Répéteur** : Démarrer / Arrêter, journal en direct

Les réglages sont sauvegardés dans `settings.json` à côté de l'exécutable
(ignoré par Git — chacun a les siens).

### Déclencheurs reconnus dans le trafic reçu

| Texte reçu contenant... | Effet |
|---|---|
| votre indicatif | démarre le rapport/QSO scripté |
| `EXC` | exercice de copie — groupes de 5 chiffres |
| `EXN` | exercice de copie — groupes de 5 lettres |
| `EXM` | exercice de copie — mots |
| `AGN` (pendant un exercice) | répète le dernier groupe |

## Comment ça marche

```
TS-990S --(audio RX, USB)--> décodeur Goertzel (cw_decoder.py)
TS-990S <--(CAT série, USB)--> rig_control.py
                                  ├─ FA;/MD;  fréquence/mode
                                  ├─ TX;/RX;  PTT
                                  ├─ SM0;     S-mètre réel
                                  └─ KY ...;  émission CW native
app.py (Tkinter) orchestre le tout, gère les réglages et l'interface.
```

Le TS-990S envoie le CW directement via sa commande CAT native `KY` — pas
de matériel externe, pas d'interface de manipulation.

## Limites connues

- Décodeur à seuil simple : fonctionne bien sur signal propre, moins bien
  en dessous d'un certain rapport signal/bruit ou à très haute vitesse
  (25-30+ mots/minute) avec du bruit — comme pour une oreille humaine, il y
  a des limites physiques
- Sensibilité, réduction de bruit et tolérance de fréquence sont chacune un
  compromis (plus de portée *ou* plus de rejet de bruit, rarement les deux)
- Testé uniquement sur Kenwood TS-990S

## Adapter à un autre poste

Le point clé à adapter est `rig_control.py`, qui utilise le jeu de
commandes CAT propriétaire Kenwood (`FA;`, `MD;`, `TX;`/`RX;`, `SM0;`, et
surtout `KY` pour l'émission CW native). Si votre poste a une commande CAT
équivalente pour envoyer du texte en CW, l'adaptation est mineure ; sinon,
il faudra une autre approche pour l'émission (ex: pilotage direct du
manipulateur).

## Point réglementaire

Un dispositif qui émet automatiquement en HF sans intervention d'un
opérateur peut relever d'un régime particulier (station à fonctionnement
automatique/balise) selon la réglementation de votre pays (en France :
ARCEP/ANFR). Vérifiez ce point avant toute mise en service réelle sur
l'antenne.

## Licence

[MIT](LICENSE) — libre d'utilisation, modification et redistribution.

## Contributions

Développé pour un usage personnel (station F4GOP) et partagé pour la
communauté radioamateur. Suggestions et *pull requests* bienvenues.
