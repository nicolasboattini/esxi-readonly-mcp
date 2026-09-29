# esxi-readonly-mcp

MCP de **solo lectura** para ESXi (pyVmomi, transporte stdio). No tiene herramientas para modificar,
apagar, borrar ni consolidar nada.

## Herramientas

| Tool | Qué devuelve |
|---|---|
| `verificar_permisos` | Usuario conectado y si tiene privilegios de escritura |
| `host_info` | Modelo, CPU, cores/hilos/HT, RAM, versión/build, uptime, política de energía, controladoras |
| `datastores` | Capacidad/usado/libre/%, % provisionado, disco físico de cada datastore, VMs |
| `salud_hardware` | Sensores (temperatura, ventiladores, fuentes, memoria, CPU) y estado de RAID/discos si hay proveedor CIM |
| `eventos` | Errores, advertencias, alarmas, tareas y logins fallidos (con la hora corregida por el desfase de ESXi) |
| `config_host` | Licencia, NTP y desfase del reloj, SSH/Shell, servicios, perfil de imagen, syslog, NICs, vSwitches, portgroups |
| `vms` | Estado, vCPU, RAM asignada/activa/consumida, balloon/swap, Tools, heartbeat, sync de hora, NICs, discos (thin/thick, usado real por datastore), espacio en el guest |
| `snapshots` | Todos, con fecha, antigüedad, tamaño real y marca > N días |
| `performance` | CPU %, Ready %, Co-Stop %, balloon/swap, latencia por disco virtual y datastore, IOPS (prom/p95/máx) |
| `historial_perf` | Lo mismo pero sobre semanas, desde la base local que llena `--collect` |
| `top_archivos` | Top N archivos por datastore, con VM dueña y posibles huérfanos |
| `overcommit` | vCPU vs hilos, RAM asignada vs física |
| `diagnostico_completo` | Todo lo anterior junto |

## 1. Usuario de solo lectura en ESXi (recomendado)

Host Client (`https://<esxi>/ui`):
1. **Host → Manage → Security & users → Roles**: clonar *Read-only* como `ReadOnly+Browse` y agregarle
   **Datastore → Browse datastore** (sin esto, `top_archivos` no ve ISOs ni archivos huérfanos).
2. **Users → Add user**: `mcp-readonly`.
3. **Host → Actions → Permissions → Add user**: `mcp-readonly` con el rol `ReadOnly+Browse`.

Así ESXi rechaza cualquier cambio, aunque algo intentara hacerlo.

## 2. Instalación

```powershell
cd <carpeta>\esxi-readonly-mcp
uv sync
uv run python -m keyring set esxi-readonly mcp-readonly   # pide la password, queda en el Administrador de credenciales de Windows
```

## 3. Registrar en Claude Code

```powershell
claude mcp add esxi-readonly --scope user -e ESXI_HOST=<ip-esxi> -e ESXI_USER=mcp-readonly -- uv run --directory "<carpeta>\esxi-readonly-mcp" python server.py
```

Variables opcionales: `ESXI_PORT` (443), `ESXI_VERIFY_SSL=1` (por defecto no verifica el certificado
autofirmado), `ESXI_PERF_DB` (ruta de la base de historial).

## 4. Historial de performance (ESXi sin vCenter guarda solo ~1 h)

Programar la recolección cada hora en horario laboral:

```powershell
$a = New-ScheduledTaskAction -Execute "uv" -Argument "run --directory `"<carpeta>\esxi-readonly-mcp`" python server.py --collect"
$t = New-ScheduledTaskTrigger -Daily -At 7:05am
$t.Repetition = (New-ScheduledTaskTrigger -Once -At 7:05am -RepetitionInterval (New-TimeSpan -Hours 1) -RepetitionDuration (New-TimeSpan -Hours 13)).Repetition
Register-ScheduledTask -TaskName "ESXi perf collect" -Action $a -Trigger $t -User $env:USERNAME
```

(La tarea necesita `ESXI_HOST` y `ESXI_USER` como variables de entorno del usuario:
`[Environment]::SetEnvironmentVariable("ESXI_HOST","<ip>","User")`, ídem `ESXI_USER`.)

Después de 2–4 semanas: `historial_perf(dias=28, solo_horario_laboral=True)`.
