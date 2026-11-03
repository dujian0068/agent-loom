#!/usr/bin/env bash
# Install a credential-free local runner; does not change Nginx or API credentials.
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo 'Run this installer with sudo; it creates the dedicated execution account and system service.' >&2
  exit 1
fi
if [[ $# -ne 3 ]]; then
  echo "Usage: $0 WORKER_USER WORKSPACE_ROOT EXEC_SERVICE_PY" >&2
  exit 1
fi
worker_user=$1
workspace_root=$(realpath -e -- "$2")
runner_source=$(realpath -e -- "$3")
[[ $worker_user =~ ^[a-z_][a-z0-9_-]*$ && $workspace_root =~ ^/[a-zA-Z0-9_./-]+$ ]]
[[ -d $workspace_root && -f $runner_source ]]
worker_uid=$(id -u "$worker_user")
worker_group=$(id -gn "$worker_user")
[[ $worker_uid -ne 0 && $worker_group =~ ^[a-z_][a-z0-9_-]*$ ]]
[[ $(stat -c %u "$workspace_root") = "$worker_uid" ]]

runner_user=agentloom-exec
if ! id "$runner_user" >/dev/null 2>&1; then
  useradd --system --user-group --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin "$runner_user"
fi
[[ $(id -u "$runner_user") -ne 0 && $(id -u "$runner_user") -ne $worker_uid ]]
[[ $(getent passwd "$runner_user" | cut -d: -f7) = /usr/sbin/nologin ]]
# Never add this account to sudo or persistently to the Worker's groups.
if id -nG "$runner_user" | tr ' ' '\n' | grep -Eq '^(sudo|wheel|adm|docker)$'; then
  echo 'The execution account has privileged group membership; installation stopped.' >&2
  exit 1
fi

backup=$(mktemp -d /var/backups/agentloom-exec.XXXXXXXX)
unit=/etc/systemd/system/agentloom-exec.service
binary=/opt/agentloom-exec/exec_service.py
[[ ! -f $unit ]] || cp -a "$unit" "$backup/service"
[[ ! -f $binary ]] || cp -a "$binary" "$backup/exec_service.py"
original_mode=$(stat -c %a "$workspace_root")
original_group=$(stat -c %g "$workspace_root")
rollback() {
  local status=$?
  if [[ $status -ne 0 ]]; then
    systemctl stop agentloom-exec.service || true
    if [[ -f $backup/service ]]; then cp -a "$backup/service" "$unit"; else rm -f "$unit"; fi
    if [[ -f $backup/exec_service.py ]]; then cp -a "$backup/exec_service.py" "$binary"; fi
    chmod "$original_mode" "$workspace_root"
    chgrp "$original_group" "$workspace_root"
    systemctl daemon-reload
    [[ ! -f $backup/service ]] || systemctl start agentloom-exec.service || true
    echo "Execution service installation failed; prior files restored. Backup: $backup" >&2
  fi
}
trap rollback EXIT

install -d -m 0755 -o root -g root /opt/agentloom-exec /var/lib/agentloom-exec /var/lib/agentloom-exec/workspaces
install -m 0644 -o root -g root "$runner_source" "$binary"
# Only the workspace root is group-traversable. Its private parent directories,
# platform.env, database and model credentials retain their original modes.
chgrp "$worker_group" "$workspace_root"
chmod 2750 "$workspace_root"
cat > "$unit" <<EOF
[Unit]
Description=AgentLoom local command execution
After=local-fs.target

[Service]
Type=simple
User=$runner_user
Group=$worker_group
ExecStart=/usr/bin/python3 -I $binary --socket /run/agentloom-exec/runner.sock --workspaces /var/lib/agentloom-exec/workspaces --worker-uid $worker_uid
WorkingDirectory=/var/lib/agentloom-exec
RuntimeDirectory=agentloom-exec
RuntimeDirectoryMode=0750
BindPaths=$workspace_root:/var/lib/agentloom-exec/workspaces
ReadWritePaths=/var/lib/agentloom-exec/workspaces
ProtectHome=yes
ProtectSystem=strict
PrivateTmp=yes
PrivateDevices=yes
ProtectKernelTunables=yes
ProtectControlGroups=yes
NoNewPrivileges=yes
CapabilityBoundingSet=
RestrictSUIDSGID=yes
UMask=0007
KillMode=control-group
TimeoutStopSec=8
TasksMax=128
MemoryMax=1G
Restart=on-failure
RestartSec=2

[Install]
WantedBy=multi-user.target
EOF
chmod 0644 "$unit"
systemd-analyze verify "$unit"
systemctl daemon-reload
systemctl enable agentloom-exec.service
systemctl restart agentloom-exec.service

# Verify as the actual Worker identity, including the different execution UID,
# output, file permissions, and the absence of inherited Platform credentials.
runuser -u "$worker_user" -- /usr/bin/python3 -I - "$workspace_root" "$(id -u "$runner_user")" <<'PY'
import json, os, pathlib, shutil, socket, sys, time, uuid
root, expected_uid = pathlib.Path(sys.argv[1]), int(sys.argv[2])

def call(payload):
    with socket.socket(socket.AF_UNIX) as client:
        client.settimeout(10)
        client.connect('/run/agentloom-exec/runner.sock')
        client.sendall(json.dumps(payload).encode() + b'\n')
        with client.makefile('rb') as response:
            return json.loads(response.readline())

for attempt in range(30):
    try:
        health = call({'op': 'ping', 'protocol': 1})
        assert health.get('ready') and health['uid'] == expected_uid
        break
    except (OSError, AssertionError):
        if attempt == 29:
            raise
        time.sleep(0.2)
scope = uuid.uuid4().hex
workspace = root / scope
workspace.mkdir(mode=0o2770)
workspace.chmod(0o2770)
try:
    command = "python3 -c 'import json, os; from pathlib import Path; Path(\"probe.txt\").write_text(\"verified\"); print(json.dumps({\"uid\":os.getuid(),\"env_keys\":sorted(os.environ)}))'"
    result = call({'op':'exec','protocol':1,'implementation':health['implementation'],'scope':scope,'command':command,'timeout':10})
    assert result.get('exit_code') == 0, result
    actual = json.loads(result['stdout'])
    assert actual['uid'] == expected_uid
    assert not any(key.startswith(('PLATFORM_', 'LANGCHAIN_', 'LANGSMITH_')) or 'KEY' in key or 'PASSWORD' in key for key in actual['env_keys'])
    assert (workspace / 'probe.txt').read_text() == 'verified'
    assert (workspace / 'probe.txt').stat().st_mode & 0o777 == 0o660
finally:
    shutil.rmtree(workspace)
print('Execution account, Unix socket, workspace writes and clean command environment: verified.')
PY

echo "Local execution service installed. Backup: $backup"
echo 'Platform setting: PLATFORM_EXEC_SOCKET_PATH=/run/agentloom-exec/runner.sock'
