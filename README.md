<!-- mcp-name: io.github.nicolasboattini/esxi-readonly-mcp -->
<div align="center">

# 🔍 esxi-readonly-mcp

**Diagnóstico completo de VMware ESXi desde tu asistente de IA, sin poder romper nada.**

Un servidor [MCP](https://modelcontextprotocol.io) que le da a Claude (o a cualquier cliente MCP) visibilidad total
sobre tu host ESXi — CPU Ready, latencia de disco, snapshots, espacio real, salud del hardware, eventos —
**sin una sola herramienta de escritura**.

[![PyPI](https://img.shields.io/pypi/v/esxi-readonly-mcp?logo=pypi&logoColor=white)](https://pypi.org/project/esxi-readonly-mcp/)
![Python](https://img.shields.io/badge/python-3.10%2B-3776AB?logo=python&logoColor=white)
![MCP](https://img.shields.io/badge/MCP-stdio-6E56CF)
![ESXi](https://img.shields.io/badge/ESXi-6.7%2B-607078?logo=vmware&logoColor=white)
![Solo lectura](https://img.shields.io/badge/modo-solo%20lectura-2EA44F)
![Licencia](https://img.shields.io/badge/licencia-MIT-blue)

</div>

---

## ¿Por qué?

Tenés un ESXi standalone (sin vCenter), el ERP anda lento y alguien propone gastar miles de dólares en hardware.
Antes de comprar, necesitás saber **qué recurso es el cuello de botella de verdad**. Eso implica mirar métricas
que el Host Client esconde o no guarda (CPU Ready, Co-Stop, latencia por disco virtual), cruzar el espacio
provisionado contra el real y revisar snapshots, logs y sensores.

Con este MCP le preguntás a Claude *"¿por qué está lenta la VM del ERP?"* y él mismo consulta el host, cruza los datos
y te responde con números.

Y como vas a darle acceso a un asistente de IA a tu infraestructura de producción, **el servidor no expone ninguna
operación que modifique algo**: no puede apagar, borrar, crear, consolidar ni reconfigurar nada. Combinado con un
usuario de ESXi con rol de solo lectura, la garantía es doble.

## ✨ Qué podés preguntarle

> *"Hacé un diagnóstico completo del host y decime cuál es el cuello de botella."*
>
> *"¿Qué VM tiene más CPU Ready en la última hora?"*
>
> *"¿Cuánto ocupa realmente cada VM en cada datastore?"*
>
> *"¿Hay snapshots de más de 3 días?"*
>
> *"¿Qué archivos grandes hay en los datastores que no pertenecen a ninguna VM?"*
>
> *"¿Algún sensor de hardware en amarillo o rojo? ¿Hubo errores de disco esta semana?"*
>
> *"¿Tengo overcommit de CPU? ¿Cuántas vCPU tengo por core físico?"*
>
> *"¿Conviene más comprar RAM o cambiar el procesador?"*

## 🧰 Herramientas

| Herramienta | Qué devuelve |
|---|---|
| `diagnostico_completo` | Todo lo de abajo en una sola llamada — el punto de partida ideal |
| `host_info` | Fabricante, modelo, serie, BIOS, CPU (cores / hilos / HT), RAM total y usada, versión y build de ESXi, uptime, política de energía, controladoras de storage |
| `salud_hardware` | Sensores (temperaturas, ventiladores, fuentes, voltajes), memoria, CPU y estado de RAID/discos físicos si el host tiene el proveedor CIM del fabricante. Lista primero lo que no está en verde |
| `config_host` | Licencia (clave enmascarada) y vencimiento, NTP y **desfase real del reloj**, SSH/Shell, servicios activos, perfil de imagen, syslog, placas de red con velocidad de enlace, vSwitches, portgroups/VLANs, VMkernel |
| `datastores` | Capacidad, usado, libre, % de uso, **% provisionado** (sobre-asignación thin), disco físico detrás de cada datastore y VMs que lo usan |
| `vms` | Por cada VM: estado, vCPU, RAM asignada / activa / consumida, ballooning, swap, reservas, límites, shares, VMware Tools, heartbeat, sincronización horaria, NICs (e1000 vs vmxnet3), controladoras, discos (thin/thick, **usado real**), espacio libre dentro del guest |
| `snapshots` | Todos los snapshots con fecha, antigüedad, tamaño real y marca de los que superan N días |
| `performance` | CPU %, **CPU Ready %**, **Co-Stop %**, latencia de CPU, memoria activa, ballooning, swap, **latencia por disco virtual y por datastore**, IOPS. Devuelve promedio, p95 y máximo con el momento en que ocurrió |
| `historial_perf` | Las mismas métricas sobre **semanas**, filtrables por horario laboral (ver [Historial](#-historial-de-performance)) |
| `eventos` | Errores, advertencias, alarmas, tareas y logins fallidos, con la hora corregida por el desfase del reloj de ESXi |
| `top_archivos` | Los archivos más grandes de cada datastore, a qué VM pertenecen y **posibles huérfanos** (vmdk sin VM registrada) |
| `overcommit` | vCPU encendidas vs hilos físicos, RAM asignada vs física, VM más grande |
| `verificar_permisos` | Con qué usuario se conecta y si ese usuario tiene privilegios de escritura en ESXi |

Todas las respuestas vienen con valores de referencia (por ejemplo: *CPU Ready > 10 % = contención*) para que el
asistente pueda interpretarlas sin adivinar.

## 🔒 Seguridad: cómo se garantiza que es solo lectura

**1. En el código.** El servidor solo lee propiedades y llama a estos métodos de la API de vSphere, todos de consulta:

| Método | Para qué |
|---|---|
| `CreateContainerView` / `Destroy` | Vista temporal de la propia sesión para listar objetos |
| `PerformanceManager.QueryPerf` y afines | Métricas de performance |
| `HostDatastoreBrowser.SearchDatastoreSubFolders_Task` | Listar archivos de los datastores |
| `EventManager.QueryEvents` / `CreateCollectorForEvents` | Leer eventos (el colector es de la sesión y se destruye al terminar) |
| `AuthorizationManager.HasPrivilegeOnEntity` / `RetrieveEntityPermissions` | Verificar los permisos del propio usuario |
| `OptionManager.QueryOptions`, `HostImageConfigManager.*Get*` | Leer opciones y perfil de imagen |

No hay ninguna herramienta MCP para modificar nada, y el servidor le indica al asistente que no existen.

**2. En ESXi.** Usá un usuario dedicado con un rol de solo lectura (paso 1 de la instalación). Aunque algo
intentara escribir, ESXi lo rechazaría. `verificar_permisos` te confirma que quedó bien configurado:

```json
{ "usuario": "mcp-readonly", "roles": ["ReadOnly+Browse"],
  "privilegios_escritura_detectados": [], "solo_lectura_garantizado_por_ESXi": true }
```

**3. Credenciales.** La contraseña nunca va en archivos de configuración: se guarda en el almacén de credenciales
del sistema operativo (Administrador de credenciales de Windows, Keychain en macOS, Secret Service en Linux)
mediante [`keyring`](https://pypi.org/project/keyring/). Las claves de licencia se muestran enmascaradas.

## 🚀 Instalación

### Requisitos

- [`uv`](https://docs.astral.sh/uv/) (se encarga de Python y de las dependencias)
- Acceso por red al puerto 443 del ESXi
- Un cliente MCP: [Claude Code](https://docs.claude.com/en/docs/claude-code), Claude Desktop u otro

### 1. Crear un usuario de solo lectura en ESXi

En el Host Client (`https://<ip-esxi>/ui`):

1. **Manage → Security & users → Roles → Add role** — nombre `ReadOnly+Browse`:
   - ✅ **System** (Anonymous, Read, View)
   - ✅ **Datastore → Browse datastore** — *solo ese; nada de Delete, FileManagement ni AllocateSpace*
2. **Manage → Security & users → Users → Add user** — por ejemplo `mcp-readonly`.
   Dejá **Enable shell access** desmarcado.
3. **Host → Actions → Permissions → Add user** — elegí `mcp-readonly`, el rol `ReadOnly+Browse`
   y marcá **Propagate to all children**.

> Sin *Browse datastore* el MCP funciona igual, pero `top_archivos` solo ve archivos de VMs registradas
> (no ISOs ni huérfanos) y el espacio "usado" de discos thin es aproximado.

### 2. Guardar la contraseña en el almacén del sistema

```bash
uvx esxi-readonly-mcp --set-password
```

Te pide el usuario de ESXi y la contraseña (sin mostrarla) y la guarda en el almacén de credenciales del sistema.
No hace falta clonar nada: `uvx` descarga el paquete de PyPI y lo ejecuta.

### 3. Registrar el servidor en tu cliente MCP

**Claude Code**

```bash
claude mcp add esxi-readonly --scope user \
  -e ESXI_HOST=192.0.2.10 -e ESXI_USER=mcp-readonly \
  -- uvx esxi-readonly-mcp
```

**Claude Desktop u otro cliente** (en `claude_desktop_config.json` o equivalente):

```json
{
  "mcpServers": {
    "esxi-readonly": {
      "command": "uvx",
      "args": ["esxi-readonly-mcp"],
      "env": { "ESXI_HOST": "192.0.2.10", "ESXI_USER": "mcp-readonly" }
    }
  }
}
```

> En Windows usá la ruta completa a `uvx.exe` si el cliente no lo encuentra en el `PATH`
> (por ejemplo `C:\\Users\\<usuario>\\.local\\bin\\uvx.exe`).

Reiniciá el cliente y pedile: *"verificá los permisos del MCP de ESXi"*.

### Desde el código fuente

```bash
git clone https://github.com/nicolasboattini/esxi-readonly-mcp.git
cd esxi-readonly-mcp
uv sync
uv run esxi-readonly-mcp --set-password
```

Y en el cliente MCP usá `uv run --directory /ruta/a/esxi-readonly-mcp esxi-readonly-mcp` como comando.

### Probar sin cliente MCP

```bash
ESXI_HOST=192.0.2.10 ESXI_USER=mcp-readonly uv run python -c "from esxi_readonly_mcp import server; import json; print(json.dumps(server._host_info(), indent=2))"
```

(En PowerShell: `$env:ESXI_HOST="192.0.2.10"; $env:ESXI_USER="mcp-readonly"; uv run python -c "..."`)

## ⚙️ Configuración

| Variable | Obligatoria | Default | Descripción |
|---|---|---|---|
| `ESXI_HOST` | ✅ | — | IP o nombre del ESXi (o del vCenter) |
| `ESXI_USER` | ✅ | — | Usuario de solo lectura |
| `ESXI_PASSWORD` | | — | Solo si no podés usar `keyring` (queda en texto plano en la config: evitalo) |
| `ESXI_PORT` | | `443` | Puerto de la API |
| `ESXI_VERIFY_SSL` | | `0` | `1` para verificar el certificado (por defecto se acepta el autofirmado de ESXi) |
| `ESXI_PERF_DB` | | `~/.esxi-readonly-mcp/perf_historial.db` | Ruta de la base SQLite del historial de performance |

## 📈 Historial de performance

Un ESXi **sin vCenter guarda solo la última hora** de métricas (muestras cada 20 s). Para ver los picos reales
de las últimas semanas en horario laboral, el servidor trae un modo recolector que guarda esa hora en una base
SQLite local:

```bash
uvx esxi-readonly-mcp --collect
```

Programalo cada hora y después pedile a Claude, por ejemplo: *"mostrame el historial de performance de las
últimas 4 semanas en horario laboral"* (`historial_perf(dias=28, solo_horario_laboral=True)`).

**Windows (Programador de tareas)**

```powershell
[Environment]::SetEnvironmentVariable("ESXI_HOST", "192.0.2.10", "User")
[Environment]::SetEnvironmentVariable("ESXI_USER", "mcp-readonly", "User")

$uvx = (Get-Command uvx).Source
$a  = New-ScheduledTaskAction -Execute $uvx -Argument 'esxi-readonly-mcp --collect'
$t  = New-ScheduledTaskTrigger -Daily -At 7:05am
$t.Repetition = (New-ScheduledTaskTrigger -Once -At 7:05am -RepetitionInterval (New-TimeSpan -Hours 1) -RepetitionDuration (New-TimeSpan -Hours 13)).Repetition
Register-ScheduledTask -TaskName "ESXi perf collect" -Action $a -Trigger $t -User $env:USERNAME
```

**Linux / macOS (cron)**

```cron
5 7-20 * * 1-5  ESXI_HOST=192.0.2.10 ESXI_USER=mcp-readonly $HOME/.local/bin/uvx esxi-readonly-mcp --collect
```

> La base contiene nombres de VMs y métricas. Por defecto vive en tu carpeta de usuario, fuera de cualquier repo.

## 🧪 Compatibilidad

| | Estado |
|---|---|
| ESXi 6.7 U3 standalone | ✅ Probado en producción |
| ESXi 7.x / 8.x standalone | Debería funcionar (misma API); reportá cualquier problema |
| vCenter | Soportado en el código (usa `QueryEvents` e intervalos históricos), sin probar a fondo |
| pyVmomi 9.x + mcp 1.x | ✅ Probado (`mcp` 2.x cambió la API: el proyecto fija `mcp<2`) |

## ⚠️ Limitaciones conocidas

- **Métricas históricas:** sin vCenter, `performance` solo ve ~1 hora. Usá `--collect` + `historial_perf`.
- **Eventos:** ESXi standalone guarda los últimos ~1000 eventos. Si un sistema de monitoreo abre sesiones
  constantemente (por ejemplo, Zabbix mal configurado), eso puede cubrir solo unas horas.
- **RAID y discos físicos:** `salud_hardware` los muestra solo si ESXi tiene el proveedor CIM del fabricante
  (Dell, HPE, Lenovo). Si no, revisalos en iDRAC / iLO / XCC.
- **Lista de VIBs/drivers:** requiere `Host.Config.Image`, que también permite modificar la imagen; el MCP no
  lo pide. Alternativa: `esxcli software vib list` por SSH.
- **Dentro del guest:** el MCP ve lo que reporta VMware Tools (unidades, espacio libre, IP), pero no archivos
  ni procesos dentro de la VM.

## 🛠️ Solución de problemas

| Síntoma | Causa probable |
|---|---|
| `No hay password...` | Falta `uvx esxi-readonly-mcp --set-password` |
| `vim.fault.InvalidLogin` | Usuario o contraseña incorrectos, o el usuario no tiene permiso asignado en el host |
| `top_archivos` avisa `NoPermission` | Al rol le falta **Datastore → Browse datastore** |
| `performance` sin datos con `intervalo="semana"` | Normal en ESXi sin vCenter: usá `realtime` o `historial_perf` |
| `No module named 'mcp.server.fastmcp'` | Se instaló `mcp` 2.x a mano; `uv sync` respeta `mcp<2` |
| El cliente no encuentra `uvx` | Poné la ruta absoluta a `uvx` en `command` |
| Horas raras en eventos | El reloj de ESXi está desfasado (sin NTP); `config_host` muestra el desfase y `eventos` ya lo corrige |

## 📁 Estructura

```
esxi-readonly-mcp/
├── src/esxi_readonly_mcp/
│   └── server.py    # servidor MCP + --collect + --set-password
├── pyproject.toml   # paquete PyPI (mcp<2, pyvmomi, keyring)
├── server.json      # ficha para el registro oficial de MCP
├── uv.lock
└── README.md
```

## 🤝 Contribuir

Issues y pull requests bienvenidos, sobre todo:

- Pruebas en ESXi 7/8 y vCenter
- Nuevas métricas o chequeos **de solo lectura**

La regla del proyecto es una sola: **ninguna herramienta puede modificar el entorno.** Un PR que agregue
operaciones de escritura no se va a aceptar, aunque sea "opcional".

## 📄 Licencia

[MIT](LICENSE)
