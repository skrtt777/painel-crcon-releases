"""
Painel CRCON - Interface local para gerenciamento de servidor Hell Let Loose
Conecta à API do CRCON para controle de automods, VIPs, configurações etc.
"""

import eel
import json
import os
import sys
import time
import threading
import subprocess
import tempfile
import shutil
from collections import deque
from pathlib import Path
from urllib import request, error

# Versão atual do aplicativo
APP_VERSION = "1.1.3"

# ==================== CONFIGURAÇÃO ====================
# Detecta se está rodando como executável PyInstaller
if getattr(sys, 'frozen', False):
    # Rodando como executável
    BASE_DIR = Path(sys._MEIPASS) if hasattr(sys, '_MEIPASS') else Path(sys.executable).parent
    APP_DIR = Path(sys.executable).parent  # Pasta onde está o .exe
else:
    # Rodando como script Python
    BASE_DIR = Path(__file__).parent
    APP_DIR = BASE_DIR

WEB_DIR = str(BASE_DIR / "web")
DATA_DIR = APP_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)

API_TIMEOUT_SEC = 8

# Conexão CRCON - URL base (API key vem do login)
CRCON_URL = "http://158.220.100.17:8010"

# Sessão do usuário atual
_current_session = {
    "api_key": None,
    "user_name": None,
    "is_superuser": False,
    "permissions": [],
    "groups": []
}

# Hot reload - rastreia última modificação dos arquivos
_file_mtimes = {}

# Cache de logs do SERVIDOR CRCON (tempo real para aba LOG)
_server_logs_lock = threading.Lock()
_server_log_seq = 0
_SERVER_LOG_CACHE_SIZE = 4000
_server_log_cache = deque(maxlen=_SERVER_LOG_CACHE_SIZE)
_server_log_signatures = deque()
_server_log_signature_set = set()
_server_log_actions = []
_server_log_action_counts = {}
_server_log_last_fetch_ts = 0.0

_SERVER_LOG_MAX_SIGNATURES = 12000
_SERVER_LOG_FETCH_INTERVAL_SEC = 2.0
_SERVER_LOG_TAIL_SIZE = 1200

_DEFAULT_SERVER_LOG_ACTIONS = [
    "MESSAGE",
    "TEAM KILL",
    "TEAMSWITCH",
    "VOTE",
    "VOTE COMPLETED",
    "VOTE EXPIRED",
    "VOTE PASSED",
    "VOTE STARTED",
    "ADMIN",
    "ADMIN ANTI-CHEAT",
    "ADMIN BANNED",
    "ADMIN IDLE",
    "ADMIN KICKED",
    "ADMIN MISC",
    "ADMIN PERMA BANNED",
    "CAMERA",
    "CHAT",
    "CHAT[ALLIES]",
    "CHAT[ALLIES][TEAM]",
    "CHAT[ALLIES][UNIT]",
    "CHAT[AXIS]",
    "CHAT[AXIS][TEAM]",
    "CHAT[AXIS][UNIT]",
    "CONNECTED",
    "DISCONNECTED",
    "KILL",
    "MATCH",
    "MATCH ENDED",
    "MATCH START",
    "TK AUTO",
    "TK AUTO BANNED",
    "TK AUTO KICKED",
]


def _normalize_action_name(action: str) -> str:
    return str(action or "OUTROS").strip().upper() or "OUTROS"


def _merge_server_actions(actions_from_api, action_counts):
    """Retorna lista ordenada de ações com cobertura completa do painel CRCON."""
    merged = set(_DEFAULT_SERVER_LOG_ACTIONS)

    for action in actions_from_api or []:
        merged.add(_normalize_action_name(action))

    for action in (action_counts or {}).keys():
        merged.add(_normalize_action_name(action))

    ordered = []
    for action in _DEFAULT_SERVER_LOG_ACTIONS:
        if action in merged:
            ordered.append(action)
            merged.discard(action)

    # Extras não previstos na lista padrão
    for action in sorted(merged):
        ordered.append(action)

    return ordered


def _map_action_to_category(action_upper: str) -> str:
    if action_upper in {"TEAM KILL", "TK AUTO", "TK AUTO KICKED", "TK AUTO BANNED"}:
        return "teamkill"
    if action_upper.startswith("VOTE"):
        return "vote"
    if action_upper.startswith("CHAT"):
        return "chat"
    if action_upper.startswith("ADMIN"):
        return "admin"
    if action_upper.startswith("MATCH"):
        return "match"
    if action_upper == "MESSAGE":
        return "message"
    if action_upper in {"CONNECTED", "DISCONNECTED", "TEAMSWITCH", "KILL"}:
        return "other"
    return "other"


def _format_log_timestamp(entry: dict) -> str:
    event_time = str(entry.get("event_time") or "")
    if "T" in event_time:
        return event_time.split("T", 1)[1][:8]

    timestamp_ms = entry.get("timestamp_ms")
    if isinstance(timestamp_ms, (int, float)):
        return time.strftime("%H:%M:%S", time.localtime(float(timestamp_ms) / 1000.0))

    return time.strftime("%H:%M:%S")


def _extract_recent_logs_payload(payload):
    """Extrai logs e ações do payload de get_recent_logs."""
    logs = []
    actions = []

    if isinstance(payload, dict):
        logs = payload.get("logs") or []
        actions = payload.get("actions") or []
    elif isinstance(payload, list):
        logs = payload

    return logs, actions


def _build_server_log_item(entry: dict):
    """Converte entrada CRCON em item padrão para frontend."""
    action = _normalize_action_name(entry.get("action"))
    raw = str(entry.get("raw") or entry.get("line_without_time") or entry.get("message") or "")
    timestamp_ms = entry.get("timestamp_ms")
    signature = f"{timestamp_ms}|{action}|{raw}"

    message = str(entry.get("line_without_time") or entry.get("message") or entry.get("raw") or "").strip()
    item = {
        "timestamp": _format_log_timestamp(entry),
        "source": "server",
        "action": action,
        "category": _map_action_to_category(action),
        "message": message,
    }
    return item, signature


def _append_server_log(item: dict, signature: str):
    """Adiciona log novo ao cache local evitando duplicados."""
    global _server_log_seq

    if signature in _server_log_signature_set:
        return

    _server_log_seq += 1
    item["id"] = _server_log_seq
    _server_log_cache.append(item)

    _server_log_signatures.append(signature)
    _server_log_signature_set.add(signature)

    while len(_server_log_signatures) > _SERVER_LOG_MAX_SIGNATURES:
        oldest = _server_log_signatures.popleft()
        _server_log_signature_set.discard(oldest)


def _refresh_server_logs(force=False):
    """Atualiza cache local lendo logs recentes do servidor CRCON."""
    global _server_log_last_fetch_ts, _server_log_actions, _server_log_action_counts

    now = time.time()
    if not force and (now - _server_log_last_fetch_ts) < _SERVER_LOG_FETCH_INTERVAL_SEC:
        return {"success": True}

    result = api_get("get_recent_logs")
    _server_log_last_fetch_ts = now

    if not result.get("success"):
        return result

    logs, actions = _extract_recent_logs_payload(result.get("data") or {})
    if not isinstance(logs, list):
        logs = []
    if not isinstance(actions, list):
        actions = []

    recent_slice = logs[:_SERVER_LOG_TAIL_SIZE]  # API retorna do mais novo para o mais antigo

    with _server_logs_lock:
        # Reverte para manter visualização cronológica (mais antigo -> mais novo)
        for entry in reversed(recent_slice):
            if not isinstance(entry, dict):
                continue
            item, signature = _build_server_log_item(entry)
            _append_server_log(item, signature)

        cache_counts = {}
        for item in _server_log_cache:
            action = _normalize_action_name(item.get("action"))
            cache_counts[action] = cache_counts.get(action, 0) + 1

        _server_log_action_counts = cache_counts
        _server_log_actions = _merge_server_actions(actions, cache_counts)

    return {"success": True}

# Controle de avisos de partida
MATCH_AVISOS_STATE_FILE = DATA_DIR / "_match_avisos_state.json"

def _load_match_avisos_state():
    """Carrega estado dos avisos de partida do disco (sobrevive a restarts)."""
    default = {
        "last_map": None,
        "start_sent": False,
        "end_sent": False,
        "match_duration": 5400,
        "last_stats_map_id": None,
        "pending_stats_send": False,
    }
    try:
        if MATCH_AVISOS_STATE_FILE.exists():
            with open(MATCH_AVISOS_STATE_FILE, "r", encoding="utf-8") as f:
                saved = json.load(f)
                return {**default, **saved}
    except Exception:
        pass
    return default

def _save_match_avisos_state():
    """Persiste estado dos avisos de partida no disco."""
    try:
        with open(MATCH_AVISOS_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(_match_avisos_state, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

_match_avisos_state = _load_match_avisos_state()

MATCH_STATS_SEND_LOG_FILE = DATA_DIR / "_match_stats_send_log.json"
_MATCH_STATS_SEND_LOG_MAX = 200
_match_stats_send_log_lock = threading.Lock()


def _load_match_stats_send_log():
    try:
        if MATCH_STATS_SEND_LOG_FILE.exists():
            with open(MATCH_STATS_SEND_LOG_FILE, "r", encoding="utf-8") as f:
                loaded = json.load(f)
                if isinstance(loaded, list):
                    return [entry for entry in loaded if isinstance(entry, dict)][:_MATCH_STATS_SEND_LOG_MAX]
    except Exception:
        pass
    return []


_match_stats_send_log = _load_match_stats_send_log()

_STATS_MONITOR_PREVIEW_CACHE_SEC = 20
_stats_monitor_preview_lock = threading.Lock()
_stats_monitor_preview_cache = {
    "at": 0.0,
    "value": None,
}


def _persist_match_stats_send_log_unlocked():
    try:
        with open(MATCH_STATS_SEND_LOG_FILE, "w", encoding="utf-8") as f:
            json.dump(_match_stats_send_log[:_MATCH_STATS_SEND_LOG_MAX], f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def _append_match_stats_send_log(entry: dict):
    with _match_stats_send_log_lock:
        _match_stats_send_log.insert(0, entry)
        del _match_stats_send_log[_MATCH_STATS_SEND_LOG_MAX:]
        _persist_match_stats_send_log_unlocked()


def _get_match_stats_send_logs(limit=40):
    limit = max(1, min(int(limit or 40), _MATCH_STATS_SEND_LOG_MAX))
    with _match_stats_send_log_lock:
        return list(_match_stats_send_log[:limit])

eel.init(WEB_DIR)


# ==================== HELPERS ====================
def deep_merge(base: dict, updates: dict) -> dict:
    """Merge profundo de dicionários - preserva estruturas aninhadas."""
    result = base.copy()
    for key, value in updates.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def get_api_key():
    """Retorna a API key da sessão atual."""
    return _current_session.get("api_key") or ""


def api_get(endpoint: str):
    """GET request à API CRCON."""
    url = f"{CRCON_URL}/api/{endpoint}"
    headers = {"Authorization": f"Bearer {get_api_key()}"}
    try:
        req = request.Request(url, headers=headers)
        with request.urlopen(req, timeout=API_TIMEOUT_SEC) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            if data.get("failed"):
                return {"success": False, "error": data.get("error", "Erro desconhecido")}
            return {"success": True, "data": data.get("result")}
    except error.HTTPError as e:
        return {"success": False, "error": f"HTTP {e.code}: {e.reason}"}
    except Exception as e:
        return {"success": False, "error": str(e)}


def api_post(endpoint: str, payload: dict):
    """POST request à API CRCON."""
    url = f"{CRCON_URL}/api/{endpoint}"
    headers = {
        "Authorization": f"Bearer {get_api_key()}",
        "Content-Type": "application/json",
    }
    try:
        data_bytes = json.dumps(payload).encode("utf-8")
        req = request.Request(url, data=data_bytes, headers=headers, method="POST")
        with request.urlopen(req, timeout=API_TIMEOUT_SEC) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            if data.get("failed"):
                return {"success": False, "error": data.get("error", "Erro desconhecido")}
            return {"success": True, "data": data.get("result")}
    except error.HTTPError as e:
        body = e.read().decode("utf-8", errors="ignore") if e.fp else ""
        return {"success": False, "error": f"HTTP {e.code}: {body[:200]}"}
    except Exception as e:
        return {"success": False, "error": str(e)}


@eel.expose
def get_panel_logs(after_id=0, limit=200):
    """Retorna logs de eventos do SERVIDOR CRCON para a aba LOG."""
    try:
        after_id = int(after_id or 0)
        limit = int(limit or 200)
        limit = max(1, min(limit, _SERVER_LOG_CACHE_SIZE))

        refresh_result = _refresh_server_logs(force=(after_id == 0))
        if not refresh_result.get("success"):
            return {"success": False, "error": refresh_result.get("error", "Falha ao obter logs do servidor")}

        with _server_logs_lock:
            logs = [item for item in _server_log_cache if item["id"] > after_id]
            if len(logs) > limit:
                logs = logs[-limit:]
            last_id = _server_log_seq
            actions = list(_server_log_actions)
            action_counts = dict(_server_log_action_counts)

        return {
            "success": True,
            "data": {
                "logs": logs,
                "last_id": last_id,
                "actions": actions,
                "action_counts": action_counts,
            },
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


@eel.expose
def clear_panel_logs():
    """Limpa apenas o cache local da aba LOG (não remove logs do servidor)."""
    try:
        global _server_log_last_fetch_ts

        with _server_logs_lock:
            _server_log_cache.clear()
            last_id = _server_log_seq

        _server_log_last_fetch_ts = 0.0
        return {"success": True, "data": {"last_id": last_id}}
    except Exception as e:
        return {"success": False, "error": str(e)}


# ==================== SISTEMA DE UPDATE ====================
# URL onde fica hospedado o arquivo de versão
UPDATE_URL = "https://raw.githubusercontent.com/skrtt777/painel-crcon-releases/main"

@eel.expose
def get_current_version():
    """Retorna a versão atual do aplicativo."""
    return {
        "success": True,
        "data": {
            "version": APP_VERSION,
            "is_exe": getattr(sys, 'frozen', False)
        }
    }


@eel.expose
def check_for_updates():
    """Verifica se há atualizações disponíveis."""
    try:
        version_url = f"{UPDATE_URL}/version.json"
        req = request.Request(version_url, headers={"User-Agent": "PainelCRCON"})
        
        with request.urlopen(req, timeout=10) as resp:
            remote_data = json.loads(resp.read().decode("utf-8"))
        
        remote_version = remote_data.get("version", "0.0.0")
        changelog = remote_data.get("changelog", [])
        download_url = remote_data.get("download_url", "")
        
        # Compara versões
        has_update = compare_versions(remote_version, APP_VERSION) > 0
        
        return {
            "success": True,
            "data": {
                "has_update": has_update,
                "current_version": APP_VERSION,
                "remote_version": remote_version,
                "changelog": changelog,
                "download_url": download_url
            }
        }
    except error.URLError as e:
        return {"success": False, "error": f"Não foi possível verificar atualizações: {e.reason}"}
    except Exception as e:
        return {"success": False, "error": str(e)}


def compare_versions(v1: str, v2: str) -> int:
    """Compara duas versões. Retorna 1 se v1 > v2, -1 se v1 < v2, 0 se iguais."""
    def parse_version(v):
        return [int(x) for x in v.split(".")]
    
    v1_parts = parse_version(v1)
    v2_parts = parse_version(v2)
    
    for i in range(max(len(v1_parts), len(v2_parts))):
        p1 = v1_parts[i] if i < len(v1_parts) else 0
        p2 = v2_parts[i] if i < len(v2_parts) else 0
        if p1 > p2:
            return 1
        elif p1 < p2:
            return -1
    return 0


@eel.expose
def download_update(download_url: str = None):
    """Baixa a atualização e prepara para instalação."""
    if not getattr(sys, 'frozen', False):
        return {"success": False, "error": "Atualização automática só funciona no executável compilado"}
    
    try:
        # Se não passou URL, busca do version.json
        if not download_url:
            check = check_for_updates()
            if not check.get("success"):
                return check
            download_url = check["data"].get("download_url")
            if not download_url:
                download_url = f"{UPDATE_URL}/Painel_CRCON.zip"
        
        print(f"📥 Baixando atualização de: {download_url}")
        
        # Cria pasta temporária para download
        temp_dir = Path(tempfile.gettempdir()) / "painel_crcon_update"
        if temp_dir.exists():
            shutil.rmtree(temp_dir, ignore_errors=True)
        temp_dir.mkdir(exist_ok=True)
        
        zip_path = temp_dir / "update.zip"
        
        # Download do arquivo - segue redirects do GitHub
        req = request.Request(download_url, headers={
            "User-Agent": "PainelCRCON/1.0",
            "Accept": "application/octet-stream"
        })
        
        print(f"📥 Conectando...")
        with request.urlopen(req, timeout=180) as resp:
            total_size = int(resp.headers.get('content-length', 0))
            downloaded = 0
            
            with open(zip_path, 'wb') as f:
                while True:
                    chunk = resp.read(16384)
                    if not chunk:
                        break
                    f.write(chunk)
                    downloaded += len(chunk)
                    if total_size > 0:
                        pct = int(downloaded * 100 / total_size)
                        if pct % 20 == 0:
                            print(f"   📥 {pct}% ({downloaded // 1024}KB / {total_size // 1024}KB)")
        
        print(f"✅ Download completo: {zip_path} ({downloaded // 1024}KB)")
        
        # Verifica se o ZIP é válido
        import zipfile
        if not zipfile.is_zipfile(str(zip_path)):
            return {"success": False, "error": "Arquivo baixado não é um ZIP válido. Tente novamente."}
        
        # Verifica se contém arquivos esperados
        with zipfile.ZipFile(str(zip_path), 'r') as zf:
            names = zf.namelist()
            has_exe = any('Painel_CRCON.exe' in n for n in names)
            has_internal = any('_internal' in n for n in names)
            print(f"   📦 ZIP contém {len(names)} arquivos, exe={has_exe}, _internal={has_internal}")
            if not has_exe:
                return {"success": False, "error": "ZIP não contém Painel_CRCON.exe. Arquivo incorreto."}
        
        return {
            "success": True,
            "data": {
                "zip_path": str(zip_path),
                "message": "Download completo. Clique em 'Instalar' para aplicar a atualização."
            }
        }
        
    except error.URLError as e:
        print(f"❌ Erro de rede no download: {e}")
        return {"success": False, "error": f"Erro de rede: {e.reason}"}
    except Exception as e:
        print(f"❌ Erro no download: {e}")
        import traceback
        traceback.print_exc()
        return {"success": False, "error": str(e)}


@eel.expose
def install_update(zip_path: str = None):
    """Instala a atualização baixada usando PowerShell com kill por PID."""
    if not getattr(sys, 'frozen', False):
        return {"success": False, "error": "Atualização automática só funciona no executável compilado"}
    
    try:
        if not zip_path:
            zip_path = Path(tempfile.gettempdir()) / "painel_crcon_update" / "update.zip"
        else:
            zip_path = Path(zip_path)
        
        if not zip_path.exists():
            return {"success": False, "error": "Arquivo de atualização não encontrado. Baixe novamente."}
        
        # Diretório atual do executável
        exe_dir = Path(sys.executable).parent
        current_pid = os.getpid()
        
        # Pasta temporária para extração
        temp_dir = Path(tempfile.gettempdir()) / "painel_crcon_update"
        temp_extract = temp_dir / "extracted"
        
        # Cria script PowerShell de atualização (mais robusto que batch)
        updater_script = temp_dir / "updater.ps1"
        
        # Escapa caminhos para PowerShell
        zip_ps = str(zip_path).replace("'", "''")
        extract_ps = str(temp_extract).replace("'", "''")
        exe_dir_ps = str(exe_dir).replace("'", "''")
        
        updater_content = f'''# Updater Script - Painel CRCON
$ErrorActionPreference = "Continue"
$Host.UI.RawUI.WindowTitle = "Atualizando Painel CRCON..."

$zipPath = '{zip_ps}'
$tempExtract = '{extract_ps}'
$exeDir = '{exe_dir_ps}'
$appPID = {current_pid}

Write-Host ""
Write-Host "========================================" -ForegroundColor Cyan
Write-Host "   ATUALIZANDO PAINEL CRCON" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""

# 1. Encerrar o processo do app
Write-Host "[1/5] Encerrando aplicativo (PID: $appPID)..." -ForegroundColor Yellow
try {{
    $proc = Get-Process -Id $appPID -ErrorAction SilentlyContinue
    if ($proc) {{
        Write-Host "   Fechando processo..."
        Stop-Process -Id $appPID -Force -ErrorAction SilentlyContinue
        # Aguarda o processo morrer completamente
        for ($i = 0; $i -lt 15; $i++) {{
            Start-Sleep -Milliseconds 500
            $proc = Get-Process -Id $appPID -ErrorAction SilentlyContinue
            if (-not $proc) {{
                Write-Host "   Processo encerrado!" -ForegroundColor Green
                break
            }}
            Write-Host "   Aguardando... ($i)"
        }}
    }} else {{
        Write-Host "   Processo ja encerrado." -ForegroundColor Green
    }}
}} catch {{
    Write-Host "   Processo ja nao existe." -ForegroundColor Green
}}

# Mata qualquer outro Painel_CRCON.exe que esteja rodando
Get-Process -Name "Painel_CRCON" -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
Start-Sleep -Seconds 2

# 2. Limpar pasta temporaria
Write-Host "[2/5] Preparando extracao..." -ForegroundColor Yellow
if (Test-Path $tempExtract) {{
    Remove-Item $tempExtract -Recurse -Force -ErrorAction SilentlyContinue
}}
New-Item -ItemType Directory -Path $tempExtract -Force | Out-Null

# 3. Extrair ZIP
Write-Host "[3/5] Extraindo atualizacao..." -ForegroundColor Yellow
try {{
    Expand-Archive -LiteralPath $zipPath -DestinationPath $tempExtract -Force
    $fileCount = (Get-ChildItem $tempExtract -Recurse -File).Count
    Write-Host "   Extraidos $fileCount arquivos!" -ForegroundColor Green
}} catch {{
    Write-Host "   ERRO na extracao: $_" -ForegroundColor Red
    Write-Host ""
    Write-Host "Pressione Enter para sair..." -ForegroundColor Red
    Read-Host
    exit 1
}}

# 4. Copiar arquivos
Write-Host "[4/5] Instalando atualizacao..." -ForegroundColor Yellow

# Detecta estrutura do ZIP
$sourceDir = $tempExtract
if (Test-Path "$tempExtract\\Painel_CRCON\\_internal") {{
    $sourceDir = "$tempExtract\\Painel_CRCON"
    Write-Host "   Estrutura: Painel_CRCON/_internal"
}} elseif (Test-Path "$tempExtract\\_internal") {{
    $sourceDir = $tempExtract
    Write-Host "   Estrutura: _internal direto"
}} else {{
    # Tenta encontrar uma subpasta que contenha _internal
    $subDirs = Get-ChildItem $tempExtract -Directory
    $found = $false
    foreach ($dir in $subDirs) {{
        if (Test-Path "$($dir.FullName)\\_internal") {{
            $sourceDir = $dir.FullName
            Write-Host "   Estrutura: $($dir.Name)/_internal"
            $found = $true
            break
        }}
    }}
    if (-not $found) {{
        Write-Host "   ERRO: Estrutura do ZIP nao reconhecida!" -ForegroundColor Red
        Write-Host "   Conteudo:" -ForegroundColor Red
        Get-ChildItem $tempExtract -Recurse -Depth 2 | ForEach-Object {{ Write-Host "   $($_.FullName)" }}
        Write-Host ""
        Write-Host "Pressione Enter para sair..." -ForegroundColor Red
        Read-Host
        exit 1
    }}
}}

try {{
    $errors = 0
    
    # Copia o EXE principal
    $srcExe = Join-Path $sourceDir "Painel_CRCON.exe"
    if (Test-Path $srcExe) {{
        Copy-Item $srcExe (Join-Path $exeDir "Painel_CRCON.exe") -Force
        Write-Host "   OK Painel_CRCON.exe" -ForegroundColor Green
    }}
    
    # Copia _internal (a parte mais pesada)
    $srcInternal = Join-Path $sourceDir "_internal"
    $dstInternal = Join-Path $exeDir "_internal"
    if (Test-Path $srcInternal) {{
        # Remove o _internal antigo primeiro para evitar conflitos
        if (Test-Path $dstInternal) {{
            Remove-Item $dstInternal -Recurse -Force -ErrorAction SilentlyContinue
            Start-Sleep -Milliseconds 500
        }}
        Copy-Item $srcInternal $dstInternal -Recurse -Force
        Write-Host "   OK _internal/" -ForegroundColor Green
    }}
    
    # Copia pastas extras (web, data, maps, etc)
    Get-ChildItem $sourceDir -Directory | Where-Object {{ $_.Name -ne "_internal" }} | ForEach-Object {{
        $dstPath = Join-Path $exeDir $_.Name
        Copy-Item $_.FullName $dstPath -Recurse -Force
        Write-Host "   OK $($_.Name)/" -ForegroundColor Green
    }}
    
    # Copia arquivos soltos (LEIA-ME.txt, version.json, etc)
    Get-ChildItem $sourceDir -File | Where-Object {{ $_.Name -ne "Painel_CRCON.exe" }} | ForEach-Object {{
        Copy-Item $_.FullName (Join-Path $exeDir $_.Name) -Force
        Write-Host "   OK $($_.Name)" -ForegroundColor Green
    }}
    
}} catch {{
    Write-Host "   ERRO ao copiar: $_" -ForegroundColor Red
    Write-Host ""
    Write-Host "Pressione Enter para sair..." -ForegroundColor Red
    Read-Host
    exit 1
}}

# 5. Limpeza
Write-Host "[5/5] Limpando arquivos temporarios..." -ForegroundColor Yellow
Remove-Item $zipPath -Force -ErrorAction SilentlyContinue
Remove-Item $tempExtract -Recurse -Force -ErrorAction SilentlyContinue

Write-Host ""
Write-Host "========================================" -ForegroundColor Green
Write-Host "   ATUALIZACAO CONCLUIDA!" -ForegroundColor Green
Write-Host "========================================" -ForegroundColor Green
Write-Host ""
Write-Host "Iniciando aplicativo em 3 segundos..."
Start-Sleep -Seconds 3

# Reinicia o app
Start-Process -FilePath (Join-Path $exeDir "Painel_CRCON.exe")

Write-Host "Pronto! Esta janela vai fechar em 3 segundos..."
Start-Sleep -Seconds 3
'''
        
        with open(updater_script, 'w', encoding='utf-8') as f:
            f.write(updater_content)
        
        print(f"📦 Script de atualização criado: {updater_script}")
        print(f"📦 PID atual: {current_pid}")
        print(f"📦 Exe dir: {exe_dir}")
        
        # Inicia o script PowerShell em nova janela
        subprocess.Popen(
            [
                'powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass',
                '-File', str(updater_script)
            ],
            creationflags=subprocess.CREATE_NEW_CONSOLE
        )
        
        print("📦 Updater iniciado! Fechando app em 2 segundos...")
        
        # Agenda o fechamento do app após enviar a resposta ao frontend
        def _shutdown():
            time.sleep(2)
            print("👋 Fechando para atualização...")
            os._exit(0)
        
        threading.Thread(target=_shutdown, daemon=True).start()
        
        return {
            "success": True,
            "data": {
                "message": "Atualização iniciada. O aplicativo será reiniciado automaticamente."
            }
        }
        
    except Exception as e:
        print(f"❌ Erro na instalação: {e}")
        return {"success": False, "error": str(e)}


# ==================== AUTENTICAÇÃO ====================
@eel.expose
def login(api_key: str):
    """Faz login com uma API key do CRCON."""
    global _current_session
    
    if not api_key or len(api_key) < 10:
        return {"success": False, "error": "API Key inválida"}
    
    # Testa a API key buscando permissões do usuário
    url = f"{CRCON_URL}/api/get_own_user_permissions"
    headers = {"Authorization": f"Bearer {api_key}"}
    
    print(f"🔐 Testando API Key: {api_key[:8]}...{api_key[-4:]}")
    print(f"🌐 URL: {url}")
    
    try:
        req = request.Request(url, headers=headers)
        with request.urlopen(req, timeout=30) as resp:
            response_body = resp.read().decode("utf-8")
            print(f"✅ Status HTTP: {resp.status}")
            print(f"📦 Resposta: {response_body[:500]}")
            
            data = json.loads(response_body)
            
            if data.get("failed"):
                error_msg = data.get("error", "Erro desconhecido")
                print(f"❌ Erro da API: {error_msg}")
                return {"success": False, "error": f"API retornou erro: {error_msg}"}
            
            result = data.get("result", {})
            
            # Salva sessão
            _current_session = {
                "api_key": api_key,
                "user_name": result.get("user_name", "Usuário"),
                "is_superuser": result.get("is_superuser", False),
                "permissions": result.get("permissions", []),
                "groups": result.get("groups", [])
            }
            
            # Salva credenciais localmente (opcional)
            save_credentials(api_key)
            
            print(f"✅ Login bem-sucedido: {_current_session['user_name']} (Superuser: {_current_session['is_superuser']})")
            print(f"📋 Permissões: {len(_current_session['permissions'])} encontradas")
            
            return {
                "success": True,
                "data": {
                    "user_name": _current_session["user_name"],
                    "is_superuser": _current_session["is_superuser"],
                    "permissions": _current_session["permissions"],
                    "groups": _current_session["groups"]
                }
            }
            
    except error.HTTPError as e:
        error_body = e.read().decode("utf-8", errors="ignore") if e.fp else ""
        print(f"❌ HTTP Error {e.code}: {e.reason}")
        print(f"📄 Corpo do erro: {error_body[:500]}")
        
        if e.code == 401 or e.code == 403:
            return {"success": False, "error": f"API Key inválida ou sem permissão (HTTP {e.code})"}
        return {"success": False, "error": f"Erro HTTP {e.code}: {error_body[:200] if error_body else e.reason}"}
    except Exception as e:
        print(f"❌ Exceção: {type(e).__name__}: {str(e)}")
        return {"success": False, "error": f"Erro de conexão: {str(e)}"}


@eel.expose
def logout():
    """Faz logout do usuário atual."""
    global _current_session
    _current_session = {
        "api_key": None,
        "user_name": None,
        "is_superuser": False,
        "permissions": [],
        "groups": []
    }
    print("🚪 Logout realizado")
    return {"success": True}


@eel.expose
def get_session():
    """Retorna dados da sessão atual."""
    if not _current_session.get("api_key"):
        return {"success": False, "error": "Não autenticado"}
    
    return {
        "success": True,
        "data": {
            "user_name": _current_session["user_name"],
            "is_superuser": _current_session["is_superuser"],
            "permissions": _current_session["permissions"],
            "groups": _current_session["groups"]
        }
    }


@eel.expose
def check_permission(permission: str):
    """Verifica se o usuário tem uma permissão específica."""
    if _current_session.get("is_superuser"):
        return True
    return permission in _current_session.get("permissions", [])


def save_credentials(api_key: str):
    """Salva credenciais localmente para auto-login."""
    try:
        creds_file = DATA_DIR / "credentials.json"
        with open(creds_file, "w") as f:
            json.dump({"api_key": api_key}, f)
    except:
        pass


def load_saved_credentials():
    """Carrega credenciais salvas."""
    try:
        creds_file = DATA_DIR / "credentials.json"
        if creds_file.exists():
            with open(creds_file, "r") as f:
                return json.load(f).get("api_key")
    except:
        pass
    return None


@eel.expose
def try_auto_login():
    """Tenta fazer login automático com credenciais salvas."""
    saved_key = load_saved_credentials()
    if saved_key:
        result = login(saved_key)
        if result.get("success"):
            return result
    return {"success": False, "error": "Sem credenciais salvas"}


# ==================== DASHBOARD ====================
@eel.expose
def get_server_status():
    """Retorna status geral do servidor."""
    return api_get("get_status")


@eel.expose
def get_game_state():
    """Retorna estado da partida atual."""
    return api_get("get_gamestate")


@eel.expose
def get_players():
    """Retorna lista de jogadores online."""
    return api_get("get_players")


@eel.expose
def get_team_view():
    """Retorna visão completa das equipes com dados detalhados dos jogadores."""
    return api_get("get_team_view")


@eel.expose
def search_players(search_term: str = ""):
    """Busca jogadores online pelo nome.
    Usa get_team_view para obter dados detalhados e filtra localmente.
    """
    result = api_get("get_team_view")
    if not result.get("success"):
        return result
    
    data = result.get("data", {})
    players = []
    
    # Processar times (allies e axis)
    for team_name in ["allies", "axis"]:
        team = data.get(team_name, {})
        if not team:
            continue
            
        squads = team.get("squads", {})
        commander = team.get("commander")
        
        # Adicionar commander se existir
        if commander and commander.get("player_id"):
            player_info = {
                "name": commander.get("name", "Unknown"),
                "player_id": commander.get("player_id", ""),
                "team": team_name.capitalize(),
                "squad": "Commander",
                "level": commander.get("level", 0),
                "role": "Commander",
                "is_vip": commander.get("is_vip", False)
            }
            players.append(player_info)
        
        # Processar squads
        for squad_name, squad_data in squads.items():
            if not squad_data:
                continue
            squad_players = squad_data.get("players", [])
            for player in squad_players:
                if not player:
                    continue
                player_info = {
                    "name": player.get("name", "Unknown"),
                    "player_id": player.get("player_id", ""),
                    "team": team_name.capitalize(),
                    "squad": squad_name,
                    "level": player.get("level", 0),
                    "role": player.get("role", ""),
                    "is_vip": player.get("is_vip", False)
                }
                players.append(player_info)
    
    # Filtrar por termo de busca (case insensitive)
    if search_term:
        search_lower = search_term.lower()
        players = [p for p in players if search_lower in p["name"].lower()]
    
    # Ordenar por nome
    players.sort(key=lambda x: x["name"].lower())
    
    return {"success": True, "data": players}


# ==================== AUTOMOD SEEDING ====================
@eel.expose
def get_seeding_config():
    """Carrega configuração do automod seeding."""
    return api_get("get_auto_mod_seeding_config")


@eel.expose
def set_seeding_config(config: dict):
    """Aplica configuração do automod seeding.
    A API exige TODOS os campos, então buscamos a config atual e mesclamos.
    """
    print(f"[DEBUG SEEDING] Recebido config: {list(config.keys())}")
    # Buscar config atual para ter todos os campos
    current = api_get("get_auto_mod_seeding_config")
    if not current.get("success"):
        print(f"[DEBUG SEEDING] Erro ao buscar config atual: {current.get('error')}")
        return current
    
    # Mesclar config atual com as alterações (deep merge)
    full_config = deep_merge(current["data"], config)
    print(f"[DEBUG SEEDING] Enviando para API com {len(full_config)} campos")
    
    result = api_post("set_auto_mod_seeding_config", full_config)
    print(f"[DEBUG SEEDING] Resultado: success={result.get('success')}, error={result.get('error')}")
    return result


# ==================== AUTOMOD LEVEL ====================
@eel.expose
def get_level_config():
    """Carrega configuração do automod de nível."""
    return api_get("get_auto_mod_level_config")


@eel.expose
def set_level_config(config: dict):
    """Aplica configuração do automod de nível.
    A API exige TODOS os campos.
    """
    print(f"[DEBUG LEVEL] Recebido config: {list(config.keys())}")
    current = api_get("get_auto_mod_level_config")
    if not current.get("success"):
        print(f"[DEBUG LEVEL] Erro ao buscar config atual: {current.get('error')}")
        return current
    
    full_config = deep_merge(current["data"], config)
    print(f"[DEBUG LEVEL] Enviando para API com {len(full_config)} campos")
    
    result = api_post("set_auto_mod_level_config", full_config)
    print(f"[DEBUG LEVEL] Resultado: success={result.get('success')}, error={result.get('error')}")
    return result


# ==================== AUTOMOD SOLO TANK ====================
@eel.expose
def get_solo_tank_config():
    """Carrega configuração do automod solo tank."""
    return api_get("get_auto_mod_solo_tank_config")


@eel.expose
def set_solo_tank_config(config: dict):
    """Aplica configuração do automod solo tank.
    A API exige TODOS os campos.
    """
    current = api_get("get_auto_mod_solo_tank_config")
    if not current.get("success"):
        return current
    
    full_config = deep_merge(current["data"], config)
    
    return api_post("set_auto_mod_solo_tank_config", full_config)


# ==================== AUTOMOD NO LEADER ====================
@eel.expose
def get_no_leader_config():
    """Carrega configuração do automod no leader."""
    return api_get("get_auto_mod_no_leader_config")


@eel.expose
def set_no_leader_config(config: dict):
    """Aplica configuração do automod no leader.
    A API exige TODOS os campos.
    """
    current = api_get("get_auto_mod_no_leader_config")
    if not current.get("success"):
        return current
    
    full_config = deep_merge(current["data"], config)
    
    return api_post("set_auto_mod_no_leader_config", full_config)


# ==================== TK BAN ====================
@eel.expose
def get_tk_ban_config():
    """Carrega configuração do TK ban."""
    return api_get("get_tk_ban_on_connect_config")


@eel.expose
def set_tk_ban_config(config: dict):
    """Aplica configuração do TK ban.
    A API exige TODOS os campos.
    """
    current = api_get("get_tk_ban_on_connect_config")
    if not current.get("success"):
        return current
    
    full_config = deep_merge(current["data"], config)
    
    return api_post("set_tk_ban_on_connect_config", full_config)


# ==================== VAC/GAME BANS ====================
@eel.expose
def get_vac_bans_config():
    """Carrega configuração de VAC/Game Bans."""
    return api_get("get_vac_game_bans_config")


@eel.expose
def set_vac_bans_config(config: dict):
    """Aplica configuração de VAC/Game Bans.
    A API exige TODOS os campos.
    """
    current = api_get("get_vac_game_bans_config")
    if not current.get("success"):
        return current
    
    full_config = deep_merge(current["data"], config)
    
    return api_post("set_vac_game_bans_config", full_config)


# ==================== NAME KICKS ====================
@eel.expose
def get_name_kick_config():
    """Carrega configuração de Name Kicks."""
    return api_get("get_name_kick_config")


@eel.expose
def set_name_kick_config(config: dict):
    """Aplica configuração de Name Kicks.
    A API exige TODOS os campos.
    """
    current = api_get("get_name_kick_config")
    if not current.get("success"):
        return current
    
    full_config = deep_merge(current["data"], config)
    
    return api_post("set_name_kick_config", full_config)


# ==================== PLAYER RECORDS ====================
@eel.expose
def get_players_history(page: int = 1, page_size: int = 50, player_name: str = "", 
                        blacklisted: bool = None, is_watched: bool = None,
                        flags: list = None, country: str = None):
    """Busca histórico de jogadores cadastrados no servidor."""
    # Tratar valores vazios como None para a API
    payload = {
        "page": page,
        "page_size": page_size
    }
    
    # Só adicionar filtros se tiverem valor
    # Garantir que caracteres especiais sejam preservados
    if player_name and player_name.strip():
        payload["player_name"] = player_name.strip()
    
    if blacklisted is not None:
        payload["blacklisted"] = blacklisted
    
    if is_watched is not None:
        payload["is_watched"] = is_watched
    
    if flags and len(flags) > 0:
        payload["flags"] = flags
    
    if country and country.strip():
        payload["country"] = country.strip()
    
    print(f"[SEARCH] Buscando jogador: '{player_name}' (caracteres: {len(player_name) if player_name else 0})")
    result = api_post("get_players_history", payload)
    print(f"[SEARCH] Resultado: {result.get('success')}, Total: {result.get('data', {}).get('total', 0) if result.get('success') else 'N/A'}")
    return result


@eel.expose
def get_player_profile(player_id: str):
    """Busca perfil completo de um jogador."""
    return api_get(f"get_player_profile?player_id={player_id}")


@eel.expose
def flag_player(player_id: str, flag: str, comment: str = ""):
    """Adiciona uma flag a um jogador."""
    payload = {
        "player_id": player_id,
        "flag": flag,
        "comment": comment
    }
    return api_post("flag_player", payload)


@eel.expose
def unflag_player(player_id: str, flag_id: int):
    """Remove uma flag de um jogador."""
    return api_post("unflag_player", {"player_id": player_id, "flag_id": flag_id})


@eel.expose
def add_player_watch(player_id: str, reason: str = "", player_name: str = ""):
    """Adiciona jogador à watchlist."""
    payload = {
        "player_id": player_id,
        "reason": reason,
        "player_name": player_name
    }
    return api_post("watch_player", payload)


@eel.expose
def remove_player_watch(player_id: str):
    """Remove jogador da watchlist."""
    return api_post("unwatch_player", {"player_id": player_id})


# ==================== VIPs ====================
@eel.expose
def get_vips():
    """Retorna lista de VIPs."""
    return api_get("get_vip_ids")


@eel.expose
def add_vip(player_id: str, name: str, expiration: str = None):
    """Adiciona VIP."""
    payload = {"player_id": player_id, "description": name}
    if expiration:
        payload["expiration"] = expiration
    return api_post("add_vip", payload)


@eel.expose
def remove_vip(player_id: str):
    """Remove VIP."""
    return api_post("remove_vip", {"player_id": player_id})


@eel.expose
def renew_vip(player_id: str, name: str, current_expiration: str = None):
    """Renova VIP adicionando +1 mês à expiração atual.
    Se não tiver expiração, define para 1 mês a partir de agora.
    """
    from datetime import datetime, timedelta
    from dateutil.relativedelta import relativedelta
    
    try:
        if current_expiration and current_expiration != 'Permanente':
            # Parsear data atual e adicionar 1 mês
            try:
                # Formato ISO: 2024-01-18T00:00:00
                exp_date = datetime.fromisoformat(current_expiration.replace('Z', ''))
            except:
                # Se falhar, tenta outros formatos
                exp_date = datetime.now()
            
            # Adicionar 1 mês
            new_expiration = exp_date + relativedelta(months=1)
        else:
            # Se permanente ou sem data, define 1 mês a partir de agora
            new_expiration = datetime.now() + relativedelta(months=1)
        
        # Formatar para ISO
        expiration_str = new_expiration.strftime('%Y-%m-%dT%H:%M:%S')
        
        # Chamar add_vip que atualiza se já existir
        payload = {
            "player_id": player_id,
            "description": name,
            "expiration": expiration_str
        }
        result = api_post("add_vip", payload)
        
        if result.get("success"):
            return {
                "success": True,
                "message": f"VIP renovado até {new_expiration.strftime('%d/%m/%Y')}",
                "new_expiration": expiration_str
            }
        return result
        
    except Exception as e:
        return {"success": False, "error": f"Erro ao renovar: {str(e)}"}


# ==================== ADMINS ====================
@eel.expose
def get_admins():
    """Retorna lista de admins."""
    return api_get("get_admin_ids")


# ==================== BANS ====================
@eel.expose
def get_bans():
    """Retorna lista de bans ativos."""
    return api_get("get_bans")


# ==================== MAPAS ====================
@eel.expose
def get_maps():
    """Retorna todos os mapas disponíveis."""
    return api_get("get_maps")


@eel.expose
def get_map_rotation():
    """Retorna rotação atual de mapas."""
    return api_get("get_map_rotation")


@eel.expose
def set_map_rotation(map_ids: list):
    """Define rotação de mapas."""
    return api_post("set_map_rotation", {"map_ids": map_ids})


# ==================== CONFIGURAÇÕES SERVIDOR ====================
@eel.expose
def get_server_settings():
    """Retorna configurações do servidor."""
    return api_get("get_server_settings")


@eel.expose
def set_team_switch_cooldown(minutes: int):
    """Define cooldown de troca de time (minutos). 0 = desativado."""
    return api_post("set_team_switch_cooldown", {"minutes": minutes})


@eel.expose
def set_autobalance_enabled(value: bool):
    """Ativa/desativa autobalance."""
    return api_post("set_autobalance_enabled", {"value": value})


@eel.expose
def set_autobalance_threshold(max_diff: int):
    """Define diferença máxima entre times para autobalance."""
    return api_post("set_autobalance_threshold", {"max_diff": max_diff})


@eel.expose
def set_idle_autokick_time(minutes: int):
    """Define tempo de inatividade para kick (minutos). 0 = desativado."""
    return api_post("set_idle_autokick_time", {"minutes": minutes})


@eel.expose
def set_max_ping_autokick(max_ms: int):
    """Define ping máximo para kick automático (ms). 0 = desativado."""
    return api_post("set_max_ping_autokick", {"max_ms": max_ms})


@eel.expose
def set_votekick_enabled(value: bool):
    """Ativa/desativa votekick."""
    return api_post("set_votekick_enabled", {"value": value})


@eel.expose
def set_queue_length(value: int):
    """Define tamanho máximo da fila."""
    return api_post("set_queue_length", {"value": value})


@eel.expose
def set_vip_slots_num(value: int):
    """Define número de slots reservados para VIP."""
    return api_post("set_vip_slots_num", {"value": value})


@eel.expose
def get_welcome_message():
    """Retorna mensagem de boas-vindas."""
    return api_get("get_welcome_message")


@eel.expose
def set_welcome_message(message: str):
    """Define mensagem de boas-vindas."""
    return api_post("set_welcome_message", {"message": message})


# ==================== AUTO BROADCAST ====================
@eel.expose
def get_auto_broadcast_config():
    """Retorna configuração de auto broadcast."""
    return api_get("get_auto_broadcasts_config")


@eel.expose
def set_auto_broadcast_config(config: dict):
    """Aplica configuração de auto broadcast.
    A API exige TODOS os campos.
    """
    current = api_get("get_auto_broadcasts_config")
    if not current.get("success"):
        return current
    
    full_config = deep_merge(current["data"], config)
    
    return api_post("set_auto_broadcasts_config", full_config)


# ==================== PROFANITY FILTER ====================
@eel.expose
def get_profanity_config():
    """Retorna configuração do filtro de profanidade."""
    return api_get("get_profanities")


@eel.expose
def set_profanity_config(words: list):
    """Aplica configuração do filtro de profanidade."""
    return api_post("set_profanities", {"profanities": words})


@eel.expose
def get_votemap_config():
    """Retorna configuração de votemap."""
    return api_get("get_votemap_config")


@eel.expose
def set_votemap_config(config: dict):
    """Aplica configuração de votemap.
    A API exige TODOS os campos.
    """
    current = api_get("get_votemap_config")
    if not current.get("success"):
        return current
    
    full_config = deep_merge(current["data"], config)
    
    return api_post("set_votemap_config", full_config)


@eel.expose
def get_votemap_whitelist():
    """Retorna a whitelist de mapas do votemap (endpoint separado)."""
    return api_get("get_votemap_whitelist")


@eel.expose
def set_votemap_whitelist(map_names: list):
    """Define a whitelist de mapas do votemap."""
    return api_post("set_votemap_whitelist", {"map_names": map_names})


@eel.expose 
def add_map_to_votemap_whitelist(map_name: str):
    """Adiciona um mapa à whitelist do votemap."""
    return api_post("add_map_to_votemap_whitelist", {"map_name": map_name})


@eel.expose
def remove_map_from_votemap_whitelist(map_name: str):
    """Remove um mapa da whitelist do votemap."""
    return api_post("remove_map_from_votemap_whitelist", {"map_name": map_name})


# ==================== BLACKLISTS ====================
@eel.expose
def get_blacklists():
    """Retorna lista de blacklists."""
    return api_get("get_blacklists")


# ==================== AÇÕES RÁPIDAS ====================
@eel.expose
def kick_player(player_id: str, reason: str):
    """Expulsa jogador."""
    return api_post("kick", {"player_id": player_id, "reason": reason})


@eel.expose
def punish_player(player_id: str, reason: str):
    """Pune jogador."""
    return api_post("punish", {"player_id": player_id, "reason": reason})


@eel.expose
def temp_ban_player(player_id: str, reason: str, duration_hours: int):
    """Bane jogador temporariamente."""
    return api_post("temp_ban", {
        "player_id": player_id,
        "reason": reason,
        "duration_hours": duration_hours
    })


@eel.expose
def perma_ban_player(player_id: str, reason: str):
    """Bane jogador permanentemente."""
    return api_post("perma_ban", {
        "player_id": player_id,
        "reason": reason
    })


@eel.expose
def unban_player(player_id: str):
    """Remove ban de um jogador."""
    return api_post("unban", {"player_id": player_id})


@eel.expose
def message_player(player_id: str, message: str):
    """Envia mensagem para jogador."""
    return api_post("message_player", {"player_id": player_id, "message": message})


@eel.expose
def broadcast_message(message: str):
    """Envia mensagem para todos."""
    return api_post("set_broadcast", {"message": message})


@eel.expose
def send_admin_message(player_id: str, message: str):
    """Envia mensagem de admin para jogador (requer confirmação com Y)."""
    # CRCON usa message_player - parâmetro: player_id (nome ou steam_id)
    result = api_post("message_player", {
        "player_id": player_id,
        "message": message
    })
    return result


@eel.expose
def send_admin_message_all(message: str):
    """Envia mensagem de admin para TODOS os jogadores do servidor."""
    try:
        # Busca jogadores via get_team_view
        team_view = api_get("get_team_view")
        if not team_view.get("success"):
            return {"success": False, "error": "Não foi possível obter lista de jogadores"}
        
        data = team_view.get("data", {}) or team_view.get("result", {})
        
        # Extrai jogadores de ambos os times
        players = []
        for team_key in ["allies", "axis"]:
            team = data.get(team_key, {})
            if isinstance(team, dict):
                squads = team.get("squads", {})
                if isinstance(squads, dict):
                    for squad_name, squad_data in squads.items():
                        if isinstance(squad_data, dict):
                            squad_players = squad_data.get("players", [])
                            players.extend(squad_players)
                # Jogadores sem squad (commander)
                commander = team.get("commander")
                if commander:
                    players.append(commander)
        
        if not players:
            return {"success": False, "error": "Nenhum jogador online no servidor"}
        
        # Envia mensagem para cada jogador
        success_count = 0
        errors = []
        
        for player in players:
            # Usa steam_id_64 ou player_id ou name
            player_identifier = player.get("steam_id_64") or player.get("player_id") or player.get("name")
            if player_identifier:
                result = api_post("message_player", {
                    "player_id": player_identifier,
                    "message": message
                })
                if result.get("success"):
                    success_count += 1
                else:
                    errors.append(player.get("name", str(player_identifier)))
        
        if success_count > 0:
            return {"success": True, "count": success_count, "errors": errors}
        else:
            return {"success": False, "error": f"Falha ao enviar para todos os jogadores"}
    except Exception as e:
        return {"success": False, "error": str(e)}


# ==================== STORAGE CENTRALIZADO NO CRCON ====================
# As configs do painel (match_avisos, aviso_por_minuto) são armazenadas
# no Auto Broadcast do CRCON como entradas especiais com prefixo __PAINEL_CFG_.
# Isso garante que TODOS os painéis vejam as mesmas configurações.
PAINEL_CFG_PREFIX = "__PAINEL_CFG_"

def _get_painel_config_from_crcon(config_key: str) -> dict:
    """Lê uma configuração do painel armazenada no Auto Broadcast do CRCON."""
    try:
        result = api_get("get_auto_broadcasts_config")
        if not result.get("success"):
            return {}
        
        broadcasts = result.get("data", {})
        messages = broadcasts.get("messages", [])
        
        tag = f"{PAINEL_CFG_PREFIX}{config_key}:"
        for msg in messages:
            text = msg.get("message", "") if isinstance(msg, dict) else str(msg)
            if text.startswith(tag):
                json_str = text[len(tag):]
                return json.loads(json_str)
        return {}
    except Exception as e:
        print(f"[CRCON Storage] Erro ao ler {config_key}: {e}")
        return {}


def _save_painel_config_to_crcon(config_key: str, config_data: dict) -> bool:
    """Salva uma configuração do painel no Auto Broadcast do CRCON."""
    try:
        result = api_get("get_auto_broadcasts_config")
        if not result.get("success"):
            print(f"[CRCON Storage] Erro ao ler broadcasts atuais: {result.get('error')}")
            return False
        
        broadcasts = result.get("data", {})
        messages = broadcasts.get("messages", [])
        
        tag = f"{PAINEL_CFG_PREFIX}{config_key}:"
        json_str = json.dumps(config_data, ensure_ascii=False)
        new_entry = {"time_sec": 1, "message": f"{tag}{json_str}"}
        
        # Remove entrada antiga com mesmo config_key
        new_messages = []
        for msg in messages:
            text = msg.get("message", "") if isinstance(msg, dict) else str(msg)
            if not text.startswith(tag):
                new_messages.append(msg)
        
        # Adiciona nova entrada no final
        new_messages.append(new_entry)
        
        # Salva no CRCON
        broadcasts["messages"] = new_messages
        save_result = api_post("set_auto_broadcasts_config", broadcasts)
        
        if save_result.get("success"):
            print(f"[CRCON Storage] ✅ Config '{config_key}' salva no servidor")
            return True
        else:
            print(f"[CRCON Storage] ❌ Erro ao salvar: {save_result.get('error')}")
            return False
    except Exception as e:
        print(f"[CRCON Storage] ❌ Exceção ao salvar {config_key}: {e}")
        return False


# ==================== AVISOS DE PARTIDA (Announcement System) ====================
MATCH_AVISOS_FILE = DATA_DIR / "match_avisos.json"

# Mensagens padrão para anúncios
DEFAULT_START_MESSAGE = """Não pegue filas e tenha prioridade no nosso servidor.
por apenas R$10,00 no Mês
Seja bem vindo Soldado, No front estaremos lado a lado.
para ser da Família 2RB abra um Ticket no Nosso discord https://discord.gg/XuUJj7M4qY."""

DEFAULT_END_MESSAGE = """A guerra te chama soldado.
precisamos de você, se aliste agora na Segunda Resistencia Brasileira.
Abra um Ticket no Nosso Discord e aguarde ser aprovado!."""

def load_match_avisos_config():
    """Carrega configuração de avisos de partida do servidor CRCON (centralizado).
    Fallback para arquivo local se o servidor não tiver a config.
    """
    default_config = {
        "enabled": False,
        "startAfterSec": 300,
        "endBeforeSec": 300,
        "startMessage": DEFAULT_START_MESSAGE,
        "endMessage": DEFAULT_END_MESSAGE
    }
    
    # 1. Tenta carregar do servidor CRCON (centralizado)
    try:
        server_config = _get_painel_config_from_crcon("match_avisos")
        if server_config:
            merged = {**default_config, **server_config}
            print(f"[DEBUG] Match avisos carregado do SERVIDOR CRCON")
            print(f"[DEBUG] Start message length: {len(merged.get('startMessage', ''))}")
            return merged
    except Exception as e:
        print(f"[DEBUG] Erro ao carregar do CRCON, usando local: {e}")
    
    # 2. Fallback: arquivo local (migração)
    if MATCH_AVISOS_FILE.exists():
        try:
            with open(MATCH_AVISOS_FILE, "r", encoding="utf-8") as f:
                config = json.load(f)
                print(f"[DEBUG] Arquivo LOCAL carregado: {config.keys()}")
                merged = {**default_config, **config}
                print(f"[DEBUG] Start message length: {len(merged.get('startMessage', ''))}")
                
                # Migra config local para o servidor
                print(f"[DEBUG] Migrando config local para servidor CRCON...")
                _save_painel_config_to_crcon("match_avisos", merged)
                
                return merged
        except Exception as e:
            print(f"[ERROR] Erro ao carregar arquivo local: {e}")
    
    return default_config


@eel.expose
def get_match_avisos_config():
    """Retorna configuração de avisos de partida (do servidor CRCON)."""
    try:
        config = load_match_avisos_config()
        return {"success": True, "data": config}
    except Exception as e:
        return {"success": False, "error": str(e)}


@eel.expose
def set_match_avisos_config(config: dict):
    """Salva configuração de avisos de partida no servidor CRCON (centralizado).
    Todos os campos são editáveis incluindo as mensagens.
    """
    try:
        print("[BACKEND] Recebido config do frontend:")
        print(f"  Keys recebidas: {config.keys()}")
        print(f"  startMessage length: {len(config.get('startMessage', ''))}")
        
        start_msg = config.get("startMessage")
        if start_msg is None:
            start_msg = DEFAULT_START_MESSAGE
        
        end_msg = config.get("endMessage")
        if end_msg is None:
            end_msg = DEFAULT_END_MESSAGE
            
        save_config = {
            "enabled": config.get("enabled", False),
            "startAfterSec": config.get("startAfterSec", 300),
            "endBeforeSec": _FINAL_MATCH_ALERT_SEC,
            "startMessage": start_msg,
            "endMessage": end_msg,
            "updatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ")
        }
        
        # Salva no servidor CRCON (centralizado para todos os painéis)
        saved_to_server = _save_painel_config_to_crcon("match_avisos", save_config)
        
        # Salva também localmente como backup
        try:
            with open(MATCH_AVISOS_FILE, "w", encoding="utf-8") as f:
                json.dump(save_config, f, ensure_ascii=False, indent=2)
        except:
            pass
        
        if saved_to_server:
            print(f"[BACKEND] ✅ Config salva no servidor CRCON (sincronizado com outros painéis)")
        else:
            print(f"[BACKEND] ⚠️ Salvo apenas localmente (servidor indisponível)")
        
        return {"success": True, "data": save_config}
    except Exception as e:
        print(f"[BACKEND ERROR] Exceção capturada: {e}")
        import traceback
        traceback.print_exc()
        return {"success": False, "error": str(e)}


@eel.expose
def test_announcement(msg_type: str):
    """Testa envio de anúncio imediatamente."""
    try:
        config = load_match_avisos_config()
        message = config.get("startMessage") if msg_type == "start" else config.get("endMessage")
        result = send_admin_message_all(message)
        if result.get("success"):
            print(f"📢 [TESTE] Mensagem de {'INÍCIO' if msg_type == 'start' else 'FIM'} enviada")
        return result
    except Exception as e:
        return {"success": False, "error": str(e)}


# ==================== AVISO POR MINUTO ====================
AVISO_POR_MINUTO_FILE = DATA_DIR / "aviso_por_minuto.json"
_aviso_por_minuto_state = {
    "last_sent": None,
    "thread_running": False
}

def load_aviso_por_minuto_config():
    """Carrega configuração do Aviso por Minuto do servidor CRCON (centralizado).
    Fallback para arquivo local se o servidor não tiver a config.
    """
    default_config = {
        "enabled": False,
        "interval_minutes": 5,
        "message": ""
    }
    
    # 1. Tenta carregar do servidor CRCON
    try:
        server_config = _get_painel_config_from_crcon("aviso_por_minuto")
        if server_config:
            merged = {**default_config, **server_config}
            print(f"[DEBUG] Aviso por minuto carregado do SERVIDOR CRCON")
            return merged
    except Exception as e:
        print(f"[DEBUG] Erro ao carregar do CRCON, usando local: {e}")
    
    # 2. Fallback: arquivo local
    if AVISO_POR_MINUTO_FILE.exists():
        try:
            with open(AVISO_POR_MINUTO_FILE, "r", encoding="utf-8") as f:
                saved = json.load(f)
                merged = {**default_config, **saved}
                
                # Migra config local para o servidor
                print(f"[DEBUG] Migrando aviso_por_minuto local para servidor CRCON...")
                _save_painel_config_to_crcon("aviso_por_minuto", merged)
                
                return merged
        except:
            pass
    
    return default_config

@eel.expose
def get_aviso_por_minuto_config():
    """Retorna configuração do Aviso por Minuto (do servidor CRCON)."""
    try:
        config = load_aviso_por_minuto_config()
        return {"success": True, "data": config}
    except Exception as e:
        return {"success": False, "error": str(e)}

@eel.expose
def set_aviso_por_minuto_config(config: dict):
    """Salva configuração do Aviso por Minuto no servidor CRCON (centralizado)."""
    try:
        # Salva no servidor CRCON
        saved_to_server = _save_painel_config_to_crcon("aviso_por_minuto", config)
        
        # Salva também localmente como backup
        try:
            with open(AVISO_POR_MINUTO_FILE, "w", encoding="utf-8") as f:
                json.dump(config, f, indent=2, ensure_ascii=False)
        except:
            pass
        
        if saved_to_server:
            print(f"[AVISO POR MINUTO] ✅ Config salva no servidor CRCON")
        else:
            print(f"[AVISO POR MINUTO] ⚠️ Salvo apenas localmente")
        
        # Reinicia o thread se necessário
        if config.get("enabled"):
            start_aviso_por_minuto_thread()
        
        return {"success": True, "data": config}
    except Exception as e:
        return {"success": False, "error": str(e)}

def aviso_por_minuto_monitor():
    """Thread que monitora e envia avisos periódicos."""
    global _aviso_por_minuto_state
    
    while True:
        try:
            config = load_aviso_por_minuto_config()
            
            if not config.get("enabled"):
                time.sleep(30)
                continue
            
            interval_seconds = config.get("interval_minutes", 5) * 60
            message = config.get("message", "")
            
            if not message.strip():
                time.sleep(30)
                continue
            
            now = time.time()
            last_sent = _aviso_por_minuto_state.get("last_sent")
            
            if last_sent is None or (now - last_sent) >= interval_seconds:
                result = send_admin_message_all(message)
                if result.get("success"):
                    _aviso_por_minuto_state["last_sent"] = now
                    print(f"⏰ [AVISO POR MINUTO] Enviado: {message[:50]}...")
                else:
                    print(f"⚠️ [AVISO POR MINUTO] Erro ao enviar: {result.get('error')}")
            
            time.sleep(30)
            
        except Exception as e:
            print(f"❌ [AVISO POR MINUTO] Erro: {e}")
            time.sleep(60)

def start_aviso_por_minuto_thread():
    """Inicia thread de aviso por minuto se ainda não estiver rodando."""
    global _aviso_por_minuto_state
    
    if not _aviso_por_minuto_state.get("thread_running"):
        _aviso_por_minuto_state["thread_running"] = True
        thread = threading.Thread(target=aviso_por_minuto_monitor, daemon=True)
        thread.start()
        print("🔄 [AVISO POR MINUTO] Thread iniciada")


# ==================== MAPS (Extended) ====================
@eel.expose
def set_map(map_id: str):
    """Muda o mapa atual do servidor."""
    return api_post("set_map", {"map_name": map_id})


@eel.expose
def add_map_to_rotation(map_id: str):
    """Adiciona um mapa à rotação."""
    return api_post("add_map_to_rotation", {"map_name": map_id})


@eel.expose
def remove_map_from_rotation(map_name: str):
    """Remove um mapa da rotação pelo nome."""
    return api_post("remove_map_from_rotation", {"map_name": map_name})


@eel.expose
def shuffle_map_rotation():
    """Embaralha a rotação de mapas."""
    return api_post("shuffle_map_rotation", {})


@eel.expose
def clear_map_rotation():
    """Limpa toda a rotação de mapas."""
    try:
        rotation = api_get("get_map_rotation")
        if rotation.get("success") and rotation.get("data"):
            for m in rotation["data"]:
                # Pega o ID do mapa (pode ser string ou objeto)
                map_name = m if isinstance(m, str) else (m.get("id") or m.get("map_name") or m.get("name", ""))
                if map_name:
                    api_post("remove_map_from_rotation", {"map_name": map_name})
        return {"success": True}
    except Exception as e:
        return {"success": False, "error": str(e)}


# ==================== VOTEMAP (Extended) ====================
@eel.expose
def set_votemap_enabled(enabled: bool):
    """Ativa/desativa o votemap."""
    return api_post("set_votemap_enabled", {"enabled": enabled})


@eel.expose
def get_votemap_status():
    """Retorna status atual da votação."""
    return api_get("get_votemap_status")


@eel.expose
def reset_votemap():
    """Reseta os votos do votemap."""
    return api_post("reset_votemap", {})


# ==================== HOT RELOAD ====================
@eel.expose
def check_file_changes():
    """Verifica se arquivos web foram modificados para hot reload."""
    global _file_mtimes
    changed = False
    
    web_path = Path(WEB_DIR)
    for ext in ['*.html', '*.css', '*.js']:
        for f in web_path.glob(ext):
            mtime = f.stat().st_mtime
            if str(f) in _file_mtimes:
                if mtime > _file_mtimes[str(f)]:
                    changed = True
                    print(f"🔄 Arquivo alterado: {f.name}")
            _file_mtimes[str(f)] = mtime
    
    return changed


# ==================== MONITOR DE AVISOS DE PARTIDA ====================
# Janela de tolerância para envio de avisos (segundos).
# Se o tempo decorrido ultrapassar o threshold + janela, o aviso é ignorado
# (evita re-envio ao reiniciar o app no meio da partida).
_ANNOUNCEMENT_WINDOW_SEC = 300  # 5 minutos de janela
_FINAL_MATCH_ALERT_SEC = 300  # 5 minutos para fim da partida


def _safe_float(value) -> float:
    try:
        if value is None:
            return 0.0
        return float(value)
    except Exception:
        return 0.0


def _format_stat_value(value) -> str:
    numeric = _safe_float(value)
    if abs(numeric - int(numeric)) < 1e-9:
        return str(int(numeric))
    return f"{numeric:.1f}"


def _short_player_name(name, max_len=14) -> str:
    text = str(name or "N/A").strip()
    if not text:
        text = "N/A"
    if len(text) <= max_len:
        return text
    return text[: max_len - 1] + "…"


def _top_players_for_metric(player_stats, metric_key: str, top_n=5):
    ranking = []
    for row in player_stats or []:
        if not isinstance(row, dict):
            continue

        ranking.append({
            "name": row.get("player") or row.get("name") or row.get("player_name") or "N/A",
            "value": _safe_float(row.get(metric_key, 0)),
            "kills": _safe_float(row.get("kills", 0)),
        })

    ranking.sort(key=lambda item: (item["value"], item["kills"]), reverse=True)
    return ranking[:top_n]


def _build_metric_top5_segment(player_stats, metric_label: str, metric_key: str) -> str:
    top_rows = _top_players_for_metric(player_stats, metric_key, top_n=5)
    if not top_rows:
        return f"{metric_label}\nSem dados"

    parts = [metric_label]
    for index, item in enumerate(top_rows, 1):
        name = _short_player_name(item["name"], max_len=18)
        value = _format_stat_value(item["value"])
        parts.append(f"{index}. {name} ({value})")

    return "\n".join(parts)


def _get_latest_finished_map_scoreboard():
    """Retorna scoreboard da última partida finalizada (/stats/games/{id})."""
    recent = api_get("get_scoreboard_maps?page=1&page_size=1")
    if not recent.get("success"):
        return {"success": False, "error": recent.get("error", "Falha ao consultar partidas finalizadas")}

    payload = recent.get("data") or {}
    maps = payload.get("maps") if isinstance(payload, dict) else None
    if not isinstance(maps, list) or not maps:
        return {"success": False, "error": "Nenhuma partida finalizada encontrada"}

    latest = maps[0]
    map_id = latest.get("id")
    if map_id is None:
        return {"success": False, "error": "Partida sem map_id"}

    details = api_get(f"get_map_scoreboard?map_id={map_id}")
    if not details.get("success"):
        return {"success": False, "error": details.get("error", "Falha ao obter scoreboard da partida")}

    data = details.get("data") or {}
    map_info = data.get("map") or latest.get("map") or {}
    result_info = data.get("result") or latest.get("result") or {}

    return {
        "success": True,
        "data": {
            "map_id": str(map_id),
            "map_name": map_info.get("pretty_name") or str(map_id),
            "allied": result_info.get("allied"),
            "axis": result_info.get("axis"),
            "player_stats": data.get("player_stats") or [],
        },
    }


def _compose_finished_match_top5_messages(scoreboard_data: dict):
    player_stats = scoreboard_data.get("player_stats") or []
    map_id = str(scoreboard_data.get("map_id") or "")
    map_name = _short_player_name(scoreboard_data.get("map_name"), max_len=28)

    allied_score = scoreboard_data.get("allied")
    axis_score = scoreboard_data.get("axis")
    score_label = ""
    if allied_score is not None and axis_score is not None:
        score_label = f"A{allied_score}xE{axis_score}"

    seg_k = _build_metric_top5_segment(player_stats, "ABATES", "kills")
    seg_ks = _build_metric_top5_segment(player_stats, "SEQUÊNCIA DE ABATES", "kills_streak")
    seg_c = _build_metric_top5_segment(player_stats, "COMBATE", "combat")
    seg_s = _build_metric_top5_segment(player_stats, "SUPORTE", "support")
    seg_o = _build_metric_top5_segment(player_stats, "OFENSIVA", "offense")
    seg_d = _build_metric_top5_segment(player_stats, "DEFESA", "defense")

    header_line = f"MAPA: {map_name}"
    if score_label:
        header_line += f" | PLACAR: {score_label}"

    formatted_message = "\n\n".join([
        "📊 ESTATÍSTICAS DA PARTIDA",
        header_line,
        "TOP 5 POR CATEGORIA",
        seg_k,
        seg_ks,
        seg_c,
        seg_s,
        seg_o,
        seg_d,
        "Aperte Y para confirmar",
    ])

    messages = [formatted_message]

    return {
        "map_id": map_id,
        "map_name": scoreboard_data.get("map_name") or map_name,
        "score": score_label,
        "messages": messages,
    }


def _send_composed_match_stats_messages(composed: dict, update_last_map_state=True, origin="auto"):
    global _match_avisos_state

    map_id = str(composed.get("map_id") or "")
    if not map_id:
        return {"success": False, "error": "Partida sem map_id"}

    messages = [str(msg or "").strip() for msg in (composed.get("messages") or []) if str(msg or "").strip()]
    if not messages:
        return {"success": False, "error": "Sem mensagens para enviar"}

    send_reports = []
    total_players = 0

    for idx, msg in enumerate(messages, 1):
        result = send_admin_message_all(msg)
        sent_count = int(result.get("count") or 0) if result.get("success") else 0
        failed_players = result.get("errors", []) if isinstance(result.get("errors"), list) else []
        total_players = max(total_players, sent_count)

        send_reports.append({
            "order": idx,
            "message": msg,
            "success": bool(result.get("success")),
            "sent_count": sent_count,
            "error": result.get("error"),
            "failed_count": len(failed_players),
        })

        if not result.get("success"):
            _append_match_stats_send_log({
                "id": int(time.time() * 1000),
                "sent_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "origin": origin,
                "map_id": map_id,
                "map_name": composed.get("map_name"),
                "score": composed.get("score", ""),
                "status": "failed",
                "players_targeted": total_players,
                "confirmations_supported": False,
                "confirmed_players": [],
                "messages": messages,
                "send_reports": send_reports,
            })
            return {"success": False, "error": result.get("error") or "Falha ao enviar mensagem"}

    if update_last_map_state:
        _match_avisos_state["last_stats_map_id"] = map_id
        _save_match_avisos_state()

    _append_match_stats_send_log({
        "id": int(time.time() * 1000),
        "sent_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "origin": origin,
        "map_id": map_id,
        "map_name": composed.get("map_name"),
        "score": composed.get("score", ""),
        "status": "success",
        "players_targeted": total_players,
        "confirmations_supported": False,
        "confirmed_players": [],
        "messages": messages,
        "send_reports": send_reports,
    })

    return {
        "success": True,
        "map_id": map_id,
        "players_targeted": total_players,
        "messages": messages,
    }


def _send_finished_match_top5_stats_once():
    """Envia TOP5 da partida recém-finalizada com os mesmos grupos do /stats/games/{id}."""
    global _match_avisos_state

    latest = _get_latest_finished_map_scoreboard()
    if not latest.get("success"):
        print(f"⚠️ [Announcement] Falha ao obter scoreboard final: {latest.get('error')}")
        return False

    composed = _compose_finished_match_top5_messages(latest.get("data") or {})
    map_id = composed.get("map_id") or ""
    if not map_id:
        return False

    if str(_match_avisos_state.get("last_stats_map_id") or "") == map_id:
        return False

    send_result = _send_composed_match_stats_messages(composed, update_last_map_state=True, origin="auto")
    if not send_result.get("success"):
        print(f"⚠️ [Announcement] Falha ao enviar estatísticas finais: {send_result.get('error')}")
        return False

    print(f"✅ [Announcement] Estatísticas TOP5 enviadas da partida #{map_id}")
    return True


def _get_cached_finished_match_preview(force_refresh=False):
    default_preview = {
        "available": False,
        "map_id": None,
        "map_name": None,
        "score": "",
        "messages": [],
    }

    try:
        now = time.time()
        with _stats_monitor_preview_lock:
            cached_at = float(_stats_monitor_preview_cache.get("at") or 0.0)
            cached_value = _stats_monitor_preview_cache.get("value")
            if (
                not force_refresh
                and isinstance(cached_value, dict)
                and (now - cached_at) < _STATS_MONITOR_PREVIEW_CACHE_SEC
            ):
                return cached_value

        latest = _get_latest_finished_map_scoreboard()
        preview = default_preview
        if latest.get("success"):
            composed = _compose_finished_match_top5_messages(latest.get("data") or {})
            preview = {
                "available": True,
                "map_id": composed.get("map_id"),
                "map_name": composed.get("map_name"),
                "score": composed.get("score", ""),
                "messages": composed.get("messages", []),
            }

        with _stats_monitor_preview_lock:
            _stats_monitor_preview_cache["at"] = now
            _stats_monitor_preview_cache["value"] = preview

        return preview
    except Exception:
        return default_preview


@eel.expose
def get_match_stats_monitor_data(limit=40, force_refresh=False):
    """Retorna preview do formato e log de envios das estatísticas de partida."""
    try:
        preview = _get_cached_finished_match_preview(bool(force_refresh))

        logs = _get_match_stats_send_logs(limit)

        return {
            "success": True,
            "data": {
                "preview": preview,
                "logs": logs,
                "confirmation": {
                    "supported": False,
                    "note": "API do CRCON não expõe quem confirmou com Y. O painel mostra apenas log de envio.",
                },
            },
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


@eel.expose
def send_match_stats_test_now():
    """Envia manualmente a mensagem de estatística da última partida finalizada."""
    try:
        latest = _get_latest_finished_map_scoreboard()
        if not latest.get("success"):
            return {"success": False, "error": latest.get("error") or "Falha ao carregar partida finalizada"}

        composed = _compose_finished_match_top5_messages(latest.get("data") or {})
        if not str(composed.get("map_id") or ""):
            return {"success": False, "error": "Partida sem map_id"}

        send_result = _send_composed_match_stats_messages(composed, update_last_map_state=False, origin="test")
        if not send_result.get("success"):
            return send_result

        return {
            "success": True,
            "data": {
                "map_id": composed.get("map_id"),
                "map_name": composed.get("map_name"),
                "score": composed.get("score", ""),
                "players_targeted": send_result.get("players_targeted", 0),
                "messages": composed.get("messages", []),
            },
        }
    except Exception as e:
        return {"success": False, "error": str(e)}

def check_match_avisos():
    """Verifica e envia avisos de início/fim de partida automaticamente.
    
    Lógica conforme documentação Bot Discord CRCON:
    - Início: X segundos APÓS início da partida (com janela de tolerância)
    - Fim: X segundos ANTES do fim OU iminência de vitória (4 pontos)
    """
    global _match_avisos_state
    
    try:
        # Carrega configuração
        config = load_match_avisos_config()

        # Busca estado do jogo
        game_state = api_get("get_gamestate")
        if not game_state.get("success"):
            return
        
        data = game_state.get("data", {})
        current_map = str(data.get("current_map", {}).get("id", "") or "")
        if not current_map:
            return

        time_remaining = data.get("time_remaining", 0)  # segundos restantes
        
        # Captura placar para detectar iminência de vitória
        allied_score = data.get("allied_score", 0)
        axis_score = data.get("axis_score", 0)
        
        # Detecta troca de mapa (nova partida)
        if current_map != _match_avisos_state["last_map"]:
            previous_map = _match_avisos_state.get("last_map")
            if previous_map:
                _match_avisos_state["pending_stats_send"] = True

            print(f"🗺️ [Announcement] Nova partida detectada: {current_map}")
            _match_avisos_state["last_map"] = current_map
            _match_avisos_state["start_sent"] = False
            _match_avisos_state["end_sent"] = False
            _match_avisos_state["match_start_time"] = time.time()
            # Estima duração da partida (warfare = 90min, offensive = 60min)
            _match_avisos_state["match_duration"] = 5400 if "warfare" in current_map.lower() else 3600
            _save_match_avisos_state()
            
            if config.get("enabled"):
                start_after = config.get("startAfterSec", 300)
                end_before = _FINAL_MATCH_ALERT_SEC
                print(f"⏰ [Announcement] Mensagem de início agendada para {start_after}s")
                print(f"⏰ [Announcement] Mensagem de fim agendada para {end_before}s antes do fim")

        # Estatísticas de fim: tentativa com retry após virada de mapa
        if _match_avisos_state.get("pending_stats_send"):
            stats_sent = _send_finished_match_top5_stats_once()
            if stats_sent:
                _match_avisos_state["pending_stats_send"] = False
                _save_match_avisos_state()
            else:
                print("⏳ [Announcement] Aguardando dados finais para registrar estatísticas/log da partida.")

        # Sistema de anúncios pode ficar desligado sem bloquear envio/log de estatística
        if not config.get("enabled"):
            return
        
        # Se ambos já foram enviados, apenas log resumido a cada 5 min
        if _match_avisos_state["start_sent"] and _match_avisos_state["end_sent"]:
            # Log silencioso - só imprime a cada ~5 minutos (10 ciclos de 30s)
            cycle = getattr(check_match_avisos, '_quiet_cycle', 0) + 1
            check_match_avisos._quiet_cycle = cycle
            if cycle >= 10:
                check_match_avisos._quiet_cycle = 0
                match_duration = _match_avisos_state["match_duration"]
                elapsed_seconds = match_duration - time_remaining
                print(f"📊 [Announcement] Partida: {current_map} | {elapsed_seconds:.0f}s/{match_duration}s | Placar: {allied_score} x {axis_score} | Avisos: ✅início ✅fim")
            return
        
        # Calcula tempo decorrido desde início da partida
        match_duration = _match_avisos_state["match_duration"]
        elapsed_seconds = match_duration - time_remaining
        
        # Status geral da partida
        print(f"📊 [Announcement] Partida: {current_map} | Tempo decorrido: {elapsed_seconds:.0f}s | Restante: {time_remaining:.0f}s | Placar: Aliados {allied_score} x {axis_score} Eixo")
        
        # Aviso de INÍCIO (X segundos APÓS início)
        if not _match_avisos_state["start_sent"]:
            start_after_sec = config.get("startAfterSec", 600)
            if elapsed_seconds >= start_after_sec:
                # Janela de tolerância: só envia se não passou muito tempo do threshold
                if elapsed_seconds <= start_after_sec + _ANNOUNCEMENT_WINDOW_SEC:
                    print(f"📨 [Announcement] Enviando mensagem de início ({elapsed_seconds:.0f}s decorridos, agendado para {start_after_sec}s)...")
                    start_message = config.get("startMessage", DEFAULT_START_MESSAGE)
                    result = send_admin_message_all(start_message)
                    if result.get("success"):
                        print(f"✅ [Announcement] Mensagem de início enviada com sucesso")
                else:
                    print(f"⏭️ [Announcement] Aviso de início IGNORADO (janela expirada: {elapsed_seconds:.0f}s >> {start_after_sec}s + {_ANNOUNCEMENT_WINDOW_SEC}s)")
                _match_avisos_state["start_sent"] = True
                _save_match_avisos_state()
            else:
                # Mostra quanto tempo falta
                restante = start_after_sec - elapsed_seconds
                print(f"⏳ [Announcement] Aviso de INÍCIO em {restante:.0f}s ({restante/60:.1f} min)")
        
        # Aviso de FIM - DUPLA CONDIÇÃO:
        # 1. Faltam X segundos para acabar o tempo, OU
        # 2. Um time está na iminência de vitória (4 de 5 pontos)
        if not _match_avisos_state["end_sent"]:
            end_before_sec = _FINAL_MATCH_ALERT_SEC
            
            # Condição 1: Tempo restante <= X segundos
            tempo_acabando = time_remaining <= end_before_sec and time_remaining > 0
            
            # Condição 2: Iminência de vitória (algum time com 4 pontos)
            iminencia_vitoria = allied_score >= 4 or axis_score >= 4
            
            # Envia aviso se qualquer condição for verdadeira
            if tempo_acabando or iminencia_vitoria:
                if iminencia_vitoria:
                    vencendo = "Aliados" if allied_score >= 4 else "Eixo"
                    print(f"📨 [Announcement] Enviando mensagem de fim (Iminência: {vencendo} com {max(allied_score, axis_score)} pontos, tempo: {time_remaining:.0f}s)...")
                else:
                    print(f"📨 [Announcement] Enviando mensagem de fim ({time_remaining:.0f}s restantes, placar: {allied_score} x {axis_score})...")
                
                end_message = config.get("endMessage", DEFAULT_END_MESSAGE)
                result = send_admin_message_all(end_message)
                if result.get("success"):
                    print(f"✅ [Announcement] Mensagem de fim enviada com sucesso")

                _match_avisos_state["end_sent"] = True
                _save_match_avisos_state()
            else:
                if time_remaining > end_before_sec:
                    print(f"⏳ [Announcement] Aviso de FIM em {time_remaining - end_before_sec:.0f}s ({(time_remaining - end_before_sec)/60:.1f} min)")
                
    except Exception as e:
        print(f"❌ [Announcement] Erro no monitor: {e}")


def match_avisos_monitor():
    """Thread que monitora avisos de partida."""
    print("⏱️ Monitor de avisos de partida iniciado")
    while True:
        try:
            check_match_avisos()
        except Exception as e:
            print(f"Erro no monitor: {e}")
        time.sleep(30)  # Verifica a cada 30 segundos


# ==================== INICIALIZAÇÃO ====================
if __name__ == "__main__":
    print("🚀 Iniciando Painel CRCON...")
    print(f"📡 Conectando a {CRCON_URL}")
    print("🔥 Hot Reload ATIVO - edite HTML/CSS/JS e salve!")
    
    # Inicia monitor de avisos de partida em background
    avisos_thread = threading.Thread(target=match_avisos_monitor, daemon=True)
    avisos_thread.start()
    
    # Inicia monitor de aviso por minuto em background
    start_aviso_por_minuto_thread()
    
    eel.start("index.html", size=(1500, 950), port=0, 
              cmdline_args=['--disk-cache-size=1', '--disable-application-cache'])
