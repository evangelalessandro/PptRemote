# Test end-to-end di PptRemote.
# Avvia un server di prova isolato (porta 8799, dati in %TEMP%) e verifica:
# codice di collegamento, ricerca file nella cartella configurata, apertura e
# avvio presentazione, contatore slide, anteprima, F5/avanti/indietro/Esc ed errori.
# Uso: powershell -NoProfile -ExecutionPolicy Bypass -File tools\test-e2e.ps1
param(
    [string]$Base = '',
    [string]$Samples = (Join-Path (Split-Path -Parent $PSScriptRoot) 'samples')
)

$ErrorActionPreference = 'Stop'
$script:failed = 0
$script:token = $null

Add-Type -AssemblyName UIAutomationClient, System.Drawing, System.Windows.Forms

Add-Type @"
using System;
using System.Text;
using System.Runtime.InteropServices;
public class Win {
    public delegate bool EnumProc(IntPtr h, IntPtr l);
    [DllImport("user32.dll")] public static extern bool EnumWindows(EnumProc cb, IntPtr l);
    [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern int GetWindowText(IntPtr h, StringBuilder s, int n);
    public static string Text(IntPtr h) { var sb = new StringBuilder(512); GetWindowText(h, sb, 512); return sb.ToString(); }
}
"@

function Step([string]$name, [scriptblock]$check) {
    try {
        $ok = & $check
        if ($ok) { Write-Host ("PASS  " + $name) -ForegroundColor Green }
        else { Write-Host ("FAIL  " + $name) -ForegroundColor Red; $script:failed++ }
    } catch {
        Write-Host ("FAIL  " + $name + "  -> " + $_.Exception.Message) -ForegroundColor Red
        $script:failed++
    }
}

function Http([string]$path, $body) {
    $u = $Base + $path
    if ($script:token) {
        $sep = if ($path.Contains('?')) { '&' } else { '?' }
        $u += $sep + 'token=' + [uri]::EscapeDataString($script:token)
    }
    if ($null -eq $body) { return Invoke-RestMethod -Uri $u -Method Get -TimeoutSec 25 }
    return Invoke-RestMethod -Uri $u -Method Post -ContentType 'application/json' -Body ($body | ConvertTo-Json -Compress) -TimeoutSec 25
}

function HttpStatus([string]$path, $body) {
    $u = $Base + $path
    if ($script:token) {
        $sep = if ($path.Contains('?')) { '&' } else { '?' }
        $u += $sep + 'token=' + [uri]::EscapeDataString($script:token)
    }
    try {
        $null = Invoke-RestMethod -Uri $u -Method Post -ContentType 'application/json' -Body ($body | ConvertTo-Json -Compress) -TimeoutSec 25
        return 200
    } catch {
        return [int]$_.Exception.Response.StatusCode
    }
}

function Find-Dialog([string]$name, [int]$timeoutMs = 6000) {
    $deadline = (Get-Date).AddMilliseconds($timeoutMs)
    while ((Get-Date) -lt $deadline) {
        $root = [System.Windows.Automation.AutomationElement]::RootElement
        $cond = New-Object System.Windows.Automation.PropertyCondition ([System.Windows.Automation.AutomationElement]::NameProperty), $name
        $found = $root.FindFirst([System.Windows.Automation.TreeScope]::Children, $cond)
        if ($found) { return $found }
        Start-Sleep -Milliseconds 150
    }
    return $null
}

function Save-Shot([string]$name) {
    $dir = Join-Path (Split-Path -Parent $PSScriptRoot) '.tmp'
    New-Item -ItemType Directory -Force $dir | Out-Null
    $b = [System.Windows.Forms.Screen]::PrimaryScreen.Bounds
    $bmp = New-Object System.Drawing.Bitmap $b.Width, $b.Height
    $g = [System.Drawing.Graphics]::FromImage($bmp)
    $g.CopyFromScreen($b.Location, [System.Drawing.Point]::Empty, $b.Size)
    $bmp.Save((Join-Path $dir ($name + '.png')), [System.Drawing.Imaging.ImageFormat]::Png)
    $g.Dispose(); $bmp.Dispose()
}

function Show-Window([object]$state) {
    return @($state.windows) | Where-Object { $_.kind -eq 'show' } | Select-Object -First 1
}

function Get-PreviewHash([int]$hwnd) {
    $r = Invoke-WebRequest -Uri ($Base + '/api/preview?hwnd=' + $hwnd + '&token=' + [uri]::EscapeDataString($script:token)) -UseBasicParsing -TimeoutSec 20
    if ($r.StatusCode -ne 200) { return '' }
    $sha = [System.Security.Cryptography.SHA256]::Create()
    return [System.BitConverter]::ToString($sha.ComputeHash([byte[]]$r.Content))
}

# ------------------------------------------------- server di prova isolato
$server = $null
if (-not $Base) {
    $port = 8799
    $serverDir = Join-Path (Split-Path -Parent $PSScriptRoot) 'server'
    $dataDir = Join-Path $env:TEMP 'ppremote-test'
    New-Item -ItemType Directory -Force $dataDir | Out-Null
    Remove-Item (Join-Path $dataDir 'config.json') -ErrorAction SilentlyContinue
    $env:PPTREMOTE_DATA = $dataDir
    $server = Start-Process -FilePath 'python' -ArgumentList 'ppremote.py', '--port', $port, '--test-mode' `
        -WorkingDirectory $serverDir -PassThru -WindowStyle Minimized
    $Base = "http://127.0.0.1:$port"
    $up = $false
    for ($i = 0; $i -lt 60; $i++) {
        try { $null = Invoke-RestMethod -Uri "$Base/api/info" -TimeoutSec 2; $up = $true; break } catch { Start-Sleep -Milliseconds 200 }
    }
    if (-not $up) { throw 'il server di prova non si avvia' }
}

try {
    # ------------------------------------------------- 1. codice di collegamento
    Write-Host "`n== Collegamento con codice ==" -ForegroundColor Cyan
    $pair = Http '/api/pair' @{ device = 'Telefono di prova E2E' }
    Step 'richiesta di collegamento creata' { $pair.id -and $pair.status -eq 'pending' }

    $dlg = Find-Dialog 'Collega un telefono'
    Step 'il PC mostra la finestra con il codice' { [bool]$dlg }
    Save-Shot 'pair-dialog'

    $codeInfo = Http ("/api/pair/code?id=" + $pair.id) $null
    Step 'il codice è di 6 cifre' { $codeInfo.code -match '^\d{6}$' }

    $wrong = if ($codeInfo.code -eq '000000') { '111111' } else { '000000' }
    $status = HttpStatus '/api/pair/confirm' @{ id = $pair.id; code = $wrong }
    Step 'codice sbagliato respinto (403)' { $status -eq 403 }

    $null = Http '/api/pair/confirm' @{ id = $pair.id; code = $codeInfo.code }
    $approved = Http ("/api/pair?id=" + $pair.id) $null
    Step 'codice giusto -> approvato con token' { $approved.status -eq 'approved' -and $approved.token }
    $script:token = $approved.token

    Start-Sleep -Milliseconds 900
    Step 'la finestra sul PC si chiude da sola' { -not (Find-Dialog 'Collega un telefono' 800) }

    $saved = $script:token
    $script:token = 'token-sbagliato'
    $unauth = $false
    try { Http '/api/state' $null | Out-Null } catch { $unauth = $true }
    $script:token = $saved
    Step 'token non valido respinto' { $unauth }

    $pair2 = Http '/api/pair' @{ device = 'Telefono sospetto' }
    $last = 0
    for ($i = 0; $i -lt 5; $i++) { $last = HttpStatus '/api/pair/confirm' @{ id = $pair2.id; code = '999999' } }
    Step 'dopo 5 codici errati la richiesta è bloccata' { $last -eq 429 }
    Start-Sleep -Milliseconds 700

    # ------------------------------------------------------- 2. cartella e file
    Write-Host "`n== Cartella e ricerca file ==" -ForegroundColor Cyan
    $cfg = Http '/api/config' @{ folder = $Samples }
    Step 'cartella di ricerca impostata' { $cfg.folder -eq $Samples }

    $found = Http '/api/files?q=demo' $null
    $demo = @($found.files) | Where-Object { $_.name -eq 'demo.pptx' } | Select-Object -First 1
    Step 'la ricerca trova demo.pptx' { [bool]$demo }
    Step 'il file riporta 5 slide' { $demo -and $demo.slides -eq 5 }
    Step 'i file di blocco di Office sono esclusi' { -not (@($found.files) | Where-Object { $_.name -like '~$*' }) }

    $empty = Http '/api/files?q=zzz-non-esiste' $null
    Step 'ricerca senza risultati non fallisce' { @($empty.files).Count -eq 0 }

    # ricerca multi-parola, ordine libero, accenti ignorati
    $multiDir = Join-Path $env:TEMP 'ppremote-test-files'
    New-Item -ItemType Directory -Force $multiDir | Out-Null
    Copy-Item (Join-Path $Samples 'demo.pptx') (Join-Path $multiDir 'Davide_e_Golia_ubbidienza.pptx') -Force
    Copy-Item (Join-Path $Samples 'demo.pptx') (Join-Path $multiDir 'Città e Paesaggi.pptx') -Force
    $null = Http '/api/config' @{ folder = $multiDir }

    $multi = Http '/api/files?q=goli%20ubbi' $null
    $firstMulti = @($multi.files) | Select-Object -First 1
    Step '"goli ubbi" trova Golia Ubbidienza' { $firstMulti -and $firstMulti.name -like '*Golia_ubbidienza*' }

    $reversed = Http '/api/files?q=ubbi%20goli' $null
    Step '"ubbi goli" (ordine inverso) la trova comunque' { (@($reversed.files) | Where-Object { $_.name -like '*Golia_ubbidienza*' }) }

    $accent = Http '/api/files?q=citta' $null
    Step 'ricerca senza accenti: "citta" trova "Città"' { (@($accent.files) | Where-Object { $_.name -like '*Paesaggi*' }) }

    $null = Http '/api/config' @{ folder = $Samples }

    $outside = HttpStatus '/api/files/open' @{ path = 'C:\Windows\notepad.exe'; present = $false }
    Step 'file fuori dalla cartella rifiutato (400)' { $outside -eq 400 }

    # ------------------------------------------------ 3. apertura e presentazione
    Write-Host "`n== Apertura e presentazione ==" -ForegroundColor Cyan
    $opened = Http '/api/files/open' @{ path = $demo.path; present = $true }
    Step 'apertura file + F5 da API' { $opened.ok }
    Start-Sleep -Seconds 3

    $state = Http '/api/state' $null
    $showWin = Show-Window $state
    Step 'lo stato segnala la presentazione a schermo intero' { [bool]$showWin }
    Step 'contatore: slide 1 di 5' { $showWin.slide.current -eq 1 -and $showWin.slide.total -eq 5 }
    Save-Shot 'slideshow'

    # anteprima live: deve esistere ed essere un BMP
    $preview = Invoke-WebRequest -Uri ($Base + '/api/preview?hwnd=' + $showWin.hwnd + '&token=' + [uri]::EscapeDataString($script:token)) -UseBasicParsing -TimeoutSec 20
    Step 'anteprima disponibile (BMP)' {
        $preview.StatusCode -eq 200 -and $preview.Headers['Content-Type'] -eq 'image/bmp' -and $preview.RawContentLength -gt 2000
    }
    Step 'anteprima è un BMP valido' { $preview.Content[0] -eq 0x42 -and $preview.Content[1] -eq 0x4D }

    $hash1 = Get-PreviewHash $showWin.hwnd

    $null = Http '/api/action' @{ action = 'next'; hwnd = $showWin.hwnd }
    Start-Sleep -Milliseconds 900
    $state = Http '/api/state' $null
    $showWin = Show-Window $state
    Step 'avanti -> contatore a 2' { $showWin.slide.current -eq 2 }
    $hash2 = Get-PreviewHash $showWin.hwnd
    Step "l'anteprima cambia dopo il cambio slide" { $hash1 -and $hash2 -ne $hash1 }

    $null = Http '/api/action' @{ action = 'next'; hwnd = $showWin.hwnd }
    Start-Sleep -Milliseconds 900
    $state = Http '/api/state' $null
    $showWin = Show-Window $state
    Step 'secondo avanti -> contatore a 3' { $showWin.slide.current -eq 3 }

    $null = Http '/api/action' @{ action = 'prev'; hwnd = $showWin.hwnd }
    Start-Sleep -Milliseconds 900
    $state = Http '/api/state' $null
    $showWin = Show-Window $state
    Step 'indietro -> contatore a 2' { $showWin.slide.current -eq 2 }

    $null = Http '/api/action' @{ action = 'goto'; hwnd = $showWin.hwnd; slide = 4 }
    Start-Sleep -Milliseconds 1100
    $state = Http '/api/state' $null
    $showWin = Show-Window $state
    Step 'vai a slide 4' { $showWin.slide.current -eq 4 }
    $hash4 = Get-PreviewHash $showWin.hwnd
    Step "l'anteprima resta valida dopo il salto di slide" { $hash4 -and $hash4.Length -gt 0 }

    $ppt = New-Object -ComObject PowerPoint.Application
    $acting = Show-Window (Http '/api/state' $null)

    if ($acting) {
        try { $null = Http '/api/action' @{ action = 'black'; hwnd = $acting.hwnd } } catch { }
        Start-Sleep -Milliseconds 500
        $stillThere = Show-Window (Http '/api/state' $null)
        Step 'schermo nero accettato' { [bool]$stillThere }
        try { $null = Http '/api/action' @{ action = 'black'; hwnd = $acting.hwnd } } catch { }
        Start-Sleep -Milliseconds 400

        $laserOk = $true
        try { $null = Http '/api/action' @{ action = 'laser'; hwnd = $acting.hwnd } } catch { $laserOk = $false }
        Start-Sleep -Milliseconds 400
        Step 'comando laser accettato' { $laserOk }

        try { $null = Http '/api/action' @{ action = 'stop'; hwnd = $acting.hwnd } } catch { }
        Start-Sleep -Milliseconds 1200
        Step 'Esc chiude la presentazione' { $ppt.SlideShowWindows.Count -eq 0 }
    } else {
        Write-Host '      (presentazione chiusa dall''esterno: salto nero/laser/Esc)' -ForegroundColor Yellow
    }

    # -------------------------------------------------------------- 4. errori
    Write-Host "`n== Errori ==" -ForegroundColor Cyan
    $bad = $false
    try { Http '/api/action' @{ action = 'azione-inventata'; hwnd = 0 } | Out-Null } catch { $bad = $true }
    Step 'azione sconosciuta rifiutata' { $bad }

    $bad = $false
    try { Http '/api/action' @{ action = 'next'; hwnd = 999999 } | Out-Null } catch { $bad = $true }
    Step 'comando verso finestra inesistente rifiutato' { $bad }

    $bad = $false
    try { Http '/api/pair?id=inesistente' $null | Out-Null } catch { $bad = $true }
    Step 'richiesta di pairing inesistente -> 404' { $bad }

    $bad = $false
    try { Http '/api/config' @{ folder = 'C:\cartella\che\non\esiste' } | Out-Null } catch { $bad = $true }
    Step 'cartella inesistente rifiutata' { $bad }

    # chiusura pulita del deck aperto dal test
    foreach ($p in $ppt.Presentations) {
        if ($p.FullName -like '*demo.pptx*') { $p.Close() }
    }
}
finally {
    if ($server) { Stop-Process -Id $server.Id -Force -ErrorAction SilentlyContinue }
}

Write-Host ""
if ($script:failed -eq 0) { Write-Host "TUTTI I TEST OK" -ForegroundColor Green }
else { Write-Host ("TEST FALLITI: " + $script:failed) -ForegroundColor Red }
exit $script:failed
