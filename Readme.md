
# Wake-LXC: Proxmox Container On-Demand Auto Start/Stop Service

**Wake-LXC** is a smart proxy service that automatically starts and stops Proxmox LXC containers based on incoming traffic, saving resources while maintaining seamless user access.

## 🌟 Features

- **Automatic Container Wake-up**: Starts stopped containers when traffic arrives
- **Smart Idle Shutdown**: Automatically stops containers after configurable idle periods
- **Beautiful Starting Page**: Real-time SSE progress updates while container starts
- **Concurrent Request Handling**: Lock-based mechanism prevents duplicate start commands
- **Circuit Breaker Pattern**: Prevents overwhelming Proxmox API during failures
- **WebSocket Support**: Full WebSocket proxying for real-time applications
- **Traefik Integration**: Works seamlessly with Traefik reverse proxy
- **Secure Credential Management**: Docker secrets support for sensitive tokens
- **Detailed Logging**: Comprehensive request tracking and debugging capabilities

## 📋 Prerequisites

- **Docker** and **Docker Compose** installed
- **Proxmox VE** server (tested with 8.x)
- **Traefik** reverse proxy (for routing traffic)
- **LXC Containers** running your services
<img width="1871" height="801" alt="red1" src="https://github.com/user-attachments/assets/ca4b2fe2-743a-4a9e-a8d5-c2619e203378" />
<img width="1861" height="888" alt="starting 3" src="https://github.com/user-attachments/assets/e054bb0b-ec5f-4c8e-96d2-b89aaea1da02" />


### 🚀 Quick Start

### 1. Clone the Repository

```bash
git clone https://github.com/itsddpanda/pub_wake_lxc.git
cd pub_wake-lxc
```

### 2. Create Proxmox API Token
You can use commands if that is suitable
```bash
https://github.com/itsddpanda/pub_wake_lxc/blob/a3c1e46f24c60102c1cba9e8eae357475b8759ca/pve_commands.md
```
#### Step 1: Create a Service User

1. Log into your Proxmox web interface
2. Navigate to **Datacenter → Permissions → Users**
3. Click **Add** and create a new user:
   - **User name**: `svc-wake` (or your preferred name)
   - **Realm**: `Proxmox VE authentication server` or `Linux PAM standard authentication`
   - **Password**: Set a secure password (won't be used for API access)

#### Step 2: Assign Permissions to User

1. Navigate to **Datacenter → Permissions**
2. Click **Add → User Permission**
3. Configure:
   - **Path**: `/` (root - applies to entire system)
   - **User**: Select `svc-wake@pve` (or your user)
   - **Role**: Create a custom role OR use built-in role:
     
     **Option A (Recommended - Minimal Permissions):**
     - Go to **Datacenter → Permissions → Roles**
     - Click **Create**
     - **Name**: `WakeLXC`
     - **Privileges**: Select:
       - `VM.PowerMgmt` - Start/stop containers
       - `VM.Audit` - Check container status
     - Save and assign this role to your user
     
     **Option B (Simpler - More Permissive):**
     - Use built-in `PVEVMAdmin` role (full VM management access)
   
   - **Propagate**: ✓ Checked (applies to all child objects)

#### Step 3: Create API Token

1. Navigate to **Datacenter → Permissions → API Tokens**
2. Click **Add**
3. Configure:
   - **User**: Select `svc-wake@pve` (the user you just created)
   - **Token ID**: `wake` (or your preferred ID)
   - **Expire**: Leave blank (never expires) or set expiration date
   - **Privilege Separation**: **☐ Unchecked** (token inherits user permissions)
     - When unchecked: Token inherits ALL permissions from the user (simpler)
     - When checked: Token needs separate permission assignment (more secure but complex)
   - **Comment**: Optional (e.g., "Wake-LXC service token")

4. Click **Add**
5. **⚠️ IMPORTANT**: Copy the displayed token value immediately
   - Format: `xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx`
   - This value is shown only once and cannot be retrieved later
   - Store it securely

#### Verification (Optional)

Test the token:

```bash
# Replace with your values
curl -k -H "Authorization: PVEAPIToken=svc-wake@pve!wake=YOUR_TOKEN_VALUE" \\
  https://YOUR_PROXMOX_IP:8006/api2/json/nodes/YOUR_NODE/lxc
```

You should see a JSON list of containers.

### 3. Set Up Secrets aka Token file

```bash
# Create secrets directory
mkdir -p secrets
chmod 700 secrets

# Create token file (replace YOUR_PROXMOX_TOKEN with your actual token)
echo -n "YOUR_PROXMOX_TOKEN" > secrets/proxmox_token_value.txt
chmod 644 secrets/proxmox_token_value.txt
```

**⚠️ Important**: Use `echo -n` to avoid adding newlines.

### 4. Configure Environment Variables

Create a `.env` file:

```bash
cat > .env << 'EOF'
# Logging
LOG_LEVEL=INFO

# Display Options
SHOW_SUBDOMAIN_ONLY=true
LOG_ACTIVITY_INTERVAL=60

# Timezone
TZ=YOUR_TIMEZONE

# Proxmox Configuration
PROXMOX_HOST=YOUR_PROXMOX_IP
PROXMOX_NODE=YOUR_PROXMOX_NODE
PROXMOX_TOKEN_USER=YOUR_TOKEN_USER
PROXMOX_TOKEN_ID=YOUR_TOKEN_ID
PROXMOX_VERIFY_TLS=false
EOF
```

**Configuration Guide**:

| Variable | Description | Example |
|----------|-------------|---------|
| `PROXMOX_HOST` | Proxmox server IP or hostname | `192.168.1.100` |
| `PROXMOX_NODE` | Proxmox node name | `pve`, `node1` |
| `PROXMOX_TOKEN_USER` | API token user | `svc-wake@pve` |
| `PROXMOX_TOKEN_ID` | API token ID | `wake` |
| `TZ` | System timezone | `America/New_York`, `Europe/London`, `Asia/Kolkata` |
| `LOG_LEVEL` | Logging verbosity | `DEBUG`, `INFO`, `WARNING` |
| `SHOW_SUBDOMAIN_ONLY` | Show only subdomain in UI | `true`, `false` |
| `LOG_ACTIVITY_INTERVAL` | Seconds between idle checks | `60` |

### 5. Configure Container Mappings

Edit provided example `config.yaml`:

```yaml
# Wake-LXC Configuration

global:
  idle_minutes: 10  # Default idle timeout for all containers
  allowed_cidrs:    # Optional: CIDR ranges allowed (not actively used)
    - YOUR_DOCKER_NETWORK_CIDR_1
    - YOUR_DOCKER_NETWORK_CIDR_2

containers:
  - vmid: YOUR_CONTAINER_ID_1
    kind: lxc
    idle_minutes: 10
    stop_mode: shutdown
    services:
      - name: YOUR_SERVICE_NAME_1
        host: YOUR_CONTAINER_IP
        port: YOUR_APP_PORT
        domain: YOUR_FULL_DOMAIN_1
      
      - name: YOUR_SERVICE_NAME_2
        host: YOUR_CONTAINER_IP
        port: YOUR_APP_PORT_2
        domain: YOUR_FULL_DOMAIN_2

  - vmid: YOUR_CONTAINER_ID_2
    kind: lxc
    idle_minutes: 30
    stop_mode: shutdown
    services:
      - name: YOUR_SERVICE_NAME_3
        host: YOUR_CONTAINER_IP_2
        port: YOUR_APP_PORT_3
        domain: YOUR_FULL_DOMAIN_3
```

**Configuration Sections**:

#### `global`
- **idle_minutes**: Default idle timeout for all containers (can be overridden per container)
- **allowed_cidrs**: Optional CIDR ranges (for documentation purposes)

#### `containers[]`
- **vmid**: Proxmox container ID (e.g., `100`, `106`, `111`)
- **kind**: Container type - `lxc` for LXC containers, `vm` for VMs
- **idle_minutes**: Minutes of inactivity before auto-stop (overrides global setting)
- **stop_mode**: Shutdown method
  - `shutdown` - Graceful shutdown (recommended)
  - `stop` - Force stop

#### `services[]` (nested inside each container)
- **name**: Service identifier for logging (e.g., `n8n`, `docmost`)
- **host**: Container's internal IP address
- **port**: Application port inside the container
- **domain**: Full domain that accesses this service (e.g., `app.example.com`)

**How to find container IP:**
```bash
# From Proxmox host
pct exec YOUR_VMID -- ip addr show eth0
```

### 6. Create Docker Networks

Create the networks that Traefik and wake-lxc will use:

```bash
# Replace network names if your Traefik uses different ones
docker network create YOUR_FRONTEND_NETWORK_NAME
docker network create YOUR_BACKEND_NETWORK_NAME
```

Then update `docker-compose.yml` to match:

```yaml
networks:
  frontend:
    external: true
    name: YOUR_FRONTEND_NETWORK_NAME
  backend:
    external: true
    name: YOUR_BACKEND_NETWORK_NAME
```

**Note**: If Traefik already has networks, use those names instead of creating new ones.

### 7. Set Up Traefik Integration

Add to your Traefik dynamic configuration (e.g., `traefik/data/config.yaml`):

```yaml
http:
  routers:
    YOUR_SERVICE_NAME:
      entryPoints:
        - "https"
      rule: "Host(`YOUR_FULL_DOMAIN`)"
      middlewares:
        - default-whitelist
        - default-headers
        - https-redirectscheme
      tls: {}
      service: wake-lxc-proxy

  services:
    wake-lxc-proxy:
      loadBalancer:
        servers:
          - url: "http://wake-lxc:8080"

```

**Replace placeholders:**
- `YOUR_SERVICE_NAME`: Router name (e.g., `n8n-router`)
- `YOUR_FULL_DOMAIN`: Complete domain (e.g., `n8n.example.com`)
- `YOUR_LAN_CIDR`: Your local network range
- `YOUR_DOCKER_*_CIDR`: Docker network ranges

**Find Docker network CIDRs:**
```bash
docker network inspect YOUR_FRONTEND_NETWORK_NAME | grep Subnet
docker network inspect YOUR_BACKEND_NETWORK_NAME | grep Subnet
```

**Key Points**:
- Route all managed services through `wake-lxc-proxy`
- Apply IP whitelisting at Traefik level for security
- wake-lxc listens on port 8080 inside Docker network
- Each service domain must match the `domain` field in config.yaml

### 8. Deploy the Service

```bash
# Build and start
docker compose up -d

# View logs
docker compose logs -f wake-lxc

# Check status
docker compose ps
```

## 📊 Monitoring and Debugging

### View Real-Time Logs

```bash
# Follow all logs
docker compose logs -f wake-lxc

# Filter for specific requests
docker compose logs wake-lxc | grep "REQ-"

# Check container start events
docker compose logs wake-lxc | grep "Starting container"
```

### Enable Debug Mode

Set `LOG_LEVEL=DEBUG` in `.env` for detailed logging:

```bash
LOG_LEVEL=DEBUG
```

Debug logs include:
- HTTP request/response headers
- Proxmox API interactions
- Circuit breaker state changes
- Lock acquisition/release events

### Health Check Endpoint

```bash
curl http://localhost:8080/healthz
```

Returns JSON with:
- Service status
- Configured containers
- Proxmox connection status

## 🔧 Configuration Examples

### Example 1: Multiple Services on One Container

```yaml
containers:
  - vmid: 100
    kind: lxc
    idle_minutes: 15
    stop_mode: shutdown
    services:
      - name: blog
        host: 10.0.0.10
        port: 8080
        domain: blog.example.com
      
      - name: wiki
        host: 10.0.0.10
        port: 8081
        domain: wiki.example.com
      
      - name: docs
        host: 10.0.0.10
        port: 8082
        domain: docs.example.com
```

### Example 2: Different Idle Times

```yaml
global:
  idle_minutes: 10  # Default for containers without specific setting

containers:
  - vmid: 100
    kind: lxc
    idle_minutes: 5        # Aggressive shutdown for dev
    stop_mode: shutdown
    services:
      - name: dev-app
        host: 10.0.0.10
        port: 8080
        domain: dev.example.com

  - vmid: 101
    kind: lxc
    idle_minutes: 60       # Keep production up longer
    stop_mode: shutdown
    services:
      - name: prod-db
        host: 10.0.0.11
        port: 5432
        domain: db.example.com

  - vmid: 102
    kind: lxc
    idle_minutes: 120      # Very long for backups
    stop_mode: shutdown
    services:
      - name: backup
        host: 10.0.0.12
        port: 9000
        domain: backup.example.com
```

### Example 3: Mixed Stop Modes

```yaml
containers:
  - vmid: 100
    kind: lxc
    idle_minutes: 10
    stop_mode: shutdown    # Graceful - waits for app to close
    services:
      - name: webapp
        host: 10.0.0.10
        port: 8080
        domain: app.example.com

  - vmid: 101
    kind: lxc
    idle_minutes: 10
    stop_mode: stop        # Force stop - immediate
    services:
      - name: test-env
        host: 10.0.0.11
        port: 8080
        domain: test.example.com
```

## 🔒 Security Best Practices

### 1. Secure Proxmox Token

- Create dedicated service user with minimal permissions
- Use unique token for wake-lxc (don't reuse)
- Never commit `secrets/` directory to Git
- Set token expiration date for production environments

### 2. Network Isolation

- Keep wake-lxc in internal Docker network
- Don't expose port 8080 to host
- Route all traffic through Traefik
- Use separate frontend/backend networks

### 3. IP Whitelisting

Apply IP restrictions at Traefik level:

```yaml
middlewares:
  default-whitelist:
    ipAllowList:
      sourceRange:
        - "YOUR_LAN_CIDR"       # e.g., 192.168.1.0/24
        - "YOUR_VPN_CIDR"       # e.g., 10.8.0.0/24
```

### 4. TLS Configuration

For production with valid Proxmox SSL:

```bash
PROXMOX_VERIFY_TLS=true
```

## 🐛 Troubleshooting

### Container Won't Start

**Symptom**: 503 error, container stays stopped

**Check**:
1. Proxmox API token permissions
   ```bash
   docker logs wake-lxc | grep "401"
   ```

2. Container VMID exists and is correct
   ```bash
   docker logs wake-lxc | grep "VMID"
   ```

3. Backend host/port is correct in config.yaml
   ```bash
   # Test from Proxmox host
   curl http://CONTAINER_IP:PORT
   ```

4. Domain in config.yaml matches Traefik router

### "Permission Denied" on Secret File

**Symptom**: `Failed to read secret from /run/secrets/proxmox_token_value`

**Fix**:
```bash
chmod 644 secrets/proxmox_token_value.txt
docker compose restart wake-lxc
```

### Circuit Breaker Open

**Symptom**: Logs show "Circuit breaker is OPEN"

**Cause**: 3+ consecutive start failures

**Fix**:
1. Check Proxmox connectivity
2. Verify API token is valid
3. Wait 5 minutes for circuit breaker to reset
4. Restart service: `docker compose restart wake-lxc`

### Containers Not Stopping

**Symptom**: Idle containers stay running

**Check**:
1. Verify idle_minutes is set in config.yaml (per-container or global)
2. Check activity logs:
   ```bash
   docker logs wake-lxc | grep "idle check"
   ```

3. Ensure no active WebSocket connections
4. Verify LOG_ACTIVITY_INTERVAL is set (default: 60 seconds)

### Wrong Domain Routing

**Symptom**: Wrong container starts or 404 errors

**Check**:
1. Domain in config.yaml must match Traefik router exactly
2. Check for duplicate domains across services
3. Verify Traefik can reach wake-lxc:
   ```bash
   docker exec traefik ping wake-lxc
   ```

## 📈 Performance Tuning

### Circuit Breaker Configuration

Edit `asgi_app.py`:

```python
CIRCUIT_BREAKER_THRESHOLD = 3    # Failures before opening
CIRCUIT_BREAKER_TIMEOUT = 300    # Seconds before retry (5 min)
```

### Container Start Timeout

Edit `asgi_app.py`:

```python
async with httpx.AsyncClient(timeout=30) as client:  # Adjust timeout
```

### Idle Check Interval

In `.env`:

```bash
LOG_ACTIVITY_INTERVAL=60  # Seconds between idle checks
```

Lower values = more responsive shutdown, higher CPU usage

## 🏗️ Architecture

```
User Request
    ↓
Traefik (IP whitelist, TLS termination)
    ↓
wake-lxc:8080 (Docker network)
    ↓
├─ Match domain to service in config.yaml
├─ Find container VMID for service
├─ Check container status (Proxmox API)
├─ Start if stopped (with lock)
├─ Show starting page (SSE updates)
└─ Proxy to backend (HTTP/WebSocket)
    ↓
LXC Container (service host:port)
```

## 🤝 Contributing

Contributions welcome! Please:
1. Fork the repository
2. Create a feature branch
3. Submit a pull request

## 📝 License

MIT License - See LICENSE file

## 🙋 Support

- **Issues**: GitHub Issues
- **Discussions**: GitHub Discussions
- **Documentation**: This README

## 🔄 Updates

Check for updates:

```bash
git pull
docker compose build --no-cache
docker compose up -d
```

---

**Made with ❤️ for homelab enthusiasts**
