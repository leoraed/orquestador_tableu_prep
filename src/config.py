import yaml
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).parent.parent
_FLOWS_YAML = BASE_DIR / "config" / "flows.yaml"


def _load_yaml() -> dict:
    with open(_FLOWS_YAML, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _save_yaml(data: dict) -> None:
    with open(_FLOWS_YAML, "w", encoding="utf-8") as f:
        yaml.dump(data, f, allow_unicode=True, default_flow_style=False, sort_keys=False)


def load_settings() -> dict:
    with open(BASE_DIR / "config" / "settings.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_flows() -> list[dict]:
    flows = _load_yaml().get("flows", []) or []
    for flow in flows:
        # Backward compat: acepta 'schedule' (str) o 'schedules' (list)
        if "schedules" not in flow:
            old = flow.pop("schedule", None)
            flow["schedules"] = [old] if old else []
        elif isinstance(flow.get("schedules"), str):
            s = flow["schedules"]
            flow["schedules"] = [s] if s else []
        flow.pop("schedule", None)
        flow["schedules"] = [s for s in (flow["schedules"] or []) if s]
        if "depends_on" not in flow:
            flow["depends_on"] = []
        flow.setdefault("reintentos", 0)
        flow.setdefault("reintento_espera_min", 5)
    return flows


def load_carpetas() -> list[str]:
    return _load_yaml().get("carpetas", []) or []


def descubrir_tfl(carpetas: list[str]) -> list[dict]:
    resultado = []
    for carpeta in carpetas:
        p = Path(carpeta)
        if not p.exists() or not p.is_dir():
            continue
        for ext in ("*.tfl", "*.tflx"):
            for tfl in sorted(p.glob(ext)):
                stat = tfl.stat()
                resultado.append({
                    "nombre": tfl.stem,
                    "path": str(tfl),
                    "carpeta": str(p),
                    "extension": tfl.suffix,
                    "size_kb": round(stat.st_size / 1024, 1),
                    "modificado": datetime.fromtimestamp(stat.st_mtime).strftime("%d/%m/%Y %H:%M"),
                })
    return resultado


settings = load_settings()
