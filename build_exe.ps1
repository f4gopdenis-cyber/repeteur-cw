# build_exe.ps1
# Fabrique un seul executable Windows (RepeteurCW.exe) contenant l'application
# complete (reglages + demarrage/arret). A executer UNE SEULE FOIS.
# Ensuite, il suffit de double-cliquer sur RepeteurCW.exe.

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

Write-Host "=== Fabrication de l'executable RepeteurCW.exe ===" -ForegroundColor Cyan
Write-Host ""

function Test-RealPython {
    param([string]$exePath)
    if (-not (Test-Path $exePath)) { return $false }
    try {
        $out = & $exePath --version 2>&1
        # L'alias Microsoft Store renvoie une chaine vide ou ouvre le Store sans rien afficher
        return ($out -match "Python \d")
    } catch {
        return $false
    }
}

# --- 1. Trouver un VRAI interpreteur Python (pas l'alias Microsoft Store) ---
$candidates = @()

# Le launcher officiel "py" est le plus fiable quand il est present
$pyLauncher = Get-Command py -ErrorAction SilentlyContinue
if ($pyLauncher) { $candidates += $pyLauncher.Source }

$pythonCmd = Get-Command python -ErrorAction SilentlyContinue
if ($pythonCmd) { $candidates += $pythonCmd.Source }

$realPython = $null
foreach ($c in $candidates) {
    if ($c -match "py\.exe$") {
        # verifie via "py -3 --version"
        try {
            $out = & $c -3 --version 2>&1
            if ($out -match "Python \d") { $realPython = "$c -3"; break }
        } catch {}
    } elseif (Test-RealPython $c) {
        $realPython = $c
        break
    }
}

if (-not $realPython) {
    Write-Host "Aucun interpreteur Python fonctionnel n'a ete trouve." -ForegroundColor Red
    Write-Host ""
    Write-Host "Cause la plus frequente sous Windows : 'python.exe' pointe vers l'ALIAS" -ForegroundColor Yellow
    Write-Host "du Microsoft Store (qui ouvre juste le Store) au lieu du vrai Python." -ForegroundColor Yellow
    Write-Host ""
    Write-Host "Pour corriger :"
    Write-Host "  1. Verifiez que Python est bien installe : https://www.python.org/downloads/"
    Write-Host "     (cochez 'Add python.exe to PATH' pendant l'installation)"
    Write-Host "  2. Si c'est deja fait, desactivez l'alias du Store :"
    Write-Host "     Parametres Windows > Applications > Fonctionnalites avancees des applications"
    Write-Host "     > Alias d'execution des applications > desactivez 'python.exe' et 'python3.exe'"
    Write-Host "  3. Fermez et rouvrez une nouvelle fenetre, puis relancez build_exe.bat"
    exit 1
}

Write-Host "Python detecte : $realPython"

# --- 2. Environnement virtuel (temporaire, sert uniquement a la fabrication) ---
if (Test-Path ".\build_venv") {
    $existingPy = ".\build_venv\Scripts\python.exe"
    if (-not (Test-RealPython $existingPy)) {
        Write-Host "Un environnement de fabrication incomplet existe deja, on le recree..." -ForegroundColor Yellow
        Remove-Item ".\build_venv" -Recurse -Force
    }
}

if (-not (Test-Path ".\build_venv")) {
    Write-Host "Creation de l'environnement de fabrication..."
    Invoke-Expression "$realPython -m venv build_venv"
    if ($LASTEXITCODE -ne 0) {
        Write-Host "Echec de la creation de l'environnement virtuel (code $LASTEXITCODE)." -ForegroundColor Red
        exit 1
    }
}

$venvPython = ".\build_venv\Scripts\python.exe"

if (-not (Test-RealPython $venvPython)) {
    Write-Host "L'environnement virtuel n'a pas ete cree correctement : $venvPython est introuvable ou invalide." -ForegroundColor Red
    Write-Host "Supprimez le dossier build_venv et relancez build_exe.bat."
    exit 1
}

Write-Host "Environnement virtuel pret : $venvPython"

# --- 3. Dependances ---
Write-Host ""
Write-Host "Installation des dependances (numpy, sounddevice, pyserial, pyinstaller)..." -ForegroundColor Cyan
& $venvPython -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) { Write-Host "Echec de la mise a jour de pip." -ForegroundColor Red; exit 1 }

& $venvPython -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) { Write-Host "Echec de l'installation des dependances." -ForegroundColor Red; exit 1 }

# --- 4bis. Verifier que les fichiers du projet sont bien presents ---
$requiredFiles = @("app.py", "cw_decoder.py", "rig_control.py")
$missing = $requiredFiles | Where-Object { -not (Test-Path (Join-Path $root $_)) }
if ($missing) {
    Write-Host ""
    Write-Host "ERREUR : fichier(s) manquant(s) dans ce dossier : $($missing -join ', ')" -ForegroundColor Red
    Write-Host "Verifiez qu'ils sont bien nommes exactement ainsi (pas de '(1)' ou autre suffixe" -ForegroundColor Red
    Write-Host "ajoute par le navigateur lors du telechargement), puis relancez build_exe.bat." -ForegroundColor Red
    exit 1
}

# --- 4. Fabrication de l'executable ---
Write-Host ""
Write-Host "Fabrication de l'executable (peut prendre 1 a 2 minutes)..." -ForegroundColor Cyan
# --hidden-import force l'inclusion de ces modules meme si l'analyse automatique
# de PyInstaller ne les detectait pas pour une raison ou une autre (filet de securite).
& $venvPython -m PyInstaller --onefile --windowed --noconfirm --name RepeteurCW `
    --hidden-import=cw_decoder --hidden-import=rig_control app.py
if ($LASTEXITCODE -ne 0) { Write-Host "Echec de la fabrication de l'executable." -ForegroundColor Red; exit 1 }

# --- 5. Copier l'executable a la racine du projet ---
Copy-Item ".\dist\RepeteurCW.exe" ".\RepeteurCW.exe" -Force

Write-Host ""
Write-Host "=== Fabrication terminee ===" -ForegroundColor Green
Write-Host "L'executable RepeteurCW.exe se trouve dans ce dossier :"
Write-Host "  $root\RepeteurCW.exe"
Write-Host ""
Write-Host "Vous pouvez desormais :"
Write-Host "  - le deplacer ou le copier ou vous voulez (il est autonome)"
Write-Host "  - le lancer par un simple double-clic"
Write-Host "  - supprimer les dossiers build\, dist\ et build_venv\ si vous le souhaitez (non necessaires a l'usage)"
Write-Host ""
Write-Host "Rappel : il vous faut aussi installer le pilote USB Kenwood du TS-990S"
Write-Host "et telecharger Hamlib (rigctld.exe) une fois, voir README.md."
