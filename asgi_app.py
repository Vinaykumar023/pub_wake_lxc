import os
import time
import logging
import yaml
import json
import re
import uuid
import traceback
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional, List
from contextvars import ContextVar
from fastapi import FastAPI, Request, Response, WebSocket, WebSocketDisconnect, HTTPException
import httpx
from starlette.responses import StreamingResponse
import asyncio
import websockets
from collections import deque

# Setup logging with unified format matching uvicorn style
# Setup logging
LOG_LEVEL = os.getenv('LOG_LEVEL', 'INFO').upper()
DEBUG_MODE = (LOG_LEVEL == 'DEBUG')  # Auto-enable debug features when log level is DEBUG

log_level_map = {
    'DEBUG': logging.DEBUG,
    'INFO': logging.INFO,
    'WARNING': logging.WARNING,
    'ERROR': logging.ERROR,
    'CRITICAL': logging.CRITICAL
}

# Configure root logger
logging.basicConfig(
    level=log_level_map.get(LOG_LEVEL, logging.INFO),
    format='%(levelname)-8s %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

logger = logging.getLogger(__name__)

# Apply formatter to all handlers
# Apply formatter to all handlers
for logger_name in ['uvicorn', 'uvicorn.access', 'uvicorn.error']:
    target_logger = logging.getLogger(logger_name)
    for handler in target_logger.handlers:
        handler.setFormatter(logging.Formatter('%(levelname)-8s %(message)s'))  # ✅ Fixed here too

# Suppress noisy httpx/httpcore logs unless in DEBUG mode
if not DEBUG_MODE:
    # Set all httpx-related loggers to WARNING to suppress INFO/DEBUG spam
    for logger_name in ['httpx', 'httpcore', 'httpcore.http11', 'httpcore.connection', 
                        'httpcore._async', 'httpcore._sync']:
        logging.getLogger(logger_name).setLevel(logging.WARNING)

logger.info(f"Log level set to {LOG_LEVEL}")
if DEBUG_MODE:
    logger.info("DEBUG mode enabled - verbose logging and request tracking active")

# Display Options
SHOW_SUBDOMAIN_ONLY = os.getenv('SHOW_SUBDOMAIN_ONLY', 'false').lower() == 'true'
LOG_ACTIVITY_INTERVAL = int(os.getenv('LOG_ACTIVITY_INTERVAL', '60'))

# Activity tracking configuration
ACTIVITY_FILE = "/app/logs/activity_tracking.json"
ACTIVITY_MAX_AGE_DAYS = int(os.getenv('ACTIVITY_MAX_AGE_DAYS', '7'))
ACTIVITY_MAX_FILE_SIZE = int(os.getenv('ACTIVITY_MAX_FILE_SIZE', str(1024 * 100)))  # 100KB default

# Monitor error handling configuration
MONITOR_MAX_CONSECUTIVE_ERRORS = 10
MONITOR_BASE_BACKOFF = 60
MONITOR_MAX_BACKOFF = 300

# Circuit breaker configuration
CIRCUIT_BREAKER_THRESHOLD = 3
CIRCUIT_BREAKER_TIMEOUT = 300

# Request tracking context variable (FIX 18)
request_id_var: ContextVar[str] = ContextVar('request_id', default=None)


# Status String Constants
class ContainerStatus:
    """Container status constants"""
    STOPPED = "stopped"
    RUNNING = "running"
    STARTING = "starting"
    UNKNOWN = "unknown"


def get_display_name(host: str) -> str:
    """Get display name for logging based on SHOW_SUBDOMAIN_ONLY setting"""
    if SHOW_SUBDOMAIN_ONLY and host:
        parts = host.split('.')
        if len(parts) > 0:
            return parts[0]
    return host


def generate_request_id() -> str:
    """Generate a short unique request ID (FIX 18)"""
    return str(uuid.uuid4())[:8]


def log_request(request_id: str, message: str, level: str = "debug"):
    """Log with request ID prefix (FIX 18) - only in DEBUG mode"""
    if DEBUG_MODE:
        logger.debug(f"[REQ-{request_id}] {message}")
#        log_func(f"[REQ-{request_id}] {message}")


# Shared httpx client for Proxmox API calls
proxmox_client: Optional[httpx.AsyncClient] = None


async def get_proxmox_client() -> httpx.AsyncClient:
    """Get or create shared Proxmox API client with connection pooling"""
    global proxmox_client
    if proxmox_client is None:
        proxmox_client = httpx.AsyncClient(
            verify=False,
            timeout=30.0,
            limits=httpx.Limits(max_connections=10, max_keepalive_connections=6)
        )
    return proxmox_client


# Status events storage
status_events = {}
status_lock = asyncio.Lock()
MAX_HOSTS = 50
STATUS_EXPIRY_HOURS = 24

# Container Startup Locks
container_locks = {}
container_starting = {}
container_start_times = {}

# Circuit Breaker
container_failures = {}

# Background task tracking
last_monitor_run: Optional[datetime] = None
monitor_error_count = 0
background_tasks = []

# Rate-limited activity logging
last_activity_log = {}


def ensure_logs_directory():
    """Ensure logs directory exists (FIX 9)"""
    logs_dir = os.path.dirname(ACTIVITY_FILE)
    try:
        if not os.path.exists(logs_dir):
            os.makedirs(logs_dir, mode=0o755, exist_ok=True)
            logger.info(f"Created logs directory: {logs_dir}")
        return True
    except Exception as e:
        logger.error(f"Failed to create logs directory {logs_dir}: {e}")
        return False


def save_activity_state():
    """Save container activity timestamps to file with safety checks (FIX 9)"""
    try:
        if not ensure_logs_directory():
            return False
        
        state = {}
        now = datetime.now()
        cutoff_time = now - timedelta(days=ACTIVITY_MAX_AGE_DAYS)
        
        for container in config.get('containers', []):
            vmid = str(container['vmid'])
            last_activity = container.get('last_activity')
            
            if last_activity and last_activity > cutoff_time:
                state[vmid] = last_activity.isoformat()
        
        json_data = json.dumps(state, indent=2)
        if len(json_data.encode('utf-8')) > ACTIVITY_MAX_FILE_SIZE:
            logger.error(f"Activity state file would exceed size limit ({ACTIVITY_MAX_FILE_SIZE} bytes). Not saving.")
            return False
        
        temp_file = ACTIVITY_FILE + ".tmp"
        with open(temp_file, 'w') as f:
            f.write(json_data)
        
        os.replace(temp_file, ACTIVITY_FILE)
        
        if DEBUG_MODE:
            logger.debug(f"Saved activity state for {len(state)} containers ({len(json_data)} bytes)")
        
        return True
        
    except Exception as e:
        logger.error(f"Failed to save activity state: {e}")
        return False


def load_activity_state():
    """Load container activity timestamps from file with validation (FIX 9)"""
    try:
        if not ensure_logs_directory():
            return
        
        if not os.path.exists(ACTIVITY_FILE):
            logger.info("No activity state file found (first run)")
            return
        
        file_size = os.path.getsize(ACTIVITY_FILE)
        if file_size > ACTIVITY_MAX_FILE_SIZE:
            logger.error(f"Activity state file too large ({file_size} bytes). Skipping load.")
            backup_file = ACTIVITY_FILE + f".backup.{datetime.now().strftime('%Y%m%d_%H%M%S')}"
            os.rename(ACTIVITY_FILE, backup_file)
            logger.info(f"Backed up oversized file to {backup_file}")
            return
        
        with open(ACTIVITY_FILE, 'r') as f:
            state = json.load(f)
        
        if not isinstance(state, dict):
            logger.error("Activity state file has invalid format")
            return
        
        now = datetime.now()
        cutoff_time = now - timedelta(days=ACTIVITY_MAX_AGE_DAYS)
        loaded_count = 0
        skipped_count = 0
        
        for container in config.get('containers', []):
            vmid = str(container['vmid'])
            if vmid in state:
                try:
                    timestamp_str = state[vmid]
                    last_activity = datetime.fromisoformat(timestamp_str)
                    
                    if last_activity > cutoff_time:
                        container['last_activity'] = last_activity
                        loaded_count += 1
                        
                        if DEBUG_MODE:
                            age_minutes = (now - last_activity).total_seconds() / 60
                            logger.debug(f"VMID {vmid}: Restored activity from {age_minutes:.1f} minutes ago")
                    else:
                        skipped_count += 1
                        logger.info(f"VMID {vmid}: Skipped stale activity (older than {ACTIVITY_MAX_AGE_DAYS} days)")
                
                except (ValueError, TypeError) as e:
                    logger.warning(f"VMID {vmid}: Invalid timestamp format: {e}")
                    continue
        
        logger.info(f"✓ Loaded activity state for {loaded_count} containers ({skipped_count} stale entries skipped)")
        
    except json.JSONDecodeError as e:
        logger.error(f"Activity state file corrupted (JSON error): {e}")
    except Exception as e:
        logger.warning(f"Could not load activity state: {e}")


async def cleanup_old_status_events():
    """Remove status event entries not accessed in 24 hours"""
    async with status_lock:
        now = datetime.now()
        hosts_to_remove = []
        
        for host, data in status_events.items():
            last_access = data.get('last_access', now)
            if (now - last_access).total_seconds() > STATUS_EXPIRY_HOURS * 3600:
                hosts_to_remove.append(host)
        
        for host in hosts_to_remove:
            del status_events[host]
            logger.info(f"Cleaned up old status events for {get_display_name(host)}")
        
        if len(status_events) > MAX_HOSTS:
            sorted_hosts = sorted(
                status_events.items(),
                key=lambda x: x[1].get('last_access', datetime.min)
            )
            for host, _ in sorted_hosts[:len(status_events) - MAX_HOSTS]:
                del status_events[host]
                logger.info(f"Removed status events for {get_display_name(host)} (limit reached)")


async def emit_status_event(host: str, message: str, level: str = "info", elapsed_seconds: int = None):
    """Emit a status event for a specific host"""
    async with status_lock:
        if host not in status_events:
            status_events[host] = {
                "queue": deque(maxlen=50),
                "last_access": datetime.now(),
                "start_time": datetime.now()
            }
        
        event = {
            "timestamp": datetime.now().isoformat(),
            "message": message,
            "level": level
        }
        
        if elapsed_seconds is not None:
            event["elapsed"] = elapsed_seconds
        
        status_events[host]["queue"].append(event)
        status_events[host]["last_access"] = datetime.now()
    
    if DEBUG_MODE:
        logger.debug(f"{get_display_name(host)} SSE: {message}")


def validate_environment_variables():
    """Validate required environment variables at startup (FIX 13) - Enhanced with secrets support"""
    required_vars = {
        'PROXMOX_HOST': ('hostname/IP', r'^[a-zA-Z0-9.-]+$', False),
        'PROXMOX_NODE': ('node name', r'^[a-zA-Z0-9-]+$', False),
        'PROXMOX_TOKEN_USER': ('token user', r'^[a-zA-Z0-9@._-]+$', False),
        'PROXMOX_TOKEN_ID': ('token ID', r'^[a-zA-Z0-9_-]+$', False),
        'PROXMOX_TOKEN_VALUE': ('token value', r'^[a-f0-9-]+$', True),  # Can use _FILE
        'WAKE_SECRET': ('secret key', None, False)
    }
    
    errors = []
    for var, (description, pattern, allow_file) in required_vars.items():
        value = os.getenv(var)
        file_var = f"{var}_FILE"
        file_path = os.getenv(file_var)
        
        # Check if either direct value or file path exists
        if allow_file and file_path:
            if not os.path.exists(file_path):
                errors.append(f"  - {file_var} points to non-existent file: {file_path}")
            continue
        
        if not value or not value.strip():
            if allow_file:
                errors.append(f"  - {var} ({description}) is missing (set {var} or {file_var})")
            else:
                errors.append(f"  - {var} ({description}) is missing")
        elif pattern and not re.match(pattern, value):
            errors.append(f"  - {var} ({description}) has invalid format")
    
    if errors:
        error_msg = "Environment variable validation failed:\n" + "\n".join(errors)
        logger.error(error_msg)
        raise RuntimeError(error_msg)
    
    logger.info("✓ Environment variable validation passed")


def get_secret(env_var_name: str, secret_file_env: str = None) -> Optional[str]:
    """Read secret from file if *_FILE env var exists, otherwise from direct env var"""
    if secret_file_env:
        secret_file = os.getenv(secret_file_env)
        if secret_file and os.path.exists(secret_file):
            try:
                with open(secret_file, 'r') as f:
                    return f.read().strip()
            except Exception as e:
                logger.warning(f"Failed to read secret from {secret_file}: {e}")
    
    # Fallback to direct environment variable
    return os.getenv(env_var_name)

def load_proxmox_config() -> dict:
    """Load Proxmox configuration from environment variables"""
    return {
        'host': os.getenv('PROXMOX_HOST'),
        'node': os.getenv('PROXMOX_NODE'),
        'token_user': os.getenv('PROXMOX_TOKEN_USER'),
        'token_id': os.getenv('PROXMOX_TOKEN_ID'),
        'token_value': get_secret('PROXMOX_TOKEN_VALUE', 'PROXMOX_TOKEN_VALUE_FILE'),
        'verify_tls': os.getenv('PROXMOX_VERIFY_TLS', 'false').lower() == 'true'
    }



def validate_config(config: dict) -> None:
    """Validate configuration file structure and required fields"""
    required_sections = ['containers']
    for section in required_sections:
        if section not in config:
            raise ValueError(f"Missing required section: {section}")
    
    if not isinstance(config['containers'], list):
        raise ValueError("'containers' must be a list")
    
    for i, container in enumerate(config['containers']):
        if 'vmid' not in container:
            raise ValueError(f"Container {i} missing 'vmid'")
        if 'services' not in container:
            raise ValueError(f"Container {i} missing 'services'")
        
        for j, service in enumerate(container['services']):
            required_service_fields = ['name', 'host', 'port', 'domain']
            for field in required_service_fields:
                if field not in service:
                    raise ValueError(f"Container {i}, service {j} missing '{field}'")


# Validate environment variables first
validate_environment_variables()

# Load Configuration
CONFIG_FILE = os.getenv('CONFIG_FILE', '/app/config.yaml')

try:
    with open(CONFIG_FILE, 'r') as f:
        config = yaml.safe_load(f)
    
    config_proxmox = load_proxmox_config()
    
    if 'global' not in config:
        config['global'] = {}
    
    wake_secret = os.getenv('WAKE_SECRET')
    if wake_secret:
        config['global']['wake_secret'] = wake_secret
    
    validate_config(config)

except FileNotFoundError:
    raise RuntimeError(f"Configuration file not found: {CONFIG_FILE}")
except yaml.YAMLError as e:
    raise RuntimeError(f"Invalid YAML in configuration file: {e}")
except ValueError as e:
    raise RuntimeError(f"Configuration validation failed: {e}")

# Build mappings
HOST_MAP = {}
DOMAIN_TO_CONTAINER = {}

for container in config.get('containers', []):
    container['last_activity'] = None
    for service in container.get('services', []):
        domain = service.get('domain')
        host = service.get('host')
        port = service.get('port')
        if domain and host and port:
            HOST_MAP[domain] = f"http://{host}:{port}"
            DOMAIN_TO_CONTAINER[domain] = container

# Load persistent activity state (FIX 9)
load_activity_state()

# FastAPI app
app = FastAPI(
    title="Proxmox LXC Wake Service",
    description="Automated container wake/sleep service with Traefik integration",
    version="1.3.0"
)

# Configure uvicorn access logging
if not DEBUG_MODE:
    import uvicorn
    uvicorn_access_logger = logging.getLogger("uvicorn.access")
    uvicorn_access_logger.disabled = True

logger.info("🚀 Proxmox LXC Wake Service Starting")
logger.info(f"✓ Configured containers: {len(config.get('containers', []))}")
logger.info(f"✓ Configured services: {len(HOST_MAP)}")
logger.info(f"✓ Global idle timeout: {config.get('global', {}).get('idle_minutes', 10)} minutes")
logger.info("✓ Using environment variables for Proxmox credentials")

if DEBUG_MODE:
    logger.debug(f"Configuration file: {CONFIG_FILE}")
    logger.debug(f"Proxmox host: {config_proxmox['host']}")
    logger.debug(f"Proxmox node: {config_proxmox['node']}")


def get_container_by_domain(domain: str) -> Optional[dict]:
    """Find container configuration by domain"""
    container = DOMAIN_TO_CONTAINER.get(domain)
    if not container:
        logger.warning(f"No container mapping for domain: {domain}")
    return container


def find_container_by_service(service_name: str) -> Optional[dict]:
    """Find container configuration by service name or domain"""
    for container in config.get('containers', []):
        for service in container.get('services', []):
            if service.get('name') == service_name or service.get('domain', '').startswith(service_name):
                return container
    return None


def update_activity(container: dict) -> None:
    """Update last activity timestamp with rate-limited logging and immediate save"""
    global last_activity_log
    now = datetime.now()
    container['last_activity'] = now
    
    vmid = container['vmid']
    idle_minutes = container.get('idle_minutes', config.get('global', {}).get('idle_minutes', 10))
    # Save activity state immediately after each update
    save_activity_state()
    last_log = last_activity_log.get(vmid)
    if last_log is None or (now - last_log).total_seconds() > LOG_ACTIVITY_INTERVAL:
        services = ', '.join([get_display_name(s.get('domain', s.get('name', '?'))) for s in container.get('services', [])])
        next_shutdown = now + timedelta(minutes=idle_minutes)
        logger.info(f"VMID {vmid}: Activity detected for [{services}]")
        logger.info(f"VMID {vmid}: Auto-shutdown in {idle_minutes} minutes at {next_shutdown.strftime('%H:%M:%S')}")
        last_activity_log[vmid] = now



def get_proxmox_headers() -> dict:
    """Get Proxmox API authentication headers"""
    token_user = config_proxmox['token_user']
    token_id = config_proxmox['token_id']
    token_value = config_proxmox['token_value']
    return {
        "Authorization": f"PVEAPIToken={token_user}!{token_id}={token_value}"
    }


def check_circuit_breaker(vmid: str) -> bool:
    """Check if circuit breaker is open for this container (FIX 5)"""
    if vmid not in container_failures:
        return False
    
    failure_data = container_failures[vmid]
    if not failure_data.get('circuit_open', False):
        return False
    
    last_failure = failure_data.get('last_failure')
    if last_failure and (datetime.now() - last_failure).total_seconds() > CIRCUIT_BREAKER_TIMEOUT:
        logger.info(f"VMID {vmid}: Circuit breaker timeout elapsed, allowing retry")
        failure_data['circuit_open'] = False
        failure_data['count'] = 0
        return False
    
    return True


def record_container_failure(vmid: str):
    """Record a container start failure for circuit breaker (FIX 5)"""
    if vmid not in container_failures:
        container_failures[vmid] = {'count': 0, 'last_failure': None, 'circuit_open': False}
    
    container_failures[vmid]['count'] += 1
    container_failures[vmid]['last_failure'] = datetime.now()
    
    if container_failures[vmid]['count'] >= CIRCUIT_BREAKER_THRESHOLD:
        container_failures[vmid]['circuit_open'] = True
        logger.error(
            f"🔴 VMID {vmid}: Circuit breaker OPENED after {container_failures[vmid]['count']} consecutive failures. "
            f"Will not retry for {CIRCUIT_BREAKER_TIMEOUT} seconds."
        )


def reset_container_failures(vmid: str):
    """Reset failure count after successful start (FIX 5)"""
    if vmid in container_failures and container_failures[vmid]['count'] > 0:
        logger.info(f"VMID {vmid}: Circuit breaker reset after successful operation")
        container_failures[vmid] = {'count': 0, 'last_failure': None, 'circuit_open': False}


async def check_container_status(vmid: str, kind: str = "lxc") -> bool:
    """Check if a container is running"""
    node = config_proxmox['node']
    pve_host = config_proxmox['host']
    
    url = f"https://{pve_host}:8006/api2/json/nodes/{node}/{kind}/{vmid}/status/current"
    client = await get_proxmox_client()
    
    try:
        resp = await client.get(url, headers=get_proxmox_headers(), timeout=10)
        resp.raise_for_status()
        status = resp.json().get('data', {}).get('status')
        return status == ContainerStatus.RUNNING
    except Exception as e:
        logger.error(f"VMID {vmid}: Failed to check status: {e}")
        return False

async def start_container(vmid: str, kind: str = "lxc", host: str = None) -> bool:
    """Start a container with circuit breaker protection"""
    if check_circuit_breaker(vmid):
        logger.warning(f"VMID {vmid}: Circuit breaker is OPEN, refusing to start container")
        if host:
            await emit_status_event(host, f"Container {vmid} in failure state, not retrying yet", "error")
        return False
    
    node = config_proxmox['node']
    pve_host = config_proxmox['host']
    
    url = f"https://{pve_host}:8006/api2/json/nodes/{node}/{kind}/{vmid}/status/start"
    
    container_start_times[vmid] = datetime.now()
    
    if host:
        await emit_status_event(host, f"Sending start command to container {vmid}...", "info", 0)
    
    client = await get_proxmox_client()
    
    try:
        resp = await client.post(url, headers=get_proxmox_headers(), timeout=30)
        resp.raise_for_status()
        logger.info(f"VMID {vmid}: Container started successfully")
        if host:
            await emit_status_event(host, f"Container {vmid} start command sent successfully", "success")
        
        reset_container_failures(vmid)
        
        # Record activity when starting a stopped container
        container = get_container_by_domain(host) if host else None
        if not container:
            # If we don't have host, try to find container by VMID
            for c in config.get('containers', []):
                if str(c['vmid']) == str(vmid):
                    container = c
                    break
        
        if container:
            update_activity(container)
            logger.info(f"VMID {vmid}: Activity recorded at container start")
        
        return True
        
    except Exception as e:
        error_msg = str(e)
        logger.error(f"VMID {vmid}: Failed to start container: {error_msg}")
        
        record_container_failure(vmid)
        
        if host:
            if "Connection" in error_msg or "timeout" in error_msg.lower():
                await emit_status_event(host, f"Cannot connect to Proxmox API at {pve_host}:8006", "error")
            elif "401" in error_msg or "403" in error_msg:
                await emit_status_event(host, "Proxmox authentication failed - check API token", "error")
            else:
                await emit_status_event(host, f"Failed to start container: {error_msg}", "error")
        
        return False

async def shutdown_container(vmid: str, kind: str = "lxc", stop_mode: str = "shutdown") -> bool:
    """Shutdown a container"""
    node = config_proxmox['node']
    pve_host = config_proxmox['host']
    
    url = f"https://{pve_host}:8006/api2/json/nodes/{node}/{kind}/{vmid}/status/{stop_mode}"
    client = await get_proxmox_client()
    
    try:
        resp = await client.post(url, headers=get_proxmox_headers(), timeout=30)
        resp.raise_for_status()
        if vmid in container_start_times:
            del container_start_times[vmid]
        return True
    except Exception as e:
        logger.error(f"VMID {vmid}: Shutdown failed: {e}")
        return False

async def check_and_shutdown_idle():
    """Background task with enhanced error handling (FIX 6)"""
    global last_monitor_run, monitor_error_count
    logger.info("🔍 Starting idle container monitor...")
    
    consecutive_errors = 0
    backoff_time = MONITOR_BASE_BACKOFF
    
    while True:
        try:
            if consecutive_errors >= MONITOR_MAX_CONSECUTIVE_ERRORS:
                logger.critical(
                    f"⚠️ CRITICAL: Idle monitor exceeded max error threshold ({MONITOR_MAX_CONSECUTIVE_ERRORS} consecutive failures). "
                    f"Monitor is paused. Manual intervention required."
                )
                await asyncio.sleep(3600)
                continue
            
            if consecutive_errors > 0:
                logger.warning(
                    f"Monitor had {consecutive_errors} consecutive error(s). "
                    f"Applying backoff: {backoff_time}s before next check"
                )
                await asyncio.sleep(backoff_time)
            else:
                await asyncio.sleep(60)
            
            now = datetime.now()
            
            await cleanup_old_status_events()
            
            for container in config.get('containers', []):
                try:
                    vmid = str(container['vmid'])
                    kind = container.get('kind', 'lxc')
                    idle_minutes = container.get('idle_minutes', config.get('global', {}).get('idle_minutes', 10))
                    last_activity = container.get('last_activity')
                    
                    is_running = await check_container_status(vmid, kind)
                    
                    if not is_running:
                        if last_activity is not None:
                            services = ', '.join([get_display_name(s.get('domain', s.get('name', '?'))) for s in container.get('services', [])])
                            logger.info(f"VMID {vmid}: Container detected as stopped for [{services}], resetting activity tracker")
                            container['last_activity'] = None
                            # Save state after resetting activity
                            save_activity_state()
                        continue
                    
                    if last_activity is None:
                        if DEBUG_MODE:
                            logger.debug(f"VMID {vmid}: No activity recorded yet, skipping idle check")
                        continue
                    
                    idle_duration = now - last_activity
                    idle_threshold = timedelta(minutes=idle_minutes)
                    idle_seconds = idle_duration.total_seconds()
                    idle_mins = idle_seconds / 60
                    
                    if DEBUG_MODE:
                        logger.debug(f"VMID {vmid}: Idle for {idle_mins:.1f} min (threshold: {idle_minutes} min)")
                    
                    if idle_duration >= idle_threshold:
                        services = ', '.join([get_display_name(s.get('domain', s.get('name', '?'))) for s in container.get('services', [])])
                        logger.info(f"VMID {vmid}: Container idle for {idle_mins:.1f} minutes (threshold: {idle_minutes} min)")
                        logger.info(f"VMID {vmid}: Shutting down [{services}]")
                        
                        success = await shutdown_container(vmid, kind, container.get('stop_mode', 'shutdown'))
                        if success:
                            logger.info(f"VMID {vmid}: ✓ Successfully shut down")
                            container['last_activity'] = None
                            # Save state after successful shutdown
                            save_activity_state()
                        else:
                            logger.error(f"VMID {vmid}: ✗ Failed to shut down")
                
                except Exception as e:
                    logger.error(f"VMID {container.get('vmid', '?')}: Error processing container: {e}", exc_info=DEBUG_MODE)
                    continue
            
            if consecutive_errors > 0:
                logger.info(f"✓ Monitor recovered after {consecutive_errors} error(s)")
            
            consecutive_errors = 0
            backoff_time = MONITOR_BASE_BACKOFF
            last_monitor_run = datetime.now()
            monitor_error_count = 0
        
        except asyncio.CancelledError:
            logger.info("Idle monitor task cancelled, shutting down gracefully")
            save_activity_state()
            break
        
        except Exception as e:
            consecutive_errors += 1
            monitor_error_count = consecutive_errors
            
            backoff_time = min(MONITOR_BASE_BACKOFF * (2 ** (consecutive_errors - 1)), MONITOR_MAX_BACKOFF)
            
            logger.error(
                f"⚠️ Error in idle monitor loop (consecutive error #{consecutive_errors}): {e}",
                exc_info=DEBUG_MODE
            )
            
            if consecutive_errors == 3:
                logger.warning(f"⚠️ Monitor has failed {consecutive_errors} times consecutively. Will retry with backoff.")
            elif consecutive_errors == 5:
                logger.error(f"🔴 Monitor has failed {consecutive_errors} times consecutively. Possible infrastructure issue.")
            elif consecutive_errors >= 8:
                logger.critical(f"🔴 CRITICAL: Monitor has failed {consecutive_errors} times. Approaching max threshold!")

@app.get("/starting")
async def starting_page(request: Request):
    """Starting page with smart state machine SSE + time-aware"""
    host = request.headers.get('x-forwarded-host') or request.headers.get('host') or request.query_params.get('host', 'unknown')
    
    html = f'''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Service Starting</title>
    <style>
        * {{
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
            min-height: 100vh;
            display: flex;
            justify-content: center;
            align-items: center;
            text-align: center;
            overflow-x: hidden;
            padding: 20px;
        }}
        .container {{
            background: rgba(255, 255, 255, 0.1);
            backdrop-filter: blur(10px);
            border-radius: 16px;
            padding: 32px 28px;
            box-shadow: 0 8px 32px rgba(31, 38, 135, 0.37);
            border: 1px solid rgba(255, 255, 255, 0.18);
            max-width: 420px;
            width: 90%;
        }}
        .logo {{
            font-size: 3rem;
            margin-bottom: 12px;
            animation: pulse 2s ease-in-out infinite alternate;
        }}
        @keyframes pulse {{
            from {{ transform: scale(1); }}
            to {{ transform: scale(1.08); }}
        }}
        h1 {{
            font-size: 1.75rem;
            margin-bottom: 12px;
            font-weight: 600;
        }}
        .spinner {{
            border: 3px solid rgba(255, 255, 255, 0.3);
            border-radius: 50%;
            border-top: 3px solid white;
            width: 40px;
            height: 40px;
            animation: spin 1s linear infinite;
            margin: 20px auto;
        }}
        @keyframes spin {{
            0% {{ transform: rotate(0deg); }}
            100% {{ transform: rotate(360deg); }}
        }}
        .status-text {{
            font-size: 1.05rem;
            line-height: 1.4;
            opacity: 0.95;
            margin: 16px 0;
            min-height: 28px;
        }}
        .timer {{
            font-size: 0.85rem;
            opacity: 0.7;
            margin: 8px 0;
        }}
        .details {{
            font-size: 0.8rem;
            opacity: 0.65;
            margin-top: 16px;
            line-height: 1.6;
        }}
        .error {{
            background: rgba(255, 107, 107, 0.2);
            border: 1px solid rgba(255, 107, 107, 0.3);
        }}
        .retry-button {{
            background: rgba(255, 255, 255, 0.2);
            border: 1px solid rgba(255, 255, 255, 0.3);
            color: white;
            padding: 10px 20px;
            border-radius: 8px;
            cursor: pointer;
            font-size: 0.9rem;
            margin-top: 16px;
            transition: all 0.3s ease;
        }}
        .retry-button:hover {{
            background: rgba(255, 255, 255, 0.3);
            transform: translateY(-1px);
        }}
    </style>
</head>
<body>
    <div class="container" id="main-container">
        <div class="logo">🚀</div>
        <h1>Service Starting</h1>
        <div class="spinner"></div>
        <div class="status-text" id="status-text">Waking up container...</div>
        <div class="timer" id="timer">Elapsed: 0s</div>
        <div class="details" id="details">Usually takes 60-120 seconds<br>Page will refresh automatically</div>
    </div>

    <script>
        const host = "{host}";
        let checkCount = 0;
        const maxChecks = 40;
        const checkInterval = 10000;
        let isChecking = false;
        let eventSource = null;
        let serverElapsedSeconds = 0;

        const statusText = document.getElementById('status-text');
        const details = document.getElementById('details');
        const container = document.getElementById('main-container');
        const timerEl = document.getElementById('timer');

        function updateTimer(elapsedSeconds) {{
            serverElapsedSeconds = elapsedSeconds;
            timerEl.textContent = `Elapsed: ${{elapsedSeconds}}s`;
        }}

        function updateStatus(message, isError = false) {{
            statusText.innerHTML = message;
            if (isError) {{
                container.classList.add('error');
            }}
        }}

        function addRetryButton() {{
            const button = document.createElement('button');
            button.className = 'retry-button';
            button.textContent = 'Retry Now';
            button.onclick = () => window.location.reload();
            container.appendChild(button);
        }}

        function connectSSE() {{
            eventSource = new EventSource(`/status-stream?host=${{encodeURIComponent(host)}}`);
            
            eventSource.onmessage = (event) => {{
                try {{
                    const data = JSON.parse(event.data);
                    
                    // Always update timer if elapsed is provided
                    if (data.elapsed !== undefined) {{
                        updateTimer(data.elapsed);
                    }}

                    // Skip updates for timer-only events
                    if (data.level === 'timer') {{
                        return;
                    }}

                    const msg = data.message;

                    // Handle error-level events
                    if (data.level === 'error') {{
                        updateStatus(`Error: ${{msg}}`, true);
                        details.innerHTML = 'Check container logs or contact support';
                        return;
                    }}

                    // Update status for ALL non-timer SSE messages
                    if (msg && msg.trim()) {{
                        updateStatus(msg);
                    }}
                    
                }} catch (e) {{
                    console.error("SSE parse error:", e);
                }}
            }};

            eventSource.onerror = (error) => {{
                console.error("SSE error:", error);
                eventSource.close();
            }};
        }}

        async function checkService() {{
            if (isChecking) return;
            isChecking = true;
            checkCount++;

            try {{
                const response = await fetch(window.location.origin, {{
                    method: 'HEAD',
                    cache: 'no-cache'
                }});

                if (response.ok) {{
                    updateStatus("✓ Ready! Redirecting...");
                    if (eventSource) eventSource.close();
                    setTimeout(() => window.location.reload(), 800);
                    return;
                }}

                // checkService() no longer updates status - SSE handles it
            }} catch (error) {{
                console.log("Check failed:", error);
            }}

            if (checkCount >= maxChecks) {{
                updateStatus("Startup timeout", true);
                details.innerHTML = "Please check container logs or refresh the page";
                addRetryButton();
                if (eventSource) eventSource.close();
                isChecking = false;
                return;
            }}

            setTimeout(() => {{
                isChecking = false;
                checkService();
            }}, checkInterval);
        }}

        connectSSE();
        setTimeout(checkService, 8000);
    </script>
</body>
</html>'''
    
    return Response(
        content=html,
        media_type="text/html",
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0"
        }
    )


@app.get("/status-stream")
async def status_stream(host: str):
    """Server-Sent Events stream for status updates with elapsed time"""
    display_name = get_display_name(host)
    
    async def event_generator():
        try:
            yield f"data: {{'message': 'Connected to status stream', 'level': 'info'}}\n\n"
            
            last_sent_count = 0
            
            container = get_container_by_domain(host)
            vmid = str(container['vmid']) if container else None
            
            while True:
                async with status_lock:
                    if host in status_events:
                        events = list(status_events[host]["queue"])
                        new_events = events[last_sent_count:]
                        for event in new_events:
                            if "elapsed" not in event and vmid and vmid in container_start_times:
                                start_time = container_start_times[vmid]
                                elapsed = int((datetime.now() - start_time).total_seconds())
                                event_with_elapsed = dict(event)
                                event_with_elapsed["elapsed"] = elapsed
                                yield f"data: {json.dumps(event_with_elapsed)}\n\n"
                            else:
                                yield f"data: {json.dumps(event)}\n\n"
                        last_sent_count = len(events)
                        status_events[host]["last_access"] = datetime.now()
                
                if vmid and vmid in container_start_times:
                    start_time = container_start_times[vmid]
                    elapsed = int((datetime.now() - start_time).total_seconds())
                    timer_event = {'elapsed': elapsed, 'level': 'timer', 'message': ''}
                    yield f"data: {json.dumps(timer_event)}\n\n"
                
                await asyncio.sleep(1)
        
        except asyncio.CancelledError:
            logger.info(f"Status stream closed for {display_name}")
            raise
        finally:
            pass
    
    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no"
        }
    )


@app.get("/healthz")
async def healthz():
    """Basic health check"""
    return Response(content="ok", media_type="text/plain")


@app.get("/health")
async def health_detailed():
    """Detailed health check"""
    try:
        monitor_status = "healthy"
        if last_monitor_run:
            time_since_run = (datetime.now() - last_monitor_run).total_seconds()
            if time_since_run > 300:
                monitor_status = "stale"
        else:
            monitor_status = "initializing"
        
        return {
            "status": "healthy",
            "proxmox_connection": "ok",
            "containers_configured": len(config.get('containers', [])),
            "services_configured": len(HOST_MAP),
            "idle_monitor_status": monitor_status,
            "last_monitor_run": last_monitor_run.isoformat() if last_monitor_run else None,
            "monitor_errors": monitor_error_count,
            "circuit_breakers": {vmid: data for vmid, data in container_failures.items() if data.get('circuit_open')},
            "timestamp": datetime.now().isoformat()
        }
    except Exception as e:
        return Response(
            content=json.dumps({'status': 'unhealthy', 'error': str(e), 'timestamp': datetime.now().isoformat()}),
            status_code=503,
            media_type="application/json"
        )
@app.websocket("/{full_path:path}")
async def proxy_ws(websocket: WebSocket, full_path: str):
    """WebSocket proxy with container wake support and service readiness check"""
    host = websocket.headers.get('x-forwarded-host') or websocket.headers.get('host')
    display_name = get_display_name(host)
    backend_url = HOST_MAP.get(host)
    
    if not backend_url:
        await websocket.accept()
        await websocket.close(code=1008, reason="Unknown host")
        return
    
    # Preserve query string for Socket.IO and other WebSocket endpoints
    query_string = websocket.scope.get('query_string', b'').decode()
    full_url_path = f"/{full_path}" + (f"?{query_string}" if query_string else "")
    ws_url = backend_url.replace('http://', 'ws://').replace('https://', 'wss://') + full_url_path
    
    backend_ws = None
    client_closed = False
    backend_closed = False
    
    try:
        # Check container status BEFORE accepting WebSocket
        container = get_container_by_domain(host)
        
        if container:
            vmid = str(container['vmid'])
            kind = container.get('kind', 'lxc')
            
            # Check if container is running
            is_running = await check_container_status(vmid, kind)
            
            if not is_running:
                # Container is stopped - start it
                logger.info(f"VMID {vmid}: WebSocket request to stopped container for {display_name}, starting...")
                
                if vmid not in container_locks:
                    container_locks[vmid] = asyncio.Lock()
                
                async with container_locks[vmid]:
                    # Double-check after acquiring lock
                    is_running = await check_container_status(vmid, kind)
                    
                    if not is_running:
                        if check_circuit_breaker(vmid):
                            logger.warning(f"VMID {vmid}: Circuit breaker is OPEN for WebSocket request")
                            await websocket.accept()
                            await websocket.close(code=1011, reason="Service temporarily unavailable")
                            return
                        
                        # Start the container
                        success = await start_container(vmid, kind, host)
                        
                        if not success:
                            logger.error(f"VMID {vmid}: Failed to start container for WebSocket connection")
                            await websocket.accept()
                            await websocket.close(code=1011, reason="Failed to start service")
                            return
            
            # Check if container just started (within last 2 minutes)
            if vmid in container_start_times:
                start_time = container_start_times[vmid]
                time_since_start = (datetime.now() - start_time).total_seconds()
                
                # If container started recently, wait for service to be ready
                if time_since_start < 120:  # 2 minutes
                    logger.info(f"VMID {vmid}: Container recently started ({time_since_start:.0f}s ago), waiting for service readiness...")
                    service_ready = False
                    max_wait = 120 - int(time_since_start)  # Wait up to 2 minutes total from start
                    wait_attempts = max(1, max_wait // 2)
                    
                    for attempt in range(wait_attempts):
                        await asyncio.sleep(2)
                        
                        # Try HTTP health check
                        try:
                            async with httpx.AsyncClient(timeout=2.0, verify=False) as test_client:
                                test_response = await test_client.get(f"{backend_url}/")
                            
                            if test_response.status_code < 500:
                                service_ready = True
                                logger.info(f"VMID {vmid}: Service ready for WebSocket after {time_since_start + (attempt + 1) * 2:.0f}s")
                                break
                        
                        except Exception:
                            if DEBUG_MODE and attempt % 10 == 0:
                                logger.debug(f"VMID {vmid}: Service not ready (attempt {attempt + 1}/{wait_attempts})")
                            continue
                    
                    if not service_ready:
                        logger.warning(f"VMID {vmid}: Service not ready after waiting, WebSocket may fail")
        
        # Accept WebSocket connection
        await websocket.accept()
        
        # Now attempt to connect to backend with retries
        max_retries = 3
        retry_delay = 2
        connection_successful = False
        
        for retry in range(max_retries):
            try:
                # Forward important headers
                extra_headers = {}
                
                if 'cookie' in websocket.headers:
                    extra_headers['Cookie'] = websocket.headers['cookie']
                
                if 'authorization' in websocket.headers:
                    extra_headers['Authorization'] = websocket.headers['authorization']
                
                if 'origin' in websocket.headers:
                    extra_headers['Origin'] = websocket.headers['origin']
                
                if 'user-agent' in websocket.headers:
                    extra_headers['User-Agent'] = websocket.headers['user-agent']
                
                if DEBUG_MODE:
                    logger.debug(f"Forwarding headers to WebSocket backend: {list(extra_headers.keys())}")
                
                async with websockets.connect(ws_url, extra_headers=extra_headers) as backend_ws:
                    # Connection successful
                    connection_successful = True
                    
                    if container:
                        update_activity(container)
                    
                    logger.info(f"connection open")
                    
                    async def to_backend():
                        nonlocal client_closed
                        try:
                            while True:
                                msg = await websocket.receive()
                                if msg['type'] == 'websocket.disconnect':
                                    disconnect_code = msg.get('code', 1000)
                                    if disconnect_code == 1000:
                                        logger.info(f"WebSocket clean disconnect for {display_name}")
                                    else:
                                        logger.warning(f"WebSocket abnormal disconnect for {display_name}: code {disconnect_code}")
                                    if not backend_closed:
                                        await backend_ws.close()
                                    return
                                elif msg['type'] == 'websocket.receive':
                                    data = msg.get('text')
                                    if data is not None:
                                        await backend_ws.send(data)
                                    else:
                                        bin_data = msg.get('bytes')
                                        if bin_data is not None:
                                            await backend_ws.send(bin_data)
                        except Exception:
                            client_closed = True
                            return
                    
                    async def to_client():
                        nonlocal backend_closed
                        try:
                            while True:
                                msg = await backend_ws.recv()
                                if isinstance(msg, bytes):
                                    await websocket.send_bytes(msg)
                                else:
                                    await websocket.send_text(msg)
                        except websockets.ConnectionClosed:
                            backend_closed = True
                            if not client_closed:
                                try:
                                    await websocket.close()
                                except Exception:
                                    pass
                            return
                        except Exception:
                            backend_closed = True
                            return
                    
                    await asyncio.gather(to_backend(), to_client())
                    
                    # Connection completed successfully, break retry loop
                    break
            
            except (OSError, ConnectionRefusedError, websockets.exceptions.WebSocketException) as e:
                if retry < max_retries - 1:
                    logger.warning(f"WebSocket connection attempt {retry + 1} failed for {display_name}: {e}, retrying in {retry_delay}s...")
                    await asyncio.sleep(retry_delay)
                else:
                    logger.error(f"WebSocket proxy error for {display_name} after {max_retries} attempts: {e}")
                    if DEBUG_MODE:
                        logger.error(f"Traceback: {traceback.format_exc()}")
            
            except Exception as e:
                logger.error(f"WebSocket proxy error for {display_name}: {e}")
                if DEBUG_MODE:
                    logger.error(f"Traceback: {traceback.format_exc()}")
                break
    
    finally:
        logger.info(f"WebSocket cleanup completed for {display_name}")

@app.api_route("/{full_path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"])
async def proxy_http(full_path: str, request: Request):
    """Main HTTP proxy with wake functionality and request journey tracking (FIX 18)"""
    request_id = generate_request_id()
    request_id_var.set(request_id)
    start_time = time.time()
    
    host = request.headers.get('x-forwarded-host') or request.headers.get('host')
    
    if not host:
        log_request(request_id, "❌ REJECTED: Missing host header")
        return Response(content="Missing host header", status_code=400)
    
    backend_url = HOST_MAP.get(host)
    
    if not backend_url:
        log_request(request_id, f"❌ REJECTED: Unknown host '{host}'")
        return Response(content=f"Unknown host: {host}", status_code=404)
    
    client_ip = request.headers.get('x-forwarded-for', request.headers.get('x-real-ip', request.client.host if request.client else 'unknown'))
    user_agent = request.headers.get('user-agent', 'unknown')[:50]
    request_method = request.method
    request_path = full_path or '/'
    display_name = get_display_name(host)
    
    log_request(
        request_id,
        f"📨 RECEIVED: {request_method} {display_name}{request_path} from {client_ip} | UA: {user_agent}"
    )
    
    is_health_check = full_path.startswith('/wake') or 'health' in full_path.lower() or full_path == '/starting' or full_path.startswith('/status-stream')
    
    if is_health_check:
        log_request(request_id, "🔍 TYPE: Health check/internal endpoint - skipping wake logic")
    
    url = f"{backend_url}/{full_path}"
    
    try:
        log_request(request_id, f"🔌 ATTEMPTING: Direct backend connection to {backend_url}")
        
        async with httpx.AsyncClient() as client:
            resp = await client.request(
                request.method,
                url,
                headers=request.headers.raw,
                content=await request.body(),
                params=request.query_params,
                timeout=5.0,
                follow_redirects=True
            )
            
            log_request(request_id, f"✅ SUCCESS: Backend responded with {resp.status_code}")
            
            if resp.status_code < 500 and not is_health_check:
                container = get_container_by_domain(host)
                if container:
                    vmid = container['vmid']
                    log_request(request_id, f"📝 ACTION: Updated activity for VMID {vmid}")
                    update_activity(container)
            
            elapsed = time.time() - start_time
            log_request(request_id, f"🏁 COMPLETED: Proxied successfully in {elapsed:.2f}s")
            
            return StreamingResponse(
                resp.aiter_bytes(),
                status_code=resp.status_code,
                headers=resp.headers
            )
    
    except (httpx.ConnectError, httpx.TimeoutException, httpx.ConnectTimeout) as e:
        log_request(request_id, f"⚠️ CONNECTION FAILED: {type(e).__name__} - Backend not available")
        logger.info(f"Backend not available for {display_name}, checking if container is up")
        
        container = get_container_by_domain(host)
        
        if not container:
            log_request(request_id, "❌ FATAL: No container mapping found")
            elapsed = time.time() - start_time
            log_request(request_id, f"🏁 ABORTED: Service unavailable in {elapsed:.2f}s")
            return Response(content=f"Service unavailable: {host}", status_code=503)
        
        vmid = str(container['vmid'])
        kind = container.get('kind', 'lxc')
        
        log_request(request_id, f"🔍 CHECKING: Container VMID {vmid} status via Proxmox API")
        is_running = await check_container_status(vmid, kind)
        
        if not is_running:
            log_request(request_id, f"🛑 STATUS: Container VMID {vmid} is found STOPPED")
            
            if vmid not in container_locks:
                container_locks[vmid] = asyncio.Lock()
            
            lock = container_locks[vmid]
            lock_acquired = not lock.locked()
            
            if lock_acquired:
                async with lock:
                    is_running = await check_container_status(vmid, kind)
                    
                    if not is_running:
                        if check_circuit_breaker(vmid):
                            log_request(request_id, f"🔴 CIRCUIT BREAKER: VMID {vmid} is in OPEN state - refusing to start")
                            logger.warning(f"VMID {vmid}: Circuit breaker is OPEN, refusing to start container")
                            await emit_status_event(host, f"Container {vmid} in failure state, not retrying yet", "error")
                            
                            elapsed = time.time() - start_time
                            log_request(request_id, f"🏁 BLOCKED: Circuit breaker prevented start in {elapsed:.2f}s")
                            return Response(content=f"Failed to start service: {host}", status_code=503)
                        
                        log_request(request_id, f"🚀 ACTION: Sending START command to VMID {vmid} via Proxmox API")
                        logger.info(
                            f"VMID {vmid}: Starting container for {display_name} "
                            f"[Triggered by: {client_ip} | {request_method} {request_path} | REQ-{request_id}]"
                        )
                        
                        await emit_status_event(host, f"Container {vmid} is stopped, starting now...", "info", 0)
                        
                        success = await start_container(vmid, kind, host)
                        
                        if not success:
                            log_request(request_id, f"❌ START FAILED: VMID {vmid} failed to start")
                            logger.error(f"VMID {vmid}: Failed to start container")
                            await emit_status_event(host, f"Failed to start container {vmid}", "error")
                            
                            elapsed = time.time() - start_time
                            log_request(request_id, f"🏁 FAILED: Start command failed in {elapsed:.2f}s")
                            return Response(content=f"Failed to start service: {host}", status_code=503)
                        
                        log_request(request_id, f"✅ START SUCCESS: VMID {vmid} start command sent successfully")
                    else:
                        log_request(request_id, f"ℹ️ RACE CONDITION: VMID {vmid} was started by another request while we waited")
            else:
                log_request(request_id, f"⏳ LOCK: Another request holds the lock for VMID {vmid} - waiting/observing")
                await emit_status_event(host, "Container start already in progress...", "info")
            
            log_request(request_id, f"📄 RESPONSE: Serving starting page (503 with Retry-After: 30)")
            starting_response = await starting_page(request)
            
            elapsed = time.time() - start_time
            log_request(request_id, f"🏁 COMPLETED: Starting page served in {elapsed:.2f}s")
            
            return Response(
                content=starting_response.body,
                media_type="text/html",
                status_code=503,
                headers={"Retry-After": "30"}
            )
        else:
            log_request(request_id, f"🟢 STATUS: Container VMID {vmid} is RUNNING but service not ready yet")
            logger.info(f"VMID {vmid}: Container running, waiting for service to be ready")
            
            if vmid not in container_start_times:
                container_start_times[vmid] = datetime.now()
                log_request(request_id, f"⏱️ TIMER: Started tracking container startup time for VMID {vmid}")
            else:
                start_time_age = (datetime.now() - container_start_times[vmid]).total_seconds()
                log_request(request_id, f"⏱️ TIMER: Container VMID {vmid} has been starting for {start_time_age:.0f}s")
            
            start_time_obj = container_start_times[vmid]
            elapsed_seconds = int((datetime.now() - start_time_obj).total_seconds())
            
            await emit_status_event(host, "Container is running, waiting for service to be ready...", "info", elapsed_seconds)
            
            log_request(request_id, f"📄 RESPONSE: Serving starting page - service initializing")
            starting_response = await starting_page(request)
            
            elapsed = time.time() - start_time
            log_request(request_id, f"🏁 COMPLETED: Starting page served in {elapsed:.2f}s")
            
            return Response(
                content=starting_response.body,
                media_type="text/html",
                status_code=503,
                headers={
                    "Cache-Control": "no-store, no-cache, must-revalidate",
                    "Retry-After": "30"
                }
            )
    
    except Exception as ex:
        log_request(request_id, f"💥 EXCEPTION: Unexpected error - {type(ex).__name__}: {str(ex)}")
        display_name = get_display_name(host)
        logger.error(f"Unexpected proxy error for {display_name}: {ex}")
        
        elapsed = time.time() - start_time
        log_request(request_id, f"🏁 ERROR: Request failed with exception in {elapsed:.2f}s")
        
        return Response(content=f"Service error: {host}", status_code=500)


@app.get("/wake/{service}")
async def wake_service_by_name(service: str):
    """Wake a specific service by name"""
    container = find_container_by_service(service)
    
    if not container:
        return Response(content="Unknown service", status_code=404)
    
    vmid = str(container['vmid'])
    kind = container.get('kind', 'lxc')
    
    is_running = await check_container_status(vmid, kind)
    
    if is_running:
        update_activity(container)
        return Response(status_code=200, headers={"X-Container-Ready": "true"})
    else:
        display_name = get_display_name(service)
        logger.info(f"VMID {vmid}: Starting container for service: {display_name}")
        success = await start_container(vmid, kind)
        if success:
            return Response(status_code=202, content="Starting container...")
        else:
            return Response(status_code=503, content="Failed to start container")

# Preserve query string for Socket.IO and other WebSocket endpoints
    # query_string = websocket.scope.get('query_string', b'').decode()
    # full_url_path = f"/{full_path}" + (f"?{query_string}" if query_string else "")
    # ws_url = backend_url.replace('http://', 'ws://').replace('https://', 'wss://') + full_url_path
    
    await websocket.accept()
    backend_ws = None
    client_closed = False
    backend_closed = False
    
    try:
        async with websockets.connect(ws_url) as backend_ws:
            container = get_container_by_domain(host)
            if container:
                update_activity(container)
            
            async def to_backend():
                nonlocal client_closed
                try:
                    while True:
                        msg = await websocket.receive()
                        if msg['type'] == 'websocket.disconnect':
                            disconnect_code = msg.get('code', 1000)
                            if disconnect_code == 1000:
                                logger.info(f"WebSocket clean disconnect for {display_name}")
                            else:
                                logger.warning(f"WebSocket abnormal disconnect for {display_name}: code {disconnect_code}")
                            if not backend_closed:
                                await backend_ws.close()
                            return
                        elif msg['type'] == 'websocket.receive':
                            data = msg.get('text')
                            if data is not None:
                                await backend_ws.send(data)
                            else:
                                bin_data = msg.get('bytes')
                                if bin_data is not None:
                                    await backend_ws.send(bin_data)
                except Exception:
                    client_closed = True
                    return
            
            async def to_client():
                nonlocal backend_closed
                try:
                    while True:
                        msg = await backend_ws.recv()
                        if isinstance(msg, bytes):
                            await websocket.send_bytes(msg)
                        else:
                            await websocket.send_text(msg)
                except websockets.ConnectionClosed:
                    backend_closed = True
                    if not client_closed:
                        try:
                            await websocket.close()
                        except Exception:
                            pass
                    return
                except Exception:
                    backend_closed = True
                    return
            
            await asyncio.gather(to_backend(), to_client())
    
    except Exception as e:
        logger.debug(f"WebSocket proxy error for {display_name}: {e}")
        logger.error(f"Traceback: {traceback.format_exc()}")
        
    finally:
        if backend_ws and not backend_ws.closed:
            try:
                await backend_ws.close()
            except Exception:
                pass
        logger.info(f"WebSocket cleanup completed for {display_name}")


@app.on_event("startup")
async def startup_event():
    """Start background tasks on startup"""
    
    # Suppress httpx logs AFTER httpx is loaded (FIX 19)
    if not DEBUG_MODE:
        for logger_name in ['httpx', 'httpcore', 'httpcore.http11', 'httpcore.connection', 
                            'httpcore._async', 'httpcore._sync', 'hpack']:
            logging.getLogger(logger_name).setLevel(logging.WARNING)
        logger.info("✓ Suppressed httpx verbose logging")
    
    task = asyncio.create_task(check_and_shutdown_idle())
    background_tasks.append(task)
    logger.info("✓ Application started - idle monitoring active")

@app.on_event("shutdown")
async def shutdown_event():
    """Cleanup on shutdown"""
    logger.info("Shutting down background tasks...")
    
    save_activity_state()
    
    for task in background_tasks:
        task.cancel()
    await asyncio.gather(*background_tasks, return_exceptions=True)
    
    global proxmox_client
    if proxmox_client:
        await proxmox_client.aclose()
        logger.info("✓ Proxmox API client closed")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080, log_level="info")