@echo off
REM ==================================================================
REM  abrir_chrome_gran.bat
REM  Abre o Chrome DEDICADO do extrator do Gran Cursos Online.
REM
REM  Porta CDP 9380 = so deste extrator. Nao mexe nas outras:
REM     9222 -> coletor_betsapi      9267 -> supermae (Superbet)
REM     9350 -> estrelabot           9370 -> ktobot
REM     9380 -> ESTE (Gran Cursos)
REM
REM  Perfil proprio em C:\chrome_gran: o login/cookies daqui NAO se
REM  misturam com o seu Chrome normal nem com o dos outros bots.
REM
REM  USO: duplo clique. Na janela que abrir, faca login no Gran UMA vez
REM       (a senha e digitada por voce, o script nunca a recebe).
REM       Depois o login fica salvo nesse perfil.
REM       Deixe essa janela ABERTA enquanto rodar o extrator.
REM ==================================================================

setlocal
set PORTA=9380
set PERFIL=C:\chrome_gran
set CHROME=

REM --- procura o chrome.exe nos lugares padrao do Windows ---
if exist "%ProgramFiles%\Google\Chrome\Application\chrome.exe" (
    set "CHROME=%ProgramFiles%\Google\Chrome\Application\chrome.exe"
) else if exist "%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe" (
    set "CHROME=%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"
) else if exist "%LocalAppData%\Google\Chrome\Application\chrome.exe" (
    set "CHROME=%LocalAppData%\Google\Chrome\Application\chrome.exe"
)

if "%CHROME%"=="" (
    echo.
    echo  [ERRO] Nao achei o chrome.exe nos lugares padrao.
    echo.
    echo  Procure com este comando e me mande o caminho:
    echo     where /r "C:\Program Files" chrome.exe
    echo.
    pause
    exit /b 1
)

echo.
echo  Chrome:  %CHROME%
echo  Porta:   %PORTA%   (CDP - so do extrator do Gran)
echo  Perfil:  %PERFIL%
echo.
echo  Abrindo... FACA LOGIN NO GRAN na janela que abrir e deixe ela aberta.
echo.

REM Mesmas flags de anti-throttling dos outros bots: sem elas, com a janela
REM oculta (RDP desconectado, minimizada) o Chrome estrangula o renderer.
start "" "%CHROME%" --remote-debugging-port=%PORTA% --user-data-dir="%PERFIL%" ^
  --disable-backgrounding-occluded-windows ^
  --disable-renderer-backgrounding ^
  --disable-background-timer-throttling ^
  --no-first-run --no-default-browser-check ^
  https://www.grancursosonline.com.br/

timeout /t 3 /nobreak >nul
echo  Pronto. Pra conferir se a porta subiu, abra no navegador:
echo     http://localhost:%PORTA%/json/version
echo.
echo  Depois de logar, abra a pagina que quer extrair e rode:
echo     python gran_inspecionar.py
echo.
pause
