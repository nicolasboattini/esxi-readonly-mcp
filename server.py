"""MCP de SOLO LECTURA para VMware ESXi (standalone o vCenter).

No expone ninguna operacion de escritura. Los unicos metodos de la API de vSphere que invoca son:
  CreateContainerView / ContainerView.Destroy  (vista temporal de la propia sesion, para listar objetos)
  PerformanceManager.QueryPerf / QueryAvailablePerfMetric / QueryPerfProviderSummary
  HostDatastoreBrowser.SearchDatastoreSubFolders_Task  (listar archivos; requiere Datastore.Browse)
  AuthorizationManager.HasPrivilegeOnEntity  (verificar permisos del usuario)
El resto son lecturas de propiedades. Igual se recomienda usar un usuario con rol Read-only en ESXi.

Uso:
  python server.py             -> servidor MCP (stdio)
  python server.py --collect   -> guarda la ultima hora de metricas en perf_historial.db (para Task Scheduler)
"""
import atexit
import datetime as dt
import logging
import os
import sqlite3
import sys
import time
from pathlib import Path

from pyVim.connect import Disconnect, SmartConnect
from pyVmomi import vim, vmodl

logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("esxi-readonly")

KEYRING_SERVICE = "esxi-readonly"
DB_PATH = Path(os.environ.get("ESXI_PERF_DB", Path(__file__).with_name("perf_historial.db")))

# ---------------------------------------------------------------- conexion

_si = None


def _password() -> str:
    pw = os.environ.get("ESXI_PASSWORD")
    if pw:
        return pw
    try:
        import keyring

        pw = keyring.get_password(KEYRING_SERVICE, os.environ["ESXI_USER"])
    except Exception as e:  # noqa: BLE001
        log.warning("keyring no disponible: %s", e)
    if not pw:
        raise RuntimeError(
            f"No hay password. Guardala en el Administrador de credenciales de Windows con: "
            f"uv run python -m keyring set {KEYRING_SERVICE} {os.environ.get('ESXI_USER', '<usuario>')}"
        )
    return pw


def _disconnect():
    global _si
    if _si is not None:
        try:
            Disconnect(_si)
        except Exception:  # noqa: BLE001
            pass
        _si = None


atexit.register(_disconnect)


def si():
    global _si
    if _si is not None:
        try:
            if _si.content.sessionManager.currentSession:
                return _si
        except Exception:  # noqa: BLE001
            pass
        _si = None
    for var in ("ESXI_HOST", "ESXI_USER"):
        if not os.environ.get(var):
            raise RuntimeError(f"Falta la variable de entorno {var}")
    kw = dict(
        host=os.environ["ESXI_HOST"],
        user=os.environ["ESXI_USER"],
        pwd=_password(),
        port=int(os.environ.get("ESXI_PORT", "443")),
    )
    if os.environ.get("ESXI_VERIFY_SSL", "0") != "1":
        kw["disableSslCertValidation"] = True
    _si = SmartConnect(**kw)
    log.info("Conectado a %s como %s", kw["host"], kw["user"])
    return _si


def _objs(tipo):
    c = si().content
    view = c.viewManager.CreateContainerView(c.rootFolder, [tipo], True)
    try:
        return list(view.view)
    finally:
        view.Destroy()


# ---------------------------------------------------------------- helpers


def gb(b):
    return None if b is None else round(b / 1024**3, 2)


def _try(fn, default=None):
    try:
        return fn()
    except Exception:  # noqa: BLE001
        return default


def _iso(d):
    return d.astimezone().isoformat(timespec="seconds") if d else None


def _now():
    return dt.datetime.now(dt.timezone.utc)


def _moid(o):
    return getattr(o, "_moId", None)


def _files_by_key(vm):
    lex = vm.layoutEx
    return {f.key: f for f in (lex.file if lex else [])}


def _unit_size(unit, files):
    return sum((files[k].size or 0) for k in unit.fileKey if k in files)


# ---------------------------------------------------------------- 1. host


def _host_info():
    out = []
    for h in _objs(vim.HostSystem):
        hw, s = h.hardware, h.summary
        qs = s.quickStats
        mhz_total = hw.cpuInfo.hz / 1e6 * hw.cpuInfo.numCpuCores
        ram_used_gb = (qs.overallMemoryUsage or 0) / 1024
        out.append({
            "nombre": h.name,
            "fabricante": hw.systemInfo.vendor,
            "modelo": hw.systemInfo.model,
            "serial": _try(lambda: hw.systemInfo.serialNumber),
            "bios": _try(lambda: f"{hw.biosInfo.biosVersion} ({hw.biosInfo.releaseDate:%Y-%m-%d})"),
            "cpu_modelo": hw.cpuPkg[0].description.strip() if hw.cpuPkg else None,
            "sockets": hw.cpuInfo.numCpuPackages,
            "cores_fisicos": hw.cpuInfo.numCpuCores,
            "hilos_logicos": hw.cpuInfo.numCpuThreads,
            "hyperthreading_activo": _try(lambda: h.config.hyperThread.active),
            "cpu_mhz_por_core": round(hw.cpuInfo.hz / 1e6),
            "cpu_uso_mhz": qs.overallCpuUsage,
            "cpu_uso_pct": round(100 * (qs.overallCpuUsage or 0) / mhz_total, 1),
            "ram_total_gb": gb(hw.memorySize),
            "ram_usada_gb": round(ram_used_gb, 2),
            "ram_uso_pct": round(100 * ram_used_gb * 1024**3 / hw.memorySize, 1),
            "version": s.config.product.fullName,
            "build": s.config.product.build,
            "boot": _iso(h.runtime.bootTime),
            "uptime_dias": round((qs.uptime or 0) / 86400, 1),
            "estado_conexion": str(h.runtime.connectionState),
            "modo_mantenimiento": h.runtime.inMaintenanceMode,
            "politica_energia": _try(lambda: h.config.powerSystemInfo.currentPolicy.shortName),
            "controladoras_storage": _try(lambda: [
                {"dispositivo": a.device, "modelo": a.model, "driver": a.driver}
                for a in h.config.storageDevice.hostBusAdapter
            ]),
        })
    return out


# ---------------------------------------------------------------- 2. datastores


def _luns(h):
    out = {}
    for lun in _try(lambda: h.config.storageDevice.scsiLun, []) or []:
        if not isinstance(lun, vim.host.ScsiDisk):
            continue
        out[lun.canonicalName] = {
            "canonical": lun.canonicalName,
            "nombre": lun.displayName,
            "vendor": (lun.vendor or "").strip(),
            "modelo": (lun.model or "").strip(),
            "capacidad_gb": gb(lun.capacity.block * lun.capacity.blockSize),
            "ssd": getattr(lun, "ssd", None),
            "local": getattr(lun, "localDisk", None),
        }
    return out


def _datastores():
    luns = {}
    for h in _objs(vim.HostSystem):
        luns.update(_luns(h))
    ds_out = []
    used_luns = set()
    for ds in _objs(vim.Datastore):
        s = ds.summary
        used = s.capacity - s.freeSpace
        info = ds.info
        extents, vmfs = [], {}
        if isinstance(info, vim.host.VmfsDatastoreInfo):
            extents = [e.diskName for e in info.vmfs.extent]
            vmfs = {"vmfs_version": info.vmfs.version, "uuid": info.vmfs.uuid}
        used_luns.update(extents)
        ds_out.append({
            "nombre": ds.name,
            "tipo": s.type,
            "capacidad_gb": gb(s.capacity),
            "usado_gb": gb(used),
            "libre_gb": gb(s.freeSpace),
            "uso_pct": round(100 * used / s.capacity, 1) if s.capacity else None,
            "provisionado_gb": gb(used + (s.uncommitted or 0)),
            "provisionado_pct": round(100 * (used + (s.uncommitted or 0)) / s.capacity, 1) if s.capacity else None,
            "accesible": s.accessible,
            "discos_fisicos": [luns.get(e, {"canonical": e}) for e in extents],
            "vms": sorted(vm.name for vm in ds.vm),
            **vmfs,
        })
    return {
        "datastores": ds_out,
        "discos_sin_datastore": [l for k, l in luns.items() if k not in used_luns],
        "nota": "Los discos que ve ESXi son los volumenes virtuales de la controladora RAID (PERC). "
                "El nivel RAID y el estado de cada disco fisico se ven en iDRAC > Storage.",
    }


# ---------------------------------------------------------------- 3. VMs


def _vm_disks(vm):
    c = vm.config
    files = _files_by_key(vm)
    chains = {d.key: d.chain for d in (vm.layoutEx.disk if vm.layoutEx else [])}
    ctrl = {d.key: type(d).__name__.replace("vim.vm.device.", "")
            for d in c.hardware.device if isinstance(d, vim.vm.device.VirtualController)}
    out = []
    for d in c.hardware.device:
        if not isinstance(d, vim.vm.device.VirtualDisk):
            continue
        b = d.backing
        if isinstance(b, vim.vm.device.VirtualDisk.RawDiskMappingVer1BackingInfo):
            tipo = "RDM"
        elif getattr(b, "thinProvisioned", False):
            tipo = "thin"
        elif getattr(b, "eagerlyScrub", False):
            tipo = "thick eager zeroed"
        else:
            tipo = "thick lazy zeroed"
        chain = chains.get(d.key, [])
        base = _unit_size(chain[0], files) if chain else None
        total = sum(_unit_size(u, files) for u in chain) if chain else None
        out.append({
            "etiqueta": d.deviceInfo.label,
            "archivo": b.fileName,
            "datastore": _try(lambda: b.datastore.name),
            "controladora": ctrl.get(d.controllerKey),
            "tipo": tipo,
            "provisionado_gb": gb(d.capacityInBytes),
            "usado_base_gb": gb(base),
            "usado_con_snapshots_gb": gb(total),
            "deltas_snapshot": max(len(chain) - 1, 0),
        })
    return out


def _vms():
    out = []
    for vm in _objs(vim.VirtualMachine):
        c, s = vm.config, vm.summary
        qs = s.quickStats
        if c is None:
            out.append({"nombre": s.config.name, "estado": str(s.runtime.powerState), "error": "config inaccesible"})
            continue
        g = vm.guest
        out.append({
            "nombre": vm.name,
            "estado": str(vm.runtime.powerState),
            "so": c.guestFullName,
            "hw_version": c.version,
            "vcpu": c.hardware.numCPU,
            "cores_por_socket": c.hardware.numCoresPerSocket,
            "ram_asignada_gb": round(c.hardware.memoryMB / 1024, 1),
            "ram_activa_gb": round((qs.guestMemoryUsage or 0) / 1024, 2),
            "ram_consumida_gb": round((qs.hostMemoryUsage or 0) / 1024, 2),
            "ballooning_mb": qs.balloonedMemory,
            "swap_mb": qs.swappedMemory,
            "comprimida_mb": _try(lambda: qs.compressedMemory // 1024),
            "cpu_uso_mhz": qs.overallCpuUsage,
            "cpu_reserva_mhz": _try(lambda: c.cpuAllocation.reservation),
            "cpu_limite_mhz": _try(lambda: c.cpuAllocation.limit),
            "cpu_shares": _try(lambda: c.cpuAllocation.shares.level),
            "ram_reserva_mb": _try(lambda: c.memoryAllocation.reservation),
            "ram_limite_mb": _try(lambda: c.memoryAllocation.limit),
            "cpu_hot_add": c.cpuHotAddEnabled,
            "ram_hot_add": c.memoryHotAddEnabled,
            "uptime_dias": round((qs.uptimeSeconds or 0) / 86400, 1),
            "tools_estado": g.toolsRunningStatus,
            "tools_version_estado": g.toolsVersionStatus2,
            "tools_version": g.toolsVersion,
            "ip": g.ipAddress,
            "hostname_guest": g.hostName,
            "storage_usado_gb": gb(s.storage.committed) if s.storage else None,
            "storage_provisionado_gb": gb((s.storage.committed + s.storage.uncommitted)) if s.storage else None,
            "necesita_consolidar": vm.runtime.consolidationNeeded,
            "tiene_snapshots": vm.snapshot is not None,
            "discos": _vm_disks(vm),
            "discos_guest": [
                {"unidad": d.diskPath, "capacidad_gb": gb(d.capacity), "libre_gb": gb(d.freeSpace),
                 "uso_pct": round(100 * (d.capacity - d.freeSpace) / d.capacity, 1) if d.capacity else None}
                for d in (g.disk or [])
            ],
        })
    return out


# ---------------------------------------------------------------- 4. snapshots


def _walk(tree, parent=None):
    for n in tree or []:
        yield n, parent
        yield from _walk(n.childSnapshotList, n)


def _snapshots(dias_alerta=3):
    now = _now()
    out = []
    for vm in _objs(vim.VirtualMachine):
        if not vm.snapshot:
            continue
        files = _files_by_key(vm)
        lex = vm.layoutEx
        snap_layout = {_moid(sl.key): sl for sl in (lex.snapshot if lex else [])}
        # todas las cadenas conocidas: actual + la de cada snapshot
        chains = {}  # disk key -> lista de cadenas (lista de units)
        for d in (lex.disk if lex else []):
            chains.setdefault(d.key, []).append(d.chain)
        for sl in snap_layout.values():
            for d in sl.disk or []:
                chains.setdefault(d.key, []).append(d.chain)
        current = _moid(vm.snapshot.currentSnapshot)
        for n, parent in _walk(vm.snapshot.rootSnapshotList):
            sl = snap_layout.get(_moid(n.snapshot))
            size = 0
            if sl:
                for k in (sl.dataKey, sl.memoryKey):
                    if k is not None and k >= 0 and k in files:
                        size += files[k].size or 0
                # delta de cada disco: el eslabon siguiente a la cadena del snapshot
                for d in sl.disk or []:
                    my = [tuple(u.fileKey) for u in d.chain]
                    for ch in chains.get(d.key, []):
                        keys = [tuple(u.fileKey) for u in ch]
                        if len(keys) > len(my) and keys[: len(my)] == my:
                            size += _unit_size(ch[len(my)], files)
                            break
            edad = (now - n.createTime).total_seconds() / 86400
            out.append({
                "vm": vm.name,
                "snapshot": n.name,
                "descripcion": n.description,
                "padre": parent.name if parent else None,
                "creado": _iso(n.createTime),
                "antiguedad_dias": round(edad, 1),
                f"mas_de_{dias_alerta}_dias": edad > dias_alerta,
                "tamano_gb_aprox": gb(size),
                "con_memoria": _try(lambda: sl.memoryKey is not None and sl.memoryKey >= 0, False),
                "quiesced": n.quiesced,
                "es_el_actual": _moid(n.snapshot) == current,
            })
    return {
        "snapshots": out,
        "total": len(out),
        "nota": "Tamano = delta de discos creado despues del snapshot + archivos .vmsn/.vmem (aprox., igual que PowerCLI SizeGB).",
    }


# ---------------------------------------------------------------- 5. performance

VM_METRICS = {
    # nombre: (instancia, unidad resultante)
    "cpu.usage.average": ("", "%"),
    "cpu.ready.summation": ("", "% por vCPU"),
    "cpu.costop.summation": ("", "% por vCPU"),
    "cpu.latency.average": ("", "%"),
    "mem.active.average": ("", "MB"),
    "mem.consumed.average": ("", "MB"),
    "mem.vmmemctl.average": ("", "MB"),
    "mem.swapped.average": ("", "MB"),
    "mem.swapinRate.average": ("", "KBps"),
    "disk.maxTotalLatency.latest": ("", "ms"),
    "disk.usage.average": ("", "KBps"),
    "virtualDisk.totalReadLatency.average": ("*", "ms"),
    "virtualDisk.totalWriteLatency.average": ("*", "ms"),
    "virtualDisk.numberReadAveraged.average": ("*", "IOPS"),
    "virtualDisk.numberWriteAveraged.average": ("*", "IOPS"),
}
HOST_METRICS = {
    "cpu.usage.average": ("", "%"),
    "cpu.utilization.average": ("", "%"),
    "mem.usage.average": ("", "%"),
    "mem.vmmemctl.average": ("", "MB"),
    "mem.swapused.average": ("", "MB"),
    "disk.maxTotalLatency.latest": ("", "ms"),
    "disk.deviceLatency.average": ("*", "ms"),
    "disk.kernelLatency.average": ("*", "ms"),
    "disk.queueLatency.average": ("*", "ms"),
    "datastore.totalReadLatency.average": ("*", "ms"),
    "datastore.totalWriteLatency.average": ("*", "ms"),
    "datastore.numberReadAveraged.average": ("*", "IOPS"),
    "datastore.numberWriteAveraged.average": ("*", "IOPS"),
}
INTERVALOS = {"realtime": None, "dia": 300, "semana": 1800, "mes": 7200, "anio": 86400}


def _convert(metric, v, interval_s, ncpu):
    if metric in ("cpu.usage.average", "cpu.utilization.average", "mem.usage.average", "cpu.latency.average"):
        return v / 100
    if metric in ("cpu.ready.summation", "cpu.costop.summation"):
        return 100 * v / (interval_s * 1000) / max(ncpu or 1, 1)
    if metric.startswith("mem.") and metric != "mem.swapinRate.average":
        return v / 1024  # KB -> MB
    return v


def _instance_names():
    names = {}
    for ds in _objs(vim.Datastore):
        u = _try(lambda: ds.info.vmfs.uuid)
        if u:
            names[u] = ds.name
    for h in _objs(vim.HostSystem):
        for k, l in _luns(h).items():
            names[k] = f"{l['nombre']}"
    return names


def _query(entity, metrics, interval_id, max_samples=None, start=None, end=None, ncpu=1):
    """Devuelve [(timestamp, metric, instance, valor_convertido, unidad)]."""
    pm = si().content.perfManager
    by_id = {c.key: f"{c.groupInfo.key}.{c.nameInfo.key}.{c.rollupType}" for c in pm.perfCounter}
    avail = pm.QueryAvailablePerfMetric(entity=entity, intervalId=interval_id) or []
    ids = []
    for m in avail:
        name = by_id.get(m.counterId)
        if name not in metrics:
            continue
        want = metrics[name][0]
        if (want == "" and m.instance == "") or (want == "*" and m.instance != ""):
            ids.append(vim.PerformanceManager.MetricId(counterId=m.counterId, instance=m.instance))
    if not ids:
        return []
    spec = vim.PerformanceManager.QuerySpec(entity=entity, metricId=ids, intervalId=interval_id, format="normal")
    if start:
        spec.startTime, spec.endTime = start, end
    else:
        spec.maxSample = max_samples
    rows = []
    for em in pm.QueryPerf(querySpec=[spec]) or []:
        info = em.sampleInfo
        for series in em.value:
            name = by_id[series.id.counterId]
            for si_, v in zip(info, series.value):
                if v < 0:
                    continue
                rows.append((si_.timestamp, name, series.id.instance,
                             _convert(name, v, si_.interval, ncpu), metrics[name][1]))
    return rows


def _en_horario(ts, h_ini, h_fin):
    loc = ts.astimezone()
    return loc.weekday() < 5 and h_ini <= loc.hour < h_fin


def _summarize(rows, names):
    agg = {}
    for ts, m, inst, v, unit in rows:
        agg.setdefault((m, inst, unit), []).append((v, ts))
    out = []
    for (m, inst, unit), vals in sorted(agg.items()):
        vs = sorted(x[0] for x in vals)
        vmax, tmax = max(vals, key=lambda x: x[0])
        out.append({
            "metrica": m,
            "instancia": names.get(inst, inst) if inst else "",
            "unidad": unit,
            "promedio": round(sum(vs) / len(vs), 2),
            "p95": round(vs[min(len(vs) - 1, int(0.95 * len(vs)))], 2),
            "maximo": round(vmax, 2),
            "momento_maximo": _iso(tmax),
            "muestras": len(vs),
        })
    return out


def _performance(intervalo="realtime", max_muestras=180, dias=None, solo_horario_laboral=False,
                 hora_inicio=8, hora_fin=19, vm=None):
    pm = si().content.perfManager
    if intervalo not in INTERVALOS:
        raise ValueError(f"intervalo debe ser uno de {list(INTERVALOS)}")
    hosts = _objs(vim.HostSystem)
    interval_id = INTERVALOS[intervalo] or pm.QueryPerfProviderSummary(entity=hosts[0]).refreshRate
    start = end = None
    if dias:
        end = _now()
        start = end - dt.timedelta(days=dias)
    names = _instance_names()
    filt = (lambda r: _en_horario(r[0], hora_inicio, hora_fin)) if solo_horario_laboral else (lambda r: True)

    res = {"intervalo_s": interval_id, "hosts": [], "vms": []}
    for h in hosts:
        rows = [r for r in _query(h, HOST_METRICS, interval_id, max_muestras, start, end) if filt(r)]
        res["hosts"].append({"host": h.name, "metricas": _summarize(rows, names)})
    for v in _objs(vim.VirtualMachine):
        if vm and v.name.lower() != vm.lower():
            continue
        if v.runtime.powerState != "poweredOn":
            continue
        ncpu = v.config.hardware.numCPU
        rows = [r for r in _query(v, VM_METRICS, interval_id, max_muestras, start, end, ncpu) if filt(r)]
        res["vms"].append({"vm": v.name, "vcpu": ncpu, "metricas": _summarize(rows, names)})
    if all(not x["metricas"] for x in res["hosts"] + res["vms"]):
        res["aviso"] = ("Sin datos para ese intervalo. Un ESXi sin vCenter solo guarda ~1 h en tiempo real: "
                        "usa intervalo='realtime' o el historial local (historial_perf) alimentado con --collect.")
    res["referencia"] = {
        "cpu_ready": "<5% por vCPU ok, 5-10% atencion, >10% contencion de CPU",
        "costop": ">3% indica demasiadas vCPU para los cores fisicos",
        "latencia_disco": "<10 ms ok para SQL; 10-20 ms atencion; >20 ms problema",
        "ballooning_swap": "cualquier valor >0 sostenido indica falta de RAM en el host",
    }
    return res


# ---------------------------------------------------------------- historial local (--collect)


def _db():
    con = sqlite3.connect(DB_PATH)
    con.execute("""CREATE TABLE IF NOT EXISTS perf (
        ts TEXT, entidad TEXT, tipo TEXT, metrica TEXT, instancia TEXT, valor REAL, unidad TEXT,
        PRIMARY KEY (ts, entidad, metrica, instancia))""")
    return con


def collect():
    pm = si().content.perfManager
    hosts = _objs(vim.HostSystem)
    rate = pm.QueryPerfProviderSummary(entity=hosts[0]).refreshRate
    names = _instance_names()
    con = _db()
    n = 0
    targets = [(h, "host", HOST_METRICS, 1) for h in hosts]
    targets += [(v, "vm", VM_METRICS, v.config.hardware.numCPU)
                for v in _objs(vim.VirtualMachine) if v.runtime.powerState == "poweredOn"]
    for ent, tipo, metrics, ncpu in targets:
        rows = _query(ent, metrics, rate, max_samples=180, ncpu=ncpu)
        cur = con.executemany(
            "INSERT OR IGNORE INTO perf VALUES (?,?,?,?,?,?,?)",
            [(ts.astimezone(dt.timezone.utc).isoformat(), ent.name, tipo, m, names.get(i, i), v, u)
             for ts, m, i, v, u in rows])
        n += cur.rowcount
    con.commit()
    con.close()
    log.info("Guardadas %d muestras nuevas en %s", n, DB_PATH)


def _historial(dias=28, solo_horario_laboral=True, hora_inicio=8, hora_fin=19, entidad=None):
    if not DB_PATH.exists():
        return {"aviso": f"No existe {DB_PATH}. Programa 'python server.py --collect' cada hora (ver README)."}
    con = _db()
    desde = (_now() - dt.timedelta(days=dias)).isoformat()
    q = "SELECT ts, entidad, tipo, metrica, instancia, valor, unidad FROM perf WHERE ts >= ?"
    args = [desde]
    if entidad:
        q += " AND lower(entidad) = lower(?)"
        args.append(entidad)
    por_ent = {}
    rango = [None, None]
    for ts, ent, tipo, m, inst, v, u in con.execute(q, args):
        t = dt.datetime.fromisoformat(ts)
        if solo_horario_laboral and not _en_horario(t, hora_inicio, hora_fin):
            continue
        rango[0] = min(rango[0] or t, t)
        rango[1] = max(rango[1] or t, t)
        por_ent.setdefault((tipo, ent), []).append((t, m, inst, v, u))
    con.close()
    return {
        "desde": _iso(rango[0]),
        "hasta": _iso(rango[1]),
        "solo_horario_laboral": solo_horario_laboral,
        "entidades": [{"tipo": t, "nombre": e, "metricas": _summarize(rows, {})}
                      for (t, e), rows in sorted(por_ent.items())],
    }


# ---------------------------------------------------------------- 6. archivos


def _wait(task, timeout=600):
    t0 = time.time()
    while task.info.state not in (vim.TaskInfo.State.success, vim.TaskInfo.State.error):
        if time.time() - t0 > timeout:
            raise TimeoutError("La busqueda en el datastore tardo demasiado")
        time.sleep(0.5)
    if task.info.state == vim.TaskInfo.State.error:
        raise task.info.error
    return task.info.result


def _top_archivos(datastore=None, top=15):
    vm_files = {}
    for vm in _objs(vim.VirtualMachine):
        for f in (vm.layoutEx.file if vm.layoutEx else []):
            vm_files[f.name] = (vm.name, f.size, f.type)
    out = []
    for ds in _objs(vim.Datastore):
        if datastore and ds.name.lower() != datastore.lower():
            continue
        prefix = f"[{ds.name}]"
        files, fuente, error = [], "browser", None
        try:
            spec = vim.host.DatastoreBrowser.SearchSpec(
                details=vim.host.DatastoreBrowser.FileInfo.Details(
                    fileSize=True, modification=True, fileType=True, fileOwner=False),
                sortFoldersFirst=True)
            result = _wait(ds.browser.SearchDatastoreSubFolders_Task(datastorePath=prefix, searchSpec=spec))
            for r in result or []:
                folder = r.folderPath if r.folderPath.endswith(("/", "]")) else r.folderPath + "/"
                if folder.endswith("]"):
                    folder += " "
                for f in r.file or []:
                    if isinstance(f, vim.host.DatastoreBrowser.FolderInfo):
                        continue
                    path = folder + f.path
                    owner = vm_files.get(path, (None,))[0]
                    files.append({"archivo": path, "gb": gb(f.fileSize), "modificado": _iso(f.modification),
                                  "vm": owner, "posible_huerfano": owner is None and not path.endswith(".iso")})
        except vmodl.MethodFault as e:
            error = type(e).__name__
            fuente = "layoutEx (solo archivos de VMs registradas)"
            files = [{"archivo": n, "gb": gb(sz), "tipo": t, "vm": v}
                     for n, (v, sz, t) in vm_files.items() if n.startswith(prefix)]
        files.sort(key=lambda x: x["gb"] or 0, reverse=True)
        item = {"datastore": ds.name, "fuente": fuente, "top": files[:top],
                "total_listado_gb": round(sum(f["gb"] or 0 for f in files), 2), "archivos": len(files)}
        if error:
            item["aviso"] = (f"No se pudo recorrer el datastore ({error}). Para ver ISOs y archivos huerfanos, "
                             "clona el rol Read-only y agregale el privilegio Datastore > Browse datastore.")
        out.append(item)
    return out


# ---------------------------------------------------------------- 7. overcommit


def _overcommit():
    hosts = _objs(vim.HostSystem)
    vms = [v for v in _objs(vim.VirtualMachine) if v.config]
    on = [v for v in vms if v.runtime.powerState == "poweredOn"]
    cores = sum(h.hardware.cpuInfo.numCpuCores for h in hosts)
    threads = sum(h.hardware.cpuInfo.numCpuThreads for h in hosts)
    ram = sum(h.hardware.memorySize for h in hosts) / 1024**3
    vcpu_on = sum(v.config.hardware.numCPU for v in on)
    ram_on = sum(v.config.hardware.memoryMB for v in on) / 1024
    return {
        "cores_fisicos": cores,
        "hilos_logicos": threads,
        "vcpu_encendidas": vcpu_on,
        "vcpu_todas": sum(v.config.hardware.numCPU for v in vms),
        "ratio_vcpu_por_hilo": round(vcpu_on / threads, 2),
        "vm_mas_grande_vcpu": max((v.config.hardware.numCPU for v in on), default=0),
        "ram_fisica_gb": round(ram, 1),
        "ram_asignada_encendidas_gb": round(ram_on, 1),
        "ram_asignada_todas_gb": round(sum(v.config.hardware.memoryMB for v in vms) / 1024, 1),
        "ratio_ram": round(ram_on / ram, 2),
        "detalle": [{"vm": v.name, "vcpu": v.config.hardware.numCPU,
                     "ram_gb": round(v.config.hardware.memoryMB / 1024, 1)} for v in on],
        "referencia": "Con SQL Server conviene ratio vCPU <= 1.5:1 y que ninguna VM tenga tantas vCPU como cores fisicos.",
    }


# ---------------------------------------------------------------- permisos

ESCRITURA = [
    "VirtualMachine.Interact.PowerOff", "VirtualMachine.Interact.PowerOn", "VirtualMachine.Inventory.Delete",
    "VirtualMachine.State.RemoveSnapshot", "VirtualMachine.State.CreateSnapshot", "VirtualMachine.Config.Resource",
    "Datastore.DeleteFile", "Datastore.FileManagement", "Host.Config.Maintenance",
]
LECTURA = ["System.Read", "System.View", "Datastore.Browse"]


def _permisos():
    c = si().content
    sess = c.sessionManager.currentSession
    privs = ESCRITURA + LECTURA
    try:
        vals = c.authorizationManager.HasPrivilegeOnEntity(entity=c.rootFolder, sessionId=sess.key, privId=privs)
    except vmodl.MethodFault as e:
        return {"usuario": sess.userName, "error": f"No se pudieron consultar permisos: {type(e).__name__}"}
    tiene = dict(zip(privs, vals))
    escritura = [p for p in ESCRITURA if tiene[p]]
    return {
        "usuario": sess.userName,
        "privilegios_lectura": {p: tiene[p] for p in LECTURA},
        "privilegios_escritura_detectados": escritura,
        "solo_lectura_garantizado_por_ESXi": not escritura,
        "nota": ("El MCP no usa privilegios de escritura, pero el usuario los tiene: conviene un usuario con rol Read-only."
                 if escritura else "El usuario no puede modificar nada."),
    }


# ---------------------------------------------------------------- MCP


def _build_mcp():
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP(
        "esxi-readonly",
        instructions="Servidor de SOLO LECTURA para ESXi. No existe ninguna herramienta para modificar, apagar, "
                     "borrar ni consolidar. Para recomendaciones de cambios, indicarle al usuario donde hacerlos.",
    )

    @mcp.tool()
    def verificar_permisos() -> dict:
        """Muestra con que usuario se conecta y si ese usuario tiene privilegios de escritura en ESXi."""
        return _permisos()

    @mcp.tool()
    def host_info() -> list:
        """Host: fabricante, modelo, CPU (cores/hilos/HT), RAM total y usada, version y build de ESXi, uptime,
        politica de energia y controladoras de storage."""
        return _host_info()

    @mcp.tool()
    def datastores() -> dict:
        """Datastores: capacidad, usado, libre, % de uso, % provisionado (thin), disco fisico donde esta cada uno y VMs."""
        return _datastores()

    @mcp.tool()
    def vms() -> list:
        """Todas las VMs: estado, vCPU, RAM asignada/activa/consumida, ballooning, swap, reservas/limites, VMware Tools,
        discos (provisionado vs usado, thin/thick, datastore, controladora) y espacio libre dentro del guest."""
        return _vms()

    @mcp.tool()
    def snapshots(dias_alerta: int = 3) -> dict:
        """Todos los snapshots con fecha, antiguedad, tamano aproximado y marca de los que superan dias_alerta."""
        return _snapshots(dias_alerta)

    @mcp.tool()
    def performance(intervalo: str = "realtime", max_muestras: int = 180, dias: float | None = None,
                    solo_horario_laboral: bool = False, hora_inicio: int = 8, hora_fin: int = 19,
                    vm: str | None = None) -> dict:
        """Metricas de host y VMs: CPU uso, CPU Ready %, Co-Stop %, memoria activa/ballooning/swap, latencia de disco
        (total, por disco virtual y por datastore) e IOPS. Devuelve promedio, p95 y maximo con su momento.
        intervalo: realtime (20 s, ~1 h en ESXi standalone), dia, semana, mes, anio (estos requieren vCenter).
        max_muestras: 180 = 1 h en realtime. dias: rango hacia atras (alternativa a max_muestras)."""
        return _performance(intervalo, max_muestras, dias, solo_horario_laboral, hora_inicio, hora_fin, vm)

    @mcp.tool()
    def historial_perf(dias: int = 28, solo_horario_laboral: bool = True, hora_inicio: int = 8,
                       hora_fin: int = 19, entidad: str | None = None) -> dict:
        """Resumen de las metricas guardadas localmente por 'server.py --collect' (semanas de historia, aunque ESXi
        solo guarde 1 h). Filtra por horario laboral (lunes a viernes, hora local)."""
        return _historial(dias, solo_horario_laboral, hora_inicio, hora_fin, entidad)

    @mcp.tool()
    def top_archivos(datastore: str | None = None, top: int = 15) -> list:
        """Archivos mas grandes de cada datastore (o de uno), con la VM a la que pertenecen y marca de posibles huerfanos."""
        return _top_archivos(datastore, top)

    @mcp.tool()
    def overcommit() -> dict:
        """Suma de vCPU vs cores/hilos fisicos y RAM asignada vs fisica (ratios de overcommit)."""
        return _overcommit()

    @mcp.tool()
    def diagnostico_completo(dias_alerta_snapshot: int = 3) -> dict:
        """Ejecuta todo junto: host, datastores, VMs, snapshots, performance realtime, top archivos, overcommit y permisos."""
        out = {}
        for k, fn in [("permisos", _permisos), ("host", _host_info), ("datastores", _datastores), ("vms", _vms),
                      ("snapshots", lambda: _snapshots(dias_alerta_snapshot)), ("performance_1h", _performance),
                      ("historial_local", _historial), ("top_archivos", _top_archivos), ("overcommit", _overcommit)]:
            try:
                out[k] = fn()
            except Exception as e:  # noqa: BLE001
                out[k] = {"error": f"{type(e).__name__}: {e}"}
        return out

    return mcp


if __name__ == "__main__":
    if "--collect" in sys.argv:
        collect()
    else:
        _build_mcp().run()
